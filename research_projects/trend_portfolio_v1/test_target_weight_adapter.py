"""Hand-computable collateral, timing and target-weight contracts."""

from __future__ import annotations

import copy
import importlib.util
import math
import unittest
from pathlib import Path

FILE = Path(__file__).with_name("target_weight_adapter.py")
if FILE.exists():
    SPEC = importlib.util.spec_from_file_location("target_weight_under_test", FILE)
    adapter = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(adapter)
else:
    adapter = None

STEP = 900000
DAY = 86400000


def fixture(count=4):
    data = {
        s: [
            dict(
                open_time=i * STEP,
                close_time=(i + 1) * STEP - 1,
                open=100.0,
                high=100.0,
                low=100.0,
                close=100.0,
                volume=1.0,
            )
            for i in range(count)
        ]
        for s in ("A", "B")
    }
    funding = {s: dict(start=0, end=count * STEP, complete=True, records=[]) for s in data}
    protocol = dict(
        symbols=["A", "B"],
        accounting=dict(initial_equity_usdt=10000),
        scoring=dict(start_inclusive_utc="1970-01-01T00:00:00Z", end_exclusive_utc="1970-01-01T01:00:00Z"),
    )
    cost = dict(id="manual", fee_rate_per_fill=0.0, adverse_slippage_bps=0.0, extra_delay_minutes=0)
    targets = [dict(decision_time_ms=0, known_at=0, weights={"A": 0.5, "B": 0.5})]
    return data, funding, protocol, cost, targets


class TargetWeightTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(adapter, "Offline target-weight adapter is absent")
        return adapter

    def replay(self, data, funding, protocol, cost, targets):
        return self.api().replay_target_weights(data, funding, targets, protocol, cost)

    def test_collateral_is_not_spot_cash_and_primary_tail_stays_open(self):
        d, f, p, c, t = fixture()
        before = copy.deepcopy((d, f, p, c, t))
        r = self.replay(d, f, p, c, t)
        self.assertEqual(r["final_cash"], 10000)
        self.assertEqual(r["final_equity"], 10000)
        self.assertEqual(r["final_positions"]["A"]["quantity"], 50)
        self.assertEqual(r["final_positions"]["B"]["quantity"], 50)
        self.assertEqual(len(r["fills"]), 2)
        self.assertEqual({x["time"] for x in r["fills"]}, {900000})
        self.assertEqual((d, f, p, c, t), before)

    def test_fees_are_reserved_by_post_friction_equity_solver(self):
        d, f, p, c, t = fixture()
        c["fee_rate_per_fill"] = 0.01
        r = self.replay(d, f, p, c, t)
        self.assertAlmostEqual(r["final_equity"], 9900.990099009901)
        self.assertAlmostEqual(r["final_positions"]["A"]["quantity"], 49.504950495049506)
        self.assertAlmostEqual(r["total_fees"], 99.00990099009901)
        self.assertLessEqual(r["rebalances"][0]["post_gross_notional"], r["rebalances"][0]["post_equity"] + 1e-9)

    def test_slippage_realized_reductions_and_quantity_conservation(self):
        d, f, p, c, t = fixture()
        c["adverse_slippage_bps"] = 100
        t.append(dict(decision_time_ms=STEP, known_at=STEP, weights={"A": 0.0, "B": 0.0}))
        r = self.replay(d, f, p, c, t)
        self.assertEqual([x["price"] for x in r["fills"]], [101.0, 101.0, 99.0, 99.0])
        self.assertAlmostEqual(r["final_cash"], 9801.980198019803)
        self.assertAlmostEqual(r["realized_gross_pnl"], -198.01980198019803)
        self.assertEqual(sum(x["quantity"] for x in r["final_positions"].values()), 0)
        self.assertAlmostEqual(sum(x["signed_quantity"] for x in r["fills"]), 0)

    def test_exact_open_funding_uses_old_quantity_and_later_event_uses_new(self):
        d, f, p, c, t = fixture()
        t[0]["weights"] = {"A": 0.5, "B": 0.0}
        t.append(dict(decision_time_ms=STEP, known_at=STEP, weights={"A": 0.0, "B": 0.0}))
        f["A"]["records"] = [
            dict(time=time, rate=0.001, mark_price=100.0) for time in [STEP, STEP + 1, 2 * STEP, 2 * STEP + 1]
        ]
        r = self.replay(d, f, p, c, t)
        self.assertEqual([x["held_quantity"] for x in r["funding"]], [0.0, 50.0, 50.0, 0.0])
        self.assertEqual([x["cashflow"] for x in r["funding"]], [0.0, -5.0, -5.0, 0.0])
        self.assertEqual(r["final_equity"], 9990)

    def test_extra_delay_uses_later_open_without_execution_range_sizing(self):
        d, f, p, c, t = fixture()
        c["extra_delay_minutes"] = 15
        d["A"][2].update(open=110.0, high=1000.0, low=1.0, close=100.0)
        r = self.replay(d, f, p, c, t)
        self.assertEqual({x["time"] for x in r["fills"]}, {1800000})
        self.assertAlmostEqual(r["final_positions"]["A"]["quantity"], 5000 / 110)
        self.assertEqual(r["fills"][0]["raw_price"], 110)

    def test_nonfinite_or_missing_inputs_fail_instead_of_becoming_cash(self):
        d, f, p, c, t = fixture()
        f["A"]["complete"] = False
        with self.assertRaises(ValueError):
            self.replay(d, f, p, c, t)
        d, f, p, c, t = fixture()
        d["A"].pop(1)
        with self.assertRaises(ValueError):
            self.replay(d, f, p, c, t)
        d, f, p, c, t = fixture()
        t[0]["weights"]["A"] = math.nan
        with self.assertRaises(ValueError):
            self.replay(d, f, p, c, t)
        d, f, p, c, t = fixture()
        t[0]["weights"] = {"A": 0.6, "B": 0.5}
        with self.assertRaises(ValueError):
            self.replay(d, f, p, c, t)

    def test_daily_signal_uses_sample_log_volatility_and_zero_is_valid(self):
        closes = [100.0]
        for i in range(84):
            closes.append(closes[-1] * math.exp(0.02 if i % 2 == 0 else 0.06))
        r = self.api().daily_weight(closes, 28)
        annual = math.sqrt((60 * 0.0004 / 59) * 365)
        self.assertAlmostEqual(r["annualized_volatility"], annual)
        self.assertAlmostEqual(r["weight"], 0.1 / annual)
        self.assertEqual(self.api().daily_weight([100.0] * 85, 84)["weight"], 0)
        self.assertEqual(self.api().daily_weight([100.0] * 85, None)["weight"], 0.5)
        with self.assertRaises(ValueError):
            self.api().daily_weight([100.0] * 60, 28)

    def test_weekly_signal_is_monday_closed_daily_only_and_no_future_reweighting(self):
        d = {s: [] for s in ["A", "B"]}
        for s in d:
            for i in range(110 * 96):
                price = 100.0 + (i // 96) * 0.1
                d[s].append(
                    dict(
                        open_time=i * STEP,
                        close_time=(i + 1) * STEP - 1,
                        open=price,
                        high=price,
                        low=price,
                        close=price,
                        volume=1.0,
                    )
                )
        p = dict(
            symbols=["A", "B"],
            candidates=[dict(id="T28_long_cash", lookback_days=28), dict(id="T84_long_cash", lookback_days=84)],
            scoring=dict(start_inclusive_utc="1970-04-01T00:00:00Z", end_exclusive_utc="1970-04-20T00:00:00Z"),
        )
        a = self.api().build_weekly_targets(d, p, "T84_long_cash")
        self.assertEqual(a[0]["decision_time_ms"], 95 * DAY)
        self.assertEqual(a[0]["features"]["A"]["last_daily_end_ms"], 95 * DAY)
        for s in d:
            for bar in d[s]:
                if bar["open_time"] >= 95 * DAY:
                    bar.update(open=1e6, high=1e6, low=1e6, close=1e6)
        b = self.api().build_weekly_targets(d, p, "T84_long_cash")
        self.assertEqual(a[0], b[0])


if __name__ == "__main__":
    unittest.main()
