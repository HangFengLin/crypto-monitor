from __future__ import annotations

import copy
import dataclasses
import unittest

import strategy

STEP = 900000


def config(strict=True):
    result = dataclasses.replace(strategy.DEFAULT_CONFIG, confirmation_mode="macd_only")
    # This also runs the real failure against the pre-feature engine, rather
    # than stopping at a missing constructor argument.
    object.__setattr__(result, "strict_candidate_lifecycle", strict)
    return result


def fixture(direction="long", count=130, first=90, second=96):
    side = 1 if direction == "long" else -1
    rows = []
    for index in range(count):
        rows.append(dict(open_time=index * STEP, close_time=(index + 1) * STEP - 1,
                         open=100.0, high=101.0, low=99.0, close=100.0, volume=100.0,
                         macd=0.0, macd_signal=0.0, macd_fast=0.0, atr=2.0,
                         rsi=30.0 if side == 1 else 70.0, ema60=110.0 if side == 1 else 90.0,
                         volume_ratio=2.0, obv_slope=float(side), chop=30.0, bb_width=.03,
                         higher_close=110.0 if side == 1 else 90.0,
                         higher_ema20=108.0 if side == 1 else 92.0, higher_ema60=100.0,
                         higher_ema200=90.0 if side == 1 else 110.0, higher_macd=float(side)))
    for index, price, macd in ((first, 90.0 if side == 1 else 110.0, -2.0 * side),
                               (second, 80.0 if side == 1 else 120.0, -1.0 * side)):
        rows[index].update(open=price, high=price + 1, low=price - 1, close=price, macd=macd)
    return rows


def cross(rows, index, direction):
    rows[index]["macd"] = 1.0 if direction == "long" else -1.0


class CandidateLifecycleTest(unittest.TestCase):
    def test_default_off_preserves_late_first_confirmation(self):
        rows = fixture(first=20, second=40)
        cross(rows, 114, "long")
        engine = strategy.ProjectSignalEngine(config(False))
        engine.detect(rows[:100])
        for end in range(101, 115):
            engine.detect(rows[:end])
        self.assertEqual(engine.detect(rows[:115])["signal"], "long")
        self.assertFalse(getattr(strategy.DEFAULT_CONFIG, "strict_candidate_lifecycle", False))

    def test_old_original_ready_time_cannot_create_a_new_candidate(self):
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                rows = fixture(direction, first=20, second=40)
                cross(rows, 99, direction)
                engine = strategy.ProjectSignalEngine(config())
                result = engine.detect(rows[:100])
                self.assertNotEqual(result["signal"], direction)
                self.assertIsNone(getattr(engine, "pending_buy" if direction == "long" else "pending_sell"))

    def test_timeout_consumes_same_divergence_before_a_later_first_cross(self):
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                rows = fixture(direction)
                cross(rows, 113, direction)
                engine = strategy.ProjectSignalEngine(config())
                engine.detect(rows[:100])
                for end in range(101, 114):
                    result = engine.detect(rows[:end])
                self.assertEqual(result["confirm_bars"], 13)
                result = engine.detect(rows[:114])
                self.assertNotEqual(result["signal"], direction)
                self.assertIsNone(getattr(engine, "pending_buy" if direction == "long" else "pending_sell"))

    def test_twelfth_bar_is_valid_but_thirteenth_bar_is_expired(self):
        for direction in ("long", "short"):
            for crossing_index, expected in ((111, True), (112, False)):
                with self.subTest(direction=direction, crossing_index=crossing_index):
                    rows = fixture(direction)
                    cross(rows, crossing_index, direction)
                    engine = strategy.ProjectSignalEngine(config())
                    engine.detect(rows[:100])
                    for end in range(101, crossing_index + 2):
                        result = engine.detect(rows[:end])
                    self.assertEqual(result["signal"] == direction, expected)

    def test_break_then_recover_during_wait_consumes_candidate(self):
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                rows = fixture(direction)
                rows[100]["low" if direction == "long" else "high"] = 78.0 if direction == "long" else 122.0
                cross(rows, 101, direction)
                engine = strategy.ProjectSignalEngine(config())
                engine.detect(rows[:100])
                engine.detect(rows[:101])
                self.assertNotEqual(engine.detect(rows[:102])["signal"], direction)

    def test_break_between_extreme_and_its_ready_bar_is_already_invalid(self):
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                rows = fixture(direction)
                rows[97]["low" if direction == "long" else "high"] = 78.0 if direction == "long" else 122.0
                cross(rows, 99, direction)
                engine = strategy.ProjectSignalEngine(config())
                self.assertNotEqual(engine.detect(rows[:100])["signal"], direction)

    def test_raw_extreme_wick_anchor_and_equality_are_preserved(self):
        for direction in ("long", "short"):
            for price in ((79.0, 79.5) if direction == "long" else (121.0, 120.5)):
                with self.subTest(direction=direction, price=price):
                    rows = fixture(direction)
                    rows[100]["low" if direction == "long" else "high"] = price
                    cross(rows, 101, direction)
                    engine = strategy.ProjectSignalEngine(config())
                    engine.detect(rows[:100])
                    engine.detect(rows[:101])
                    self.assertEqual(engine.detect(rows[:102])["signal"], direction)

    def test_first_cross_with_failed_context_is_terminal(self):
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                rows = fixture(direction)
                cross(rows, 100, direction)
                rows[100]["higher_close"] = 90.0 if direction == "long" else 110.0
                rows[102]["macd"] = -.2 if direction == "long" else .2
                cross(rows, 103, direction)
                engine = strategy.ProjectSignalEngine(config())
                engine.detect(rows[:100])
                self.assertNotEqual(engine.detect(rows[:101])["signal"], direction)
                engine.detect(rows[:102])
                engine.detect(rows[:103])
                self.assertNotEqual(engine.detect(rows[:104])["signal"], direction)

    def test_successful_confirmation_cannot_emit_same_setup_again(self):
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                rows = fixture(direction)
                cross(rows, 100, direction)
                engine = strategy.ProjectSignalEngine(config())
                engine.detect(rows[:100])
                self.assertEqual(engine.detect(rows[:101])["signal"], direction)
                self.assertNotEqual(engine.detect(rows[:101])["signal"], direction)

    def test_new_divergence_remains_independently_eligible(self):
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                rows = fixture(direction)
                cross(rows, 100, direction)
                side = 1 if direction == "long" else -1
                price = 70.0 if direction == "long" else 130.0
                rows[110].update(open=price, high=price + 1, low=price - 1, close=price, macd=-.5 * side)
                cross(rows, 113, direction)
                engine = strategy.ProjectSignalEngine(config())
                for end in range(100, 115):
                    result = engine.detect(rows[:end])
                self.assertEqual(result["signal"], direction)
                self.assertEqual(result["divergence_time"], 99900000 - 1)

    def test_missing_wait_bar_fails_closed(self):
        rows = fixture()
        cross(rows, 104, "long")
        engine = strategy.ProjectSignalEngine(config())
        engine.detect(rows[:100])
        self.assertNotEqual(engine.detect(rows[:103] + rows[104:105])["signal"], "long")

    def test_invalid_wait_timestamp_fails_closed_without_exception(self):
        rows = fixture()
        cross(rows, 100, "long")
        engine = strategy.ProjectSignalEngine(config())
        engine.detect(rows[:100])
        rows[100]["close_time"] = None
        try:
            result = engine.detect(rows[:101])
        except (TypeError, ValueError, KeyError) as error:
            self.fail(f"strict invalid-data cancellation raised {type(error).__name__}")
        self.assertNotEqual(result["signal"], "long")
        self.assertIsNone(engine.pending_buy)

    def test_first_late_observation_uses_original_ready_age_and_metadata(self):
        rows = fixture()
        cross(rows, 107, "long")
        engine = strategy.ProjectSignalEngine(config())
        engine.detect(rows[:106])  # First observation six bars after ready 99.
        result = engine.detect(rows[:108])
        self.assertEqual(result["signal"], "long")
        self.assertEqual(result["confirm_bars"], 8)
        self.assertEqual(result["signal_ready_time"], 89999999)
        self.assertEqual(result["candidate_lifecycle"], dict(
            version="strict_candidate_v1", anchor=79.0, anchor_close_time=87299999,
            first_ready_time=89999999, checked_through_close_time=97199999))

    def test_rolling_window_and_future_append_preserve_prior_decisions(self):
        rows = fixture(count=330, first=290, second=296)
        cross(rows, 300, "long")
        prefix = copy.deepcopy(rows[:306])
        future = copy.deepcopy(rows[306:])
        future[-1].update(open=10000.0, high=20000.0, low=1.0, close=15000.0, macd=-1000.0)

        def replay(data, rolling):
            engine = strategy.ProjectSignalEngine(config())
            return [engine.detect(data[max(0, end - 250) if rolling else 0:end])
                    for end in range(100, len(data) + 1)]

        outputs = replay(prefix, False)
        self.assertEqual(outputs, replay(prefix, True))
        self.assertEqual(outputs, replay(prefix + future, True)[:len(outputs)])
        self.assertEqual(sum(result["signal"] == "long" for result in outputs), 1)
        self.assertEqual(prefix, rows[:306])


if __name__ == "__main__":
    unittest.main()
