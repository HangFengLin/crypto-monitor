"""Hand-constructed causal observation lifecycles; no strategy-return claims."""

import copy
import json
import unittest

try:
    from pattern_observer import PatternObserver
except ModuleNotFoundError:
    PatternObserver = None


def bar(number, *, open_=100, high=101, low=99, close=100, volume=10):
    return {"open_time": (number - 1) * 1000, "close_time": number * 1000 - 1,
            "open": open_, "high": high, "low": low, "close": close, "volume": volume}


def reflected(row):
    return {**row, "open": 200 - row["open"], "high": 200 - row["low"],
            "low": 200 - row["high"], "close": 200 - row["close"]}


def quiet_history():
    return [bar(index) for index in range(1, 41)]


def breakout_rows():
    return [bar(41, open_=100, high=102, low=100, close=101.6),
            bar(42, open_=101.6, high=101.7, low=100.9, close=101.2),
            bar(43, open_=101.2, high=102, low=101.2, close=101.8)]


def double_rows():
    return [bar(41, high=101, low=98, close=100),
            bar(42, open_=99, high=99, low=96, close=98),
            bar(43, open_=96, high=97, low=95, close=96),
            bar(44, open_=98, high=99, low=96, close=98),
            bar(45, high=101, low=98, close=100),
            bar(46, open_=100, high=103, low=99, close=101),
            bar(47, high=102, low=98, close=100),
            bar(48, open_=98, high=100, low=96, close=98),
            bar(49, open_=96.5, high=98, low=95.3, close=96.5),
            bar(50, open_=98, high=99, low=96, close=98),
            bar(51, high=101, low=98, close=100),
            bar(52, open_=100, high=104, low=100, close=103.6)]


class PatternObserverTest(unittest.TestCase):
    def make(self, config=None):
        self.assertIsNotNone(PatternObserver, "causal pattern observer has not been implemented")
        return PatternObserver(config)

    def feed(self, observer, rows, symbol="ETHUSDT", interval="15m"):
        events = []
        for row in rows:
            events.extend(observer.process_bar(symbol, interval, row, available_at_ms=row["close_time"] + 20))
        return events

    def test_breakout_requires_a_separate_retest_bar_and_later_confirmation(self):
        observer = self.make()
        self.assertEqual(self.feed(observer, quiet_history()), [])
        first, retest, confirm = breakout_rows()
        detected = self.feed(observer, [first])
        self.assertEqual([(e["pattern"], e["direction"], e["stage"]) for e in detected],
                         [("breakout_retest", "long", "forming")])
        self.assertEqual(detected[0]["level"], 101)
        self.assertEqual(detected[0]["invalid_level"], 100)
        self.assertEqual(self.feed(observer, [retest]), [])
        confirmed = self.feed(observer, [confirm])
        self.assertEqual([(e["id"], e["stage"]) for e in confirmed], [(detected[0]["id"], "confirmed")])
        self.assertEqual(confirmed[0]["available_at_ms"], 43019)

    def test_short_breakout_is_the_mirrored_lifecycle(self):
        observer = self.make()
        self.feed(observer, quiet_history())
        events = self.feed(observer, list(map(reflected, breakout_rows())))
        self.assertEqual([(e["direction"], e["stage"]) for e in events],
                         [("short", "forming"), ("short", "confirmed")])
        self.assertEqual(events[0]["level"], 99)
        self.assertEqual(events[0]["invalid_level"], 100)

    def test_touch_and_rebound_in_one_bar_cannot_also_confirm(self):
        observer = self.make()
        self.feed(observer, quiet_history() + breakout_rows()[:1])
        bounced = bar(42, open_=101.6, high=104, low=100.8, close=103.5)
        self.assertEqual(self.feed(observer, [bounced]), [])
        self.assertEqual([item["stage"] for item in observer.latest()], ["forming"])

    def test_first_retest_boundary_is_frozen_instead_of_following_later_lower_highs(self):
        observer = self.make()
        self.feed(observer, quiet_history() + breakout_rows()[:2])
        later_retest = bar(43, open_=101.2, high=101.5, low=100.9, close=101.3)
        self.assertEqual(self.feed(observer, [later_retest]), [])
        still_below_first_high = bar(44, open_=101.3, high=101.7, low=101.2, close=101.6)
        self.assertEqual(self.feed(observer, [still_below_first_high]), [])
        confirmed = self.feed(observer, [bar(45, open_=101.6, high=102, low=101.5, close=101.8)])
        self.assertEqual([e["stage"] for e in confirmed], ["confirmed"])

    def test_double_bottom_is_known_only_after_right_side_pivot_confirmation(self):
        observer = self.make()
        self.feed(observer, quiet_history())
        earlier = self.feed(observer, double_rows()[:10])
        self.assertFalse(any(e["pattern"] == "double_bottom" for e in earlier))
        formed = [e for e in self.feed(observer, double_rows()[10:11]) if e["pattern"] == "double_bottom"]
        self.assertEqual(len(formed), 1)
        self.assertEqual(formed[0]["stage"], "forming")
        self.assertEqual(formed[0]["bar_close_time"], 50999)
        self.assertEqual(formed[0]["detected_at_ms"], 51019)
        self.assertEqual(formed[0]["points"][-1]["time_ms"], 48999)
        self.assertEqual([point["kind"] for point in formed[0]["points"]],
                         ["first_bottom", "neckline", "second_bottom"])
        self.assertEqual(formed[0]["level"], 103)
        confirmed = [e for e in self.feed(observer, double_rows()[11:]) if e["pattern"] == "double_bottom"]
        self.assertEqual([(e["id"], e["stage"]) for e in confirmed], [(formed[0]["id"], "confirmed")])

    def test_double_top_confirms_only_after_neckline_break(self):
        observer = self.make()
        events = self.feed(observer, quiet_history() + list(map(reflected, double_rows())))
        pattern = [event for event in events if event["pattern"] == "double_top"]
        self.assertEqual([event["stage"] for event in pattern], ["forming", "confirmed"])
        self.assertEqual(pattern[0]["direction"], "short")
        self.assertEqual(pattern[0]["level"], 97)

    def test_close_through_frozen_boundary_invalidates_the_existing_pattern(self):
        observer = self.make()
        self.feed(observer, quiet_history())
        formed = self.feed(observer, breakout_rows()[:1])[0]
        failed = self.feed(observer, [bar(42, open_=101.6, high=101.6, low=99.5, close=99.9)])
        own = [event for event in failed if event["id"] == formed["id"]]
        self.assertEqual([event["stage"] for event in own], ["invalidated"])
        self.assertEqual(own[0]["invalid_level"], 100)

    def test_forming_expiry_is_a_single_event_after_twelve_subsequent_bars(self):
        observer = self.make()
        self.feed(observer, quiet_history())
        formed = self.feed(observer, breakout_rows()[:1])[0]
        flat = [bar(index, open_=101.6, high=102, low=101.5, close=101.6) for index in range(42, 55)]
        events = [event for event in self.feed(observer, flat) if event["id"] == formed["id"]]
        self.assertEqual([(event["stage"], event["bar_close_time"]) for event in events], [("expired", 52999)])

    def test_confirmed_observation_expires_after_twenty_four_subsequent_bars(self):
        observer = self.make()
        events = self.feed(observer, quiet_history() + breakout_rows())
        formation_id = events[0]["id"]
        flat = [bar(index, open_=101.8, high=102, low=101.6, close=101.8) for index in range(44, 69)]
        ending = [event for event in self.feed(observer, flat) if event["id"] == formation_id]
        self.assertEqual([(event["stage"], event["bar_close_time"]) for event in ending], [("expired", 66999)])

    def test_future_processing_never_mutates_previously_returned_events(self):
        observer = self.make()
        previous = self.feed(observer, quiet_history() + breakout_rows()[:1])
        captured = copy.deepcopy(previous)
        self.feed(observer, breakout_rows()[1:])
        self.feed(observer, [bar(44, high=102, low=98, close=99)])
        self.assertEqual(previous, captured)
        self.assertEqual(previous[0]["stage"], "forming")

    def test_restart_json_roundtrip_preserves_confirmation_and_deduplication(self):
        observer = self.make()
        self.feed(observer, quiet_history() + breakout_rows()[:2])
        saved = json.loads(json.dumps(observer.dump_state(), allow_nan=False))
        restarted = PatternObserver.from_state(saved)
        confirmation = breakout_rows()[2]
        self.assertEqual(self.feed(restarted, [confirmation]), self.feed(observer, [confirmation]))
        self.assertEqual(self.feed(restarted, [confirmation]), [])

    def test_revised_or_invalid_bar_does_not_mutate_any_state(self):
        observer = self.make()
        self.feed(observer, quiet_history())
        saved = observer.dump_state()
        invalid = [dict(bar(40), high=102), dict(bar(41), close=float("nan")),
                   dict(bar(41), low=102), dict(bar(41), volume=-1), dict(bar(41), confirmed=False)]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                observer.process_bar("ETHUSDT", "15m", row, available_at_ms=50000)
            self.assertEqual(observer.dump_state(), saved)
        with self.assertRaises(ValueError):
            observer.process_bar("ETHUSDT", "15m", bar(41), available_at_ms=40000)
        self.assertEqual(observer.dump_state(), saved)

    def test_history_and_retained_pattern_records_remain_bounded(self):
        observer = self.make()
        self.feed(observer, [bar(index) for index in range(1, 1001)])
        self.assertEqual(len(observer.bars_for("ETHUSDT", "15m")), 240)
        self.assertLessEqual(len(observer.latest()), 256)
        self.assertEqual(observer.bars_for("ETHUSDT", "15m")[0]["close_time"], 760999)

    def test_rejects_unknown_or_inconsistent_checkpoint(self):
        observer = self.make()
        with self.assertRaises(ValueError):
            PatternObserver.from_state({"schema_version": 999})
        state = observer.dump_state()
        state["streams"] = "invalid"
        with self.assertRaises(ValueError):
            PatternObserver.from_state(state)

    def test_corrupt_retest_checkpoint_is_rejected_before_new_bars_are_consumed(self):
        observer = self.make()
        self.feed(observer, quiet_history() + breakout_rows()[:2])
        saved = observer.dump_state()
        active = saved["streams"][0]["active"]["breakout_retest:long"]
        active["_retest"]["index"] = 999999
        with self.assertRaises(ValueError):
            PatternObserver.from_state(saved)

    def test_impossible_pivot_window_is_rejected_instead_of_silently_disabling_shapes(self):
        self.assertIsNotNone(PatternObserver)
        with self.assertRaises(ValueError):
            PatternObserver({"pivot_left": 200, "pivot_right": 200})

    def test_wide_past_range_is_not_mistaken_for_tight_breakout_setup(self):
        observer = self.make()
        wide = [bar(index, high=110, low=90) for index in range(1, 10)]
        self.feed(observer, wide + [bar(index) for index in range(10, 41)])
        events = self.feed(observer, [bar(41, high=112, low=100, close=111)])
        self.assertFalse(any(event["pattern"] == "breakout_retest" for event in events))

    def test_streams_are_independent_and_historical_exact_replays_are_idempotent(self):
        observer = self.make()
        self.feed(observer, quiet_history(), symbol="BTCUSDT")
        self.feed(observer, quiet_history(), symbol="ETHUSDT")
        btc = self.feed(observer, breakout_rows()[:1], symbol="BTCUSDT")
        self.assertEqual(len(btc), 1)
        self.assertEqual(observer.bars_for("ETHUSDT", "15m")[-1]["close_time"], 39999)
        state = observer.dump_state()
        self.assertEqual(self.feed(observer, quiet_history()[10:11], symbol="ETHUSDT"), [])
        self.assertEqual(observer.dump_state(), state)
