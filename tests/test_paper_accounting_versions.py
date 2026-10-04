import copy
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


if __name__ == "__main__":
    unittest.main()
