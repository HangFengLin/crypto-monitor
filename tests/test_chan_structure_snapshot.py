from __future__ import annotations

import copy
import dataclasses
import unittest

import strategy

STEP = 900_000
OFFSET = 200


def config(snapshot=True, strict=True):
    result = dataclasses.replace(strategy.DEFAULT_CONFIG, confirmation_mode="macd_only",
                                 strict_candidate_lifecycle=strict)
    # Exercise the real pre-feature behavior for RED, rather than a missing
    # dataclass constructor argument.
    object.__setattr__(result, "chan_structure_snapshot", snapshot)
    return result


def fixture(direction="long", count=340):
    side = 1 if direction == "long" else -1
    rows = []
    for index in range(count):
        rows.append(dict(open_time=index * STEP, close_time=(index + 1) * STEP - 1,
                         open=100., high=101., low=99., close=100., volume=100.,
                         macd=0., macd_signal=0., macd_fast=0., atr=2.,
                         rsi=30. if side == 1 else 70., ema60=110. if side == 1 else 90.,
                         volume_ratio=2., obv_slope=float(side), chop=30., bb_width=.03,
                         higher_close=110. if side == 1 else 90.,
                         higher_ema20=108. if side == 1 else 92., higher_ema60=100.,
                         higher_ema200=90. if side == 1 else 110., higher_macd=float(side)))
    for index, long_price in ((82, 130.), (90, 90.), (92, 120.), (96, 80.),
                              (100, 110.), (101, 95.), (104, 108.), (105, 97.)):
        price = long_price if direction == "long" else 200 - long_price
        rows[index + OFFSET].update(open=price, high=price + 1, low=price - 1, close=price)
    rows[90 + OFFSET]["macd"] = -2. * side
    rows[96 + OFFSET]["macd"] = -1. * side
    rows[108 + OFFSET]["macd"] = float(side)
    return rows


def observe(rows, direction="long", snapshot=True, late=False):
    engine = strategy.ProjectSignalEngine(config(snapshot))
    if not late:
        for end in range(100 + OFFSET, 109 + OFFSET):
            engine.detect(rows[:end])
    return engine, engine.detect(rows[:109 + OFFSET])


class ChanStructureSnapshotTest(unittest.TestCase):
    def test_default_off_preserves_old_first_observation_dependency(self):
        rows = fixture()
        _, early = observe(rows, snapshot=False)
        _, late = observe(rows, snapshot=False, late=True)
        self.assertEqual((early["stop_loss"], late["stop_loss"]), (77., 92.))
        self.assertNotIn("structure_snapshot", early)
        self.assertFalse(getattr(strategy.DEFAULT_CONFIG, "chan_structure_snapshot", False))

    def test_same_setup_early_and_late_observation_have_identical_structure(self):
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                rows = fixture(direction)
                _, early = observe(rows, direction)
                _, late = observe(rows, direction, late=True)
                self.assertEqual(early["signal"], direction)
                self.assertEqual(late["signal"], direction)
                self.assertEqual(early, late)

    def test_snapshot_metadata_is_bound_to_ready_and_original_pivot(self):
        rows = fixture()
        _, signal = observe(rows, late=True)
        metadata = signal.get("structure_snapshot")
        self.assertIsInstance(metadata, dict)
        self.assertEqual(metadata["version"], "chan_structure_snapshot_v1")
        self.assertEqual(metadata["reference_close_time"], 300 * STEP - 1)
        self.assertEqual(metadata["window_start_close_time"], 101 * STEP - 1)
        self.assertEqual(metadata["window_end_close_time"], 300 * STEP - 1)
        self.assertEqual(metadata["divergence_close_time"], 297 * STEP - 1)
        self.assertEqual(metadata["source_bar_count"], 200)
        self.assertEqual([s["end_close_time"] for s in metadata["source_strokes"]],
                         [291 * STEP - 1, 293 * STEP - 1, 297 * STEP - 1])
        self.assertTrue(all(s["end_close_time"] <= metadata["divergence_close_time"]
                            for s in metadata["source_strokes"]))
        self.assertEqual(metadata["zone"], dict(lower=90., upper=120., mid=105., width=30.))
        self.assertEqual(metadata["stop_anchor"], 80.)
        self.assertEqual(signal["candidate_lifecycle"]["anchor"], 79.)

    def test_snapshot_is_retained_and_response_mutation_does_not_change_pending(self):
        rows = fixture()
        engine = strategy.ProjectSignalEngine(config())
        first = engine.detect(rows[:300])
        snapshot = copy.deepcopy(first.get("structure_snapshot"))
        self.assertIsInstance(snapshot, dict)
        first["structure_snapshot"]["source_strokes"].clear()
        for end in range(301, 309):
            signal = engine.detect(rows[:end])
            self.assertEqual(signal["structure_snapshot"], snapshot)
        self.assertEqual(engine.detect(rows[:309])["structure_snapshot"], snapshot)

    def test_current_atr_is_not_frozen_and_raw_lifecycle_anchor_is_unchanged(self):
        rows = fixture()
        rows[308]["atr"] = 4.
        _, signal = observe(rows, late=True)
        self.assertEqual(signal["signal"], "long")
        self.assertEqual(signal["stop_loss"], 74.)
        self.assertEqual(signal["candidate_lifecycle"]["anchor"], 79.)

    def test_rolling_prefix_and_future_append_preserve_recorded_responses(self):
        rows = fixture()
        prefix = copy.deepcopy(rows[:310])
        future = copy.deepcopy(rows[310:])
        future[-1].update(open=10_000., high=20_000., low=1., close=15_000., macd=-1000.)
        def replay(data, rolling):
            engine = strategy.ProjectSignalEngine(config())
            return [engine.detect(data[max(0, end - 250) if rolling else 0:end])
                    for end in range(100, len(data) + 1)]
        outputs = replay(prefix, False)
        self.assertEqual(outputs, replay(prefix, True))
        self.assertEqual(outputs, replay(prefix + future, True)[:len(outputs)])
        self.assertEqual(sum(x["signal"] == "long" for x in outputs), 1)
        self.assertEqual(prefix, rows[:310])

    def test_missing_original_ready_context_fails_closed_and_consumes_setup(self):
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                rows = fixture(direction)
                engine = strategy.ProjectSignalEngine(config())
                result = engine.detect(rows[115:309])
                self.assertNotEqual(result["signal"], direction)
                self.assertIsNone(getattr(engine, "pending_buy" if direction == "long" else "pending_sell"))
                self.assertNotEqual(engine.detect(rows[:309])["signal"], direction)

    def test_bad_bar_in_ready_context_fails_closed(self):
        for mutation in ("gap", "timestamp", "unclosed", "macd"):
            with self.subTest(mutation=mutation):
                rows = fixture()
                if mutation == "gap":
                    del rows[200]
                elif mutation == "timestamp":
                    rows[200]["close_time"] = None
                elif mutation == "unclosed":
                    rows[200]["is_closed"] = False
                else:
                    rows[200]["macd"] = None
                self.assertNotEqual(strategy.ProjectSignalEngine(config()).detect(rows[:309])["signal"], "long")

    def test_snapshot_requires_strict_lifecycle(self):
        with self.assertRaisesRegex(ValueError, "strict_candidate_lifecycle"):
            strategy.ProjectSignalEngine(config(snapshot=True, strict=False))

    def test_new_divergence_can_create_its_own_ready_snapshot(self):
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                rows = fixture(direction)
                side = 1 if direction == "long" else -1
                price = 70. if direction == "long" else 130.
                rows[310].update(open=price, high=price + 1, low=price - 1, close=price, macd=-.5 * side)
                rows[313]["macd"] = float(side)
                engine, old = observe(rows, direction)
                for end in range(310, 315):
                    new = engine.detect(rows[:end])
                self.assertEqual(new["signal"], direction)
                self.assertNotEqual(new["divergence_time"], old["divergence_time"])
                self.assertEqual(new["structure_snapshot"]["divergence_close_time"], 311 * STEP - 1)
                self.assertEqual(new["structure_snapshot"]["reference_close_time"], 314 * STEP - 1)

    def test_data_older_than_frozen_200_bar_window_does_not_change_snapshot(self):
        rows = fixture()
        _, expected = observe(rows, late=True)
        rows[99].update(open=1000., high=2000., low=1., close=1500., macd=-100.)
        _, actual = observe(rows, late=True)
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
