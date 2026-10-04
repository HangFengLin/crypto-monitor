import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import app
from backtest_statistics import grouped_trade_statistics


class ResearchApiEvidenceTest(unittest.TestCase):
    def test_marginal_groups_retain_their_actual_dimension_without_invented_fields(self):
        frame = pd.DataFrame({
            "signal": ["short"] * 35, "return_pct": [.01] * 35,
            "signal_score": [10] * 35, "structure_score": [2] * 35,
            "confirm_bars": [1] * 35,
        })
        grouped = grouped_trade_statistics(frame, ["signal"], min_trades=30)
        serialize = getattr(app, "serialize_backtest_groups", None)
        self.assertTrue(callable(serialize), "Marginal research groups need a schema-aware serializer")
        result = serialize(grouped)
        self.assertEqual(result[0]["group_dimensions"], "signal")
        self.assertEqual(result[0]["group_value"], "short")
        self.assertEqual(result[0]["trades"], 35)
        self.assertAlmostEqual(result[0]["avg_return"], .01)
        self.assertNotIn("trend", result[0])
        self.assertNotIn("divergence_type", result[0])

    def test_two_dimensional_groups_do_not_publish_nan_as_a_signal(self):
        grouped = pd.DataFrame([{
            "group_dimensions": "divergence_type × strength_bucket",
            "group_value": "macd × strong", "divergence_type": "macd",
            "strength_bucket": "strong", "signal": float("nan"),
            "trades": 40, "win_rate": .55, "avg_return": -.002,
        }])
        serialize = getattr(app, "serialize_backtest_groups", None)
        self.assertTrue(callable(serialize), "Marginal research groups need a schema-aware serializer")
        result = serialize(grouped)
        self.assertNotIn("signal", result[0])
        self.assertEqual(result[0]["group_value"], "macd × strong")

    def test_evidence_endpoint_reads_reports_without_running_backtests(self):
        application = app.create_app()
        endpoint = next((route.endpoint for route in application.routes
                         if getattr(route, "path", None) == "/api/research-evidence"), None)
        self.assertIsNotNone(endpoint, "The research workspace needs its evidence endpoint")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, "REPORTS_DIR", Path(directory)), \
             patch.object(app, "run_signal_backtest_summary", side_effect=AssertionError("read-only endpoint")):
            result = asyncio.run(endpoint())
        self.assertEqual(result, {"scope": "RESEARCH_ONLY", "validations": []})


if __name__ == "__main__":
    unittest.main()
