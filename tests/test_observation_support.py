import importlib
import math
import unittest


class ObservationSupportTests(unittest.TestCase):
    def level(self, price=100, support=100, tolerance_pct=1, **updates):
        return {
            "type": "support", "symbol": "ETHUSDT", "interval": "1h",
            "price": price, "support": support, "tolerance_pct": tolerance_pct,
            "distance_pct": 999, "touched": True, "kline_close_time": 123,
            **updates,
        }

    def advance(self, previous, level):
        try:
            advance_support = importlib.import_module("observation_support").advance_support
        except (ImportError, AttributeError):
            self.fail("Missing implementation of support observation transitions")
        return advance_support(previous, level)

    def test_first_touch_preserves_raw_fields_and_recomputes_distance(self):
        original = self.level(price=100.5)
        state, events = self.advance(None, original)
        self.assertEqual(state, {"support": 100.0, "status": "near", "tolerance_pct": 1.0})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["support_transition"], "first_touch")
        self.assertEqual(events[0]["support_status"], "near")
        self.assertEqual(events[0]["price"], 100.5)
        self.assertEqual(events[0]["kline_close_time"], 123)
        self.assertAlmostEqual(events[0]["distance_pct"], .5)
        self.assertEqual(original["distance_pct"], 999)

    def test_sustained_touch_does_not_repeat_when_kline_changes(self):
        previous = {"support": 100, "status": "near", "tolerance_pct": 1}
        state, events = self.advance(previous, self.level(price=99.6, kline_close_time=456))
        self.assertEqual(state, previous)
        self.assertEqual(events, [])
        self.assertEqual(previous, {"support": 100, "status": "near", "tolerance_pct": 1})

    def test_near_latch_uses_hysteresis_then_rearms_after_leaving(self):
        previous = {"support": 100, "status": "near", "tolerance_pct": 1}
        for price in (101.1, 102):
            with self.subTest(price=price):
                state, events = self.advance(previous, self.level(price=price))
                self.assertEqual(state["status"], "near")
                self.assertEqual(events, [])
        far, events = self.advance(previous, self.level(price=102.01))
        self.assertEqual(far["status"], "far")
        self.assertEqual(events, [])
        _, events = self.advance(far, self.level(price=100.2))
        self.assertEqual([event["support_transition"] for event in events], ["retest"])

    def test_far_state_does_not_trigger_in_the_hysteresis_buffer(self):
        previous = {"support": 100, "status": "far", "tolerance_pct": 1}
        state, events = self.advance(previous, self.level(price=101.5))
        self.assertEqual(state["status"], "far")
        self.assertEqual(events, [])

    def test_changed_level_notifies_even_when_previous_level_is_active(self):
        previous = {"support": 100, "status": "near", "tolerance_pct": 1}
        state, events = self.advance(previous, self.level(price=100.5, support=101))
        self.assertEqual(state, {"support": 101.0, "status": "near", "tolerance_pct": 1.0})
        self.assertEqual([event["support_transition"] for event in events], ["level_changed"])
        self.assertEqual(events[0]["support"], 101)

    def test_old_break_is_reported_before_new_lower_level_touch(self):
        previous = {"support": 100, "status": "near", "tolerance_pct": 1}
        state, events = self.advance(previous, self.level(price=98, support=98))
        self.assertEqual(state["support"], 98)
        self.assertEqual(
            [(event["support"], event["support_transition"], event["support_status"]) for event in events],
            [(100, "broken", "broken"), (98, "level_changed", "near")],
        )
        self.assertAlmostEqual(events[0]["distance_pct"], -2)
        self.assertAlmostEqual(events[1]["distance_pct"], 0)

    def test_broken_support_does_not_repeat_and_return_to_near_retests(self):
        state, events = self.advance(None, self.level(price=98.99))
        self.assertEqual(state["status"], "broken")
        self.assertEqual([event["support_transition"] for event in events], ["broken"])
        state, events = self.advance(state, self.level(price=98))
        self.assertEqual(events, [])
        _, events = self.advance(state, self.level(price=99))
        self.assertEqual([event["support_transition"] for event in events], ["retest"])

    def test_tolerance_boundaries_are_inclusive_and_zero_tolerance_is_supported(self):
        for price in (99, 101):
            with self.subTest(price=price):
                state, events = self.advance(None, self.level(price=price))
                self.assertEqual(state["status"], "near")
                self.assertEqual(events[0]["support_transition"], "first_touch")
        for price, expected in ((100, "near"), (99.99, "broken"), (100.01, "far")):
            with self.subTest(price=price, tolerance=0):
                state, _ = self.advance(None, self.level(price=price, tolerance_pct=0))
                self.assertEqual(state["status"], expected)

    def test_floating_noise_does_not_create_a_new_structure(self):
        previous = {"support": 100, "status": "near", "tolerance_pct": 1}
        state, events = self.advance(previous, self.level(support=100.00000001))
        self.assertEqual(state, previous)
        self.assertEqual(events, [])

    def test_invalid_observations_do_not_falsely_recover_or_replace_previous_state(self):
        previous = {"support": 100, "status": "broken", "tolerance_pct": 1}
        invalid_updates = (
            {"price": None}, {"price": math.nan}, {"price": math.inf}, {"price": 0},
            {"support": -1}, {"support": math.nan}, {"support": True},
            {"tolerance_pct": -1}, {"tolerance_pct": math.inf}, {"tolerance_pct": None},
        )
        for updates in invalid_updates:
            with self.subTest(updates=updates):
                state, events = self.advance(previous, self.level(**updates))
                self.assertEqual(state, previous)
                self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
