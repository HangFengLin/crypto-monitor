from __future__ import annotations

import math
import unittest

import position_manager as positions

MODE = "partial_1r_breakeven"


class PartialExitLifecycleTest(unittest.TestCase):
    def lifecycle(self, direction="long", **overrides):
        values = dict(
            low=99, high=106, close=105, entry_price=100,
            initial_stop_loss=95, target_price=110, protection_price=105,
            stop_mode=MODE, fee_rate=0.001, return_model=positions.LINEAR_USDM_V1,
            remaining_fraction=1.0, partial_taken=False,
        )
        if direction == "short":
            values.update(low=94, high=101, close=95, initial_stop_loss=105,
                          target_price=90, protection_price=95)
        values.update(overrides)
        try:
            return positions.evaluate_lifecycle_bar(direction, **values)
        except TypeError as exc:
            self.fail(f"Partial lifecycle API is unavailable: {exc}")

    def test_first_one_r_closes_exactly_half_initial_quantity_in_both_directions(self):
        for direction, price in [("long", 105), ("short", 95)]:
            with self.subTest(direction=direction):
                result = self.lifecycle(direction)
                self.assertIsNone(result.decision)
                self.assertEqual(result.remaining_fraction, 0.5)
                self.assertTrue(result.partial_taken)
                self.assertTrue(result.protection_activated)
                self.assertEqual(len(result.partial_exits), 1)
                partial = result.partial_exits[0]
                self.assertEqual((partial.reason, partial.exit_price, partial.fraction, partial.outcome),
                                 ("partial_1r", price, 0.5, "win"))
                self.assertEqual(partial.fraction + result.remaining_fraction, 1.0)

    def test_remainder_breakeven_stop_uses_actual_linear_fees(self):
        for direction, price in [("long", 100.2002002002002), ("short", 99.8001998001998)]:
            with self.subTest(direction=direction):
                result = self.lifecycle(direction)
                self.assertAlmostEqual(result.active_stop, price, places=12)
                self.assertEqual(result.protected_stop_price, result.active_stop)
                self.assertAlmostEqual(positions.calculate_return_pct(
                    direction, 100, result.active_stop, 0.001,
                    return_model=positions.LINEAR_USDM_V1,
                ), 0.0, places=14)

    def test_same_bar_initial_stop_and_one_r_or_two_r_never_reduces_first(self):
        for direction in ["long", "short"]:
            for reaches_target in [False, True]:
                with self.subTest(direction=direction, reaches_target=reaches_target):
                    values = dict(low=94, high=111 if reaches_target else 106) if direction == "long" else dict(
                        high=106, low=89 if reaches_target else 94)
                    result = self.lifecycle(direction, **values)
                    self.assertEqual(result.decision.reason, "stop_loss")
                    self.assertEqual(result.decision.exit_price, 95 if direction == "long" else 105)
                    self.assertEqual(result.partial_exits, ())
                    self.assertEqual(result.remaining_fraction, 0.0)
                    self.assertFalse(result.partial_taken)

    def test_same_bar_one_r_and_two_r_books_half_at_each_threshold(self):
        for direction, partial_price, final_price in [("long", 105, 110), ("short", 95, 90)]:
            with self.subTest(direction=direction):
                result = self.lifecycle(direction, **(dict(high=111) if direction == "long" else dict(low=89)))
                self.assertEqual(result.partial_exits[0].exit_price, partial_price)
                self.assertEqual(result.partial_exits[0].fraction, 0.5)
                self.assertEqual((result.decision.reason, result.decision.exit_price), ("take_profit", final_price))
                self.assertEqual(result.remaining_fraction, 0.0)
                # The final decision consumes the input remainder minus the new partial fill.
                final_fraction = 1.0 - sum(part.fraction for part in result.partial_exits)
                self.assertEqual(final_fraction, 0.5)
                self.assertEqual(sum(part.fraction for part in result.partial_exits) + final_fraction, 1.0)

    def test_new_breakeven_stop_only_applies_from_next_bar(self):
        for direction in ["long", "short"]:
            with self.subTest(direction=direction):
                armed = self.lifecycle(direction)
                # The first bar crosses the future breakeven stop, but not the original stop.
                self.assertIsNone(armed.decision)
                next_values = dict(low=100.1, high=104, close=101) if direction == "long" else dict(
                    low=96, high=99.9, close=99)
                stopped = self.lifecycle(direction, remaining_fraction=0.5, partial_taken=True,
                                         active_stop=armed.active_stop, protection_activated=True,
                                         protected_stop_price=armed.protected_stop_price, **next_values)
                self.assertEqual((stopped.decision.reason, stopped.decision.outcome), ("protected_stop", "breakeven"))
                self.assertEqual(stopped.remaining_fraction, 0.0)
                self.assertEqual(stopped.partial_exits, ())

    def test_repeated_one_r_after_partial_never_emits_another_fill(self):
        for direction in ["long", "short"]:
            with self.subTest(direction=direction):
                armed = self.lifecycle(direction)
                next_values = dict(low=101, high=106, close=105) if direction == "long" else dict(
                    low=94, high=99, close=95)
                repeated = self.lifecycle(direction, remaining_fraction=0.5, partial_taken=True,
                                          active_stop=armed.active_stop, protection_activated=True,
                                          protected_stop_price=armed.protected_stop_price, **next_values)
                self.assertIsNone(repeated.decision)
                self.assertEqual(repeated.partial_exits, ())
                self.assertEqual(repeated.remaining_fraction, 0.5)
                self.assertTrue(repeated.partial_taken)

    def test_partial_never_widens_a_tighter_stop(self):
        for direction, tight_stop, values in [("long", 102, dict(low=103)),
                                               ("short", 98, dict(high=97))]:
            with self.subTest(direction=direction):
                result = self.lifecycle(direction, active_stop=tight_stop, **values)
                self.assertEqual(result.active_stop, tight_stop)
                self.assertEqual(result.protected_stop_price, tight_stop)
                self.assertEqual(result.remaining_fraction, 0.5)

    def test_untriggered_bar_keeps_full_quantity_and_no_partial_events(self):
        for direction in ["long", "short"]:
            with self.subTest(direction=direction):
                result = self.lifecycle(direction, low=99, high=101, close=100)
                self.assertIsNone(result.decision)
                self.assertEqual(result.partial_exits, ())
                self.assertEqual(result.remaining_fraction, 1.0)
                self.assertFalse(result.partial_taken)

    def test_small_partial_gross_profit_is_classified_as_net_loss(self):
        for direction in ["long", "short"]:
            with self.subTest(direction=direction):
                values = dict(initial_stop_loss=99.9, protection_price=100.1, target_price=100.2,
                              low=99.99, high=100.11, close=100.1) if direction == "long" else dict(
                                  initial_stop_loss=100.1, protection_price=99.9, target_price=99.8,
                                  high=100.01, low=99.89, close=99.9)
                result = self.lifecycle(direction, **values)
                self.assertEqual(result.partial_exits[0].outcome, "loss")

    def test_partial_mode_rejects_legacy_model_and_inconsistent_quantity_state(self):
        for values in [dict(return_model=positions.LEGACY_RATIO_V1),
                       dict(remaining_fraction=0.5, partial_taken=False),
                       dict(remaining_fraction=1.0, partial_taken=True),
                       dict(remaining_fraction=0), dict(remaining_fraction=1.1),
                       dict(remaining_fraction=-0.5), dict(remaining_fraction=math.nan),
                       dict(remaining_fraction=math.inf), dict(remaining_fraction=True),
                       dict(partial_taken=1), dict(partial_taken="false"),
                       dict(remaining_fraction=0.5, partial_taken=True, active_stop=95),
                       dict(initial_stop_loss=100), dict(protection_price=104),
                       dict(target_price=104)]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.lifecycle(**values)

    def test_old_modes_keep_original_events_and_default_quantity_fields(self):
        result = positions.evaluate_lifecycle_bar("long", 99, 106, 105, 100, 95, 110, 105)
        self.assertEqual((result.decision.reason, result.decision.exit_price), ("protection_reached", 105))
        self.assertEqual(getattr(result, "remaining_fraction", None), 1.0)
        self.assertFalse(getattr(result, "partial_taken", None))
        self.assertEqual(getattr(result, "partial_exits", None), ())


if __name__ == "__main__":
    unittest.main()
