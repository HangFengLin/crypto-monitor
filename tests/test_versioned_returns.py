from __future__ import annotations

import math
import unittest

import position_manager as positions

LEGACY = "legacy_ratio_v1"
LINEAR = "linear_usdm_v1"


class VersionedReturnsTest(unittest.TestCase):
    def call_versioned(self, function, *args, **kwargs):
        """Turn the absent versioned API into an explicit feature failure."""
        try:
            return function(*args, **kwargs)
        except TypeError as exc:
            self.fail(f"Versioned accounting API is unavailable: {exc}")

    def lifecycle(self, direction="long", **overrides):
        values = dict(
            low=99.99,
            high=100.11,
            close=100.1,
            entry_price=100,
            initial_stop_loss=99.9,
            target_price=100.2,
            protection_price=100.1,
            fee_rate=0.001,
            return_model=LINEAR,
        )
        if direction == "short":
            values.update(low=99.89, high=100.01, close=99.9,
                          initial_stop_loss=100.1, target_price=99.8,
                          protection_price=99.9)
        values.update(overrides)
        return self.call_versioned(positions.evaluate_lifecycle_bar, direction, **values)

    def test_linear_net_returns_charge_each_fill_notional(self):
        # One coin: +10 USDT PnL minus 0.1 + 0.11 long fees, or 0.1 + 0.09 short fees.
        for direction, exit_price, want in [("long", 110, 0.0979), ("short", 90, 0.0981)]:
            with self.subTest(direction=direction):
                actual = self.call_versioned(
                    positions.calculate_return_pct, direction, 100, exit_price,
                    0.001, return_model=LINEAR,
                )
                self.assertAlmostEqual(actual, want, places=14)

    def test_legacy_defaults_and_explicit_version_preserve_exact_values(self):
        for direction, exit_price, want in [("long", 110, 110 / 100 - 1 - 0.002),
                                            ("short", 90, 100 / 90 - 1 - 0.002)]:
            with self.subTest(direction=direction):
                self.assertEqual(positions.calculate_return_pct(direction, 100, exit_price, 0.001), want)
                self.assertEqual(self.call_versioned(
                    positions.calculate_return_pct, direction, 100, exit_price,
                    0.001, return_model=LEGACY,
                ), want)

    def test_linear_breakeven_prices_make_net_return_zero(self):
        for direction, want in [("long", 100.2002002002002), ("short", 99.8001998001998)]:
            with self.subTest(direction=direction):
                price = self.call_versioned(positions.breakeven_stop_price, direction, 100,
                                            0.001, return_model=LINEAR)
                self.assertAlmostEqual(price, want, places=12)
                self.assertAlmostEqual(self.call_versioned(
                    positions.calculate_return_pct, direction, 100, price,
                    0.001, return_model=LINEAR,
                ), 0.0, places=14)
                self.assertEqual(self.call_versioned(
                    positions.classify_return_outcome, direction, 100, price,
                    0.001, return_model=LINEAR,
                ), "breakeven")

    def test_legacy_breakeven_prices_preserve_exact_values(self):
        self.assertEqual(positions.breakeven_stop_price("long", 100, 0.001), 100 * 1.002)
        self.assertEqual(positions.breakeven_stop_price("short", 100, 0.001), 100 / 1.002)

    def test_small_one_r_profit_is_classified_after_costs(self):
        for direction, exit_price in [("long", 100.1), ("short", 99.9)]:
            with self.subTest(direction=direction):
                self.assertEqual(self.call_versioned(
                    positions.classify_return_outcome, direction, 100, exit_price,
                    0.001, return_model=LINEAR,
                ), "loss")
                self.assertEqual(self.lifecycle(direction).decision.outcome, "loss")

    def test_linear_take_profit_uses_net_classification_for_all_stop_modes(self):
        for direction in ["long", "short"]:
            for mode in positions.STOP_MODE_CHOICES:
                with self.subTest(direction=direction, mode=mode):
                    overrides = dict(high=100.3, target_price=100.15) if direction == "long" else dict(low=99.7, target_price=99.85)
                    result = self.lifecycle(direction, stop_mode=mode, **overrides)
                    self.assertEqual(result.decision.reason, "take_profit")
                    self.assertEqual(result.decision.outcome, "loss")

    def test_legacy_exit_outcomes_keep_previous_trigger_classification(self):
        for direction in ["long", "short"]:
            with self.subTest(direction=direction):
                self.assertEqual(self.lifecycle(direction, return_model=LEGACY).decision.outcome, "win")

    def test_breakeven_mode_forwards_model_into_stop_and_later_exit(self):
        for direction, want in [("long", 100.2002002002002), ("short", 99.8001998001998)]:
            with self.subTest(direction=direction):
                values = dict(high=106, low=99, close=105, initial_stop_loss=95,
                              protection_price=105, target_price=110) if direction == "long" else dict(
                                  high=101, low=94, close=95, initial_stop_loss=105,
                                  protection_price=95, target_price=90)
                mode = positions.STRUCTURE_ATR_BREAKEVEN_AFTER_1R_STOP_MODE
                armed = self.lifecycle(direction, stop_mode=mode, **values)
                self.assertIsNone(armed.decision)
                self.assertAlmostEqual(armed.active_stop, want, places=12)
                stopped = self.lifecycle(direction, stop_mode=mode, active_stop=armed.active_stop,
                                         protection_activated=True, high=100.3, low=99.7, close=100,
                                         **{key: values[key] for key in ["initial_stop_loss", "protection_price", "target_price"]})
                self.assertEqual(stopped.decision.reason, "protected_stop")
                self.assertEqual(stopped.decision.outcome, "breakeven")

    def test_trailing_mode_uses_linear_fee_adjusted_breakeven_floor(self):
        for direction, want in [("long", 100.2002002002002), ("short", 99.8001998001998)]:
            with self.subTest(direction=direction):
                values = dict(high=106, low=99, close=105, initial_stop_loss=95,
                              protection_price=105, target_price=110) if direction == "long" else dict(
                                  high=101, low=94, close=95, initial_stop_loss=105,
                                  protection_price=95, target_price=90)
                result = self.lifecycle(direction, stop_mode=positions.ATR_TRAILING_AFTER_1R_STOP_MODE,
                                        atr_value=10, **values)
                self.assertIsNone(result.decision)
                self.assertAlmostEqual(result.active_stop, want, places=12)

    def test_linear_same_bar_stop_retains_conservative_priority(self):
        result = self.lifecycle(low=94, high=111, close=100, initial_stop_loss=95,
                                target_price=110, protection_price=105)
        self.assertEqual((result.decision.reason, result.decision.exit_price, result.decision.outcome),
                         ("stop_loss", 95, "loss"))

    def test_linear_bar_and_price_exit_classify_net_return(self):
        decision = self.call_versioned(positions.evaluate_bar_exit, "long", 99.99, 100.11,
                                      99.9, 100.2, 100.1, entry_price=100,
                                      fee_rate=0.001, return_model=LINEAR)
        self.assertEqual(decision.outcome, "loss")
        decision = self.call_versioned(positions.evaluate_price_exit, "short", 99.9,
                                      100.1, 99.8, 99.9, entry_price=100,
                                      fee_rate=0.001, return_model=LINEAR)
        self.assertEqual(decision.outcome, "loss")

    def test_linear_rejects_invalid_prices_and_fees(self):
        for invalid in [0, -1, math.nan, math.inf, -math.inf, True, "100", None]:
            for field in ["entry_price", "exit_price"]:
                with self.subTest(field=field, value=invalid):
                    arguments = dict(entry_price=100, exit_price=110, fee_rate=0.001)
                    arguments[field] = invalid
                    with self.assertRaises(ValueError):
                        self.call_versioned(positions.calculate_return_pct, "long", return_model=LINEAR, **arguments)
        for invalid in [-0.001, 1, math.nan, math.inf, True, "0.001", None]:
            with self.subTest(fee_rate=invalid), self.assertRaises(ValueError):
                self.call_versioned(positions.calculate_return_pct, "long", 100, 110,
                                    invalid, return_model=LINEAR)
        with self.assertRaises(ValueError):
            self.call_versioned(positions.calculate_return_pct, "flat", 100, 110, return_model=LINEAR)
        with self.assertRaises(ValueError):
            self.call_versioned(positions.calculate_return_pct, "long", 1e-308, 1e308, return_model=LINEAR)
        self.assertAlmostEqual(self.call_versioned(positions.calculate_return_pct, "long", 100, 100,
                                                  0.5, return_model=LINEAR), -1.0)

    def test_unknown_model_fails_even_when_no_exit_would_trigger(self):
        functions = [
            (positions.calculate_return_pct, ("long", 100, 110), {}),
            (positions.classify_return_outcome, ("long", 100, 110), {}),
            (positions.breakeven_stop_price, ("long", 100), {}),
            (positions.update_trailing_stop, ("long", 95, 100, 5, 101, None), {}),
            (positions.evaluate_bar_exit, ("long", 99, 101, 95, 110, 105), {}),
            (positions.evaluate_price_exit, ("long", 101, 95, 110, 105), {}),
            (positions.evaluate_lifecycle_bar, ("long", 99, 101, 100, 100, 95, 110, 105), {}),
        ]
        for function, args, kwargs in functions:
            with self.subTest(function=function.__name__), self.assertRaises(ValueError):
                self.call_versioned(function, *args, return_model="unknown_v1", **kwargs)

    def test_linear_lifecycle_rejects_invalid_untriggered_input(self):
        for values in [dict(low=math.nan), dict(entry_price=0), dict(fee_rate=-0.001),
                       dict(active_stop=math.inf), dict(target_price=-10)]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.lifecycle(high=100.01, **values)


if __name__ == "__main__":
    unittest.main()
