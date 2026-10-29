"""TPR1 research entry: finalized trend context and an unbroken EMA reclaim."""

from __future__ import annotations

import copy
import math
import unittest

from strategy import ProjectSignalEngine

STEP = 900_000


def reclaim_bars():
    return [
        dict(open_time=14_400_000, close_time=15_299_999, open=99.5,
             high=100.5, low=98.5, close=99.0, ema20=100.0,
             confirmed=True, is_closed=True),
        dict(open_time=15_300_000, close_time=16_199_999, open=99.2,
             high=101.5, low=98.5, close=101.0, ema20=100.0, atr=2.0,
             confirmed=True, is_closed=True, higher_close_time=14_399_999,
             higher_close=110.0, higher_ema20=105.0, higher_ema60=100.0,
             higher_prev_ema20=104.0),
    ]


class TrendPullbackReclaimTests(unittest.TestCase):
    def detect(self, rows):
        candidate = getattr(ProjectSignalEngine(), "detect_trend_pullback_reclaim", None)
        self.assertTrue(callable(candidate), "explicit TPR1 research entry is not implemented")
        return candidate(rows)

    def test_completed_reclaim_emits_independent_long_with_atr_risk_floor(self):
        rows = reclaim_bars()
        original = copy.deepcopy(rows)
        result = self.detect(rows)
        self.assertEqual(result["signal"], "long")
        self.assertEqual(result["entry_hypothesis"], "TPR1")
        self.assertEqual(result["entry_rule"], "next_boundary_after_observation")
        self.assertEqual(result["price"], 101.0)
        self.assertEqual(result["stop_loss"], 98.0)
        self.assertEqual(result["anchor"], 98.5)
        self.assertEqual(result["atr"], 2.0)
        self.assertEqual(result["signal_ready_time"], 16_199_999)
        self.assertEqual(result["decision_close"], 16_199_999)
        self.assertEqual(result["higher_close_time"], 14_399_999)
        self.assertNotIn("divergence_time", result)
        self.assertNotIn("signal_score", result)
        self.assertEqual(rows, original)

    def test_structure_anchor_wins_when_atr_stop_is_tighter(self):
        rows = reclaim_bars()
        rows[-1]["atr"] = 0.5
        self.assertEqual(self.detect(rows)["stop_loss"], 98.5)

    def test_future_partial_stale_or_missing_four_hour_clock_cannot_signal(self):
        for clock in (28_799_999, 14_399_998, -1, True, None):
            with self.subTest(clock=clock):
                rows = reclaim_bars()
                rows[-1]["higher_close_time"] = clock
                self.assertEqual(self.detect(rows)["signal"], "wait")

    def test_missing_nonfinite_or_nonpositive_required_features_fail_closed(self):
        for index, key in ((0, "ema20"), (1, "ema20"), (1, "atr"),
                           (1, "higher_close"), (1, "higher_ema20"),
                           (1, "higher_ema60"), (1, "higher_prev_ema20")):
            for value in (None, math.nan, math.inf, 0, -1, True):
                with self.subTest(index=index, key=key, value=value):
                    rows = reclaim_bars()
                    rows[index][key] = value
                    result = self.detect(rows)
                    self.assertEqual(result["signal"], "wait")
                    self.assertTrue(result["reason"])

    def test_both_bars_must_be_explicitly_finalized(self):
        for index in (0, 1):
            for key in ("is_closed", "confirmed"):
                for value in (False, None):
                    with self.subTest(index=index, key=key, value=value):
                        rows = reclaim_bars()
                        rows[index][key] = value
                        self.assertEqual(self.detect(rows)["signal"], "wait")

    def test_noncontiguous_or_misaligned_fifteen_minute_bars_are_rejected(self):
        for change in (STEP, 1):
            rows = reclaim_bars()
            rows[-1]["open_time"] += change
            rows[-1]["close_time"] += change
            self.assertEqual(self.detect(rows)["signal"], "wait")

    def test_reclaim_requires_pullback_breakout_and_unbroken_anchor(self):
        changes = ((0, "low", 100.1), (0, "close", 100.2),
                   (1, "close", 100.0), (1, "close", 100.5),
                   (1, "low", 98.4))
        for index, key, value in changes:
            with self.subTest(index=index, key=key):
                rows = reclaim_bars()
                rows[index][key] = value
                self.assertEqual(self.detect(rows)["signal"], "wait")

    def test_higher_trend_and_positive_ema_slope_are_required(self):
        for key, value in (("higher_close", 105.0), ("higher_ema60", 105.0),
                           ("higher_prev_ema20", 105.0), ("higher_prev_ema20", 106.0)):
            with self.subTest(key=key, value=value):
                rows = reclaim_bars()
                rows[-1][key] = value
                self.assertEqual(self.detect(rows)["signal"], "wait")

    def test_bearish_mirror_does_not_generate_a_short_candidate(self):
        rows = reclaim_bars()
        for row in rows:
            row.update(open=200-row["open"], high=200-row["low"],
                       low=200-row["high"], close=200-row["close"])
        rows[-1].update(higher_close=90.0, higher_ema20=95.0,
                        higher_ema60=100.0, higher_prev_ema20=96.0)
        self.assertEqual(self.detect(rows)["signal"], "wait")

    def test_missing_pair_and_invalid_ohlc_fail_closed(self):
        self.assertEqual(self.detect([])["signal"], "wait")
        self.assertEqual(self.detect(reclaim_bars()[-1:])["signal"], "wait")
        rows = reclaim_bars()
        rows[-1]["high"] = 100.0
        self.assertEqual(self.detect(rows)["signal"], "wait")


if __name__ == "__main__":
    unittest.main()
