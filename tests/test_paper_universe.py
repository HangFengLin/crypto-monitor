import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import strategy_universe


class BinancePaperUniverseTest(unittest.TestCase):
    def test_selects_market_cap_order_only_from_active_usdt_perpetuals(self):
        select = getattr(strategy_universe, "select_binance_paper_universe", None)
        self.assertTrue(callable(select), "Binance paper universe selection is missing")
        coins = [
            {"id": "dogecoin", "symbol": "DOGE", "market_cap_rank": 10},
            {"id": "tether", "symbol": "USDT", "market_cap_rank": 3},
            {"id": "bitcoin", "symbol": "BTC", "market_cap_rank": 1},
            {"id": "shiba-inu", "symbol": "SHIB", "market_cap_rank": 12},
            {"id": "one-inch", "symbol": "1INCH", "market_cap_rank": 110},
        ]
        instruments = [dict(symbol=s, baseAsset=b, quoteAsset="USDT", status="TRADING", contractType="PERPETUAL")
                       for s, b in [("BTCUSDT", "BTC"), ("DOGEUSDT", "DOGE"), ("1000SHIBUSDT", "1000SHIB"),
                                    ("1INCHUSDT", "1INCH"), ("USDTUSDT", "USDT")]]
        instruments += [{**instruments[0], "symbol": "BTCUSDT_260925", "contractType": "CURRENT_QUARTER"}]
        result = select(coins, instruments, top_n=3)
        self.assertEqual([r["symbol"] for r in result], ["BTCUSDT", "DOGEUSDT", "1000SHIBUSDT"])
        self.assertEqual(result[2]["coin_id"], "shiba-inu")
        self.assertEqual(select(coins, instruments, top_n=4)[-1]["symbol"], "1INCHUSDT")
        instruments[1]["status"] = "SETTLING"
        self.assertNotIn("DOGEUSDT", [r["symbol"] for r in select(coins, instruments, top_n=4)])

    def test_ambiguous_symbols_and_invalid_ranks_do_not_enter_pool(self):
        select = getattr(strategy_universe, "select_binance_paper_universe", None)
        self.assertTrue(callable(select), "Binance paper universe selection is missing")
        coins = [dict(id=i, symbol="ABC", market_cap_rank=r) for i, r in [("a", 1), ("b", 2)]]
        coins.append(dict(id="bad", symbol="BAD", market_cap_rank=None))
        instruments = [dict(symbol=s+"USDT", baseAsset=s, quoteAsset="USDT", status="TRADING", contractType="PERPETUAL") for s in ("ABC", "BAD")]
        self.assertEqual(select(coins, instruments, top_n=100), [])


class PaperPoolCacheTest(unittest.TestCase):
    def test_failed_refresh_blocks_entries_but_retains_last_snapshot_for_review(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec("paper_universe"), "Durable paper pool is missing")
        from paper_universe import PaperUniverse

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pool.json"
            pool = PaperUniverse(path, top_n=1, refresh_seconds=100)
            pool.refresh(lambda: [dict(symbol="BTCUSDT", market_cap_rank=1, coin_id="bitcoin")], now=1000)
            self.assertTrue(pool.status(now=1050)["ready"])
            archive = path.parent / "pool-snapshots" / (pool.status(now=1050)["snapshot_id"] + ".json")
            self.assertTrue(archive.exists(), "Entry snapshot must remain inspectable after the next pool refresh")
            restored = PaperUniverse(path, top_n=1, refresh_seconds=100)
            self.assertEqual(restored.status(now=1050)["snapshot_id"], pool.status(now=1050)["snapshot_id"])
            self.assertFalse(restored.status(now=1101)["ready"])
            def fail():
                raise RuntimeError("upstream unavailable")
            restored.refresh(fail, now=1101)
            self.assertFalse(restored.status(now=1101)["ready"])
            self.assertEqual(restored.status(now=1101)["members"][0]["symbol"], "BTCUSDT")
            self.assertIn("RuntimeError", restored.status(now=1101)["error"])


class PaperPoolIntegrationTest(unittest.TestCase):
    def test_new_entries_require_current_pool_membership_and_snapshot(self):
        import app
        from paper_universe import PaperUniverse
        with tempfile.TemporaryDirectory() as folder:
            pool = PaperUniverse(Path(folder) / "pool.json", top_n=1)
            pool.refresh(lambda: [dict(symbol="BTCUSDT", market_cap_rank=1, coin_id="bitcoin")])
            snapshot_id = pool.status()["snapshot_id"]
            with (patch.object(app, "state", app.MonitorState()),
                  patch.object(app, "PAPER_UNIVERSE_ENABLED", True, create=True),
                  patch.object(app, "PAPER_UNIVERSE", pool, create=True),
                  patch.object(app, "STRATEGY_TRADES_FILE", Path(folder) / "trades.json")):
                signal = dict(symbol="ETHUSDT", interval="15m", signal="long", price=100, stop_loss=95,
                              divergence_time=1, paper_universe_snapshot_id=snapshot_id)
                app.register_strategy_signal(signal)
                self.assertEqual(app.state.strategy_trades, [], "Out-of-pool entry was accepted")
                app.register_strategy_signal({**signal, "symbol": "BTCUSDT", "paper_universe_snapshot_id": "old"})
                self.assertEqual(app.state.strategy_trades, [], "Old pool snapshot was accepted")
                app.register_strategy_signal({**signal, "symbol": "BTCUSDT"})
                self.assertEqual(len(app.state.strategy_trades), 1)
                self.assertEqual(app.state.strategy_trades[0]["paper_universe"]["snapshot_id"], snapshot_id)
                pool.error = "failed refresh"
                app.register_strategy_signal({**signal, "symbol": "BTCUSDT", "divergence_time": 2})
                self.assertEqual(len(app.state.strategy_trades), 1)

    def test_pool_scans_independently_of_watchlist_and_records_each_bar_once(self):
        import app
        from paper_universe import PaperUniverse
        scan = getattr(app, "scan_paper_universe_once", None)
        self.assertTrue(callable(scan), "Independent paper scanner is missing")
        with tempfile.TemporaryDirectory() as folder:
            pool = PaperUniverse(Path(folder) / "pool.json", top_n=1)
            pool.refresh(lambda: [dict(symbol="ETHUSDT", market_cap_rank=2, coin_id="ethereum")])
            close_time = int(time.time() // 900) * 900000 - 1
            signal = dict(symbol="ETHUSDT", interval="15m", signal="long", price=100, stop_loss=95,
                          divergence_time=1, kline_close_time=close_time)
            with (patch.object(app, "state", app.MonitorState(watchlist=[{"symbol": "BTCUSDT"}])),
                  patch.object(app, "PAPER_UNIVERSE_ENABLED", True), patch.object(app, "PAPER_UNIVERSE", pool),
                  patch.object(app, "STRATEGY_TRADES_FILE", Path(folder) / "trades.json"),
                  patch.object(app, "EVENT_LOG_FILE", Path(folder) / "events.jsonl"),
                  patch.object(app, "detect_project_signal", return_value=signal) as detect):
                asyncio.run(scan())
                asyncio.run(scan())
                self.assertEqual([t["symbol"] for t in app.state.strategy_trades], ["ETHUSDT"])
                self.assertEqual(detect.call_count, 1)
                self.assertEqual(app.state.paper_pool_scan["checked_count"], 1)

    def test_removing_symbol_from_pool_does_not_stop_existing_position_exit(self):
        import app
        from paper_universe import PaperUniverse
        with tempfile.TemporaryDirectory() as folder:
            pool = PaperUniverse(Path(folder) / "pool.json", top_n=1)
            pool.refresh(lambda: [dict(symbol="BTCUSDT", market_cap_rank=1, coin_id="bitcoin")])
            with (patch.object(app, "state", app.MonitorState()),
                  patch.object(app, "PAPER_UNIVERSE_ENABLED", True), patch.object(app, "PAPER_UNIVERSE", pool),
                  patch.object(app, "STRATEGY_TRADES_FILE", Path(folder) / "trades.json"),
                  patch.object(app, "async_fetch_klines", new=AsyncMock(return_value=[
                      dict(close_time=2000, low=94, high=101, close=96)]))):
                app.register_strategy_signal(dict(symbol="BTCUSDT", interval="15m", signal="long", price=100,
                    stop_loss=95, kline_close_time=1000, paper_universe_snapshot_id=pool.status()["snapshot_id"]))
                pool.data["members"] = [dict(symbol="ETHUSDT", market_cap_rank=2, coin_id="ethereum")]
                asyncio.run(app.update_strategy_trades_with_prices_async({"BTCUSDT": {"lastPrice": 96}}))
                self.assertEqual(app.state.strategy_trades[0]["status"], "closed")
                self.assertEqual(app.state.strategy_trades[0]["exit_reason"], "stop_loss")
