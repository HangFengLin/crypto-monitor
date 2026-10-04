"""Independent fixtures for causal, fee-inclusive portfolio accounting."""

import copy
import importlib
import unittest

try:
    _module = importlib.import_module("research_portfolio")
except ModuleNotFoundError:
    _module = None

STEP = 900_000


def bars(count=6, price=100.0):
    return [dict(open_time=i * STEP, close_time=(i + 1) * STEP - 1,
                 open=price, high=price, low=price, close=price, volume=10.0)
            for i in range(count)]


def event(symbol="A", direction="long", index=1, stop=90.0, setup="one"):
    return dict(symbol=symbol, direction=direction, stop_loss=stop, setup_id=setup,
                signal_time=index * STEP, known_at=index * STEP - 1)


def funding_for(data, records=None, complete=True):
    union = [row for rows in data.values() for row in rows]
    start = min(row["open_time"] for row in union)
    end = max(row["close_time"] for row in union) + 1
    return {s: dict(complete=complete, start=rows[0]["open_time"] if rows else start,
                    end=rows[-1]["close_time"] + 1 if rows else end,
                    records=(records or {}).get(s, []))
            for s, rows in data.items()}


class ResearchPortfolioTests(unittest.TestCase):
    def run_portfolio(self, data, events, funding=None, **kwargs):
        self.assertIsNotNone(_module, "research_portfolio replay engine is missing")
        options = dict(initial_equity=10_000, risk_fraction=.005, max_positions=5,
                       max_risk_fraction=.025, direction_risk_fraction=.025,
                       fee_rate=0.0, slippage_bps=0.0, max_hold_bars=96,
                       stop_mode="structure_atr", strict_funding=True)
        options.update(kwargs)
        return _module.replay_portfolio(data, events,
                                       funding_for(data) if funding is None else funding,
                                       **options)

    def test_entries_use_next_open_and_never_signal_close(self):
        data = {"A": bars()}
        data["A"][1].update(open=105, high=105, low=105, close=105)
        result = self.run_portfolio(data, [event()])
        position = result["open_positions"][0]
        self.assertEqual(position["entry_time"], STEP)
        self.assertEqual(position["entry_price"], 105)
        self.assertAlmostEqual(position["quantity"], 50 / 15)
        self.assertEqual(result["fills"][0]["phase"], "open")

    def test_risk_sizing_includes_both_fill_fees_long_and_short(self):
        for direction, stop, extremes in [("long", 90, dict(low=90, high=120)),
                                           ("short", 110, dict(low=80, high=110))]:
            with self.subTest(direction=direction):
                data = {"A": bars()}
                data["A"][1].update(extremes)
                result = self.run_portfolio(data, [event(direction=direction, stop=stop)], fee_rate=.001)
                trade = result["trades"][0]
                self.assertAlmostEqual(trade["net_pnl"], -50.0)
                self.assertAlmostEqual(trade["initial_risk"], 50.0)
                self.assertEqual(trade["exit_reason"], "stop_loss")
                self.assertAlmostEqual(result["equity"][-1]["equity"], 9950.0)

    def test_entry_fee_is_debited_once_before_mtm(self):
        data = {"A": bars()}
        result = self.run_portfolio(data, [event()], fee_rate=.001)
        row = result["equity"][1]
        self.assertAlmostEqual(row["cash"], 9999.509322865554)
        self.assertAlmostEqual(row["equity"], row["cash"])
        self.assertEqual(row["positions"], 1)
        self.assertEqual(result["trades"], [])

    def test_mark_to_market_includes_open_positions(self):
        data = {"A": bars()}
        for row in data["A"][2:]:
            row.update(open=105, high=105, low=105, close=105)
        result = self.run_portfolio(data, [event()])
        self.assertEqual(result["trades"], [])
        self.assertEqual(result["equity"][2]["cash"], 10000)
        self.assertEqual(result["equity"][2]["unrealized_pnl"], 25)
        self.assertEqual(result["equity"][2]["equity"], 10025)

    def test_gap_stop_fills_at_worse_open_then_slippage(self):
        data = {"A": bars()}
        data["A"][2].update(open=80, low=79, high=85, close=82)
        result = self.run_portfolio(data, [event()], slippage_bps=10)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "gap_stop")
        self.assertAlmostEqual(trade["exit_price"], 79.92)
        self.assertLess(trade["net_pnl"], -50)
        self.assertEqual(trade["exit_time"], 2 * STEP)
        self.assertEqual(trade["exit_phase"], "open")
        self.assertEqual(result["fills"][-1]["phase"], "open")

    def test_short_gap_stop_fills_at_worse_open(self):
        data = {"A": bars()}
        data["A"][2].update(open=120, low=115, high=125, close=122)
        result = self.run_portfolio(data, [event(direction="short", stop=110)], slippage_bps=10)
        trade = result["trades"][0]
        self.assertAlmostEqual(trade["exit_price"], 120.12)
        self.assertLess(trade["net_pnl"], -50)

    def test_stop_first_is_shared_lifecycle_rule(self):
        data = {"A": bars()}
        data["A"][1].update(low=89, high=125)
        result = self.run_portfolio(data, [event()])
        self.assertEqual(result["trades"][0]["exit_price"], 90)
        self.assertEqual(result["trades"][0]["exit_reason"], "stop_loss")

    def test_duplicate_signal_is_rejected_not_silently_double_counted(self):
        data = {"A": bars()}
        result = self.run_portfolio(data, [event(), event()])
        self.assertEqual(len(result["open_positions"]), 1)
        self.assertEqual(result["rejections"][0]["reason"], "duplicate_event")

    def test_same_symbol_overlap_is_rejected_by_default(self):
        data = {"A": bars()}
        result = self.run_portfolio(data, [event(), event(index=2, setup="two")])
        self.assertEqual(len(result["open_positions"]), 1)
        self.assertEqual(result["rejections"][0]["reason"], "position_exists")

    def test_explicit_overlap_retains_both_positions_and_their_mtm(self):
        data = {"A": bars()}
        result = self.run_portfolio(data, [event(), event(index=2, setup="two")], allow_overlap=True)
        self.assertEqual(len(result["open_positions"]), 2)
        self.assertEqual(result["equity"][2]["positions"], 2)
        self.assertEqual(result["equity"][2]["gross_notional"], 1000)

    def test_fixed_symbol_setup_sort_controls_capacity_independent_of_input_order(self):
        data = {"B": bars(), "A": bars()}
        events = [event("B"), event("A")]
        left = self.run_portfolio(data, events, max_positions=1)
        right = self.run_portfolio(data, list(reversed(events)), max_positions=1)
        self.assertEqual(left, right)
        self.assertEqual(left["open_positions"][0]["symbol"], "A")
        self.assertEqual(left["rejections"][0]["reason"], "max_positions")

    def test_aggregate_and_same_direction_risk_caps_reject_new_signal(self):
        data = {"A": bars(), "B": bars()}
        for limits, reason in [(dict(max_risk_fraction=.005), "risk_budget"),
                               (dict(direction_risk_fraction=.005), "direction_risk_budget")]:
            with self.subTest(limits=limits):
                result = self.run_portfolio(data, [event("A"), event("B")], **limits)
                self.assertEqual(len(result["open_positions"]), 1)
                self.assertEqual(result["rejections"][0]["reason"], reason)

    def test_loose_research_caps_do_not_invent_position_limit(self):
        data = {str(i): bars() for i in range(7)}
        result = self.run_portfolio(data, [event(s) for s in data], max_positions=None,
                                    max_risk_fraction=1, direction_risk_fraction=1)
        self.assertEqual(len(result["open_positions"]), 7)

    def test_one_times_gross_cap_reduces_quantity_instead_of_exceeding_equity(self):
        data = {"A": bars()}
        result = self.run_portfolio(data, [event(stop=99.9)], fee_rate=.001)
        row = result["equity"][1]
        self.assertLessEqual(row["gross_notional"], row["equity"] + 1e-8)
        self.assertAlmostEqual(result["open_positions"][0]["quantity"], 99.9000999000999)

    def test_exhausted_short_gross_budget_never_opens_floating_point_ghost(self):
        data = {"A": bars(price=3.0), "B": bars(price=3.0)}
        events = [event(s, direction="short", stop=3.00003) for s in data]
        for capital in (10_000.0, 1_000_000.0, .01):
            with self.subTest(capital=capital):
                result = self.run_portfolio(data, events, initial_equity=capital,
                                            fee_rate=.001, slippage_bps=2, max_positions=None,
                                            max_risk_fraction=1, direction_risk_fraction=1)
                # Algebraically the first position exhausts 1x gross. At $10k,
                # float arithmetic leaves +1.8189894035458565e-12 in the budget.
                self.assertEqual([p["symbol"] for p in result["open_positions"]], ["A"])
                self.assertEqual(result["equity"][1]["positions"], 1)
                self.assertEqual(result["rejections"][0]["reason"], "notional_budget")

    def test_long_gross_budget_does_not_create_capacity_from_double_slippage_reserve(self):
        data = {"A": bars(price=3.0), "B": bars(price=3.0)}
        events = [event(s, direction="long", stop=2.99997) for s in data]
        result = self.run_portfolio(data, events, fee_rate=.001, slippage_bps=2,
                                    max_positions=None, max_risk_fraction=1, direction_risk_fraction=1)
        # One full-cap entry exhausts collateral after one fee and one adverse
        # mark at the open. A second entry is not justified by reserved slippage.
        self.assertEqual([p["symbol"] for p in result["open_positions"]], ["A"])
        self.assertEqual(result["rejections"][0]["reason"], "notional_budget")

    def test_full_gross_size_equals_post_entry_equity_for_both_directions(self):
        for direction, stop in (("long", 2.99997), ("short", 3.00003)):
            with self.subTest(direction=direction):
                data = {"A": bars(price=3.0)}
                result = self.run_portfolio(data, [event(direction=direction, stop=stop)],
                                            fee_rate=.001, slippage_bps=2)
                row = result["equity"][1]
                position = result["open_positions"][0]
                q, entry = position["quantity"], position["entry_price"]
                side = 1 if direction == "long" else -1
                independently_marked_equity = 10000 - q * entry * .001 + side * q * (3.0 - entry)
                self.assertAlmostEqual(row["equity"], independently_marked_equity)
                self.assertAlmostEqual(q * 3.0, independently_marked_equity, delta=3e-11)

    def test_real_sub_microdollar_budget_above_machine_precision_is_not_a_minimum_lot(self):
        data = {"A": bars(price=3.0), "B": bars(price=3.0)}
        records = {"A": [dict(time=2 * STEP, rate=1e-11, mark_price=3.0)]}
        result = self.run_portfolio(data, [event("A", direction="short", stop=3.00003),
                                           event("B", direction="short", stop=3.00003, index=2)],
                                    funding_for(data, records), fee_rate=.001, slippage_bps=2,
                                    max_positions=None, max_risk_fraction=1, direction_risk_fraction=1)
        self.assertEqual(result["rejections"], [])
        second = next(p for p in result["open_positions"] if p["symbol"] == "B")
        self.assertGreater(second["quantity"] * second["entry_price"], 1e-8)
        self.assertLess(second["quantity"] * second["entry_price"], 1e-6)

    def test_gross_cap_also_reserves_slippage_for_short_entry(self):
        data = {"A": bars()}
        result = self.run_portfolio(data, [event(direction="short", stop=100.01)], slippage_bps=10)
        row = result["equity"][1]
        self.assertLessEqual(row["gross_notional"], row["equity"] + 1e-8)

    def test_stop_budget_includes_adverse_slippage_on_both_fills(self):
        data = {"A": bars()}
        data["A"][1].update(low=89, high=125)
        result = self.run_portfolio(data, [event()], fee_rate=.001, slippage_bps=10)
        self.assertAlmostEqual(result["trades"][0]["net_pnl"], -50)
        self.assertAlmostEqual(result["equity"][-1]["equity"], 9950)

    def test_later_funding_credit_precedes_known_timeout_close(self):
        data = {"A": bars()}
        records = {"A": [dict(time=STEP + 10, rate=-.01, mark_price=100)]}
        result = self.run_portfolio(data, [event()], funding_for(data, records), max_hold_bars=1)
        self.assertEqual(result["trades"][0]["funding_cashflow"], 5)
        self.assertFalse(result["trades"][0]["funding_timing_ambiguous"])

    def test_timeout_counts_entry_as_bar_one_and_ignores_bar_97(self):
        data = {"A": bars(98)}
        data["A"][97].update(open=200, low=200, high=200, close=200)
        result = self.run_portfolio(data, [event()], max_hold_bars=96)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "timeout")
        self.assertEqual(trade["exit_time"], 97 * STEP)
        self.assertEqual(trade["holding_bars"], 96)
        self.assertEqual(trade["exit_price"], 100)

    def test_shorter_prefix_does_not_force_close_or_change_prior_equity(self):
        data = {"A": bars(8)}
        for row in data["A"][5:]:
            row.update(open=95, low=95, high=95, close=95)
        short = self.run_portfolio({"A": data["A"][:5]}, [event()])
        full = self.run_portfolio(data, [event()])
        self.assertEqual(short["equity"], full["equity"][:5])
        self.assertEqual(short["trades"], [])
        self.assertEqual(len(short["open_positions"]), 1)

    def test_finalize_is_explicit_and_reconciles_account_equity(self):
        data = {"A": bars()}
        result = self.run_portfolio(data, [event()], finalize=True, fee_rate=.001)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "fold_end")
        self.assertEqual(result["open_positions"], [])
        self.assertAlmostEqual(result["equity"][-1]["equity"], 9999.018645731107)
        self.assertAlmostEqual(10000 + trade["net_pnl"], result["equity"][-1]["cash"])

    def test_future_known_signal_and_incomplete_bar_data_fail_closed(self):
        data = {"A": bars()}
        e = event()
        e["known_at"] = e["signal_time"] + 1
        with self.assertRaisesRegex(ValueError, "future|known_at"):
            self.run_portfolio(data, [e])
        bad = copy.deepcopy(data)
        bad["A"][2]["is_closed"] = False
        with self.assertRaisesRegex(ValueError, "closed|bar"):
            self.run_portfolio(bad, [])

    def test_internal_symbol_or_union_gaps_fail_closed(self):
        a = bars()
        for data in [{"A": a, "B": a[:2] + a[3:]}, {"A": a[:2], "B": a[3:]}]:
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, "calendar|synchron"):
                self.run_portfolio(data, [])

    def test_ragged_prefix_suffix_and_empty_symbols_are_explicitly_audited(self):
        data = {"A": bars(), "B": bars()[2:5], "C": []}
        result = self.run_portfolio(data, [event("B", index=3)], finalize=True)
        self.assertEqual(len(result["equity"]), 6)
        self.assertEqual(result["bar_coverage"]["B"]["missing_prefix_bars"], 2)
        self.assertEqual(result["bar_coverage"]["B"]["missing_suffix_bars"], 1)
        self.assertEqual(result["bar_coverage"]["C"]["all_missing_bar_count"], 6)
        self.assertFalse(result["bar_coverage"]["B"]["pit_listing_authority"])
        self.assertFalse(result["promotion_eligible"])

    def test_ragged_event_requires_both_observed_signal_and_execution_bars(self):
        data = {"A": bars(), "B": bars()[2:4], "C": []}
        result = self.run_portfolio(data, [event("B", index=2), event("B", index=4, setup="end"),
                                           event("C", setup="empty")])
        self.assertEqual([row["reason"] for row in result["rejections"]],
                         ["no_execution_bar", "no_observed_signal_bar", "no_execution_bar"])
        self.assertEqual(result["open_positions"], [])

    def test_ragged_suffix_open_position_fails_without_explicit_finalize(self):
        data = {"A": bars(), "B": bars()[:4]}
        with self.assertRaisesRegex(ValueError, "missing.*position|coverage"):
            self.run_portfolio(data, [event("B")])
        result = self.run_portfolio(data, [event("B")], finalize=True)
        self.assertEqual(result["trades"][0]["exit_reason"], "coverage_end")
        self.assertEqual(result["trades"][0]["exit_time"], 4 * STEP)
        self.assertEqual(result["equity"][4]["positions"], 0)

    def test_all_empty_symbols_fail_without_a_union_calendar(self):
        with self.assertRaisesRegex(ValueError, "calendar|nonempty"):
            _module.replay_portfolio({"A": [], "B": []}, [], strict_funding=False)

    def test_distinct_direction_limits_freeze_and_enforce_short_two_risks(self):
        data = {str(i): bars() for i in range(6)}
        events = [event(str(i), "short" if i < 3 else "long", stop=110 if i < 3 else 90) for i in range(6)]
        caps = {"long": .025, "short": .01}
        result = self.run_portfolio(data, events, direction_risk_fraction=caps,
                                    max_risk_fraction=1, max_positions=None)
        self.assertEqual(result["equity"][1]["short_risk"], 100)
        self.assertEqual(result["equity"][1]["long_risk"], 150)
        self.assertEqual(result["rejections"][0]["symbol"], "2")
        self.assertEqual(result["parameters"]["direction_risk_fraction"], caps)
        for bad in [{"long": .1}, {"long": .1, "short": .1, "extra": .1},
                    {"long": .1, "short": float("nan")}, {"long": .1, "short": 0}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.run_portfolio(data, [], direction_risk_fraction=bad)

    def test_future_intrabar_exit_cannot_release_open_admission_capacity(self):
        data = {"A": bars(), "B": bars()}
        data["A"][2].update(high=110)
        result = self.run_portfolio(data, [event("A"), event("B", index=2)], max_positions=1)
        self.assertEqual(result["rejections"][0]["reason"], "max_positions")
        self.assertEqual(result["trades"][0]["symbol"], "A")
        self.assertEqual(result["open_positions"], [])

    def test_known_open_partial_cannot_be_overridden_by_later_old_stop(self):
        data = {"A": bars()}
        data["A"][2].update(open=110, high=115, low=89, close=100)
        result = self.run_portfolio(data, [event()], stop_mode="partial_1r_breakeven")
        trade = result["trades"][0]
        self.assertEqual(trade["partial_exits"][0]["time"], 2 * STEP)
        self.assertEqual(trade["partial_exits"][0]["exit_price"], 110)
        self.assertEqual(trade["exit_price"], 100)
        self.assertEqual(trade["net_pnl"], 25)
        self.assertEqual(trade["partial_exits"][0]["phase"], "open")
        self.assertEqual(trade["exit_phase"], "bar_end")
        self.assertEqual(result["fills"][-1]["phase"], "bar_end")

    def test_known_open_two_r_uses_trigger_prices_and_releases_capacity(self):
        data = {"A": bars(), "B": bars()}
        data["A"][2].update(open=125, high=130, low=89, close=100)
        result = self.run_portfolio(data, [event("A"), event("B", index=2)],
                                    stop_mode="partial_1r_breakeven", max_positions=1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_time"], 2 * STEP)
        self.assertEqual(trade["partial_exits"][0]["exit_price"], 110)
        self.assertEqual(trade["exit_price"], 120)
        self.assertEqual(trade["net_pnl"], 75)
        self.assertEqual(result["open_positions"][0]["symbol"], "B")
        self.assertEqual(trade["exit_phase"], "open")
        self.assertEqual(trade["partial_exits"][0]["phase"], "open")

    def test_intrabar_partial_credit_keeps_only_definitely_surviving_quantity(self):
        data = {"A": bars()}
        data["A"][2].update(high=110, low=99, close=105)
        for rate, expected in [(-.01, 2.5), (.01, -5)]:
            with self.subTest(rate=rate):
                records = {"A": [dict(time=2 * STEP + 10, rate=rate, mark_price=100)]}
                result = self.run_portfolio(data, [event()], funding_for(data, records),
                                            stop_mode="partial_1r_breakeven", finalize=True)
                self.assertEqual(result["trades"][0]["funding_cashflow"], expected)
                self.assertTrue(result["trades"][0]["funding_timing_ambiguous"])

    def test_missing_funding_is_partial_or_hard_failure_never_promotion(self):
        data = {"A": bars()}
        with self.assertRaisesRegex(ValueError, "funding"):
            self.run_portfolio(data, [event()], funding={})
        result = self.run_portfolio(data, [event()], funding={}, strict_funding=False)
        self.assertEqual(result["funding_status"], "PARTIAL")
        self.assertEqual(result["status"], "RESEARCH_ONLY")
        self.assertFalse(result["promotion_eligible"])

    def test_funding_at_open_charges_old_positions_before_new_entries(self):
        data = {"A": bars(), "B": bars()}
        records = {s: [dict(time=2 * STEP, rate=.01, mark_price=100)] for s in data}
        result = self.run_portfolio(data, [event("A"), event("B", index=2)],
                                    funding_for(data, records), finalize=True)
        old, new = result["trades"]
        self.assertEqual(old["funding_cashflow"], -5)
        self.assertEqual(new["funding_cashflow"], 0)
        self.assertAlmostEqual(new["initial_risk"], 49.975)
        self.assertAlmostEqual(result["equity"][-1]["equity"], 9995)

    def test_intrabar_exit_funding_debits_are_paid_ambiguous_credits_withheld(self):
        for rate, expected in [(.01, -5), (-.01, 0)]:
            with self.subTest(rate=rate):
                data = {"A": bars()}
                data["A"][2].update(high=110)
                records = {"A": [dict(time=2 * STEP + 10, rate=rate, mark_price=100)]}
                result = self.run_portfolio(data, [event()], funding_for(data, records))
                trade = result["trades"][0]
                self.assertEqual(trade["funding_cashflow"], expected)
                self.assertTrue(trade["funding_timing_ambiguous"])

    def test_open_gap_exit_does_not_pay_later_intrabar_funding(self):
        data = {"A": bars()}
        data["A"][2].update(open=80, low=79, high=85, close=82)
        records = {"A": [dict(time=2 * STEP + 10, rate=.01, mark_price=100)]}
        result = self.run_portfolio(data, [event()], funding_for(data, records))
        self.assertEqual(result["trades"][0]["funding_cashflow"], 0)

    def test_duplicate_or_invalid_funding_is_not_accepted_as_complete(self):
        data = {"A": bars()}
        record = dict(time=2 * STEP, rate=.01, mark_price=100)
        with self.assertRaisesRegex(ValueError, "funding"):
            self.run_portfolio(data, [], funding_for(data, {"A": [record, record]}))

    def test_exact_open_funding_debit_precedes_gap_exit_and_capacity_reuse(self):
        data = {"A": bars(), "B": bars()}
        data["A"][2].update(open=80, low=79, high=85, close=82)
        records = {"A": [dict(time=2 * STEP, rate=.01, mark_price=80)]}
        result = self.run_portfolio(data, [event("A"), event("B", index=2)],
                                    funding_for(data, records), max_positions=1)
        self.assertEqual(result["trades"][0]["funding_cashflow"], -4)
        self.assertEqual(result["open_positions"][0]["symbol"], "B")
        self.assertAlmostEqual(result["open_positions"][0]["initial_risk"], 49.48)

    def test_direction_cap_counts_short_and_long_separately(self):
        data = {"A": bars(), "B": bars()}
        result = self.run_portfolio(data, [event("A"), event("B", "short", stop=110)],
                                    direction_risk_fraction=.005)
        self.assertEqual(len(result["open_positions"]), 2)
        self.assertEqual(result["equity"][1]["long_risk"], 50)
        self.assertEqual(result["equity"][1]["short_risk"], 50)
        self.assertEqual(result["equity"][1]["net_notional"], 0)
        self.assertEqual(result["equity"][1]["long_positions"], 1)
        self.assertEqual(result["equity"][1]["short_positions"], 1)

    def test_ragged_prefix_invariance_while_all_held_symbols_have_marks(self):
        data = {"A": bars(8), "B": bars(8)[3:], "C": []}
        short_data = {s: [row for row in rows if row["open_time"] < 6 * STEP] for s, rows in data.items()}
        events = [event("A"), event("B", index=4)]
        short = self.run_portfolio(short_data, events)
        full = self.run_portfolio(data, events)
        self.assertEqual(short["equity"], full["equity"][:6])
        self.assertEqual(short["fills"], full["fills"])

    def test_short_open_profit_partial_arms_be_before_later_range(self):
        data = {"A": bars()}
        data["A"][2].update(open=90, low=85, high=111, close=100)
        result = self.run_portfolio(data, [event(direction="short", stop=110)],
                                    stop_mode="partial_1r_breakeven")
        trade = result["trades"][0]
        self.assertEqual(trade["partial_exits"][0]["exit_price"], 90)
        self.assertEqual(trade["exit_price"], 100)
        self.assertEqual(trade["net_pnl"], 25)

    def test_open_partial_funding_credit_uses_known_half_with_no_ambiguity(self):
        data = {"A": bars()}
        data["A"][2].update(open=110, low=105, high=115, close=110)
        for row in data["A"][3:]:
            row.update(open=110, low=110, high=110, close=110)
        records = {"A": [dict(time=2 * STEP + 10, rate=-.01, mark_price=100)]}
        result = self.run_portfolio(data, [event()], funding_for(data, records),
                                    stop_mode="partial_1r_breakeven", finalize=True)
        self.assertEqual(result["trades"][0]["funding_cashflow"], 2.5)
        self.assertFalse(result["trades"][0]["funding_timing_ambiguous"])

    def test_open_trailing_state_cannot_use_current_bar_close_atr(self):
        data = {"A": bars(3)}
        data["A"][1]["atr"] = 20
        data["A"][2].update(open=115, low=108, high=116, close=112, atr=.01)
        result = self.run_portfolio(data, [event()], stop_mode="atr_trailing_after_1r")
        self.assertEqual(result["trades"], [])
        self.assertEqual(result["equity"][2]["positions"], 1)
        self.assertAlmostEqual(result["open_positions"][0]["active_stop"], 115.988)

    def test_gap_losses_can_exceed_budget_and_block_new_risk_at_negative_equity(self):
        data = {"A": bars(), "B": bars()}
        data["A"][2].update(open=5000, low=5000, high=5000, close=5000)
        result = self.run_portfolio(data, [event("A", "short", stop=110), event("B", index=2)])
        self.assertEqual(result["equity"][2]["equity"], -14500)
        self.assertEqual(result["rejections"][0]["reason"], "nonpositive_equity")
        self.assertEqual(result["open_positions"], [])

    def test_delay_moves_execution_without_using_intervening_future_prices(self):
        data = {"A": bars()}
        data["A"][2].update(open=102, low=102, high=102, close=102)
        result = self.run_portfolio(data, [event()], delay_bars=1)
        self.assertEqual(result["open_positions"][0]["entry_time"], 2 * STEP)
        self.assertEqual(result["open_positions"][0]["entry_price"], 102)
        self.assertEqual(result["equity"][1]["positions"], 0)

    def test_invalid_event_boundary_is_rejected_without_execution(self):
        data = {"A": bars()}
        malformed = event()
        malformed.update(signal_time=STEP + 100, known_at=STEP)
        result = self.run_portfolio(data, [malformed])
        self.assertEqual(result["rejections"][0]["reason"], "no_execution_bar")
        self.assertEqual(result["open_positions"], [])

    def test_invalid_parameters_and_legacy_return_model_are_rejected(self):
        data = {"A": bars()}
        for options in [dict(risk_fraction=0), dict(max_positions=0), dict(fee_rate=-1),
                        dict(slippage_bps=float("nan")), dict(return_model="legacy_ratio_v1"),
                        dict(delay_bars=-1), dict(max_hold_bars=0), dict(stop_mode="unknown")]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_portfolio(data, [], **options)

    def test_inputs_are_not_mutated(self):
        data = {"A": bars()}
        events = [event()]
        funding = funding_for(data)
        before = copy.deepcopy((data, events, funding))
        self.run_portfolio(data, events, funding)
        self.assertEqual((data, events, funding), before)

    def test_partial_one_r_realizes_half_and_be_arms_only_next_bar(self):
        data = {"A": bars()}
        data["A"][1].update(high=110, low=99, close=105)
        data["A"][2].update(open=105, high=105, low=99, close=102)
        result = self.run_portfolio(data, [event()], stop_mode="partial_1r_breakeven")
        self.assertEqual(result["equity"][1]["positions"], 1)
        self.assertEqual(result["equity"][1]["cash"], 10025)
        self.assertEqual(result["equity"][1]["unrealized_pnl"], 12.5)
        trade = result["trades"][0]
        self.assertEqual(trade["quantity"], 5)
        self.assertEqual(trade["partial_exits"][0]["quantity"], 2.5)
        self.assertEqual(trade["exit_quantity"], 2.5)
        self.assertEqual(trade["exit_reason"], "protected_stop")
        self.assertEqual(trade["net_pnl"], 25)

    def test_partial_same_bar_two_r_prices_each_initial_quantity_fraction(self):
        data = {"A": bars()}
        data["A"][1].update(high=120)
        result = self.run_portfolio(data, [event()], stop_mode="partial_1r_breakeven")
        trade = result["trades"][0]
        self.assertEqual(trade["net_pnl"], 75)
        self.assertEqual(trade["partial_exits"][0]["exit_price"], 110)
        self.assertEqual(trade["exit_price"], 120)
        self.assertEqual(result["equity"][-1]["cash"], 10075)

    def test_partial_stop_collision_does_not_record_half_exit(self):
        data = {"A": bars()}
        data["A"][1].update(high=125, low=89)
        result = self.run_portfolio(data, [event()], stop_mode="partial_1r_breakeven")
        self.assertEqual(result["trades"][0]["partial_exits"], [])
        self.assertEqual(result["trades"][0]["net_pnl"], -50)

    def test_partial_fee_accounting_is_per_fill_not_double_entry_fee(self):
        data = {"A": bars()}
        data["A"][1].update(high=120)
        result = self.run_portfolio(data, [event()], stop_mode="partial_1r_breakeven", fee_rate=.001)
        trade = result["trades"][0]
        # Initial qty=50/10.19; gross=qty*(.5*10+.5*20), all fees=qty*(.1+.055+.06).
        self.assertAlmostEqual(trade["net_pnl"], 72.54661432777233)
        self.assertAlmostEqual(10000 + trade["net_pnl"], result["equity"][-1]["equity"])

    def test_partial_remaining_quantity_alone_pays_next_open_funding(self):
        data = {"A": bars()}
        data["A"][1].update(high=110, close=105)
        data["A"][2].update(open=105, low=105, high=105, close=105)
        records = {"A": [dict(time=2 * STEP, rate=.01, mark_price=100)]}
        result = self.run_portfolio(data, [event()], funding_for(data, records),
                                    stop_mode="partial_1r_breakeven", finalize=True)
        self.assertEqual(result["trades"][0]["funding_cashflow"], -2.5)

    def test_partial_state_survives_repeated_future_flat_bars_without_second_sale(self):
        data = {"A": bars()}
        for row in data["A"][1:]:
            row.update(open=105 if row["open_time"] > STEP else 100, low=105 if row["open_time"] > STEP else 100,
                       high=110, close=105)
        result = self.run_portfolio(data, [event()], stop_mode="partial_1r_breakeven")
        position = result["open_positions"][0]
        self.assertEqual(position["remaining_fraction"], .5)
        self.assertEqual(len(position["partial_exits"]), 1)
        self.assertEqual(result["equity"][-1]["cash"], 10025)


if __name__ == "__main__":
    unittest.main()
