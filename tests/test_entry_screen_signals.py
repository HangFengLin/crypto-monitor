"""Fixed hourly mechanisms, exact aggregation and point-in-time context."""
from __future__ import annotations

import copy
import importlib
import importlib.util
import math
import unittest
from decimal import Decimal

HOUR, STEP, FOUR = 3_600_000, 900_000, 14_400_000


def prepared(count=50):
    rows = []
    for index in range(count):
        opening = 1000 * FOUR + index * HOUR
        rows.append(dict(open_time=opening, close_time=opening + HOUR - 1,
                         open=100., high=101., low=99., close=100., volume=10.,
                         atr=1., ema20=100., ema60=90., bb_lower=98., bb_upper=102.,
                         higher_close_time=((opening + HOUR)//FOUR)*FOUR-1,
                         higher_ema20=100., higher_ema60=90., higher_adx=10.,
                         confirmed=True, is_closed=True))
    return rows


def mirror(rows):
    result = copy.deepcopy(rows)
    for row in result:
        old = dict(row)
        for field in ("open", "close", "ema20", "ema60", "higher_ema20", "higher_ema60"):
            row[field] = 200 - old[field]
        row.update(high=200-old["low"], low=200-old["high"],
                   bb_lower=200-old["bb_upper"], bb_upper=200-old["bb_lower"])
    return result


class EntryScreenSignalsTests(unittest.TestCase):
    def module(self):
        try:
            spec = importlib.util.find_spec("research_projects.entry_screen_20261025.signals")
        except ModuleNotFoundError:
            spec = None
        self.assertIsNotNone(spec,
                             "new hourly signal module is not implemented")
        return importlib.import_module("research_projects.entry_screen_20261025.signals")

    def last(self, rows, candidate):
        result = self.module().generate_events({"BTCUSDT": rows}, rows[-1]["open_time"], rows[-1]["close_time"]+1)
        self.assertEqual(len(result["decisions"]), 4)
        return result["events"][candidate]

    def test_d48_excludes_current_extreme_and_emits_both_directions_at_future_boundary(self):
        rows = prepared()
        rows[0]["high"] = 150.  # Outside the last 48 preceding hours.
        rows[-1].update(close=102., high=103.)
        for data, direction, stop in ((rows, "long", 99.), (mirror(rows), "short", 101.)):
            event = self.last(data, "D48")[0]
            self.assertEqual(event["direction"], direction)
            self.assertEqual(event["stop_loss"], stop)
            self.assertEqual(event["known_at"], data[-1]["close_time"]+1)
            self.assertEqual(event["signal_time"], data[-1]["close_time"]+1+STEP)
        rows[-1]["close"] = 101.
        self.assertEqual(self.last(rows, "D48"), [])

    def test_b20_uses_both_trigger_lows_and_strict_reentry(self):
        rows = prepared()
        rows[-2].update(close=97., low=96.)
        rows[-1].update(open=98., close=99., low=97., high=100.)
        self.assertEqual(self.last(rows, "B20")[0]["stop_loss"], 96.)
        self.assertEqual(self.last(mirror(rows), "B20")[0]["stop_loss"], 104.)
        for change in ({"close": 98.}, {"close": 102., "high": 103.}, {"higher_adx": 20.},
                       {"bb_lower": 99., "bb_upper": 99.}):
            changed = copy.deepcopy(rows)
            changed[-1].update(change)
            self.assertEqual(self.last(changed, "B20"), [])

    def test_s24_excludes_current_and_requires_strict_sweep_reclaim_and_body(self):
        rows = prepared()
        rows[-26]["low"] = 1.  # Outside the last 24 preceding hours.
        rows[-1].update(low=98., close=100.5)
        self.assertEqual(self.last(rows, "S24")[0]["direction"], "long")
        self.assertEqual(self.last(mirror(rows), "S24")[0]["direction"], "short")
        for change in ({"low": 99.}, {"close": 99.}, {"open": 100.5}):
            changed = copy.deepcopy(rows)
            changed[-1].update(change)
            self.assertEqual(self.last(changed, "S24"), [])

    def test_ec_cross_allows_equal_prior_state_but_requires_strict_current_and_higher_alignment(self):
        rows = prepared()
        rows[-2].update(ema20=90., ema60=90.)
        rows[-1].update(ema20=91., ema60=90.)
        self.assertEqual(self.last(rows, "EC20_60")[0]["direction"], "long")
        self.assertEqual(self.last(mirror(rows), "EC20_60")[0]["direction"], "short")
        for change in ({"ema20": 90.}, {"higher_ema20": 90.}):
            changed = copy.deepcopy(rows)
            changed[-1].update(change)
            self.assertEqual(self.last(changed, "EC20_60"), [])

    def test_missing_or_nonfinite_risk_and_future_context_are_waits(self):
        rows = prepared()
        rows[-1].update(close=102., high=103.)
        for change in ({"atr": None}, {"atr": math.nan}, {"higher_close_time": rows[-1]["close_time"]+FOUR}):
            changed = copy.deepcopy(rows)
            changed[-1].update(change)
            result = self.module().generate_events({"BTCUSDT": changed}, changed[-1]["open_time"], changed[-1]["close_time"]+1)
            self.assertTrue(all(d["signal"] == "wait" and d["reason"] for d in result["decisions"]))

    def test_decimal_aggregation_is_exact_and_a_missing_quarter_hour_is_rejected(self):
        opening = 1000 * FOUR
        lower = [dict(open_time=opening+i*STEP, close_time=opening+(i+1)*STEP-1,
                      open=Decimal("100.1"), high="101.2", low="99.3", close="100.4", volume="0.1") for i in range(4)]
        higher = [dict(open_time=opening-FOUR, close_time=opening-1,
                       open="100", high="101", low="99", close="100", volume="1")]
        row = self.module().prepare_hourly(lower, higher)[0]
        self.assertEqual((row["open"], row["high"], row["low"], row["close"], row["volume"]), (100.1,101.2,99.3,100.4,.4))
        with self.assertRaises(ValueError):
            self.module().prepare_hourly(lower[:2]+lower[3:], higher)

    def test_prefix_and_future_four_hour_shock_leave_earlier_features_unchanged(self):
        opening = 1000 * FOUR
        lower = []
        for i in range(480):
            price = 100 + math.sin(i/11)
            lower.append(dict(open_time=opening+i*STEP, close_time=opening+(i+1)*STEP-1,
                              open=price, high=price+1, low=price-1, close=price+.1, volume=10.))
        higher = [dict(open_time=t, close_time=t+FOUR-1, open=100., high=102., low=98., close=100.+.001*i, volume=100.)
                  for i,t in enumerate(range(opening-300*FOUR, opening+120*HOUR, FOUR))]
        full = self.module().prepare_hourly(lower, higher)
        prefix = self.module().prepare_hourly(lower[:400], higher)
        self.assertEqual(full[:100], prefix)
        shock = copy.deepcopy(higher)
        shock[-1].update(high=10_001., close=10_000.)
        changed = self.module().prepare_hourly(lower, shock)
        self.assertEqual(full[:100], changed[:100])

    def test_bollinger_uses_twenty_closes_and_population_standard_deviation(self):
        opening = 1000 * FOUR
        lower = []
        for hour in range(20):
            for quarter in range(4):
                stamp = opening + hour*HOUR + quarter*STEP
                lower.append(dict(open_time=stamp, close_time=stamp+STEP-1,
                                  open=100.+hour, high=121., low=99., close=100.+hour, volume=1.))
        higher = [dict(open_time=t, close_time=t+FOUR-1, open=100., high=121., low=99., close=100., volume=1.)
                  for t in range(opening-FOUR, opening+20*HOUR, FOUR)]
        rows = self.module().prepare_hourly(lower, higher)
        self.assertIsNone(rows[-2]["bb_lower"])
        self.assertAlmostEqual(rows[-1]["bb_lower"], 109.5-2*math.sqrt(33.25))
        self.assertAlmostEqual(rows[-1]["bb_upper"], 109.5+2*math.sqrt(33.25))


if __name__ == "__main__":
    unittest.main()
