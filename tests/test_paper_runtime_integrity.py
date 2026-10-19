"""Offline regression for the independently reproduced paper integrity failures."""
import asyncio
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app
from product_settings import SettingsStore


class PaperRuntimeIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.ledger = self.directory / "trades.json"
        self.settings = SettingsStore(self.directory / "settings.json")
        replacements = (
            patch.object(app, "state", app.MonitorState()),
            patch.object(app, "STRATEGY_TRADES_FILE", self.ledger),
            patch.object(app, "RUNTIME_SETTINGS", self.settings),
            patch.object(app, "SIGNAL_ENGINES", {}),
            patch.object(app, "PAPER_UNIVERSE_ENABLED", False),
            patch.object(app, "PUBLIC_DIR", self.directory),
            patch.object(app, "REPORTS_DIR", self.directory),
            patch.object(app, "load_all_event_history", return_value=[]),
            patch.object(app, "load_watchlist", return_value=[]),
        )
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)

    def trade(self, mode="structure_atr", direction="long"):
        values = copy.deepcopy(self.settings.get()["values"])
        values["paper"]["stop_mode"] = mode
        runtime = self.settings.document(values)
        return app.build_strategy_trade_from_signal(dict(
            symbol="TESTUSDT", interval="15m", signal=direction, price=100.,
            stop_loss=95. if direction == "long" else 105., created_at=1,
            kline_close_time=999, divergence_time=999, runtime_config=runtime))

    def test_corrupt_ledger_startup_preserves_bytes_and_blocks_registration(self):
        broken = b'{"trades":[{"status":"open"}'
        self.ledger.write_bytes(broken)
        with self.assertRaises(ValueError):
            app.initialize_state()
        self.assertEqual(self.ledger.read_bytes(), broken)
        accepted = app.register_strategy_signal(dict(symbol="TESTUSDT", interval="15m", signal="long",
            price=100., stop_loss=95., created_at=1, kline_close_time=999))
        self.assertFalse(accepted)
        self.assertEqual(self.ledger.read_bytes(), broken)

    def test_invalid_ledger_shape_does_not_turn_into_empty_history(self):
        for payload in ({"trades": "not a list"}, {"trades": [None]}, {}):
            with self.subTest(payload=payload):
                original = json.dumps(payload).encode()
                self.ledger.write_bytes(original)
                with self.assertRaises(ValueError):
                    app.initialize_state()
                self.assertEqual(self.ledger.read_bytes(), original)

    def test_same_batch_duplicate_candle_does_not_retest_new_trailing_stop(self):
        bar = dict(open_time=1000, close_time=1999, open=100., low=99., high=106.,
                   close=106., atr=1., confirmed=True)
        once = self.trade("atr_trailing_after_1r")
        repeated = copy.deepcopy(once)
        app.evaluate_open_strategy_trade_with_bars(once, [bar], None)
        app.evaluate_open_strategy_trade_with_bars(repeated, [bar, copy.deepcopy(bar)], None)
        self.assertEqual(repeated, once)
        self.assertEqual(repeated["status"], "open")
        self.assertAlmostEqual(repeated["active_stop"], 104.8)

    def test_conflicting_duplicate_candles_fail_before_mutating_trade(self):
        bar = dict(close_time=1999, open=100., low=99., high=106., close=106., atr=1.)
        trade = self.trade("atr_trailing_after_1r")
        before = copy.deepcopy(trade)
        with self.assertRaises(ValueError):
            app.evaluate_open_strategy_trade_with_bars(trade, [bar, {**bar, "low": 94.}], None)
        self.assertEqual(trade, before)

    def test_explicit_unclosed_bar_never_changes_position_or_progress(self):
        for flags in ({"confirmed": False}, {"is_closed": False}, {"confirmed": True, "is_closed": False}):
            with self.subTest(flags=flags):
                trade = self.trade()
                before = copy.deepcopy(trade)
                bar = dict(close_time=1999, open=100., low=94., high=101., close=96., **flags)
                self.assertFalse(app.evaluate_open_strategy_trade_with_bars(trade, [bar], None))
                self.assertEqual(trade, before)

    def test_invalid_bar_range_or_price_cannot_book_paper_exit(self):
        for price in (float("nan"), float("inf"), 0., -1.):
            with self.subTest(price=price):
                trade = self.trade()
                before = copy.deepcopy(trade)
                with self.assertRaises(ValueError):
                    app.evaluate_open_strategy_trade_with_bars(trade,
                        [dict(close_time=1999, open=100., low=price, high=101., close=96.)], None)
                self.assertEqual(trade, before)

    def test_timeout_and_stale_or_unknown_quote_cannot_close_position(self):
        for metadata in ({}, {"closeTime": 0}, {"closeTime": 900000}, {"closeTime": 1000001}):
            with self.subTest(metadata=metadata), patch.object(app.time, "time", return_value=1000.):
                app.state.strategy_trades = [self.trade()]
                before = copy.deepcopy(app.state.strategy_trades[0])
                with patch.object(app, "async_fetch_klines", new=AsyncMock(side_effect=TimeoutError())), \
                     patch.object(app, "save_strategy_trades"):
                    asyncio.run(app.update_strategy_trades_with_prices_async(
                        {"TESTUSDT": dict(lastPrice=94., **metadata)}))
                self.assertEqual(app.state.strategy_trades[0], before)
                self.assertTrue(any(app.state.module_errors.values()))

    def test_fresh_quote_fallback_can_close_at_actual_adverse_quote(self):
        with patch.object(app.time, "time", return_value=1000.), \
             patch.object(app, "async_fetch_klines", new=AsyncMock(side_effect=TimeoutError())), \
             patch.object(app, "save_strategy_trades"):
            app.state.strategy_trades = [self.trade()]
            asyncio.run(app.update_strategy_trades_with_prices_async(
                {"TESTUSDT": dict(lastPrice=94., closeTime=999999)}))
        self.assertEqual(app.state.strategy_trades[0]["status"], "closed")
        self.assertEqual(app.state.strategy_trades[0]["exit_price"], 94.)

    def test_paper_gap_books_observed_open_instead_of_unavailable_stop(self):
        for direction, open_price, low, high, expected in (
            ("long", 80., 79., 82., 80.), ("short", 120., 118., 121., 120.),
        ):
            with self.subTest(direction=direction):
                trade = self.trade(direction=direction)
                app.evaluate_open_strategy_trade_with_bars(trade,
                    [dict(open_time=1000, close_time=1999, open=open_price,
                          low=low, high=high, close=open_price, confirmed=True)], None)
                self.assertEqual(trade["status"], "closed")
                self.assertEqual(trade["exit_reason"], "stop_loss")
                self.assertEqual(trade["exit_price"], expected)

    def test_open_phase_exit_does_not_observe_later_close_or_extremes(self):
        trade = self.trade()
        app.evaluate_open_strategy_trade_with_bars(trade,
            [dict(open_time=1000, close_time=1999, open=80., high=120., low=70., close=120.)], None)
        self.assertEqual(trade["exit_price"], 80.)
        self.assertEqual(trade["exit_phase"], "open")
        self.assertEqual(trade["current_price"], 80.)
        self.assertEqual(trade["highest_price"], 100.)
        self.assertEqual(trade["lowest_price"], 80.)

    def test_web_backtest_archive_binds_actual_engine_and_missing_defaults(self):
        from test_execution_metadata import flat_bars

        import product_settings
        import project_signal_backtest as backtest
        from research_archive import ResearchArchive
        historical = self.settings.get()
        historical["engine_version"] = "123456789abc"
        del historical["effective_strategy"]["chan_structure_snapshot"]
        payload = {key: historical[key] for key in ("values", "effective_strategy", "engine_version")}
        historical["revision"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
        original = copy.deepcopy(historical)
        archive = ResearchArchive(self.directory / "archive")
        with patch.object(self.settings, "get", return_value=historical), \
             patch.object(app, "RESEARCH_ARCHIVE", archive), \
             patch.object(backtest, "fetch_exchange_klines", return_value=flat_bars()), \
             patch.object(backtest, "fetch_higher_timeframe_context", side_effect=lambda symbol, bars, *args, **kw: bars):
            result = app.run_signal_backtest_summary("TESTUSDT", "15m", 4, 2., 96, .001, "structure_atr")
        saved = archive.get(result["run_id"])
        self.assertEqual(saved["runtime_config"]["engine_version"], product_settings.ENGINE_VERSION)
        self.assertEqual(saved["config_revision"], historical["revision"])
        self.assertEqual(saved["config_engine_version"], "123456789abc")
        self.assertIn("chan_structure_snapshot", saved["runtime_config"]["strategy_defaults_applied"])
        self.assertEqual(historical, original)

    def test_actual_app_pending_structure_survives_engine_cache_loss(self):
        from test_chan_structure_snapshot import fixture
        rows = fixture()
        with patch.object(app, "market_data_source", return_value="binance_usdm"), \
             patch.object(app, "build_project_signal_bars") as source, \
             patch.object(app.time, "time", return_value=1000.):
            for n in range(100, 309):
                source.return_value = rows[max(0, n - 250):n]
                app.detect_project_signal("TESTUSDT", "15m", include_td=False)
            app.SIGNAL_ENGINES.clear()
            source.return_value = rows[309 - 250:309]
            resumed = app.detect_project_signal("TESTUSDT", "15m", include_td=False)
        self.assertEqual(resumed["signal"], "long")
        self.assertEqual(resumed["stop_loss"], 77.)
        self.assertEqual(resumed["divergence_time"], 267299999)
        self.assertEqual(resumed["signal_ready_time"], 269999999)

    def test_corrupt_signal_checkpoint_does_not_generate_replacement_signal(self):
        from test_chan_structure_snapshot import fixture
        rows = fixture()
        with patch.object(app, "market_data_source", return_value="binance_usdm"), \
             patch.object(app, "build_project_signal_bars", return_value=rows[:300]):
            app.detect_project_signal("TESTUSDT", "15m", include_td=False)
            checkpoints = list((self.directory / "signal-engine-state").glob("*.json"))
            self.assertTrue(checkpoints, "real signal state must be durable")
            checkpoint = checkpoints[0]
            checkpoint.write_bytes(b'{"broken"')
            app.SIGNAL_ENGINES.clear()
            with self.assertRaises(ValueError):
                app.detect_project_signal("TESTUSDT", "15m", include_td=False)
            self.assertEqual(checkpoint.read_bytes(), b'{"broken"')


if __name__ == "__main__":
    unittest.main()
