"""E1 fixtures: fixed initial stop, full 2R target, and existing execution rules."""

import unittest

import position_manager as pm
from research_portfolio import replay_portfolio

MODE = "structure_atr_target_only"
STEP = 900_000


def bars(count=4):
    return [dict(open_time=i * STEP, close_time=(i + 1) * STEP - 1,
                 open=100.0, high=100.0, low=100.0, close=100.0,
                 volume=10.0, atr=1.0) for i in range(count)]


def event(direction="long", symbol="A", index=1, setup="one"):
    known = index * STEP - 1
    return dict(symbol=symbol, direction=direction, stop_loss=90.0 if direction == "long" else 110.0,
                setup_id=setup, signal_time=index * STEP, known_at=known,
                candidate_lifecycle=dict(version="strict_candidate_v1",
                                         anchor=80.0 if direction == "long" else 130.0,
                                         anchor_close_time=0, first_ready_time=known,
                                         checked_through_close_time=known))


def replay(data, events, **overrides):
    funding = {symbol: dict(complete=True, start=rows[0]["open_time"],
                            end=rows[-1]["close_time"] + 1, records=[])
               for symbol, rows in data.items()}
    options = dict(stop_mode=MODE, reward_risk=2, max_hold_bars=96,
                   initial_equity=10_000, risk_fraction=.005, fee_rate=0,
                   slippage_bps=0, require_candidate_anchor_at_open=True)
    options.update(overrides)
    return replay_portfolio(data, events, funding, **options)


class TargetOnlyLifecycleTests(unittest.TestCase):
    def evaluate(self, direction, low, high, **overrides):
        options = dict(stop_mode=MODE, return_model=pm.LINEAR_USDM_V1,
                       fee_rate=.001, atr_value=1)
        options.update(overrides)
        return pm.evaluate_lifecycle_bar(
            direction, low, high, 100, 100, 90 if direction == "long" else 110,
            120 if direction == "long" else 80, 110 if direction == "long" else 90,
            **options)

    def test_one_r_neither_exits_arms_protection_nor_changes_initial_stop(self):
        for direction, low, high, stop in [("long", 99, 111, 90), ("short", 89, 101, 110)]:
            with self.subTest(direction=direction):
                result = self.evaluate(direction, low, high)
                self.assertIsNone(result.decision)
                self.assertEqual(result.active_stop, stop)
                self.assertFalse(result.protection_activated)
                self.assertEqual(result.protected_stop_price, 0)
                self.assertEqual(result.remaining_fraction, 1)
                self.assertEqual(result.partial_exits, ())
                self.assertFalse(result.partial_taken)

    def test_retrace_through_fee_breakeven_keeps_full_position_alive(self):
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                first = self.evaluate(direction, 99 if direction == "long" else 89,
                                      111 if direction == "long" else 101)
                second = self.evaluate(direction, 99, 101, active_stop=first.active_stop,
                                       protection_activated=first.protection_activated,
                                       protected_stop_price=first.protected_stop_price,
                                       highest_price=first.highest_price, lowest_price=first.lowest_price)
                self.assertIsNone(second.decision)
                self.assertFalse(second.protection_activated)
                self.assertEqual(second.active_stop, 90 if direction == "long" else 110)

    def test_initial_stop_wins_same_bar_target_collision_for_both_directions(self):
        for direction, stop in [("long", 90), ("short", 110)]:
            with self.subTest(direction=direction):
                result = self.evaluate(direction, 79, 121)
                self.assertEqual((result.decision.reason, result.decision.exit_price,
                                  result.decision.outcome), ("stop_loss", stop, "loss"))
                self.assertEqual(result.partial_exits, ())

    def test_two_r_exits_full_position_with_linear_fee_classification(self):
        for direction, low, high, target, net in [
                ("long", 99, 121, 120, .1978), ("short", 79, 101, 80, .1982)]:
            with self.subTest(direction=direction):
                result = self.evaluate(direction, low, high)
                self.assertEqual((result.decision.reason, result.decision.exit_price,
                                  result.decision.outcome), ("take_profit", target, "win"))
                self.assertAlmostEqual(pm.calculate_return_pct(direction, 100, target, .001,
                                                               return_model=pm.LINEAR_USDM_V1), net)
                self.assertEqual(result.partial_exits, ())

    def test_tiny_two_r_target_is_a_loss_after_fees(self):
        for direction, stop, target, low, high in [
                ("long", 99.95, 100.10, 100, 100.11),
                ("short", 100.05, 99.90, 99.89, 100)]:
            with self.subTest(direction=direction):
                result = pm.evaluate_lifecycle_bar(
                    direction, low, high, 100, 100, stop, target, (100 + target) / 2,
                    stop_mode=MODE, return_model=pm.LINEAR_USDM_V1, fee_rate=.001)
                self.assertEqual((result.decision.reason, result.decision.outcome),
                                 ("take_profit", "loss"))

    def test_target_only_rejects_state_from_protection_or_partial_modes(self):
        for state in [dict(active_stop=95), dict(protection_activated=True),
                      dict(protected_stop_price=100), dict(remaining_fraction=.5),
                      dict(remaining_fraction=True), dict(partial_taken=True)]:
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.evaluate("long", 99, 111, **state)

    def test_default_legacy_structure_mode_still_exits_at_one_r(self):
        for direction, low, high, stop, target, one_r in [
                ("long", 99, 111, 90, 120, 110), ("short", 89, 101, 110, 80, 90)]:
            with self.subTest(direction=direction):
                implicit = pm.evaluate_lifecycle_bar(direction, low, high, 100, 100, stop, target, one_r)
                explicit = pm.evaluate_lifecycle_bar(
                    direction, low, high, 100, 100, stop, target, one_r,
                    stop_mode=pm.STRUCTURE_ATR_STOP_MODE, return_model=pm.LEGACY_RATIO_V1)
                self.assertEqual(implicit, explicit)
                self.assertEqual((implicit.decision.reason, implicit.decision.exit_price),
                                 ("protection_reached", one_r))


class TargetOnlyExecutionTests(unittest.TestCase):
    def test_c1_validated_one_r_retains_quantity_and_entire_stop_risk(self):
        for direction, extreme in [("long", dict(high=111)), ("short", dict(low=89))]:
            with self.subTest(direction=direction):
                data = {"A": bars()}
                data["A"][1].update(extreme)
                result = replay(data, [event(direction)])
                self.assertEqual(result["trades"], [])
                self.assertEqual(len(result["fills"]), 1)
                position = result["open_positions"][0]
                self.assertEqual(position["quantity"], 5)
                self.assertEqual(position["remaining_fraction"], 1)
                self.assertEqual(position["active_stop"], 90 if direction == "long" else 110)
                self.assertFalse(position["protection_activated"])
                self.assertEqual(result["equity"][1]["committed_risk"], 50)
                self.assertIsNone(result["parameters"]["min_net_reward_risk"])

    def test_target_fills_fees_and_cash_match_hand_priced_long_and_short(self):
        for direction, extremes, target, per_unit_risk, net_per_unit in [
                ("long", dict(high=121), 120, 10.19, 19.78),
                ("short", dict(low=79), 80, 10.21, 19.82)]:
            with self.subTest(direction=direction):
                data = {"A": bars()}
                data["A"][1].update(extremes)
                result = replay(data, [event(direction)], fee_rate=.001)
                trade = result["trades"][0]
                quantity = 50 / per_unit_risk
                self.assertAlmostEqual(trade["quantity"], quantity)
                self.assertEqual((trade["exit_reason"], trade["exit_price"]), ("take_profit", target))
                self.assertAlmostEqual(trade["net_pnl"], quantity * net_per_unit)
                self.assertAlmostEqual(result["fills"][0]["fee"], quantity * .1)
                self.assertAlmostEqual(result["fills"][1]["fee"], quantity * target * .001)
                self.assertAlmostEqual(result["fills"][1]["quantity"], quantity)
                self.assertAlmostEqual(result["equity"][-1]["cash"], 10_000 + quantity * net_per_unit)
                self.assertEqual(result["open_positions"], [])
                self.assertEqual(trade["partial_exits"], [])

    def test_adverse_stop_gap_uses_actual_open_and_two_sided_slippage(self):
        for direction, gap, entry, exit_price in [("long", 80, 100.1, 79.92),
                                                 ("short", 120, 99.9, 120.12)]:
            with self.subTest(direction=direction):
                data = {"A": bars()}
                data["A"][2].update(open=gap, low=gap, high=gap, close=gap)
                result = replay(data, [event(direction)], slippage_bps=10)
                trade = result["trades"][0]
                self.assertEqual((trade["exit_reason"], trade["exit_phase"], trade["exit_time"]),
                                 ("gap_stop", "open", 2 * STEP))
                self.assertAlmostEqual(trade["entry_price"], entry)
                self.assertAlmostEqual(trade["exit_price"], exit_price)
                self.assertLess(trade["net_pnl"], -50)

    def test_favorable_gap_uses_fixed_target_then_exit_slippage(self):
        # Entry slip gives fixed targets 120.3 / 79.7; exit slip gives these fills.
        for direction, gap, exit_price in [("long", 130, 120.1797),
                                           ("short", 70, 79.7797)]:
            with self.subTest(direction=direction):
                data = {"A": bars()}
                data["A"][2].update(open=gap, low=gap, high=gap, close=gap)
                result = replay(data, [event(direction)], slippage_bps=10)
                trade = result["trades"][0]
                self.assertEqual((trade["exit_reason"], trade["exit_phase"]), ("take_profit", "open"))
                self.assertAlmostEqual(trade["exit_price"], exit_price)

    def test_timeout_closes_full_quantity_on_entry_inclusive_bar_96(self):
        for direction, extreme in [("long", dict(high=111)), ("short", dict(low=89))]:
            with self.subTest(direction=direction):
                data = {"A": bars(98)}
                data["A"][1].update(extreme)
                data["A"][97].update(open=200, low=200, high=200, close=200)
                trade = replay(data, [event(direction)])["trades"][0]
                self.assertEqual((trade["exit_reason"], trade["exit_time"], trade["holding_bars"],
                                  trade["exit_price"]), ("timeout", 97 * STEP, 96, 100))
                self.assertEqual(trade["partial_exits"], [])

    def test_future_range_target_exit_cannot_release_open_capacity(self):
        data = {"A": bars(), "B": bars()}
        data["A"][2].update(high=121)
        result = replay(data, [event(symbol="A"), event(symbol="B", index=2, setup="two")],
                        max_positions=1)
        self.assertEqual(result["rejections"][0]["reason"], "max_positions")
        self.assertEqual(result["trades"][0]["exit_phase"], "bar_end")
        self.assertEqual(len(result["fills"]), 2)

    def test_funding_after_one_r_is_paid_or_received_by_full_quantity(self):
        for direction, first, target, net in [
                ("long", dict(high=111), dict(high=121), 95),
                ("short", dict(low=89), dict(low=79), 105)]:
            with self.subTest(direction=direction):
                data = {"A": bars(5)}
                data["A"][1].update(first)
                data["A"][3].update(target)
                funding = {"A": dict(complete=True, start=0, end=5 * STEP,
                                     records=[dict(time=2 * STEP, rate=.01, mark_price=100)])}
                result = replay_portfolio(
                    data, [event(direction)], funding, stop_mode=MODE,
                    reward_risk=2, max_hold_bars=96, fee_rate=0, slippage_bps=0,
                    require_candidate_anchor_at_open=True)
                trade = result["trades"][0]
                self.assertEqual(trade["funding_cashflow"], -5 if direction == "long" else 5)
                self.assertEqual(trade["net_pnl"], net)
                self.assertEqual(result["equity"][-1]["equity"], 10_000 + net)

    def test_future_bars_do_not_rewrite_one_r_open_position_equity(self):
        data = {"A": bars(6)}
        data["A"][1].update(high=111)
        data["A"][4].update(high=121)
        prefix = replay({"A": data["A"][:4]}, [event()])
        full = replay(data, [event()])
        self.assertEqual(prefix["trades"], [])
        self.assertEqual(full["equity"][:4], prefix["equity"])
        self.assertEqual(full["fills"][:1], prefix["fills"])


if __name__ == "__main__":
    unittest.main()
