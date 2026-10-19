"""Stop triggers cannot guarantee fills above an adverse opening gap."""
from __future__ import annotations

import unittest

import pandas as pd

from position_manager import LINEAR_USDM_V1, evaluate_lifecycle_bar
from project_signal_backtest import evaluate_trade


def rows(prices):
    return [dict(time=pd.Timestamp(i * 900_000, unit="ms", tz="UTC"),
                 open_time=i * 900_000, close_time=(i + 1) * 900_000 - 1,
                 open=price, high=price, low=price, close=price, atr=1., volume=1.)
            for i, price in enumerate(prices)]


class GapExecutionTest(unittest.TestCase):
    def replay(self, direction, bars, stop, **options):
        defaults = dict(return_model=LINEAR_USDM_V1, legacy_holding_limit=False)
        defaults.update(options)
        return evaluate_trade(dict(signal=direction, stop_loss=stop), bars, 1, 2., 96, .001, **defaults)

    def test_long_gap_stop_uses_open_and_exit_notional_fee(self):
        bars = rows([100., 100., 80.])
        bars[2].update(high=85., low=79., close=82.)
        trade = self.replay("long", bars, 90.)
        self.assertEqual(trade.exit_price, 80.)
        self.assertAlmostEqual(trade.return_pct, -.2018)
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertEqual(trade.exit_phase, "open")
        self.assertEqual(trade.exit_time, bars[2]["time"])

    def test_short_gap_stop_uses_open_and_exit_notional_fee(self):
        bars = rows([100., 100., 120.])
        bars[2].update(high=121., low=115., close=118.)
        trade = self.replay("short", bars, 110.)
        self.assertEqual(trade.exit_price, 120.)
        self.assertAlmostEqual(trade.return_pct, -.2022)
        self.assertEqual(trade.exit_phase, "open")

    def test_legacy_ratio_model_is_preserved_at_new_gap_fill(self):
        trade = self.replay("short", rows([100., 100., 120.]), 110.,
                            return_model="legacy_ratio_v1", legacy_holding_limit=True)
        self.assertEqual(trade.exit_price, 120.)
        self.assertAlmostEqual(trade.return_pct, 100 / 120 - 1 - .002)

    def test_normal_same_bar_stop_and_target_remains_stop_first(self):
        for direction, stop in [("long", 90.), ("short", 110.)]:
            with self.subTest(direction=direction):
                bars = rows([100., 100., 100.])
                bars[1].update(high=121., low=79.)
                trade = self.replay(direction, bars, stop)
                self.assertEqual((trade.exit_price, trade.exit_reason), (stop, "stop_loss"))

    def test_known_open_profit_precedes_later_range_stop(self):
        bars = rows([100., 100., 125.])
        bars[2].update(low=80., close=100.)
        trade = self.replay("long", bars, 90.)
        self.assertEqual((trade.exit_price, trade.exit_reason), (120., "take_profit"))
        self.assertEqual(trade.exit_phase, "open")

    def test_lifecycle_optional_open_fills_stop_at_observed_price(self):
        try:
            result = evaluate_lifecycle_bar("long", 79., 85., 82., 100., 90., 120., 110.,
                                            open_price=80., return_model=LINEAR_USDM_V1)
        except TypeError as exc:
            self.fail(f"opening-price contract missing: {exc}")
        self.assertEqual(result.decision.exit_price, 80.)
        self.assertEqual(result.exit_phase, "open")

    def test_new_trailing_stop_does_not_retest_its_arming_bar(self):
        bars = rows([100., 100., 106.])
        bars[1].update(high=106., low=99., close=106.)
        bars[2].update(high=107., low=105.2, close=106.5)
        trade = self.replay("long", bars, 95., stop_mode="atr_trailing_after_1r")
        self.assertEqual(trade.exit_reason, "timeout")
        self.assertEqual(trade.exit_time, bars[2]["time"])

    def test_partial_range_arming_bar_does_not_retest_new_stop(self):
        bars = rows([100., 100., 101.])
        bars[1].update(high=106., low=99., close=105.)
        bars[2].update(high=104., low=100.1, close=101.)
        trade = self.replay("long", bars, 95., stop_mode="partial_1r_breakeven")
        self.assertEqual(len(trade.exit_legs), 2)
        self.assertEqual(trade.exit_legs[0]["exit_time"], bars[1]["time"])
        self.assertEqual(trade.exit_time, bars[2]["time"])
        self.assertAlmostEqual(trade.return_pct, .023975)

    def test_partial_profit_at_open_preserves_half_quantity_and_known_order(self):
        bars = rows([100., 100., 106.])
        bars[2].update(high=107., low=99., close=102.)
        trade = self.replay("long", bars, 95., stop_mode="partial_1r_breakeven")
        self.assertEqual(len(trade.exit_legs), 2)
        self.assertEqual(trade.exit_legs[0]["fraction"], .5)
        self.assertEqual(trade.exit_legs[0].get("phase"), "open")
        self.assertEqual(trade.exit_reason, "protected_stop")
        self.assertEqual(trade.exit_legs[1]["fraction"], .5)
        self.assertAlmostEqual(trade.return_pct, .023975)

    def test_holding_limit_counts_are_unchanged(self):
        for legacy, expected_index in [(False, 2), (True, 3)]:
            with self.subTest(legacy=legacy):
                bars = rows([100., 100., 101., 102., 103.])
                trade = evaluate_trade(dict(signal="long", stop_loss=50.), bars, 1, 2., 2, 0.,
                                       return_model=LINEAR_USDM_V1, legacy_holding_limit=legacy)
                self.assertEqual(trade.exit_time, bars[expected_index]["time"])
                self.assertEqual(trade.exit_price, bars[expected_index]["close"])


if __name__ == "__main__":
    unittest.main()
