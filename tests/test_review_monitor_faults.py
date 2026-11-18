"""Regressions for monitor fault attribution and return-model grouping."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import app


class ReviewMonitorFaultTests(unittest.TestCase):
    def run_round(self, *, ticker_error=None, indicator_error=None):
        state = app.MonitorState()
        state.strategy_trades = [{"symbol": "BTCUSDT", "status": "open"}]
        stop = asyncio.Event()

        async def finish(_complete):
            stop.set()

        with patch.object(app, "state", state), \
             patch.object(app, "async_fetch_tickers", new=AsyncMock(side_effect=ticker_error, return_value={})), \
             patch.object(app, "update_strategy_trades_with_prices_async", new=AsyncMock()), \
             patch.object(app, "evaluate_support_alerts_async", new=AsyncMock()), \
             patch.object(app, "evaluate_chanlun_signals_async", new=AsyncMock()), \
             patch.object(app, "evaluate_indicator_signals_async", new=AsyncMock(side_effect=indicator_error)), \
             patch.object(app, "finish_observation_round_async", new=finish), \
             patch.object(app, "maybe_run_site_monitor_async", new=AsyncMock()), \
             patch.object(app, "publish_monitor_faults"):
            asyncio.run(app.monitor_loop_async(stop))
        return state

    def test_indicator_failure_does_not_report_paper_market_failure(self):
        state = self.run_round(indicator_error=ValueError("indicator failed"))
        self.assertEqual(state.last_error, "indicator failed")
        self.assertIsNone(state.module_errors.get("paper_data"))
        self.assertEqual(state.paper_data_details, [])

    def test_ticker_failure_reports_paper_market_failure(self):
        state = self.run_round(ticker_error=TimeoutError())
        self.assertTrue(state.module_errors.get("paper_data"))
        self.assertEqual(state.paper_data_details[0]["symbol"], "BTCUSDT")
        self.assertEqual(state.paper_data_details[0]["kind"], "market_unavailable")

    def test_explicit_null_and_missing_return_model_remain_distinct(self):
        stats = app.calculate_strategy_stats([
            {"status": "closed", "return_pct": 0.1, "return_model": None},
            {"status": "closed", "return_pct": 0.2},
        ])
        self.assertEqual(set(stats["return_model_stats"]), {"null", "legacy_ratio_v1"})
        self.assertFalse(stats["return_model_stats"]["null"]["recognized"])
        self.assertTrue(stats["return_model_stats"]["legacy_ratio_v1"]["recognized"])
        self.assertIsNone(stats["total_return"])


if __name__ == "__main__":
    unittest.main()
