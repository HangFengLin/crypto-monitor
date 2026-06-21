from __future__ import annotations

import argparse
import unittest

import pandas as pd

from project_signal_backtest import SignalTrade
from resume_strategy_validation import frame_to_trades, replace_coverage_rows, report_runtime_args


class StrategyValidationResumeTest(unittest.TestCase):
    def test_report_runtime_args_preserve_methodology_and_override_workers(self) -> None:
        manifest = {
            "arguments": {"interval": "15m", "bootstrap_iterations": 2000, "cache_dir": "old"},
            "bootstrap_seed": 7,
            "min_group_trades": 30,
        }
        cli = argparse.Namespace(cache_dir="new", data_workers=2, data_executor="thread", evaluation_workers=3)
        args = report_runtime_args(manifest, cli)
        self.assertEqual(args.interval, "15m")
        self.assertEqual(args.cache_dir, "new")
        self.assertEqual(args.data_workers, 2)
        self.assertEqual(args.seed, 7)

    def test_replace_coverage_rows_replaces_failed_symbol(self) -> None:
        existing = pd.DataFrame([
            {"exchange": "binance", "symbol": "BTCUSDT", "bars": 10},
            {"exchange": "okx", "symbol": "BTCUSDT", "bars": 0},
        ])
        result = replace_coverage_rows(existing, [{"exchange": "okx", "symbol": "BTCUSDT", "bars": 20}])
        self.assertEqual(len(result), 2)
        self.assertEqual(int(result.loc[result["exchange"] == "okx", "bars"].iloc[0]), 20)

    def test_frame_to_trades_restores_times_and_symbol(self) -> None:
        values = {
            "signal": "long",
            "stop_mode": "structure_atr",
            "entry_time": "2025-01-01T00:00:00Z",
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "target_price": 110.0,
            "exit_time": "2025-01-01T01:00:00Z",
            "exit_price": 101.0,
            "exit_reason": "timeout",
            "outcome": "win",
            "return_pct": 0.01,
            "bars_held": 4,
            "confirm_bars": 1,
            "strength": 0.5,
            "divergence_type": "macd",
            "trend": "up",
            "higher_trend": "up",
            "signal_grade": "normal",
            "signal_score": 8,
            "signal_score_max": 20,
            "structure_score": 1,
            "structure_text": "test",
            "score_text": "test",
            "filter_text": "test",
            "symbol": "BTCUSDT",
        }
        trades = frame_to_trades(pd.DataFrame([values]))
        self.assertIsInstance(trades[0], SignalTrade)
        self.assertEqual(trades[0].entry_time, pd.Timestamp("2025-01-01T00:00:00Z"))
        self.assertEqual(getattr(trades[0], "symbol"), "BTCUSDT")


if __name__ == "__main__":
    unittest.main()
