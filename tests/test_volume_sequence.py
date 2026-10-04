from __future__ import annotations

import copy
import dataclasses
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import strategy


def bars(count: int) -> list[dict]:
    return [
        dict(open=100.0, high=101.0, low=99.0, close=100.0, volume=100.0,
             open_time=index * 900000, close_time=(index + 1) * 900000 - 1,
             macd=1.0, macd_signal=1.0, volume_ratio=2.0, obv_slope=1.0,
             rsi=30.0, ema60=105.0)
        for index in range(count)
    ]


def sequence_fixture(count: int = 102) -> list[dict]:
    result = bars(count)
    result[100].update(volume=75.0, close=99.5)
    if count > 101:
        result[101].update(open=100.0, high=103.0, low=100.0, close=102.0, volume=148.125)
    return result


class VolumeSequenceTest(unittest.TestCase):
    def check(self, direction, data, created=None, anchor=98.0):
        helper = getattr(strategy, "check_volume_sequence", None)
        self.assertTrue(callable(helper), "ordered volume confirmation is missing")
        return helper(direction, data, data[99]["close_time"] if created is None else created,
                      strategy.DEFAULT_CONFIG, stop_anchor=anchor)

    def test_prior_twenty_denominator_excludes_confirmation_bar(self):
        # A=75/100; B=148.125/98.75=1.5. Including B lowers RVOL below 1.5.
        passed, _, cancelled = self.check("long", sequence_fixture())
        self.assertTrue(passed)
        self.assertFalse(cancelled)

    def test_short_sequence_requires_bearish_body_and_prior_low_break(self):
        data = sequence_fixture()
        data[100].update(close=100.5)
        data[101].update(open=100.0, close=98.0, low=97.0, high=100.0)
        self.assertTrue(self.check("short", data, anchor=104.0)[0])
        data[101]["open"] = 97.0
        self.assertFalse(self.check("short", data, anchor=104.0)[0])

    def test_pre_candidate_contraction_cannot_confirm(self):
        data = bars(102)
        data[98].update(volume=50.0, close=99.5)
        data[101].update(volume=200.0, open=100.0, close=102.0, high=103.0)
        self.assertFalse(self.check("long", data)[0])

    def test_first_contraction_locks_window_despite_later_contraction(self):
        data = sequence_fixture(105)
        data[101].update(volume=50.0, close=99.0, open=100.0, high=101.0, low=99.0)
        data[104].update(volume=200.0, open=100.0, close=102.0, high=103.0, low=100.0)
        passed, _, cancelled = self.check("long", data)
        self.assertFalse(passed)
        self.assertTrue(cancelled)

    def test_third_bar_without_breakout_cancels(self):
        data = sequence_fixture(104)
        data[101].update(volume=100.0, close=100.0, high=101.0, low=99.0)
        self.assertTrue(self.check("long", data)[2])

    def test_breakout_on_third_bar_is_allowed(self):
        data = sequence_fixture(104)
        data[101].update(volume=100.0, close=100.0, high=101.0, low=99.0)
        data[103].update(volume=148.125, open=100.0, close=102.0, high=103.0, low=100.0)
        self.assertTrue(self.check("long", data)[0])

    def test_stage_a_requires_counter_direction_close(self):
        data = sequence_fixture()
        data[100]["close"] = 100.5
        self.assertFalse(self.check("long", data)[0])

    def test_same_bar_contraction_and_breakout_are_rejected(self):
        data = sequence_fixture(101)
        data[100].update(open=98.0, close=99.5, high=103.0)
        self.assertFalse(self.check("long", data)[0])

    def test_wick_break_without_close_break_is_rejected(self):
        data = sequence_fixture()
        data[101].update(close=100.5, high=103.0)
        self.assertFalse(self.check("long", data)[0])

    def test_missing_zero_and_short_history_fail_closed(self):
        for value in (None, 0.0, float("nan")):
            with self.subTest(volume=value):
                data = sequence_fixture()
                data[95]["volume"] = value
                passed, _, cancelled = self.check("long", data)
                self.assertFalse(passed)
                self.assertTrue(cancelled)
        data = sequence_fixture()[90:]
        passed, _, cancelled = self.check("long", data, created=data[9]["close_time"])
        self.assertFalse(passed)
        self.assertTrue(cancelled)

    def test_gap_and_structure_break_cancel(self):
        data = sequence_fixture()
        data[101]["open_time"] += 900000
        self.assertTrue(self.check("long", data)[2])
        data = sequence_fixture()
        data[100]["low"] = 97.0
        self.assertTrue(self.check("long", data)[2])

    def test_touching_structure_anchor_without_break_remains_valid(self):
        self.assertTrue(self.check("long", sequence_fixture(), anchor=99.0)[0])

    def test_unconfirmed_volume_history_cannot_enter_denominator(self):
        data = sequence_fixture()
        data[95]["confirmed"] = False
        passed, _, cancelled = self.check("long", data)
        self.assertFalse(passed)
        self.assertTrue(cancelled)

    def test_future_append_or_perturbation_preserves_prefix_result(self):
        prefix = sequence_fixture()
        expected = self.check("long", prefix)
        extended = prefix + bars(110)[102:]
        extended[-1].update(close=10000.0, volume=1000000.0)
        self.assertEqual(expected, self.check("long", extended[:102]))
        before = copy.deepcopy(prefix)
        self.check("long", prefix)
        self.assertEqual(before, prefix)

    def test_default_flag_keeps_original_single_bar_volume_gate(self):
        passed, _ = strategy.check_volume_confirmation("long", {"volume_ratio": 0.5})
        self.assertTrue(passed)
        self.assertFalse(getattr(strategy.DEFAULT_CONFIG, "volume_sequence_enabled", False))

    def test_default_engine_keeps_immediate_original_confirmation(self):
        config = dataclasses.replace(strategy.DEFAULT_CONFIG, require_higher_trend_alignment=False,
                                     chop_filter_enabled=False)
        engine = strategy.ProjectSignalEngine(config)
        info = dict(index=96, confirm_index=99, price_low=98.0, type="macd")
        with patch.object(strategy, "detect_bullish_divergence", return_value=(True, .8, info)), \
             patch.object(strategy, "detect_bearish_divergence", return_value=(False, 0.0, None)), \
             patch.object(strategy, "build_chan_structure_context", return_value=None), \
             patch.object(strategy, "latest_ma_direction", return_value="up"):
            result = engine.detect(bars(100))
        self.assertEqual(result["signal"], "long")
        self.assertEqual(result["confirm_bars"], 0)
        self.assertIsNone(engine.pending_buy)

    def test_volume_override_registers_one_family_without_mutating_default(self):
        from strategy_validation import config_with_overrides, load_candidates
        with TemporaryDirectory() as folder:
            path = Path(folder) / "v1.yaml"
            path.write_text("strategy:\n  volume_sequence_enabled: true\n")
            candidates = load_candidates(str(path))
        candidate = candidates[-1]
        self.assertEqual(candidate["family"], "volume_sequence")
        self.assertTrue(config_with_overrides(candidate["overrides"]).volume_sequence_enabled)
        self.assertFalse(strategy.DEFAULT_CONFIG.volume_sequence_enabled)

    def test_engine_waits_for_post_candidate_ordered_sequence_and_emits_once(self):
        self.assertTrue(hasattr(strategy.DEFAULT_CONFIG, "volume_sequence_enabled"), "candidate flag is missing")
        config = dataclasses.replace(strategy.DEFAULT_CONFIG, volume_sequence_enabled=True,
                                     require_higher_trend_alignment=False, chop_filter_enabled=False)
        engine = strategy.ProjectSignalEngine(config)
        data = sequence_fixture()
        info = dict(index=96, confirm_index=99, price_low=98.0, type="macd")
        with patch.object(strategy, "detect_bullish_divergence", return_value=(True, .8, info)), \
             patch.object(strategy, "detect_bearish_divergence", return_value=(False, 0.0, None)), \
             patch.object(strategy, "build_chan_structure_context", return_value=None), \
             patch.object(strategy, "latest_ma_direction", return_value="up"):
            self.assertNotEqual(engine.detect(data[:100])["signal"], "long")
            self.assertNotEqual(engine.detect(data[:101])["signal"], "long")
            signal = engine.detect(data)
            self.assertEqual(signal["signal"], "long")
            self.assertEqual(signal["confirm_bars"], 2)
            self.assertNotEqual(engine.detect(data)["signal"], "long")

    def test_failed_original_cross_at_first_b_cannot_retry_same_divergence(self):
        config = dataclasses.replace(strategy.DEFAULT_CONFIG, volume_sequence_enabled=True,
                                     require_higher_trend_alignment=False, chop_filter_enabled=False)
        engine = strategy.ProjectSignalEngine(config)
        data = sequence_fixture(103)
        info = dict(index=96, confirm_index=99, price_low=98.0, type="macd")
        with patch.object(strategy, "detect_bullish_divergence", return_value=(True, .8, info)), \
             patch.object(strategy, "detect_bearish_divergence", return_value=(False, 0.0, None)), \
             patch.object(strategy, "build_chan_structure_context", return_value=None), \
             patch.object(strategy, "latest_ma_direction", return_value="none") as ma, \
             patch.object(strategy, "latest_macd_direction", return_value="none"):
            engine.detect(data[:100])
            engine.detect(data[:101])
            self.assertEqual(engine.detect(data[:102])["signal"], "filtered_buy")
            ma.return_value = "up"
            self.assertNotEqual(engine.detect(data)["signal"], "long")
            self.assertIsNone(engine.pending_buy)

    def test_sequence_backtest_fills_after_b_at_next_open(self):
        import pandas as pd

        from project_signal_backtest import run_backtest
        config = dataclasses.replace(strategy.DEFAULT_CONFIG, volume_sequence_enabled=True,
                                     require_higher_trend_alignment=False, chop_filter_enabled=False)
        data = sequence_fixture(104)
        for index, bar in enumerate(data):
            bar["time"] = pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=15 * index)
        data[102].update(open=110.0, high=112.0, low=109.0, close=111.0)
        info = dict(index=96, confirm_index=99, price_low=98.0, type="macd")
        with patch.object(strategy, "detect_bullish_divergence", return_value=(True, .8, info)), \
             patch.object(strategy, "detect_bearish_divergence", return_value=(False, 0.0, None)), \
             patch.object(strategy, "build_chan_structure_context", return_value=None), \
             patch.object(strategy, "latest_ma_direction", return_value="up"):
            trades, _ = run_backtest(data, 2.0, 1, 0.001, strategy_config=config)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].entry_price, 110.0)
        self.assertEqual(trades[0].entry_time, data[102]["time"])

    def test_replayed_prefix_and_stage_a_clock_do_not_change_with_future_input(self):
        config = dataclasses.replace(strategy.DEFAULT_CONFIG, volume_sequence_enabled=True,
                                     require_higher_trend_alignment=False, chop_filter_enabled=False)
        info = dict(index=96, confirm_index=99, price_low=98.0, type="macd")
        prefix = sequence_fixture(105)
        prefix[101].update(volume=50.0, close=99.0, open=100.0, high=101.0, low=99.0)
        prefix[104].update(volume=200.0, close=102.0, high=103.0, low=100.0)
        future = bars(110)[105:]
        future[-1].update(volume=1000000.0, close=10000.0)

        def replay(data):
            engine = strategy.ProjectSignalEngine(config)
            outputs = []
            for end in range(100, len(data) + 1):
                outputs.append(engine.detect(data[:end]))
            return outputs

        with patch.object(strategy, "detect_bullish_divergence", return_value=(True, .8, info)), \
             patch.object(strategy, "detect_bearish_divergence", return_value=(False, 0.0, None)), \
             patch.object(strategy, "build_chan_structure_context", return_value=None), \
             patch.object(strategy, "latest_ma_direction", return_value="up"):
            frozen = replay(prefix)
            self.assertEqual(frozen, replay(prefix + future)[:len(frozen)])
        self.assertFalse(any(result["signal"] == "long" for result in frozen))
        self.assertIn("取消", frozen[4]["signal_name"])


if __name__ == "__main__":
    unittest.main()
