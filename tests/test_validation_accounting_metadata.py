"""Formal validation must pin accounting through workers, archives and resume."""

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import project_signal_backtest as backtest
import resume_strategy_validation as resume
import strategy_validation as validation
from strategy import DEFAULT_CONFIG


class OneShortSignal:
    def __init__(self, config):
        pass

    def detect(self, bars, clean=False):
        return bars[-1].get("fixture_signal", {"signal": "wait"}) if bars else {"signal": "wait"}


def fixture_bars():
    start = pd.Timestamp("2025-01-01", tz="UTC")
    rows = []
    for index, prices in enumerate(((100, 101, 99, 100), (100, 101, 98, 99), (100, 101, 94, 96))):
        timestamp = start + pd.Timedelta(minutes=15 * index)
        rows.append(dict(time=timestamp, open_time=int(timestamp.timestamp() * 1000),
                         close_time=int(timestamp.timestamp() * 1000) + 899999,
                         open=prices[0], high=prices[1], low=prices[2], close=prices[3],
                         volume=10, macd=1, atr=1))
    rows[0]["fixture_signal"] = dict(signal="short", divergence_time=1, stop_loss=105)
    return rows


class ValidationAccountingMetadataTest(unittest.TestCase):
    def parse(self, *flags):
        with patch("sys.argv", ["strategy_validation.py", "--max-hold-bars", "1", *flags]), \
             contextlib.redirect_stderr(io.StringIO()):
            try:
                return validation.parse_args()
            except SystemExit:
                self.fail("Formal validation CLI cannot pin the requested accounting semantics")

    def replay(self, args):
        rows = fixture_bars()
        with patch.object(backtest, "ProjectSignalEngine", OneShortSignal):
            trades, _, _ = validation.run_window(
                {"BTCUSDT": rows}, DEFAULT_CONFIG, rows[0]["time"],
                rows[0]["time"] + pd.Timedelta(days=1), args,
            )
        return trades[0]

    def test_new_default_replay_uses_linear_fees_and_entry_bar_as_first(self):
        trade = self.replay(self.parse("--evaluation-workers", "1"))
        # One coin short: 1 USDT gross less 0.100 entry and 0.099 exit fees.
        self.assertAlmostEqual(trade.return_pct, .00801)
        self.assertEqual(trade.exit_reason, "timeout")
        self.assertEqual(trade.exit_time, fixture_bars()[1]["time"])
        self.assertEqual(trade.return_model, "linear_usdm_v1")

    def test_explicit_old_model_and_holding_window_reproduce_legacy_replay(self):
        args = self.parse("--return-model", "legacy_ratio_v1", "--holding-limit-convention",
                          "legacy_inclusive_end", "--evaluation-workers", "1")
        trade = self.replay(args)
        self.assertAlmostEqual(trade.return_pct, 100 / 95 - 1 - .002)
        self.assertEqual(trade.exit_time, fixture_bars()[2]["time"])
        self.assertEqual(trade.return_model, "legacy_ratio_v1")

    def test_real_smoke_manifest_pins_execution_version_and_boundaries(self):
        rows = fixture_bars()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def frozen_snapshot(args, output, minimum):
                snapshot = {"binance": [{"symbol": "BTCUSDT"}], "okx": []}
                validation.write_json(output / "universe_snapshot.json", snapshot)
                return snapshot

            with patch("sys.argv", ["strategy_validation.py", "--smoke", "--end", "2025-01-08",
                                    "--skip-okx", "--max-hold-bars", "1", "--reports-dir", directory,
                                    "--cache-dir", str(root / "cache"), "--evaluation-workers", "1"]), \
                 patch.object(validation, "load_or_build_snapshot", side_effect=frozen_snapshot), \
                 patch.object(validation, "prepare_snapshot_datasets", return_value=(
                     {"BTCUSDT": rows}, [{"exchange": "binance", "symbol": "BTCUSDT", "bars": 3}])), \
                 patch.object(validation, "load_candidates", return_value=[
                     {"name": "baseline", "family": "baseline", "overrides": {}}]), \
                 patch.object(backtest, "ProjectSignalEngine", OneShortSignal), \
                 contextlib.redirect_stdout(io.StringIO()):
                validation.main()
            manifest = json.loads(next(root.glob("strategy_validation_*/manifest.json")).read_text())
            metadata = manifest.get("execution_metadata", {})
            self.assertEqual(metadata.get("return_model"), "linear_usdm_v1")
            self.assertEqual(metadata.get("holding_limit_convention"), "entry_bar_is_first")
            self.assertEqual(metadata.get("max_observed_bars"), 1)
            self.assertFalse(metadata.get("funding_included", True))
            self.assertFalse(metadata.get("independent_slippage_included", True))
            self.assertEqual(metadata.get("execution_scope"), "multi_symbol_signal_validation")
            self.assertEqual(manifest["arguments"].get("return_model"), "linear_usdm_v1")
            self.assertEqual(manifest["arguments"].get("holding_limit_convention"), "entry_bar_is_first")

    def resume_args(self, manifest):
        return resume.report_runtime_args(manifest, argparse.Namespace(
            cache_dir=None, data_workers=1, data_executor="thread", evaluation_workers=1))

    def test_resume_manifest_without_version_retains_both_old_semantics(self):
        original = self.parse("--evaluation-workers", "1")
        values = vars(original).copy()
        values.pop("return_model", None)
        values.pop("holding_limit_convention", None)
        args = self.resume_args({"arguments": values})
        self.assertEqual(getattr(args, "return_model", None), "legacy_ratio_v1")
        self.assertEqual(getattr(args, "holding_limit_convention", None), "legacy_inclusive_end")
        self.assertAlmostEqual(self.replay(args).return_pct, 100 / 95 - 1 - .002)

    def test_resume_new_manifest_preserves_new_semantics(self):
        args = self.resume_args({"arguments": vars(self.parse("--evaluation-workers", "1"))})
        self.assertAlmostEqual(self.replay(args).return_pct, .00801)

    def test_resume_rejects_conflicting_or_partial_accounting_metadata(self):
        for manifest in (
            {"arguments": {"return_model": "linear_usdm_v1"}},
            {"arguments": {"holding_limit_convention": "entry_bar_is_first"}},
            {"arguments": {"return_model": "linear_usdm_v1", "holding_limit_convention": "entry_bar_is_first"},
             "execution_metadata": {"return_model": "legacy_ratio_v1", "holding_limit_convention": "legacy_inclusive_end"}},
            {"arguments": {"return_model": "unknown", "holding_limit_convention": "entry_bar_is_first"}},
        ):
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                self.resume_args(manifest)

    def test_double_cost_uses_each_linear_exit_notional_preserving_legacy(self):
        trade = backtest.evaluate_trade({"signal": "short", "stop_loss": 105},
                                       fixture_bars()[1:], 0, 2, 1, .001,
                                       return_model="linear_usdm_v1", legacy_holding_limit=False)
        adjusted = validation.apply_extra_roundtrip_cost([trade], .001)[0]
        self.assertAlmostEqual(adjusted.return_pct, .00602)
        self.assertEqual(trade.return_pct, .00801)
        legacy = backtest.evaluate_trade({"signal": "short", "stop_loss": 105},
                                        fixture_bars()[1:], 0, 2, 1, .001)
        self.assertAlmostEqual(validation.apply_extra_roundtrip_cost([legacy], .001)[0].return_pct,
                               legacy.return_pct - .002)

    def test_resume_old_csv_blank_version_stays_legacy_after_column_merge(self):
        legacy = backtest.evaluate_trade({"signal": "short", "stop_loss": 105},
                                        fixture_bars()[1:], 0, 2, 1, .001)
        frame = pd.DataFrame(validation.trade_records([legacy]))
        frame["return_model"] = float("nan")
        self.assertEqual(resume.frame_to_trades(frame)[0].return_model, "legacy_ratio_v1")

    def test_resume_refuses_records_from_another_model_than_frozen_manifest(self):
        legacy = backtest.evaluate_trade({"signal": "short", "stop_loss": 105},
                                        fixture_bars()[1:], 0, 2, 1, .001)
        legacy.symbol = "BTCUSDT"
        start = fixture_bars()[0]["time"]
        records = pd.DataFrame(validation.trade_records([legacy], candidate="baseline", fold="one"))
        window = dict(fold="one", train_start=start, train_end=start,
                      validation_start=start, validation_end=start + pd.Timedelta(days=1))
        with self.assertRaises(ValueError):
            resume.rebuild_validation_statistics(
                records, [{"name": "baseline", "family": "baseline", "overrides": {}}],
                [window], self.parse("--evaluation-workers", "1"),
            )

    def test_resume_full_exit_linear_csv_roundtrip_preserves_cost_pressure(self):
        original = backtest.evaluate_trade({"signal": "short", "stop_loss": 105},
                                          fixture_bars()[1:], 0, 2, 1, .001,
                                          return_model="linear_usdm_v1", legacy_holding_limit=False)
        serialized = pd.DataFrame(validation.trade_records([original])).to_csv(index=False)
        restored = resume.frame_to_trades(pd.read_csv(io.StringIO(serialized)))[0]
        try:
            stressed = validation.apply_extra_roundtrip_cost([restored], .001)[0]
        except TypeError:
            self.fail("Full-exit formal validation incorrectly depends on parsing serialized partial-exit legs")
        self.assertAlmostEqual(stressed.return_pct, .00602)

    def test_formal_cost_pressure_rejects_unsupported_partial_exit_records(self):
        partial = backtest.evaluate_trade({"signal": "long", "stop_loss": 95},
                                         [dict(time=pd.Timestamp("2025-01-01", tz="UTC"),
                                               open=100, high=111, low=99, close=110, atr=1)],
                                         0, 2, 1, .001, "partial_1r_breakeven",
                                         return_model="linear_usdm_v1", legacy_holding_limit=False)
        with self.assertRaises(ValueError):
            validation.apply_extra_roundtrip_cost([partial], .001)


if __name__ == "__main__":
    unittest.main()
