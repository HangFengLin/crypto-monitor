import copy
import json
import unittest

import app


class PaperAccountingVersionsTest(unittest.TestCase):
    def signal(self, direction="short", stop=105):
        return {"symbol": "BTCUSDT", "interval": "15m", "signal": direction,
                "price": 100, "stop_loss": stop, "created_at": 1,
                "kline_close_time": 1000}

    def test_new_record_pins_linear_model_and_correctly_books_both_fees(self):
        trade = app.build_strategy_trade_from_signal(self.signal())
        self.assertEqual(trade.get("return_model"), "linear_usdm_v1")
        app.close_strategy_trade(trade, 90, "take_profit", "win")
        self.assertAlmostEqual(trade["return_pct"], 0.0981)

    def test_old_record_without_model_keeps_legacy_math_after_restart(self):
        trade = {"status": "open", "direction": "short", "entry_price": 100,
                 "fee_rate": .001}
        app.close_strategy_trade(trade, 90, "take_profit", "win")
        self.assertAlmostEqual(trade["return_pct"], 100 / 90 - 1 - .002)
        self.assertNotIn("return_model", trade)

    def test_fee_negative_protection_exit_is_loss_in_new_model(self):
        trade = app.build_strategy_trade_from_signal(self.signal("long", 99.9))
        app.close_strategy_trade(trade, 100.1, "protection_reached", "win")
        self.assertLess(trade["return_pct"], 0)
        self.assertEqual(trade["outcome"], "loss")

    def test_marks_and_protected_stop_use_the_recorded_accounting_model(self):
        trade = app.build_strategy_trade_from_signal(self.signal())
        app.refresh_strategy_trade_mark(trade, 90)
        self.assertAlmostEqual(trade["unrealized_pct"], .0981)
        trade["exit_mode"] = "structure_atr_breakeven_after_1r"
        self.assertFalse(app.apply_strategy_lifecycle(trade, 94, 100, 95))
        self.assertAlmostEqual(trade["active_stop"], 99.9 / 1.001)
        self.assertTrue(app.apply_strategy_lifecycle(trade, 95, 100, 99))
        self.assertEqual(trade["outcome"], "breakeven")

    def test_mixed_statistics_disclose_model_groups_without_rewriting_history(self):
        records = [{"status": "closed", "outcome": "win", "return_pct": .1},
                   {"status": "closed", "outcome": "loss", "return_pct": -.02,
                    "return_model": "linear_usdm_v1"}]
        before = copy.deepcopy(records)
        stats = app.calculate_strategy_stats(records)
        self.assertEqual(stats.get("return_models"), {"legacy_ratio_v1": 1, "linear_usdm_v1": 1})
        self.assertTrue(stats.get("mixed_return_models"))
        self.assertEqual(records, before)

    def test_mixed_statistics_expose_per_model_results_without_a_pooled_performance(self):
        records = [
            {"status": "closed", "outcome": "loss", "return_pct": .1,
             "exit_reason": "protection_reached"},
            {"status": "closed", "outcome": "win", "return_pct": -.02,
             "return_model": "linear_usdm_v1", "exit_reason": "stop_loss"},
        ]
        stats = app.calculate_strategy_stats(records)
        self.assertEqual(stats["summary_scope"], "SPLIT_BY_RETURN_MODEL")
        self.assertEqual(stats["closed_trades"], 2)
        for field in ("wins", "losses", "breakevens", "win_rate", "expectancy",
                      "total_return", "avg_win", "avg_loss", "payoff_ratio",
                      "profit_factor", "stop_loss_rate", "protection_rate"):
            with self.subTest(field=field):
                self.assertIsNone(stats[field])
        legacy = stats["return_model_stats"]["legacy_ratio_v1"]
        linear = stats["return_model_stats"]["linear_usdm_v1"]
        self.assertEqual((legacy["wins"], legacy["losses"], legacy["win_rate"]), (1, 0, 1.0))
        self.assertEqual((linear["wins"], linear["losses"], linear["win_rate"]), (0, 1, 0.0))
        self.assertEqual(legacy["protection_rate"], 1.0)
        self.assertEqual(linear["stop_loss_rate"], 1.0)
        self.assertAlmostEqual(legacy["total_return"], .1)
        self.assertAlmostEqual(linear["total_return"], -.02)

    def test_statistics_classify_net_returns_and_ignore_missing_or_nonfinite_results(self):
        invalid = [None, "", " ", True, False, float("nan"), float("inf"),
                   -float("inf"), "NaN", "inf", "invalid", [], {}, 10 ** 1000]
        records = [{"status": "closed", "outcome": "win", "return_pct": value}
                   for value in invalid]
        records += [
            {"status": "closed", "outcome": "win"},
            {"status": "closed", "outcome": "loss", "return_pct": .03},
            {"status": "closed", "outcome": "win", "return_pct": -.01},
            {"status": "closed", "outcome": "win", "return_pct": "0"},
            {"status": "closed", "outcome": "loss", "return_pct": 1e-13},
        ]
        before = copy.deepcopy(records)
        stats = app.calculate_strategy_stats(records)
        self.assertEqual(stats["summary_scope"], "SINGLE_RETURN_MODEL")
        self.assertEqual(stats["valid_return_count"], 4)
        self.assertEqual(stats["missing_return_count"], 15)
        self.assertEqual((stats["wins"], stats["losses"], stats["breakevens"]), (1, 1, 2))
        self.assertEqual(stats["win_rate"], .25)
        self.assertAlmostEqual(stats["expectancy"], .005)
        self.assertAlmostEqual(stats["avg_win"], .03)
        self.assertAlmostEqual(stats["avg_loss"], .01)
        self.assertAlmostEqual(stats["payoff_ratio"], 3)
        self.assertAlmostEqual(stats["profit_factor"], 3)
        self.assertEqual([record.get("outcome") for record in records],
                         [record.get("outcome") for record in before])
        json.dumps(stats, allow_nan=False)

    def test_only_absent_model_is_legacy_and_explicit_unknown_models_are_quarantined(self):
        records = [{"status": "closed", "return_pct": .1}]
        records += [{"status": "closed", "return_pct": 100, "return_model": model}
                    for model in (None, "", "future_model", False, [])]
        stats = app.calculate_strategy_stats(records)
        self.assertEqual(stats["summary_scope"], "SPLIT_BY_RETURN_MODEL")
        self.assertIsNone(stats["win_rate"])
        self.assertEqual(stats["return_model_stats"]["legacy_ratio_v1"]["closed_trades"], 1)
        self.assertAlmostEqual(stats["return_model_stats"]["legacy_ratio_v1"]["expectancy"], .1)
        unknown = [group for group in stats["return_model_stats"].values() if not group["recognized"]]
        self.assertEqual(sum(group["closed_trades"] for group in unknown), 5)
        for group in unknown:
            self.assertIsNone(group["expectancy"])
            self.assertIsNone(group["win_rate"])
        self.assertEqual(records[1]["return_model"], None)
        self.assertNotIn("return_model", records[0])

    def test_empty_and_open_only_models_have_no_fabricated_zero_performance(self):
        for records in ([], [{"status": "open", "return_model": "linear_usdm_v1"}]):
            with self.subTest(records=records):
                stats = app.calculate_strategy_stats(records)
                self.assertEqual(stats["summary_scope"], "NO_CLOSED_RECORDS")
                self.assertEqual(stats["closed_trades"], 0)
                self.assertIsNone(stats["win_rate"])
                self.assertIsNone(stats["expectancy"])
                self.assertIsNone(stats["total_return"])
                self.assertEqual(stats["return_models"], {})
        group = app.calculate_strategy_stats(records)["return_model_stats"]["linear_usdm_v1"]
        self.assertEqual(group["open_trades"], 1)
        self.assertEqual(group["closed_trades"], 0)
        self.assertEqual(group["valid_return_count"], 0)
        self.assertIsNone(group["win_rate"])

    def test_unknown_only_model_and_known_model_with_missing_returns_do_not_claim_performance(self):
        for records in ([{"status": "closed", "return_pct": .2, "return_model": "v9"}],
                        [{"status": "closed", "return_pct": None, "return_model": "linear_usdm_v1"}]):
            with self.subTest(records=records):
                stats = app.calculate_strategy_stats(records)
                for field in ("win_rate", "expectancy", "total_return", "payoff_ratio", "profit_factor"):
                    self.assertIsNone(stats[field])

    def test_single_model_preserves_zero_returns_and_undefined_ratios_are_null(self):
        stats = app.calculate_strategy_stats([
            {"status": "closed", "return_pct": 0, "return_model": "linear_usdm_v1"},
        ])
        self.assertEqual(stats["valid_return_count"], 1)
        self.assertEqual(stats["breakevens"], 1)
        self.assertEqual(stats["win_rate"], 0)
        self.assertEqual(stats["expectancy"], 0)
        self.assertEqual(stats["total_return"], 0)
        self.assertIsNone(stats["payoff_ratio"])
        self.assertIsNone(stats["profit_factor"])


if __name__ == "__main__":
    unittest.main()
