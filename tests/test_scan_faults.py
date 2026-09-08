import asyncio
import unittest
from unittest.mock import patch

import app


class ScanFaultTest(unittest.TestCase):
    def test_symbol_type_error_is_isolated_and_named(self):
        previous = app.state
        app.state = app.MonitorState()
        self.addCleanup(setattr, app, "state", previous)

        def detect(symbol, interval):
            if symbol == "BADUSDT":
                raise TypeError("missing indicator")
            return {"signal": "wait", "symbol": symbol}

        with patch.object(app, "detect_project_signal", side_effect=detect):
            asyncio.run(app.evaluate_chanlun_signals_async([{"symbol": "BADUSDT"}, {"symbol": "BTCUSDT"}]))
        self.assertIn("BTCUSDT:15m", app.state.signals)
        self.assertIn("BADUSDT", app.state.module_errors["strategy"])

    def test_success_in_one_module_does_not_clear_other_fault(self):
        previous = app.state
        app.state = app.MonitorState()
        self.addCleanup(setattr, app, "state", previous)
        app.state.module_errors["support"] = "support failure"
        asyncio.run(app.evaluate_chanlun_signals_async([]))
        self.assertEqual(app.state.module_errors["support"], "support failure")
