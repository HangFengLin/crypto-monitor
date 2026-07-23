from __future__ import annotations

import unittest

from position_manager import (
    ATR_TRAILING_AFTER_1R_STOP_MODE,
    STRUCTURE_ATR_BREAKEVEN_AFTER_1R_STOP_MODE,
    STRUCTURE_ATR_STOP_MODE,
    calculate_return_pct,
    calculate_target_levels,
    evaluate_bar_exit,
    evaluate_lifecycle_bar,
    evaluate_price_exit,
)


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

    def test_lifecycle_breakeven_arms_without_exiting_at_one_r(self) -> None:
        result = evaluate_lifecycle_bar(
            "long",
            low=99,
            high=106,
            close=105,
            entry_price=100,
            initial_stop_loss=95,
            target_price=110,
            protection_price=105,
            stop_mode=STRUCTURE_ATR_BREAKEVEN_AFTER_1R_STOP_MODE,
            fee_rate=0.001,
        )
        self.assertIsNone(result.decision)
        self.assertTrue(result.protection_activated)
        self.assertAlmostEqual(result.active_stop, 100.2)

        stopped = evaluate_lifecycle_bar(
            "long",
            low=100.1,
            high=106,
            close=100.2,
            entry_price=100,
            initial_stop_loss=95,
            target_price=110,
            protection_price=105,
            active_stop=result.active_stop,
            highest_price=result.highest_price,
            lowest_price=result.lowest_price,
            protection_activated=result.protection_activated,
            protected_stop_price=result.protected_stop_price,
            stop_mode=STRUCTURE_ATR_BREAKEVEN_AFTER_1R_STOP_MODE,
            fee_rate=0.001,
        )
        self.assertEqual((stopped.decision.reason, stopped.decision.exit_price, stopped.decision.outcome), ("protected_stop", 100.2, "breakeven"))

    def test_lifecycle_default_remains_structure_atr(self) -> None:
        result = evaluate_lifecycle_bar(
            "long",
            low=99,
            high=106,
            close=105,
            entry_price=100,
            initial_stop_loss=95,
            target_price=110,
            protection_price=105,
        )
        self.assertEqual(STRUCTURE_ATR_STOP_MODE, "structure_atr")
        self.assertEqual((result.decision.reason, result.decision.exit_price), ("protection_reached", 105))

    def test_lifecycle_atr_trailing_long_moves_stop_after_one_r(self) -> None:
        result = evaluate_lifecycle_bar(
            "long",
            low=99,
            high=107,
            close=106,
            entry_price=100,
            initial_stop_loss=95,
            target_price=115,
            protection_price=105,
            stop_mode=ATR_TRAILING_AFTER_1R_STOP_MODE,
            fee_rate=0.001,
            atr_value=2,
        )
        self.assertIsNone(result.decision)
        self.assertTrue(result.protection_activated)
        self.assertAlmostEqual(result.active_stop, 104.6)

        stopped = evaluate_lifecycle_bar(
            "long",
            low=104.5,
            high=106,
            close=105,
            entry_price=100,
            initial_stop_loss=95,
            target_price=115,
            protection_price=105,
            active_stop=result.active_stop,
            highest_price=result.highest_price,
            lowest_price=result.lowest_price,
            protection_activated=result.protection_activated,
            protected_stop_price=result.protected_stop_price,
            stop_mode=ATR_TRAILING_AFTER_1R_STOP_MODE,
            fee_rate=0.001,
            atr_value=2,
        )
        self.assertEqual((stopped.decision.reason, stopped.decision.exit_price, stopped.decision.outcome), ("trailing_stop", 104.6, "win"))

    def test_lifecycle_atr_trailing_short_moves_stop_after_one_r(self) -> None:
        result = evaluate_lifecycle_bar(
            "short",
            low=93,
            high=101,
            close=94,
            entry_price=100,
            initial_stop_loss=105,
            target_price=85,
            protection_price=95,
            stop_mode=ATR_TRAILING_AFTER_1R_STOP_MODE,
            fee_rate=0.001,
            atr_value=2,
        )
        self.assertIsNone(result.decision)
        self.assertTrue(result.protection_activated)
        self.assertAlmostEqual(result.active_stop, 95.4)

        stopped = evaluate_lifecycle_bar(
            "short",
            low=94,
            high=95.5,
            close=95,
            entry_price=100,
            initial_stop_loss=105,
            target_price=85,
            protection_price=95,
            active_stop=result.active_stop,
            highest_price=result.highest_price,
            lowest_price=result.lowest_price,
            protection_activated=result.protection_activated,
            protected_stop_price=result.protected_stop_price,
            stop_mode=ATR_TRAILING_AFTER_1R_STOP_MODE,
            fee_rate=0.001,
            atr_value=2,
        )
        self.assertEqual((stopped.decision.reason, stopped.decision.exit_price, stopped.decision.outcome), ("trailing_stop", 95.4, "win"))

    def test_return_calculation_supports_fees(self) -> None:
        self.assertAlmostEqual(calculate_return_pct("long", 100, 110, 0.001), 0.098)
        self.assertAlmostEqual(calculate_return_pct("short", 100, 90), 100 / 90 - 1)


if __name__ == "__main__":
    unittest.main()
