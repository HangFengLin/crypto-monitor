import unittest
from unittest.mock import patch

import pandas as pd

from project_signal_backtest import evaluate_trade, parse_args


def bars(values):
    return [{"time": pd.Timestamp("2025-01-01", tz="UTC") + pd.Timedelta(minutes=15*i),
             "open": opening, "high": high, "low": low, "close": close, "atr": 1}
            for i, (opening, high, low, close) in enumerate(values)]


class BacktestAccountingVersionsTest(unittest.TestCase):
    def test_cli_defaults_to_linear_and_accepts_explicit_legacy_replay(self):
        with patch("sys.argv", ["project_signal_backtest.py"]):
            self.assertEqual(getattr(parse_args(), "return_model", None), "linear_usdm_v1")
        with patch("sys.argv", ["project_signal_backtest.py", "--return-model", "legacy_ratio_v1"]):
            self.assertEqual(parse_args().return_model, "legacy_ratio_v1")

    def test_linear_model_reaches_lifecycle_and_realized_and_horizon_returns(self):
        signal = {"signal": "short", "stop_loss": 105}
        series = bars([(100, 101, 94, 96)])
        trade = evaluate_trade(signal, series, 0, 2, 1, .001, return_model="linear_usdm_v1")
        self.assertAlmostEqual(trade.return_pct, .04805)
        self.assertAlmostEqual(trade.horizon_close_return_pct, .03804)
        self.assertEqual(trade.return_model, "linear_usdm_v1")

    def test_legacy_model_stays_reproducible(self):
        trade = evaluate_trade({"signal": "short", "stop_loss": 105},
                               bars([(100, 101, 94, 96)]), 0, 2, 1, .001)
        self.assertAlmostEqual(trade.return_pct, 100/95 - 1 - .002)

    def test_holding_limit_counts_entry_bar_as_first_bar(self):
        series = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 110, 99, 110)])
        trade = evaluate_trade({"signal": "long", "stop_loss": 95}, series, 0, 2, 2, .001,
                               legacy_holding_limit=False)
        self.assertEqual(trade.exit_time, series[1]["time"])
        self.assertEqual(trade.exit_reason, "timeout")
        self.assertEqual(trade.bars_held, 1)

    def test_partial_exit_books_half_one_r_and_remaining_two_r(self):
        trade = evaluate_trade({"signal": "long", "stop_loss": 95},
                               bars([(100, 111, 99, 110)]), 0, 2, 1, .001,
                               "partial_1r_breakeven", return_model="linear_usdm_v1")
        self.assertAlmostEqual(trade.return_pct, .072925)
        self.assertEqual([leg["fraction"] for leg in trade.exit_legs], [.5, .5])
        self.assertEqual([leg["exit_price"] for leg in trade.exit_legs], [105, 110])


if __name__ == "__main__":
    unittest.main()
