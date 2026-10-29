"""Cost re-evaluation preserves the trend rule and treats episodes as trades."""
from __future__ import annotations

import copy
import importlib
import importlib.util
import unittest

STEP = 900_000


class T28CostReviewTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("scripts.t28_cost_review"), "T28 review helper missing")
        return importlib.import_module("scripts.t28_cost_review")

    def test_cost_mapping_changes_no_signal_parameters_or_source_protocol(self):
        module = self.module()
        original = dict(scoring=dict(end_exclusive_utc="2026-06-20T00:00:00Z",
                                    predeclared_segment_boundaries_utc=["2024-08-01T00:00:00Z", "2026-06-20T00:00:00Z"]),
                        sizing=dict(window=60), candidates=[dict(id="T28_long_cash", lookback_days=28)])
        before = copy.deepcopy(original)
        extended = module.extend_protocol(original, "2026-10-25T00:00:00Z")
        self.assertEqual(original, before)
        self.assertEqual(extended["sizing"], original["sizing"])
        self.assertEqual(extended["candidates"], original["candidates"])
        self.assertEqual(module.target_cost(dict(id="verified", fee_rate=.0005, slippage_bps=2, delay_bars=1)),
                         dict(id="verified", fee_rate_per_fill=.0005, adverse_slippage_bps=2, extra_delay_minutes=15))

    def test_scale_changes_are_one_episode_and_same_time_funding_belongs_to_old_hold(self):
        fills = []
        for i, (side, signed, price, fee, gross, after) in enumerate([
            ("buy", 1., 100., 1., 0., 1.), ("buy", 1., 100., 1., 0., 2.),
            ("sell", -1., 110., 1.1, 10., 1.), ("sell", -1., 120., 1.2, 20., 0.),
            ("buy", 1., 100., 1., 0., 1.), ("sell", -1., 90., .9, -10., 0.)], 1):
            fills.append(dict(symbol="BTCUSDT", time=i*STEP, side=side, signed_quantity=signed,
                              quantity=abs(signed), notional=abs(signed)*price, fee=fee,
                              realized_gross_pnl=gross, quantity_after=after))
        result = dict(fills=fills, funding=[dict(symbol="BTCUSDT", time=4*STEP, phase="before_open_fills",
                                               held_quantity=1., cashflow=-1.)],
                      by_symbol={"BTCUSDT":dict(unrealized_pnl=0.)},
                      initial_equity=10000., final_equity=10012.8,
                      realized_gross_pnl=20., total_fees=6.2, funding_cashflow=-1.)
        stats = self.module().episode_statistics(result)
        self.assertEqual(stats["closed_episodes"], 2)
        self.assertEqual(stats["fills"], 6)
        self.assertEqual(stats["net_cash_win_rate"], .5)
        self.assertAlmostEqual(stats["cash_payoff_ratio"], 24.7/11.9)
        self.assertAlmostEqual(stats["normalized_episode_payoff_ratio"], .1235/.119)
        self.assertAlmostEqual(stats["closed"][0]["funding_cashflow"], -1.)
        self.assertAlmostEqual(stats["equity_reconciliation_error"], 0.)

    def test_open_tail_is_marked_and_excluded_from_episode_win_rate(self):
        result = dict(fills=[dict(symbol="BTCUSDT", time=STEP, side="buy", signed_quantity=1.,
                                 quantity=1., notional=100., fee=1., realized_gross_pnl=0., quantity_after=1.)],
                      funding=[], by_symbol={"BTCUSDT":dict(unrealized_pnl=5.)},
                      initial_equity=10000., final_equity=10004., realized_gross_pnl=0., total_fees=1., funding_cashflow=0.)
        stats = self.module().episode_statistics(result)
        self.assertEqual(stats["closed_episodes"], 0)
        self.assertIsNone(stats["net_cash_win_rate"])
        self.assertAlmostEqual(stats["open"][0]["marked_net_pnl"], 4.)


if __name__ == "__main__":
    unittest.main()
