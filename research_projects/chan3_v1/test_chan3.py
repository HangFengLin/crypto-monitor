"""Deterministic tests for the frozen causal rules.

Run from the repository root:
    .venv/bin/python -m unittest research_projects/chan3_v1/test_chan3.py -v
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chan3 import (  # noqa: E402
    Bars,
    Fractal,
    Signal,
    Stroke,
    build_strokes_and_signals,
    detect_third_buy,
    find_fractals,
    funding_cost_pct,
    signal_entry_status,
    simulate_trade,
    week_key,
    wilder_atr,
)


def make_stroke(
    start_kind: str,
    start_index: int,
    start_price: float,
    end_index: int,
    end_price: float,
    confirm_index: int,
) -> Stroke:
    start = Fractal(start_kind, start_index, start_price, 0)
    end_kind = "bottom" if start_kind == "top" else "top"
    end = Fractal(end_kind, end_index, end_price, confirm_index)
    return Stroke(start, end, max(start_price, end_price), min(start_price, end_price), confirm_index)


def make_bars(n: int = 120) -> Bars:
    open_time = [i * 900_000 for i in range(n)]
    close_time = [t + 899_999 for t in open_time]
    return Bars(
        open_time=open_time,
        open=[100.0] * n,
        high=[101.0] * n,
        low=[99.0] * n,
        close=[100.0] * n,
        close_time=close_time,
    )


class FractalTests(unittest.TestCase):
    def test_top_and_bottom_confirmed_two_bars_later(self):
        highs = [10, 12, 15, 12, 10, 8, 6, 8, 10, 12, 14, 12, 10]
        lows = [8, 10, 13, 10, 8, 7, 5, 7, 9, 11, 13, 11, 9]
        fractals = find_fractals(highs, lows, wing=2)
        self.assertEqual(
            [(f.kind, f.index, f.confirm_index) for f in fractals],
            [("top", 2, 4), ("bottom", 6, 8), ("top", 10, 12)],
        )

    def test_fractal_not_visible_before_confirmation(self):
        highs = [10, 12, 15, 12, 10]
        lows = [8, 10, 13, 10, 8]
        # With only indices 0..3 the top at 2 is not confirmed.
        self.assertEqual(find_fractals(highs[:4], lows[:4], wing=2), [])
        self.assertEqual([f.index for f in find_fractals(highs, lows, wing=2)], [2])

    def test_equal_highs_are_not_a_fractal(self):
        highs = [10, 12, 15, 12, 15, 12, 10]
        lows = [8, 10, 13, 10, 12, 10, 8]
        self.assertEqual(find_fractals(highs, lows, wing=2), [])


class StrokeAndThirdBuyTests(unittest.TestCase):
    def _five_strokes(self, pullback_low: float = 110.0) -> list[Stroke]:
        return [
            make_stroke("top", 0, 110, 4, 100, 6),
            make_stroke("bottom", 4, 100, 8, 108, 10),
            make_stroke("top", 8, 108, 12, 102, 14),
            make_stroke("bottom", 12, 102, 16, 115, 18),
            make_stroke("top", 16, 115, 20, pullback_low, 22),
        ]

    def test_valid_third_buy(self):
        atr = [1.0] * 30
        signal = detect_third_buy(self._five_strokes(), atr, symbol="BTCUSDT")
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.signal_index, 22)
        self.assertEqual(signal.entry_index, 23)
        self.assertAlmostEqual(signal.zone_zg, 108.0)
        self.assertAlmostEqual(signal.zone_zd, 102.0)
        self.assertAlmostEqual(signal.stop, 107.75)

    def test_pullback_at_or_below_zg_is_not_third_buy(self):
        atr = [1.0] * 30
        self.assertIsNone(detect_third_buy(self._five_strokes(108.0), atr))
        self.assertIsNone(detect_third_buy(self._five_strokes(107.0), atr))

    def test_departure_must_clear_zg(self):
        strokes = self._five_strokes()
        # Replace the departure with a top exactly at ZG; the pullback is then
        # above the departure, so this can never be a valid third buy.
        strokes[3] = make_stroke("bottom", 12, 102, 16, 108, 18)
        strokes[4] = make_stroke("top", 16, 108, 20, 110, 22)
        self.assertIsNone(detect_third_buy(strokes, [1.0] * 30))

    def test_zone_requires_overlap(self):
        strokes = [
            make_stroke("top", 0, 120, 4, 100, 6),
            make_stroke("bottom", 4, 100, 8, 118, 10),
            make_stroke("top", 8, 118, 12, 116, 14),
            make_stroke("bottom", 12, 116, 16, 130, 18),
            make_stroke("top", 16, 130, 20, 125, 22),
        ]
        # ZG = min(120,118,118)=118, ZD = max(100,100,116)=116 -> valid.
        # Use a genuinely non-overlapping three-stroke set below.
        strokes[2] = make_stroke("top", 8, 118, 12, 101, 14)
        # ZG = min(120,118,118)=118, ZD = max(100,100,101)=101 -> still valid.
        # Force ZG <= ZD by making the middle stroke entirely above the first.
        strokes = [
            make_stroke("top", 0, 110, 4, 100, 6),
            make_stroke("bottom", 4, 100, 8, 120, 10),
            make_stroke("top", 8, 120, 12, 115, 14),
            make_stroke("bottom", 12, 115, 16, 130, 18),
            make_stroke("top", 16, 130, 20, 125, 22),
        ]
        # ZG=min(110,120,120)=110, ZD=max(100,100,115)=115 -> ZG < ZD.
        self.assertIsNone(detect_third_buy(strokes, [1.0] * 30))

    def test_build_emits_signal_only_after_pullback_confirmation(self):
        fractals = [
            Fractal("top", 0, 110, 2),
            Fractal("bottom", 4, 100, 6),
            Fractal("top", 8, 108, 10),
            Fractal("bottom", 12, 102, 14),
            Fractal("top", 16, 115, 18),
        ]
        atr = [1.0] * 40
        _strokes, signals = build_strokes_and_signals(fractals, atr, symbol="BTCUSDT")
        self.assertEqual(signals, [])
        fractals.append(Fractal("bottom", 20, 110, 22))
        _strokes, signals = build_strokes_and_signals(fractals, atr, symbol="BTCUSDT")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].signal_index, 22)

    def test_later_fractals_do_not_rewrite_emitted_signal(self):
        fractals = [
            Fractal("top", 0, 110, 2),
            Fractal("bottom", 4, 100, 6),
            Fractal("top", 8, 108, 10),
            Fractal("bottom", 12, 102, 14),
            Fractal("top", 16, 115, 18),
            Fractal("bottom", 20, 110, 22),
        ]
        atr = [1.0] * 60
        _strokes, before = build_strokes_and_signals(fractals, atr, symbol="BTCUSDT")
        extended = fractals + [
            Fractal("top", 24, 125, 26),
            Fractal("bottom", 28, 112, 30),
            Fractal("top", 32, 130, 34),
        ]
        _strokes2, after = build_strokes_and_signals(extended, atr, symbol="BTCUSDT")
        self.assertEqual(before, after[: len(before)])


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.bars = make_bars(120)
        self.funding_times = [k * 28_800_000 for k in range(0, 10)]
        self.funding_rates = [0.0] * len(self.funding_times)
        self.signal = Signal(
            symbol="BTCUSDT",
            signal_index=0,
            entry_index=1,
            zone_zg=108.0,
            zone_zd=102.0,
            departure_high=115.0,
            pullback_low=110.0,
            stop=95.0,
            atr=1.0,
        )

    def _trade(self):
        trade = simulate_trade(
            self.bars, self.signal, self.funding_times, self.funding_rates
        )
        self.assertIsNotNone(trade)
        assert trade is not None
        return trade

    def test_stop_wins_when_both_levels_are_in_same_bar(self):
        self.bars.high[2] = 111.0
        self.bars.low[2] = 94.0
        trade = self._trade()
        self.assertEqual(trade.reason, "stop")
        self.assertLess(trade.exit, 95.0)

    def test_gap_through_stop_uses_open(self):
        self.bars.open[2] = 90.0
        self.bars.high[2] = 91.0
        self.bars.low[2] = 89.0
        trade = self._trade()
        self.assertEqual(trade.reason, "stop_gap")
        self.assertAlmostEqual(trade.exit, 90.0 * (1.0 - 0.0002))

    def test_target_exit(self):
        self.bars.high[2] = 111.0
        trade = self._trade()
        self.assertEqual(trade.reason, "target")
        self.assertGreater(trade.net_pct, 0.0)

    def test_time_exit_at_bar_96(self):
        trade = self._trade()
        self.assertEqual(trade.reason, "time")
        self.assertEqual(trade.exit_index, 96)

    def test_fee_and_funding_math(self):
        self.bars.high[2] = 111.0
        trade = self._trade()
        expected_gross = trade.exit / trade.entry - 1.0
        expected_fee = 0.001 * (1.0 + trade.exit / trade.entry)
        self.assertAlmostEqual(trade.gross_pct, expected_gross)
        self.assertAlmostEqual(trade.fee_pct, expected_fee)
        self.assertAlmostEqual(trade.net_pct, expected_gross - expected_fee)

    def test_missing_funding_is_excluded(self):
        trade = simulate_trade(self.bars, self.signal, [], [])
        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertTrue(trade.funding_missing)
        self.assertTrue(trade.excluded)

    def test_funding_sum(self):
        times = [0, 28_800_000, 57_600_000]
        rates = [0.0001, -0.0002, 0.0001]
        total, missing = funding_cost_pct(0, 57_600_000, times, rates)
        self.assertFalse(missing)
        self.assertAlmostEqual(total, 0.0)

    def test_cannot_complete_holding_window(self):
        bars = make_bars(50)
        self.assertIsNone(
            simulate_trade(bars, self.signal, self.funding_times, self.funding_rates)
        )

    def test_entry_status_rejects_gap_back_into_zone(self):
        signal = Signal(
            symbol="BTCUSDT",
            signal_index=0,
            entry_index=1,
            zone_zg=98.0,
            zone_zd=92.0,
            departure_high=110.0,
            pullback_low=101.0,
            stop=95.0,
            atr=1.0,
        )
        bars = make_bars(120)
        self.assertEqual(signal_entry_status(signal, bars), "candidate")
        bars.open[1] = 97.0
        self.assertEqual(signal_entry_status(signal, bars), "entry_reentered_zone")
        bars.open[1] = 94.0
        self.assertEqual(signal_entry_status(signal, bars), "entry_below_stop")
        self.assertEqual(signal_entry_status(signal, make_bars(50)), "no_forward_window")


class AtrAndWeekTests(unittest.TestCase):
    def test_wilder_atr_known_values(self):
        highs = [10, 11, 12, 11, 13, 14, 13, 15]
        lows = [9, 10, 10, 9, 11, 12, 11, 13]
        closes = [9.5, 10.5, 11, 10, 12, 13, 12, 14]
        atr = wilder_atr(highs, lows, closes, period=3)
        self.assertIsNone(atr[2])
        self.assertIsNotNone(atr[3])
        self.assertGreater(atr[3], 0)

    def test_week_key(self):
        # 2020-01-01 00:00 UTC is ISO week 1 of 2020.
        self.assertEqual(week_key(1577836800000), (2020, 1))


if __name__ == "__main__":
    unittest.main()
