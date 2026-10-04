from __future__ import annotations

import dataclasses
import unittest
from unittest.mock import patch

import strategy


def bars(count):
    return [dict(open=100.0, high=101.0, low=99.0, close=100.0, volume=100.0,
                 macd=1.0, macd_signal=1.0, close_time=index, rsi=70.0,
                 ema60=95.0, volume_ratio=2.0)
            for index in range(count)]


class ShortConfirmationTimeoutTest(unittest.TestCase):
    def test_same_bearish_divergence_does_not_reset_thirteen_bar_timeout(self):
        engine = strategy.ProjectSignalEngine()
        info = dict(index=96, confirm_index=99, price_high=101.0, type="macd")
        with patch.object(strategy, "detect_bullish_divergence", return_value=(False, 0.0, None)), \
             patch.object(strategy, "detect_bearish_divergence", return_value=(True, .8, info)), \
             patch.object(strategy, "build_chan_structure_context", return_value=None), \
             patch.object(strategy, "latest_ma_direction", return_value="none"), \
             patch.object(strategy, "latest_macd_direction", return_value="none"):
            engine.detect(bars(100))
            for size in range(101, 114):
                result = engine.detect(bars(size))
        self.assertEqual(result["signal_name"], "顶背驰确认超时")
        self.assertEqual(result["confirm_bars"], 13)
        self.assertIsNone(engine.pending_sell)

    def test_new_bearish_divergence_replaces_stale_pending_candidate(self):
        engine = strategy.ProjectSignalEngine()
        info = dict(index=96, confirm_index=99, price_high=101.0, type="macd")
        with patch.object(strategy, "detect_bullish_divergence", return_value=(False, 0.0, None)), \
             patch.object(strategy, "detect_bearish_divergence", return_value=(True, .8, info)) as divergence, \
             patch.object(strategy, "build_chan_structure_context", return_value=None), \
             patch.object(strategy, "latest_ma_direction", return_value="none"), \
             patch.object(strategy, "latest_macd_direction", return_value="none"):
            engine.detect(bars(100))
            divergence.return_value = (True, .8, {**info, "index": 99, "confirm_index": 102})
            result = engine.detect(bars(104))
        self.assertEqual(result["divergence_time"], 99)
        self.assertEqual(engine.pending_sell["created_close_time"], 103)

    def test_confirmation_after_wait_preserves_elapsed_bar_count(self):
        config = dataclasses.replace(strategy.DEFAULT_CONFIG, require_higher_trend_alignment=False,
                                     chop_filter_enabled=False)
        engine = strategy.ProjectSignalEngine(config)
        info = dict(index=96, confirm_index=99, price_high=101.0, type="macd")
        with patch.object(strategy, "detect_bullish_divergence", return_value=(False, 0.0, None)), \
             patch.object(strategy, "detect_bearish_divergence", return_value=(True, .8, info)), \
             patch.object(strategy, "build_chan_structure_context", return_value=None), \
             patch.object(strategy, "latest_ma_direction", return_value="none") as ma, \
             patch.object(strategy, "latest_macd_direction", return_value="none"):
            engine.detect(bars(100))
            engine.detect(bars(101))
            ma.return_value = "down"
            result = engine.detect(bars(103))
        self.assertEqual(result["signal"], "short")
        self.assertEqual(result["confirm_bars"], 3)


if __name__ == "__main__":
    unittest.main()
