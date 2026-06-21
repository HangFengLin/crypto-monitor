from __future__ import annotations

import unittest

from position_manager import calculate_return_pct, calculate_target_levels, evaluate_bar_exit, evaluate_price_exit


class PositionManagerTest(unittest.TestCase):
    def test_target_levels_cover_long_and_short(self) -> None:
        self.assertEqual(
            calculate_target_levels("long", 100, 95, 2),
            {"risk": 5, "target_price": 110, "protection_price": 105},
        )
        self.assertEqual(
            calculate_target_levels("short", 100, 105, 2),
            {"risk": 5, "target_price": 90, "protection_price": 95},
        )

    def test_invalid_stop_or_direction_has_no_levels(self) -> None:
        self.assertIsNone(calculate_target_levels("long", 100, 100, 2))
        self.assertIsNone(calculate_target_levels("short", 100, 99, 2))
        self.assertIsNone(calculate_target_levels("flat", 100, 95, 2))

    def test_same_bar_stop_and_target_uses_conservative_stop_first(self) -> None:
        long_decision = evaluate_bar_exit("long", 94, 111, 95, 110, 105)
        short_decision = evaluate_bar_exit("short", 89, 106, 105, 90, 95)
        self.assertEqual((long_decision.reason, long_decision.exit_price), ("stop_loss", 95))
        self.assertEqual((short_decision.reason, short_decision.exit_price), ("stop_loss", 105))

    def test_target_has_priority_over_protection(self) -> None:
        decision = evaluate_bar_exit("long", 99, 111, 95, 110, 105)
        self.assertEqual((decision.reason, decision.exit_price, decision.outcome), ("take_profit", 110, "win"))

    def test_protection_and_no_exit(self) -> None:
        decision = evaluate_price_exit("short", 94, 105, 90, 95)
        self.assertEqual((decision.reason, decision.exit_price), ("protection_reached", 95))
        self.assertIsNone(evaluate_price_exit("long", 102, 95, 110, 105))

    def test_return_calculation_supports_fees(self) -> None:
        self.assertAlmostEqual(calculate_return_pct("long", 100, 110, 0.001), 0.098)
        self.assertAlmostEqual(calculate_return_pct("short", 100, 90), 100 / 90 - 1)


if __name__ == "__main__":
    unittest.main()
