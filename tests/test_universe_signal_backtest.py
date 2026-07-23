from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import universe_signal_backtest as universe


def completed_result(symbol: str, return_pct: float = 0.01) -> dict:
    return {
        "status": "completed",
        "symbol": symbol,
        "coverage": {
            "bars": 500,
            "first_time": "2024-01-01T00:00:00+00:00",
            "last_time": "2024-01-06T04:45:00+00:00",
        },
        "higher_coverage": {"bars": 32},
        "fit_metrics": {"total_trades": 1},
        "holdout_metrics": {},
        "trades": [
            {
                "signal": "long",
                "stop_mode": "structure_atr",
                "entry_time": "2024-01-02T00:00:00+00:00",
                "entry_price": 100.0,
                "stop_loss": 99.0,
                "target_price": 102.0,
                "exit_time": "2024-01-02T01:00:00+00:00",
                "exit_price": 101.0,
                "exit_reason": "target",
                "outcome": "win" if return_pct > 0 else "loss",
                "return_pct": return_pct,
                "bars_held": 4,
                "confirm_bars": 1,
                "strength": 0.8,
                "divergence_type": "bottom",
                "trend": "up",
                "higher_trend": "up",
                "signal_grade": "A",
                "signal_score": 10,
                "signal_score_max": 12,
                "structure_score": 2,
                "structure_text": "ok",
                "score_text": "ok",
                "filter_text": "ok",
                "symbol": symbol,
                "sample": "fit",
            }
        ],
    }


class UniverseSignalBacktestTest(unittest.TestCase):
    def test_child_signal_filters_are_research_parameters(self) -> None:
        args = universe.parse_args(
            [
                "--signal-direction",
                "long",
                "--structure-text-exact",
                "底分型确认",
            ]
        )
        payload = universe.argument_payload(args)
        self.assertEqual(payload["signal_direction"], "long")
        self.assertEqual(payload["structure_text_exact"], "底分型确认")
        self.assertIsNone(payload["structure_text_contains"])

    def test_breakeven_stop_mode_is_a_research_parameter(self) -> None:
        args = universe.parse_args(["--stop-mode", "structure_atr_breakeven_after_1r"])
        payload = universe.argument_payload(args)
        self.assertEqual(payload["stop_mode"], "structure_atr_breakeven_after_1r")

    def test_okx_minimum_quote_volume_is_forwarded(self) -> None:
        args = universe.parse_args(
            ["--top-n", "123", "--min-quote-volume", "7654321", "--max-symbols", "1"]
        )
        with patch(
            "universe_signal_backtest.build_okx_market_cap_universe",
            return_value=[{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}],
        ) as build:
            result = universe.build_universe(args)
        build.assert_called_once_with(123, "USDT", "SWAP", 7_654_321.0)
        self.assertEqual(result, [{"symbol": "BTCUSDT"}])

    def test_default_window_ends_at_last_complete_bar(self) -> None:
        args = universe.parse_args(["--limit", "4", "--interval", "15m"])
        universe.validate_args(args)
        start, end = universe.resolve_window(args, pd.Timestamp("2024-01-01T01:07:00Z"))
        self.assertEqual(end, pd.Timestamp("2024-01-01T01:00:00Z"))
        self.assertEqual(start, pd.Timestamp("2024-01-01T00:00:00Z"))

    def test_time_split_uses_one_sided_embargo_and_holding_horizon(self) -> None:
        windows = universe.split_windows(
            pd.Timestamp("2024-01-01T00:00:00Z"),
            pd.Timestamp("2024-01-11T00:00:00Z"),
            0.30,
            96,
            "15m",
            96,
        )
        self.assertEqual(windows["split"], pd.Timestamp("2024-01-08T00:00:00Z"))
        self.assertEqual(windows["fit_entry_end"], pd.Timestamp("2024-01-07T00:00:00Z"))
        self.assertEqual(windows["holdout_start"], pd.Timestamp("2024-01-09T00:00:00Z"))
        self.assertEqual(windows["holdout_entry_end"], pd.Timestamp("2024-01-10T00:00:00Z"))

    def test_same_timestamp_cannot_cross_fit_and_holdout(self) -> None:
        windows = universe.split_windows(
            pd.Timestamp("2024-01-01T00:00:00Z"),
            pd.Timestamp("2024-01-05T00:00:00Z"),
            0.5,
            4,
            "1h",
            4,
        )
        split = windows["split"]
        self.assertLess(windows["fit_entry_end"], split)
        self.assertGreater(windows["holdout_start"], split)

    def test_base_and_higher_timeframes_use_frozen_warmup_ranges(self) -> None:
        args = universe.parse_args(
            ["--limit", "500", "--max-hold-bars", "24", "--embargo-bars", "24"]
        )
        universe.validate_args(args)
        start = pd.Timestamp("2024-01-10T00:00:00Z")
        end = pd.Timestamp("2024-01-15T05:00:00Z")
        windows = universe.split_windows(start, end, 0.3, 24, "15m", 24)
        calls = []

        def fake_fetch(_args, _symbol, interval, fetch_start, fetch_end):
            calls.append((interval, fetch_start, fetch_end))
            step = pd.Timedelta(interval)
            count = 750 if interval == "15m" else 282
            return [
                {
                    "time": fetch_start + step * index,
                    "open_time": int((fetch_start + step * index).timestamp() * 1000),
                    "close_time": int((fetch_start + step * (index + 1)).timestamp() * 1000) - 1,
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "close": 1.0,
                    "volume": 1.0,
                }
                for index in range(count)
            ]

        with patch("universe_signal_backtest.fetch_range", side_effect=fake_fetch), patch(
            "universe_signal_backtest.calculate_indicators", side_effect=lambda bars: bars
        ), patch(
            "universe_signal_backtest.add_higher_timeframe_context", side_effect=lambda bars, _higher: bars
        ), patch(
            "universe_signal_backtest.run_backtest", return_value=([], pd.Series(dtype=float))
        ):
            result = universe.run_one_symbol(args, "BTCUSDT", start, end, windows)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls[0], ("15m", start - pd.Timedelta(minutes=15 * 250), end))
        self.assertEqual(calls[1], ("4h", start - pd.Timedelta(hours=4 * 250), end))

    def test_symbol_run_forwards_child_signal_filters(self) -> None:
        args = universe.parse_args(
            [
                "--limit",
                "500",
                "--signal-direction",
                "long",
                "--structure-text-exact",
                "底分型确认",
            ]
        )
        universe.validate_args(args)
        start = pd.Timestamp("2024-01-10T00:00:00Z")
        end = pd.Timestamp("2024-01-15T05:00:00Z")
        windows = universe.split_windows(start, end, 0, 96, "15m", 96)

        def fake_fetch(_args, _symbol, interval, fetch_start, _fetch_end):
            step = pd.Timedelta(interval)
            count = 750 if interval == "15m" else 282
            return [
                {
                    "time": fetch_start + step * index,
                    "open_time": int((fetch_start + step * index).timestamp() * 1000),
                    "close_time": int((fetch_start + step * (index + 1)).timestamp() * 1000) - 1,
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "close": 1.0,
                    "volume": 1.0,
                }
                for index in range(count)
            ]

        with patch("universe_signal_backtest.fetch_range", side_effect=fake_fetch), patch(
            "universe_signal_backtest.calculate_indicators", side_effect=lambda bars: bars
        ), patch(
            "universe_signal_backtest.add_higher_timeframe_context", side_effect=lambda bars, _higher: bars
        ), patch(
            "universe_signal_backtest.run_backtest", return_value=([], pd.Series(dtype=float))
        ) as run:
            result = universe.run_one_symbol(args, "BTCUSDT", start, end, windows)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(run.call_args.kwargs["signal_direction"], "long")
        self.assertEqual(run.call_args.kwargs["structure_text_exact"], "底分型确认")

    def test_reconcile_uses_symbol_files_as_source_of_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            (run_dir / "symbols").mkdir()
            universe.atomic_json(run_dir / "symbols" / "BTCUSDT.json", completed_result("BTCUSDT"))
            universe.atomic_json(
                run_dir / "symbols" / "ETHUSDT.json",
                {"status": "failed", "symbol": "ETHUSDT", "attempts": 3, "error": "timeout", "trades": []},
            )
            checkpoint = {"completed_symbols": ["STALE"], "failures": {}, "skipped_symbols": []}
            reconciled = universe.reconcile_checkpoint(run_dir, checkpoint)
            self.assertEqual(reconciled["completed_symbols"], ["BTCUSDT"])
            self.assertEqual(reconciled["total_trades"], 1)
            self.assertEqual(reconciled["failures"]["ETHUSDT"]["attempts"], 3)

    def test_incremental_checkpoint_update_replaces_one_symbol_without_rescan(self) -> None:
        checkpoint = {
            "completed_symbols": [],
            "skipped_symbols": [],
            "failures": {},
            "total_trades": 0,
            "symbol_trade_counts": {},
        }
        universe.update_checkpoint_with_result(checkpoint, completed_result("BTCUSDT"))
        self.assertEqual(checkpoint["completed_symbols"], ["BTCUSDT"])
        self.assertEqual(checkpoint["total_trades"], 1)

        universe.update_checkpoint_with_result(
            checkpoint,
            {"status": "skipped", "symbol": "BTCUSDT", "reason": "no data", "trades": []},
        )
        self.assertEqual(checkpoint["completed_symbols"], [])
        self.assertEqual(checkpoint["skipped_symbols"], ["BTCUSDT"])
        self.assertEqual(checkpoint["total_trades"], 0)

    def test_final_status_is_failed_when_no_symbol_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            (run_dir / "symbols").mkdir()
            universe.atomic_json(
                run_dir / "symbols" / "BTCUSDT.json",
                {"status": "failed", "symbol": "BTCUSDT", "attempts": 3, "error": "boom", "trades": []},
            )
            args = universe.parse_args(
                ["--start", "2024-01-01T00:00:00Z", "--end", "2024-02-01T00:00:00Z"]
            )
            checkpoint = universe.reconcile_checkpoint(
                run_dir,
                {
                    "run_id": "failed-run",
                    "total_symbols": 1,
                    "completed_symbols": [],
                    "skipped_symbols": [],
                    "failures": {},
                },
            )
            summary, report, candidates = universe.write_final_artifacts(
                run_dir, args, checkpoint, 0.0
            )
            self.assertEqual(summary["status"], "failed")
            self.assertTrue(report.exists())
            self.assertEqual(candidates, 0)

    def test_final_status_is_no_data_when_all_symbols_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            (run_dir / "symbols").mkdir()
            universe.atomic_json(
                run_dir / "symbols" / "BTCUSDT.json",
                {"status": "skipped", "symbol": "BTCUSDT", "reason": "insufficient bars", "trades": []},
            )
            args = universe.parse_args(
                ["--start", "2024-01-01T00:00:00Z", "--end", "2024-02-01T00:00:00Z"]
            )
            checkpoint = universe.reconcile_checkpoint(
                run_dir,
                {
                    "run_id": "no-data-run",
                    "total_symbols": 1,
                    "completed_symbols": [],
                    "skipped_symbols": [],
                    "failures": {},
                },
            )
            summary, report, candidates = universe.write_final_artifacts(
                run_dir, args, checkpoint, 0.0
            )
            self.assertEqual(summary["status"], "no_data")
            self.assertTrue(report.exists())
            self.assertEqual(candidates, 0)

    def test_interrupt_resume_skips_processed_symbols_and_aggregates_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {}, clear=True):
            reports = Path(tmpdir) / "reports"
            cache = Path(tmpdir) / "cache"
            args = universe.parse_args(
                [
                    "--run-id",
                    "resume-test",
                    "--reports-dir",
                    str(reports),
                    "--cache-dir",
                    str(cache),
                    "--start",
                    "2024-01-01T00:00:00Z",
                    "--end",
                    "2024-02-01T00:00:00Z",
                    "--holdout-fraction",
                    "0",
                    "--min-group-trades",
                    "1",
                    "--max-symbol-attempts",
                    "2",
                    "--sleep-between-symbols",
                    "0",
                ]
            )
            market = [{"symbol": value} for value in ("AAAUSDT", "BBBUSDT", "CCCUSDT")]
            calls: list[str] = []

            def first_run(_args, symbol, _start, _end, _windows):
                calls.append(symbol)
                if symbol == "AAAUSDT":
                    return completed_result(symbol)
                if symbol == "BBBUSDT":
                    raise RuntimeError("temporary failure")
                raise KeyboardInterrupt

            with patch("universe_signal_backtest.build_universe", return_value=market), patch(
                "universe_signal_backtest.run_one_symbol", side_effect=first_run
            ):
                self.assertEqual(universe.execute(args), 130)
            self.assertEqual(calls.count("AAAUSDT"), 1)
            self.assertEqual(calls.count("BBBUSDT"), 2)
            self.assertEqual(calls.count("CCCUSDT"), 1)

            run_dir = reports / "universe_backtest_resume-test"
            checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["status"], "interrupted")
            self.assertEqual(checkpoint["completed_symbols"], ["AAAUSDT"])
            self.assertIn("BBBUSDT", checkpoint["failures"])

            resume_args = universe.parse_args(["--resume", str(run_dir)])
            resumed_calls: list[str] = []

            def second_run(_args, symbol, _start, _end, _windows):
                resumed_calls.append(symbol)
                return completed_result(symbol, 0.02)

            with patch("universe_signal_backtest.run_one_symbol", side_effect=second_run):
                self.assertEqual(universe.execute(resume_args), 0)
            self.assertEqual(resumed_calls, ["CCCUSDT"])
            trades = pd.read_csv(run_dir / "trades.csv")
            self.assertEqual(len(trades), 2)
            self.assertEqual(set(trades["symbol"]), {"AAAUSDT", "CCCUSDT"})
            final_checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(final_checkpoint["status"], "completed")
            self.assertTrue((run_dir / "report.html").exists())
            self.assertTrue((run_dir / "artifact_hashes.json").exists())

    def test_existing_run_id_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            reports = Path(tmpdir)
            run_dir = reports / "universe_backtest_fixed"
            run_dir.mkdir()
            (run_dir / "sentinel").write_text("keep", encoding="utf-8")
            args = universe.parse_args(
                [
                    "--run-id",
                    "fixed",
                    "--reports-dir",
                    str(reports),
                    "--start",
                    "2024-01-01T00:00:00Z",
                    "--end",
                    "2024-02-01T00:00:00Z",
                ]
            )
            with self.assertRaises(FileExistsError):
                universe.execute(args)
            self.assertEqual((run_dir / "sentinel").read_text(encoding="utf-8"), "keep")

    def test_resume_rejects_research_parameter_overrides(self) -> None:
        args = universe.parse_args(["--resume", "/tmp/example-run", "--top-n", "50"])
        with self.assertRaisesRegex(ValueError, "--top-n"):
            universe.load_resume_args(args)


if __name__ == "__main__":
    unittest.main()
