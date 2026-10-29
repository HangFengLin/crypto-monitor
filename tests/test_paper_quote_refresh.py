"""Point quotes are refreshed after slow history loads without relaxing freshness."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app
import data_client
from product_settings import SettingsStore


class PaperQuoteRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.settings = SettingsStore(self.directory / "settings.json")
        self.clock = 1000.
        self.requests = []
        self.response = {"symbol": "TESTUSDT", "price": "94", "time": 999999}
        for replacement in (
            patch.object(app, "state", app.MonitorState()),
            patch.object(app, "STRATEGY_TRADES_FILE", self.directory / "trades.json"),
            patch.object(app, "RUNTIME_SETTINGS", self.settings),
            patch.object(app, "PAPER_UNIVERSE_ENABLED", False),
            patch.object(app, "market_data_source", return_value="binance_usdm"),
            patch.object(app.time, "time", side_effect=lambda: self.clock),
            patch.object(data_client, "async_read_json_url", side_effect=self.read_point_quote),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        runtime = self.settings.document(copy.deepcopy(self.settings.get()["values"]))
        self.trade = app.build_strategy_trade_from_signal(dict(
            symbol="TESTUSDT", interval="15m", signal="long", price=100., stop_loss=95.,
            created_at=1, kline_close_time=999, divergence_time=999, runtime_config=runtime))
        app.state.strategy_trades = [self.trade]

    async def read_point_quote(self, url, **options):
        self.requests.append((url, options))
        if isinstance(self.response, Exception):
            raise self.response
        return copy.deepcopy(self.response)

    def run_update(self, prices, bars=None):
        with patch.object(app, "async_fetch_klines", new=AsyncMock(return_value=bars or [])):
            asyncio.run(app.update_strategy_trades_with_prices_async(prices))

    def test_slow_kline_load_refreshes_expired_quote_before_point_exit(self):
        async def slow_bars(*args):
            self.clock = 1031.
            return []

        self.response["time"] = 1030999
        with patch.object(app, "async_fetch_klines", new=slow_bars):
            asyncio.run(app.update_strategy_trades_with_prices_async(
                {"TESTUSDT": {"lastPrice": 90., "closeTime": 1000000}}))
        self.assertEqual(self.trade["status"], "closed")
        self.assertEqual(self.trade["exit_price"], 94.)
        self.assertIsNone(app.state.module_errors.get("paper_data"))
        self.assertEqual(self.requests, [(
            "https://fapi.binance.com/fapi/v2/ticker/price?symbol=TESTUSDT",
            {"timeout": 3, "attempts": 1, "max_elapsed": 3},
        )])

    def test_invalid_refreshed_source_clock_or_timeout_never_books_point_exit(self):
        variants = [
            {"symbol": "TESTUSDT", "price": "94", "time": 969999},
            {"symbol": "TESTUSDT", "price": "94", "time": 1000001},
            {"symbol": "TESTUSDT", "price": "94"},
            {"symbol": "OTHERUSDT", "price": "94", "time": 999999},
            TimeoutError("injected timeout"),
        ]
        for response in variants:
            with self.subTest(response=response):
                self.requests.clear()
                self.response = response
                before = copy.deepcopy(self.trade)
                self.run_update({"TESTUSDT": {"lastPrice": 90., "closeTime": 900000}})
                self.assertEqual(self.trade, before)
                self.assertTrue(app.state.module_errors.get("paper_data"))
                self.assertIn("source_age=", app.state.module_errors["paper_data"])
                if isinstance(response, TimeoutError):
                    self.assertIn("refresh=TimeoutError", app.state.module_errors["paper_data"])
                self.assertEqual(len(self.requests), 1)

    def test_absent_quote_and_no_bars_reports_fault_after_one_failed_refresh(self):
        self.response = TimeoutError("injected timeout")
        before = copy.deepcopy(self.trade)
        self.run_update({})
        self.assertEqual(self.trade, before)
        self.assertIn("TESTUSDT:15m", app.state.module_errors.get("paper_data") or "")
        self.assertEqual(len(self.requests), 1)

    def test_valid_quote_exits_without_extra_quote_request(self):
        self.response = AssertionError("fresh input must not request another quote")
        self.run_update({"TESTUSDT": {"lastPrice": 94., "closeTime": 999999}})
        self.assertEqual(self.trade["status"], "closed")
        self.assertEqual(self.trade["exit_price"], 94.)
        self.assertEqual(self.requests, [])

    def test_bars_only_replay_stays_healthy_when_optional_quote_refresh_fails(self):
        self.response = TimeoutError("injected timeout")
        self.run_update({}, [dict(open_time=1000, close_time=1999, open=100.,
                                 high=101., low=94., close=96., confirmed=True)])
        self.assertEqual(self.trade["status"], "closed")
        self.assertEqual(self.trade["exit_price"], 95.)
        self.assertIsNone(app.state.module_errors.get("paper_data"))

    def test_request_failure_keeps_structured_diagnostics_without_exception_text(self):
        self.response = TimeoutError("https://secret.invalid/token/private")
        self.run_update({"TESTUSDT": {"lastPrice": 90., "closeTime": 900000}})
        details = getattr(app.state, "paper_data_details", [])
        self.assertEqual(details, [{
            "symbol": "TESTUSDT", "interval": "15m", "kind": "quote_request_failed",
            "source_age_ms": 100000, "refresh_error": "TimeoutError",
        }])

    def test_old_transaction_clock_is_distinct_from_request_failure(self):
        self.response["time"] = 960000
        before = copy.deepcopy(self.trade)
        self.run_update({"TESTUSDT": {"lastPrice": 90., "closeTime": 900000}})
        self.assertEqual(self.trade, before)
        self.assertEqual(getattr(app.state, "paper_data_details", []), [{
            "symbol": "TESTUSDT", "interval": "15m", "kind": "stale_trade",
            "source_age_ms": 40000, "refresh_error": "invalid",
        }])


if __name__ == "__main__":
    unittest.main()
