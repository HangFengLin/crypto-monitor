"""Postprocessing does not alter frozen accounts or confuse PF with payoff."""
from __future__ import annotations

import importlib
import importlib.util
import unittest


class T28CostStatsTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("scripts.t28_cost_stats"))
        return importlib.import_module("scripts.t28_cost_stats")

    def test_cash_profit_factor_uses_total_positive_and_negative_episode_money(self):
        rows = [dict(net_cash_pnl=x) for x in (10.,20.,30.,-10.)]
        result = self.module().cash_profit_factor(rows)
        self.assertEqual(result["cash_profit_factor"], 6.)
        self.assertEqual(result["positive_net_pnl_usdt"], 60.)
        self.assertEqual(result["negative_net_pnl_abs_usdt"], 10.)

    def test_no_closed_episodes_does_not_produce_infinite_or_zero_profit_factor(self):
        self.assertIsNone(self.module().cash_profit_factor([])["cash_profit_factor"])
        self.assertIsNone(self.module().cash_profit_factor([dict(net_cash_pnl=10.)])["cash_profit_factor"])

    def test_equivalence_bridge_matches_values_not_scenario_names(self):
        old = dict(scenarios=[dict(id="legacy_base", fee_rate=.001, slippage_bps=2, delay_bars=0)])
        new = dict(scenarios=[dict(id="base", fee_rate=.001, slippage_bps=2, delay_bars=0)])
        self.assertEqual(self.module().scenario_equivalence(old,new), {"base":"legacy_base"})
        new["scenarios"][0]["fee_rate"] = .0005
        with self.assertRaises(ValueError):
            self.module().scenario_equivalence(old,new)


if __name__ == "__main__":
    unittest.main()
