"""Hand-priced research admissions using the actual 1R full-exit reference."""

import copy
import unittest

from research_portfolio import replay_portfolio

STEP = 900_000


def fixture(direction="long", stop=99.3, count=5):
    rows = [dict(open_time=i * STEP, close_time=(i + 1) * STEP - 1,
                 open=100.0, high=100.0, low=100.0, close=100.0, volume=10.0)
            for i in range(count)]
    event = dict(symbol="A", direction=direction, stop_loss=stop, setup_id="one",
                 signal_time=STEP, known_at=STEP - 1)
    funding = {"A": dict(complete=True, start=0, end=count * STEP, records=[])}
    return {"A": rows}, [event], funding


def run(inputs, **options):
    parameters = dict(fee_rate=.001, slippage_bps=0, stop_mode="structure_atr",
                      min_net_reward_risk=.5)
    parameters.update(options)
    return replay_portfolio(*inputs, **parameters)


def entry_fills(result):
    return [fill for fill in result["fills"] if fill["kind"] == "entry"]


def anchor_fixture(direction="long", count=5):
    inputs = fixture(direction, 97.0 if direction == "long" else 103.0, count)
    inputs[1][0]["candidate_lifecycle"] = dict(
        version="strict_candidate_v1", anchor=100.0, anchor_close_time=0,
        first_ready_time=STEP - 1, checked_through_close_time=STEP - 1)
    return inputs


def run_anchor(inputs, **options):
    parameters = dict(fee_rate=.001, slippage_bps=2,
                      require_candidate_anchor_at_open=True)
    parameters.update(options)
    return replay_portfolio(*inputs, **parameters)


class FeeAwareAdmissionTests(unittest.TestCase):
    def test_long_and_short_hand_priced_one_r_acceptance_and_rejection(self):
        # Long: .7-.2007 reward / (.7+.1993) loss passes; .5-.2005 /
        # (.5+.1995) fails. Short has different exit/stop actual notionals.
        cases = [("long", 99.3, .4993, .8993, True),
                 ("long", 99.5, .2995, .6995, False),
                 ("short", 100.7, .5007, .9007, True),
                 ("short", 100.5, .3005, .7005, False)]
        for direction, stop, reward, loss, admitted in cases:
            with self.subTest(direction=direction, stop=stop):
                result = run(fixture(direction, stop))
                self.assertEqual(bool(entry_fills(result)), admitted)
                record = entry_fills(result)[0] if admitted else result["rejections"][0]
                if not admitted:
                    self.assertEqual(record["reason"], "net_reward_risk")
                self.assertEqual(record["reward_reference"], "protection_price_1r")
                self.assertAlmostEqual(record["potential_reward_per_unit"], reward)
                self.assertAlmostEqual(record["potential_loss_per_unit"], loss)
                self.assertAlmostEqual(record["net_reward_risk"], reward / loss)
                self.assertEqual(record["min_net_reward_risk"], .5)

    def test_two_r_diagnostic_cannot_approve_an_inadequate_one_r_exit(self):
        result = run(fixture(stop=99.5))
        self.assertEqual(entry_fills(result), [])
        rejection = result["rejections"][0]
        self.assertLess(rejection["net_reward_risk"], .5)
        # 2R target=101; reward=1-.201=.799, while stop loss=.6995.
        self.assertAlmostEqual(rejection["target_net_reward_risk"], .799 / .6995)
        self.assertGreater(rejection["target_net_reward_risk"], 1)

    def test_actual_entry_and_both_exit_slippages_are_in_the_hand_prices(self):
        cases = [("long", 99.3, 100.02, 100.74, 100.719852, 99.28014,
                  .499112148, .93916014),
                 ("short", 100.7, 99.98, 99.26, 99.279852, 100.72014,
                  .500888148, .94084014)]
        for direction, stop, entry, reference, fill, stop_fill, reward, loss in cases:
            with self.subTest(direction=direction):
                result = run(fixture(direction, stop), slippage_bps=2)
                record = entry_fills(result)[0]
                self.assertAlmostEqual(record["entry_price"], entry)
                self.assertAlmostEqual(record["reward_reference_price"], reference)
                self.assertAlmostEqual(record["reward_fill_price"], fill)
                self.assertAlmostEqual(record["stop_fill_price"], stop_fill)
                self.assertAlmostEqual(record["potential_reward_per_unit"], reward)
                self.assertAlmostEqual(record["potential_loss_per_unit"], loss)
                self.assertAlmostEqual(record["net_reward_risk"], reward / loss)

    def test_increasing_costs_reprices_admission_in_each_scenario(self):
        for direction, stop in [("long", 99.35), ("short", 100.65)]:
            with self.subTest(direction=direction):
                inputs = fixture(direction, stop)
                self.assertTrue(entry_fills(run(inputs, fee_rate=.001, slippage_bps=2)))
                for costs in [dict(fee_rate=.002, slippage_bps=2),
                              dict(fee_rate=.001, slippage_bps=4)]:
                    with self.subTest(costs=costs):
                        result = run(inputs, **costs)
                        self.assertEqual(entry_fills(result), [])
                        self.assertEqual(result["rejections"][0]["reason"], "net_reward_risk")

    def test_delayed_admission_uses_the_delayed_actual_open(self):
        inputs = fixture()
        inputs[0]["A"][2].update(open=99.5, high=99.5, low=99.5, close=99.5)
        self.assertTrue(entry_fills(run(inputs)))
        result = run(inputs, delay_bars=1)
        self.assertEqual(entry_fills(result), [])
        rejection = result["rejections"][0]
        self.assertEqual(rejection["execution_time"], 2 * STEP)
        self.assertEqual(rejection["entry_price"], 99.5)
        self.assertAlmostEqual(rejection["potential_reward_per_unit"], .0008)
        self.assertAlmostEqual(rejection["potential_loss_per_unit"], .3988)

    def test_future_high_low_and_funding_do_not_change_admission(self):
        original = fixture()
        accepted = entry_fills(run(original))
        changed = copy.deepcopy(original)
        # Entry bar's unknown range/close may change realized exits, not entry.
        changed[0]["A"][1].update(high=1000, low=1, close=500)
        changed[0]["A"][3].update(open=500, high=1000, low=1, close=500)
        changed[2]["A"]["records"] = [dict(time=3 * STEP, rate=.25, mark_price=500)]
        self.assertEqual(entry_fills(run(changed)), accepted)
        negative = copy.deepcopy(changed)
        negative[2]["A"]["records"][0]["rate"] = -.25
        self.assertEqual(entry_fills(run(negative)), accepted)
        self.assertIs(accepted[0]["funding_included"], False)

    def test_gate_cannot_use_a_favorable_future_range_to_rescue_rejection(self):
        inputs = fixture(stop=99.5)
        expected = run(inputs)["rejections"]
        inputs[0]["A"][1].update(high=1000, low=100, close=500)
        inputs[0]["A"][4].update(high=1000, low=100, close=500)
        self.assertEqual(run(inputs)["rejections"], expected)

    def test_none_preserves_admission_and_lifecycle_for_every_old_mode(self):
        # Existing behavior admits this fee-dominated tiny stop. Every mode
        # reaches its 2R target in this fixture; disabling C2 must keep it.
        for mode in ("structure_atr", "structure_atr_breakeven_after_1r",
                     "atr_trailing_after_1r", "partial_1r_breakeven"):
            with self.subTest(mode=mode):
                inputs = fixture(stop=99.95)
                inputs[0]["A"][1].update(high=100.25, close=100.1)
                default = replay_portfolio(*inputs, fee_rate=.001, slippage_bps=0, stop_mode=mode)
                explicit = run(inputs, min_net_reward_risk=None, stop_mode=mode)
                self.assertEqual(default, explicit)
                self.assertEqual(len(entry_fills(default)), 1)
                self.assertEqual(default["trades"][0]["exit_reason"], "take_profit")
                self.assertLess(default["trades"][0]["net_pnl"], 0)
                self.assertNotIn("net_reward_risk", entry_fills(default)[0])

    def test_threshold_equality_is_admitted(self):
        inputs = fixture()
        result = run(inputs, fee_rate=0, min_net_reward_risk=1)
        self.assertEqual(len(entry_fills(result)), 1)
        self.assertEqual(entry_fills(result)[0]["net_reward_risk"], 1)

    def test_invalid_threshold_and_inapplicable_exit_contract_fail_closed(self):
        for options in [dict(min_net_reward_risk=-.1), dict(min_net_reward_risk=float("nan")),
                        dict(min_net_reward_risk=float("inf")), dict(min_net_reward_risk=True),
                        dict(min_net_reward_risk=".5"), dict(stop_mode="partial_1r_breakeven"),
                        dict(stop_mode="atr_trailing_after_1r"),
                        dict(stop_mode="structure_atr_breakeven_after_1r"), dict(reward_risk=.5)]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                run(fixture(), **options)

    def test_parameters_disclose_reference_and_unknown_funding_boundary(self):
        result = run(fixture())
        self.assertEqual(result["parameters"]["min_net_reward_risk"], .5)
        self.assertEqual(result["parameters"]["net_reward_reference"], "protection_price_1r")
        self.assertEqual(result["parameters"]["net_reward_risk_excludes"],
                         ["future_funding", "gap_beyond_stop"])
        inputs = fixture()
        before = copy.deepcopy(inputs)
        run(inputs)
        self.assertEqual(inputs, before)


class CandidateAnchorAdmissionTests(unittest.TestCase):
    def test_strict_actual_open_break_rejects_long_and_short(self):
        for direction, raw_open in [("long", 99.0), ("short", 101.0)]:
            with self.subTest(direction=direction):
                inputs = anchor_fixture(direction)
                inputs[0]["A"][1].update(open=raw_open, high=raw_open,
                                          low=raw_open, close=raw_open)
                result = run_anchor(inputs)
                self.assertEqual(entry_fills(result), [])
                rejection = result["rejections"][0]
                self.assertEqual(rejection["reason"], "candidate_invalidated_at_open")
                self.assertEqual(rejection["raw_open"], raw_open)
                self.assertEqual(rejection["candidate_anchor"], 100.0)
                self.assertEqual(rejection["execution_time"], STEP)

    def test_anchor_touch_is_allowed_with_both_unfavorable_entry_slippages(self):
        for direction, expected_entry in [("long", 100.02), ("short", 99.98)]:
            with self.subTest(direction=direction):
                record = entry_fills(run_anchor(anchor_fixture(direction)))[0]
                self.assertAlmostEqual(record["entry_price"], expected_entry)
                self.assertEqual(record["candidate_anchor"], 100.0)
                self.assertEqual(record["candidate_delay_bars_checked"], 0)

    def test_slipped_fill_cannot_rescue_a_broken_raw_open(self):
        for direction, raw_open in [("long", 99.99), ("short", 100.01)]:
            with self.subTest(direction=direction):
                inputs = anchor_fixture(direction)
                inputs[0]["A"][1].update(open=raw_open, high=raw_open,
                                          low=raw_open, close=raw_open)
                # 20bps moves the hypothetical fill to the valid anchor side.
                result = run_anchor(inputs, slippage_bps=20)
                self.assertEqual(entry_fills(result), [])
                self.assertEqual(result["rejections"][0]["reason"], "candidate_invalidated_at_open")

    def test_completed_delay_bar_break_cannot_be_reset_by_recovered_open(self):
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                inputs = anchor_fixture(direction)
                inputs[0]["A"][1]["low" if direction == "long" else "high"] = (
                    99.0 if direction == "long" else 101.0)
                result = run_anchor(inputs, delay_bars=2)
                self.assertEqual(entry_fills(result), [])
                rejection = result["rejections"][0]
                self.assertEqual(rejection["reason"], "candidate_invalidated_during_delay")
                self.assertEqual(rejection["invalidated_bar_open_time"], STEP)
                self.assertEqual(rejection["execution_time"], 3 * STEP)
                self.assertEqual(rejection["raw_open"], 100.0)

    def test_touching_delay_ranges_and_the_execution_open_remains_valid(self):
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                result = run_anchor(anchor_fixture(direction), delay_bars=2)
                record = entry_fills(result)[0]
                self.assertEqual(record["time"], 3 * STEP)
                self.assertEqual(record["candidate_delay_bars_checked"], 2)
                self.assertEqual(record["candidate_anchor_checked_at"], 3 * STEP)

    def test_delayed_actual_open_is_checked_after_intact_delay_bars(self):
        for direction, raw_open in [("long", 99.0), ("short", 101.0)]:
            with self.subTest(direction=direction):
                inputs = anchor_fixture(direction)
                inputs[0]["A"][2].update(open=raw_open, high=raw_open,
                                          low=raw_open, close=raw_open)
                result = run_anchor(inputs, delay_bars=1)
                self.assertEqual(entry_fills(result), [])
                self.assertEqual(result["rejections"][0]["reason"], "candidate_invalidated_at_open")
                self.assertEqual(result["rejections"][0]["execution_time"], 2 * STEP)

    def test_execution_range_and_future_ranges_do_not_change_admission(self):
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                inputs = anchor_fixture(direction)
                expected = entry_fills(run_anchor(inputs, delay_bars=1))
                # This bar's open is observed, its subsequent wick is not.
                inputs[0]["A"][2]["low" if direction == "long" else "high"] = (
                    99.0 if direction == "long" else 101.0)
                inputs[0]["A"][4].update(high=1000, low=1, close=500)
                self.assertEqual(entry_fills(run_anchor(inputs, delay_bars=1)), expected)

    def test_missing_or_future_candidate_metadata_fails_closed(self):
        base = anchor_fixture()
        original = base[1][0]["candidate_lifecycle"]
        missing_fields = [{k: v for k, v in original.items() if k != field}
                          for field in original]
        malformed = [None, {}, {**original, "version": "unknown"},
                     {**original, "anchor": float("nan")}, {**original, "anchor": 0},
                     {**original, "anchor": True}, {**original, "anchor_close_time": True},
                     {**original, "first_ready_time": STEP},
                     {**original, "checked_through_close_time": 2 * STEP - 1}]
        for metadata in missing_fields + malformed:
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                inputs = copy.deepcopy(base)
                inputs[1][0]["candidate_lifecycle"] = metadata
                run_anchor(inputs)
        missing = copy.deepcopy(base)
        missing[1][0].pop("candidate_lifecycle")
        with self.assertRaises(ValueError):
            run_anchor(missing)
        known_late = copy.deepcopy(base)
        known_late[1][0]["known_at"] = STEP - 2
        with self.assertRaises(ValueError):
            run_anchor(known_late)

    def test_missing_completed_delay_bar_fails_closed(self):
        inputs = anchor_fixture(count=6)
        del inputs[0]["A"][2]
        with self.assertRaises(ValueError):
            run_anchor(inputs, delay_bars=2)

    def test_disabled_default_ignores_metadata_and_preserves_every_mode(self):
        for mode in ("structure_atr", "structure_atr_breakeven_after_1r",
                     "atr_trailing_after_1r", "partial_1r_breakeven"):
            with self.subTest(mode=mode):
                inputs = anchor_fixture()
                inputs[1][0]["candidate_lifecycle"] = {"anchor": float("nan")}
                inputs[0]["A"][1].update(open=99, high=99, low=99, close=99)
                implicit = replay_portfolio(*inputs, stop_mode=mode)
                explicit = replay_portfolio(*inputs, stop_mode=mode,
                                            require_candidate_anchor_at_open=False)
                self.assertEqual(implicit, explicit)
                self.assertEqual(len(entry_fills(explicit)), 1)
                self.assertNotIn("candidate_anchor", entry_fills(explicit)[0])

    def test_candidate_flag_is_boolean_and_scope_is_disclosed_without_input_changes(self):
        for flag in (None, 1, "true"):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                run_anchor(anchor_fixture(), require_candidate_anchor_at_open=flag)
        inputs = anchor_fixture()
        before = copy.deepcopy(inputs)
        result = run_anchor(inputs)
        self.assertEqual(inputs, before)
        self.assertIs(result["parameters"]["require_candidate_anchor_at_open"], True)
        self.assertEqual(result["parameters"]["candidate_anchor_boundary"],
                         "completed_delay_bars_then_actual_open; execution_bar_range_excluded")


if __name__ == "__main__":
    unittest.main()
