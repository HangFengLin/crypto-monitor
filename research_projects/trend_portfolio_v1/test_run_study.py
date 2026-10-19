"""Original experiment scope and economic-screen decisions, no real returns."""

import copy
import importlib.util
import json
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILE = HERE / "run_study.py"
if FILE.exists():
    SPEC = importlib.util.spec_from_file_location("trend_runner_under_test", FILE)
    runner = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(runner)
else:
    runner = None
PROTOCOL = json.loads((HERE / "protocol.json").read_text())


class TrendStudyTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(runner, "Frozen12-scenario trend driver is absent")
        return runner

    def test_frozen12_scenarios_only_and_existing_protocol_unchanged(self):
        self.assertEqual(
            self.api().validate_scope(PROTOCOL),
            ["T28_long_cash", "T84_long_cash", "vol_scaled_hold", "equal_weight_buy_hold"],
        )
        for area, key, value in [
            ("sizing", "volatility_window_returns", 61),
            ("economic_screen", "all_required", []),
            ("research_budget", "candidate_count", 3),
        ]:
            p = copy.deepcopy(PROTOCOL)
            p[area][key] = value
            with self.assertRaises(ValueError):
                self.api().validate_scope(p)

    def test_economic_screen_requires_absolute_double_increment_and_drawdown(self):
        result = {
            s: {c["id"]: dict(net_return=0.1, max_drawdown=0.1) for c in PROTOCOL["cost_scenarios"]}
            for s in ["T28_long_cash", "T84_long_cash", "vol_scaled_hold", "equal_weight_buy_hold"]
        }
        result["vol_scaled_hold"]["base"]["net_return"] = 0.08
        d = self.api().economic_decisions(result, PROTOCOL)
        self.assertTrue(d["T28_long_cash"]["continue_diagnostic_research"])
        self.assertFalse(d["T28_long_cash"]["promotion_eligible"])
        for scenario, key, value in [
            ("base", "net_return", 0.0),
            ("double_cost", "net_return", 0.0),
            ("base", "net_return", 0.07),
            ("base", "max_drawdown", 0.11),
        ]:
            p = copy.deepcopy(result)
            p["T28_long_cash"][scenario][key] = value
            d = self.api().economic_decisions(p, PROTOCOL)
            self.assertFalse(d["T28_long_cash"]["continue_diagnostic_research"])

    def test_calendar_segments_and_week_differences_do_not_reset_capital(self):
        score = dict(
            start_inclusive_utc="1970-01-01T00:00:00Z",
            end_exclusive_utc="1970-01-15T00:00:00Z",
            predeclared_segment_boundaries_utc=["1970-01-01T00:00:00Z", "1970-01-08T00:00:00Z", "1970-01-15T00:00:00Z"],
        )
        curve = [dict(time=i * 86400000, equity=10000.0 + i * 100) for i in range(1, 15)]
        a = self.api().calendar_metrics(curve, 10000.0, score)
        self.assertEqual(a["segments"][0]["start_equity"], 10000.0)
        self.assertEqual(a["segments"][1]["start_equity"], 10700.0)
        self.assertAlmostEqual(a["segments"][1]["net_return"], 11400 / 10700 - 1)
        self.assertEqual(a["full_weeks"][0]["start_ms"], 345600000)
        self.assertAlmostEqual(a["full_weeks"][0]["net_return"], 11100 / 10400 - 1)


if __name__ == "__main__":
    unittest.main()
