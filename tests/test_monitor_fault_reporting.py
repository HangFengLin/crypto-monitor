import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import app


class MonitorFaultReportingTest(unittest.TestCase):
    def setUp(self):
        replacement = patch.object(app, "state", app.MonitorState())
        replacement.start()
        self.addCleanup(replacement.stop)

    def run_monitor_once(self, site_error=None, market_error=None):
        async def run():
            stop = asyncio.Event()

            async def inspect_site():
                stop.set()
                if site_error:
                    raise site_error

            with ExitStack() as patches:
                for name in (
                    "async_fetch_tickers", "update_strategy_trades_with_prices_async",
                    "evaluate_support_alerts_async", "evaluate_chanlun_signals_async",
                    "evaluate_indicator_signals_async", "send_hourly_market_summary_async",
                ):
                    patches.enter_context(patch.object(app, name, new=AsyncMock(return_value={})))
                if market_error is not None:
                    patches.enter_context(patch.object(app, "async_fetch_tickers", new=AsyncMock(side_effect=market_error)))
                patches.enter_context(patch.object(app, "maybe_run_site_monitor_async", side_effect=inspect_site))
                await app.monitor_loop_async(stop)

        asyncio.run(run())

    def fault_input(self, monitor=None):
        with patch.object(app, "site_monitor_snapshot", return_value=monitor or {"enabled": False}), \
             patch.object(app, "list_report_files", return_value=[]):
            return app.automatic_notification_inputs()[2]

    def test_paper_quote_failure_is_not_reported_as_signal_calculation_failure(self):
        app.state.module_errors = {"paper_data": "VIRTUALUSDT:15m 缺少有效新鲜报价"}
        self.run_monitor_once()
        fault = self.fault_input()
        self.assertIn("模拟持仓行情", fault)
        self.assertNotIn("信号计算", fault)

    def test_multiple_module_failures_keep_distinct_module_identities(self):
        app.state.module_errors = {"strategy": "TypeError", "paper_pool": "TimeoutError"}
        self.run_monitor_once()
        fault = self.fault_input()
        self.assertIn("策略信号", fault)
        self.assertIn("模拟币池扫描", fault)

    def test_empty_market_timeout_is_not_published_as_healthy(self):
        self.run_monitor_once(market_error=TimeoutError())
        self.assertTrue(app.state.last_error)
        self.assertIn("TimeoutError", app.state.last_error)
        self.assertIn("行情", self.fault_input())

    def test_single_site_probe_failure_does_not_bypass_failure_threshold(self):
        monitor = {"enabled": True, "ok": False, "results": [
            {"id": "http:health", "ok": False, "consecutive_failures": 1, "alert_active": False},
        ]}
        self.assertIsNone(self.fault_input(monitor))
        monitor["results"][0].update(consecutive_failures=3, alert_active=True)
        self.assertIn("站点巡检", self.fault_input(monitor))

    def test_repeated_site_task_failure_still_reports_after_threshold(self):
        with patch.object(app, "site_monitor_failure_threshold", return_value=3):
            for attempt in range(1, 4):
                self.run_monitor_once(site_error=RuntimeError("probe task failed"))
                fault = self.fault_input(app.state.site_monitor)
                if attempt < 3:
                    self.assertIsNone(fault)
                else:
                    self.assertIn("站点巡检", fault)

    def test_successful_site_run_clears_task_failure_state(self):
        app.state.site_monitor = {
            "enabled": True, "ok": False, "task_failures": 3,
            "alert_active": True, "last_error": "probe task failed",
        }
        result = {"id": "http:health", "ok": True}
        with patch.object(app, "site_monitor_enabled", return_value=True), \
             patch.object(app, "_site_monitor_last_run_at", None), \
             patch.object(app, "_site_monitor_alert_state", {}), \
             patch.object(app, "configured_site_monitor_targets", return_value=[{"id": "http:health"}]), \
             patch.object(app, "probe_site_monitor_target", return_value=result), \
             patch.object(app, "build_site_monitor_runtime_checks", return_value=[]):
            asyncio.run(app.maybe_run_site_monitor_async())
        self.assertTrue(app.state.site_monitor["ok"])
        self.assertNotIn("last_error", app.state.site_monitor)
        self.assertIsNone(self.fault_input(app.state.site_monitor))

    def test_empty_timeout_message_is_preserved_for_indicators_and_support(self):
        for module, operation, detector, args in (
            ("indicators", app.evaluate_indicator_signals_async, "detect_indicator_signal_async",
             ([{"symbol": "BTCUSDT"}],)),
            ("support", app.evaluate_support_alerts_async, "detect_support_level_async",
             ([{"symbol": "BTCUSDT"}], {})),
        ):
            with self.subTest(module=module), patch.object(app, detector, new=AsyncMock(side_effect=TimeoutError())):
                asyncio.run(operation(*args))
                error = app.state.module_errors[module]
                self.assertTrue(error)
                self.assertIn("TimeoutError", error)
                self.assertIn("BTCUSDT", error)
