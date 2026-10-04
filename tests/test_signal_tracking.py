import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app


class SignalTrackingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "trades.json"
        for replacement in (
            patch.object(app, "state", app.MonitorState()),
            patch.object(app, "STRATEGY_TRADES_FILE", self.ledger),
            patch.object(app, "PAPER_UNIVERSE_ENABLED", False),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def signal(self, **changes):
        return dict(symbol="BTCUSDT", interval="15m", signal="long", price=100,
                    stop_loss=95, created_at=1, kline_close_time=1000, **changes)

    def test_open_tracks_survive_history_limit_and_are_all_visible(self):
        with patch.object(app, "STRATEGY_HISTORY_LIMIT", 2):
            for i in range(25):
                app.register_strategy_signal(self.signal(divergence_time=i + 1))
        payload = app.snapshot()
        self.assertEqual(len(payload.get("signal_tracking", {}).get("positions", [])), 25)
        self.assertEqual(len(app.load_strategy_trades()), 25)
        self.assertNotIn("okx_bot_status", payload)

    def test_duplicate_signal_records_only_one_paper_track(self):
        app.register_strategy_signal(self.signal())
        app.register_strategy_signal(self.signal())
        self.assertEqual(len(app.load_strategy_trades()), 1)
        self.assertEqual(app.load_strategy_trades()[0].get("mode"), "paper")

    def test_website_paper_lifecycle_and_notification_outbox_stay_in_sync(self):
        from automatic_notifications import Outbox

        path = Path(self.tmp.name) / "outbox.json"
        box = Outbox(path)
        with patch.object(app, "list_report_files", return_value=[]), patch.object(
            app, "site_monitor_snapshot", return_value={"enabled": True, "ok": True}
        ):
            box.observe(*app.automatic_notification_inputs())
            app.register_strategy_signal(self.signal())
            app.register_strategy_signal(self.signal())
            box.observe(*app.automatic_notification_inputs())
            self.assertEqual(len(app.snapshot()["signal_tracking"]["positions"]), 1)
            self.assertEqual(len(box.data["pending"]), 1)
            box.deliver(lambda *args: {"ok": False, "error": "injected timeout"})
            box = Outbox(path)
            self.assertEqual(box.data["pending"][0]["attempts"], 1)
            bars = [{"close_time": 2000, "low": 94, "high": 101, "close": 96}]
            with patch.object(app, "async_fetch_klines", new=AsyncMock(return_value=bars)):
                asyncio.run(app.update_strategy_trades_with_prices_async({"BTCUSDT": {"lastPrice": 96}}))
            box.observe(*app.automatic_notification_inputs())
            self.assertEqual(app.snapshot()["signal_tracking"]["positions"], [])
            self.assertEqual(len(box.data["pending"]), 2)
            self.assertIn("stop_loss", box.data["pending"][1]["body"])
            sent = []
            for message in box.data["pending"]:
                message["next_attempt"] = 0
            box.deliver(lambda *args: sent.append(args) or {"ok": True})
            self.assertEqual([entry[0] for entry in sent], ["signal", "signal"])
            box = Outbox(path)
            box.observe(*app.automatic_notification_inputs())
            self.assertEqual(box.data["pending"], [])

    def test_indicator_and_support_alerts_reach_durable_feishu_queue(self):
        from automatic_notifications import Outbox

        path = Path(self.tmp.name) / "outbox.json"
        box = Outbox(path)
        old = dict(type="indicator", event_key="old", symbol="BTCUSDT", interval="15m", price=100)
        with patch.object(app, "list_report_files", return_value=[]), patch.object(
            app, "site_monitor_snapshot", return_value={"enabled": False}
        ):
            # An existing queue upgrades without replaying historical alerts.
            box.observe([], [], None)
            app.state.events = [old]
            box.observe(*app.automatic_notification_inputs())
            self.assertEqual(box.data["pending"], [])
            app.state.events = [
                dict(type="support", event_key="support:new", symbol="ETHUSDT", interval="1h", support=90, price=91),
                {**old, "event_key": "indicator:new", "indicator_label": "均线向上"},
                old,
            ]
            box.observe(*app.automatic_notification_inputs())
            self.assertEqual([m["channel"] for m in box.data["pending"]], ["signal", "signal"])
            self.assertIn("均线向上", box.data["pending"][0]["body"])
            self.assertIn("ETHUSDT", box.data["pending"][1]["body"])
            box.deliver(lambda *args: {"ok": False, "error": "timeout"})
            box = Outbox(path)
            box.observe(*app.automatic_notification_inputs())
            self.assertEqual(len(box.data["pending"]), 2)
            for message in box.data["pending"]:
                message["next_attempt"] = 0
            box.deliver(lambda *args: {"ok": True})
            box = Outbox(path)
            box.observe(*app.automatic_notification_inputs())
            self.assertEqual(box.data["pending"], [])

    def test_marks_and_progress_are_saved_even_without_exit(self):
        app.register_strategy_signal(self.signal())
        bars = [{"close_time": 2000, "low": 100, "high": 102, "close": 101}]
        with patch.object(app, "async_fetch_klines", new=AsyncMock(return_value=bars)):
            asyncio.run(app.update_strategy_trades_with_prices_async({"BTCUSDT": {"lastPrice": 102}}))
        saved = app.load_strategy_trades()[0]
        self.assertEqual(saved["current_price"], 102)
        self.assertEqual(saved["last_evaluated_close_time"], 2000)
        self.assertEqual(saved["status"], "open")

    def test_forming_and_previously_evaluated_bars_do_not_trigger_false_exit(self):
        trade = app.build_strategy_trade_from_signal(self.signal())
        trade["last_evaluated_close_time"] = 2000
        closed = app.evaluate_open_strategy_trade_with_bars(trade, [
            {"close_time": 2000, "low": 90, "high": 105, "close": 100},
            {"close_time": 3000, "low": 90, "high": 105, "close": 100, "confirmed": False},
        ], None)
        self.assertFalse(closed)
        self.assertEqual(trade["status"], "open")

    def test_exit_is_persisted_with_reason_and_is_visible_in_history(self):
        app.register_strategy_signal(self.signal())
        bars = [{"close_time": 2000, "low": 94, "high": 101, "close": 96}]
        with patch.object(app, "async_fetch_klines", new=AsyncMock(return_value=bars)):
            asyncio.run(app.update_strategy_trades_with_prices_async({"BTCUSDT": {"lastPrice": 96}}))
        saved = app.load_strategy_trades()[0]
        self.assertEqual((saved["status"], saved["exit_reason"], saved["exit_price"]), ("closed", "stop_loss", 95))
        self.assertEqual(saved.get("exit_kline_close_time"), 2000)
        self.assertEqual(app.snapshot()["signal_tracking"]["positions"], [])
        self.assertEqual(app.snapshot()["strategy_trades"][0]["exit_price"], 95)

    def test_restart_restores_ledger_without_backfilling_old_alerts(self):
        app.register_strategy_signal(self.signal())
        directory = Path(self.tmp.name)
        events = directory / "events.jsonl"
        events.write_text(json.dumps({**self.signal(divergence_time=999), "type": "chanlun"}) + "\n")
        with patch.object(app, "state", app.MonitorState()), patch.object(app, "EVENT_LOG_FILE", events), patch.object(
            app, "STATE_FILE", directory / "watchlist.json"
        ), patch.object(app, "PUBLIC_DIR", directory / "public"), patch.object(app, "REPORTS_DIR", directory / "reports"):
            app.initialize_state()
            self.assertEqual(len(app.snapshot()["signal_tracking"]["positions"]), 1)
            self.assertEqual(app.state.strategy_trades[0]["id"], "BTCUSDT:15m:long:1000")

    def test_old_track_that_exits_now_is_first_in_recent_history(self):
        for i in range(25):
            app.register_strategy_signal(self.signal(divergence_time=i + 1))
        app.close_strategy_trade(app.state.strategy_trades[0], 95, "stop_loss", "loss")
        self.assertEqual(app.snapshot()["strategy_trades"][0]["id"], "BTCUSDT:15m:long:1")

    def test_okx_demo_ledger_preserves_closed_records_and_estimated_pnl(self):
        okx_state = Path(self.tmp.name) / "okx_market_cap_bot_state.json"
        okx_state.write_text(
            json.dumps(
                {
                    "positions": [
                        {
                            "id": "BTCUSDT:15m:long:1",
                            "symbol": "BTCUSDT",
                            "inst_id": "BTC-USDT-SWAP",
                            "interval": "15m",
                            "direction": "long",
                            "status": "closed",
                            "size": "0.5",
                            "entry_price": 100,
                            "exit_price": 110,
                            "opened_at": 10,
                            "closed_at": 20,
                            "exit_reason": "protection_reached",
                            "return_pct": 0.098,
                            "position_sizing": {"notional_usdt": "50"},
                        },
                        {
                            "id": "ETHUSDT:15m:short:2",
                            "symbol": "ETHUSDT",
                            "inst_id": "ETH-USDT-SWAP",
                            "interval": "15m",
                            "direction": "short",
                            "status": "open",
                            "size": "1",
                            "entry_price": 200,
                            "opened_at": 30,
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        with patch.object(app, "OKX_DEMO_LEDGER_FILE", okx_state):
            ledger = app.load_okx_demo_ledger()
        self.assertEqual(ledger["stats"]["total_trades"], 2)
        self.assertEqual(ledger["stats"]["open_trades"], 1)
        self.assertEqual(ledger["stats"]["closed_trades"], 1)
        self.assertEqual(ledger["stats"]["wins"], 1)
        self.assertAlmostEqual(ledger["stats"]["total_return"], 0.098)
        self.assertAlmostEqual(ledger["stats"]["realized_pnl_usdt"], 4.9)
        self.assertTrue(ledger["trades"][1]["pnl_usdt_estimated"])
        self.assertEqual(ledger["trades"][1]["pnl_usdt"], 4.9)


if __name__ == "__main__":
    unittest.main()
