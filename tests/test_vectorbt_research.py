from __future__ import annotations

import gzip
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from project_signal_backtest import SignalTrade, run_backtest
from vectorbt_research import (
    ResearchCandidate,
    SignalEvent,
    build_candidate_grid,
    cross_market_candidate_gate,
    extract_signal_events,
    load_prepared_bars,
    pooled_candidate_robustness,
    research_boundaries,
    run_vectorbt_poc,
    select_candidate_trades,
    trade_sequence_parity,
    vectorbt_candidate_metrics,
    write_research_artifacts,
)


def trade(index: int, return_pct: float, bars_held: int = 1) -> SignalTrade:
    entry_time = pd.Timestamp("2025-01-01T00:00:00Z") + pd.Timedelta(minutes=15 * index)
    return SignalTrade(
        signal="long",
        stop_mode="structure_atr",
        entry_time=entry_time,
        entry_price=100.0,
        stop_loss=95.0,
        target_price=110.0,
        exit_time=entry_time + pd.Timedelta(minutes=15 * bars_held),
        exit_price=100.0 * (1.0 + return_pct),
        exit_reason="protection_reached" if return_pct > 0 else "stop_loss",
        outcome="win" if return_pct > 0 else "loss",
        return_pct=return_pct,
        bars_held=bars_held,
        confirm_bars=1,
        strength=0.5,
        divergence_type="macd",
        trend="up",
        higher_trend="up",
        signal_grade="normal",
        signal_score=8,
        signal_score_max=20,
        structure_score=2,
        structure_text="fixture",
        score_text="fixture",
        filter_text="fixture",
    )


def event(signal_index: int, return_pct: float, bars_held: int = 1, **signal_overrides: object) -> SignalEvent:
    signal = {
        "signal": "long",
        "signal_score": 8,
        "structure_score": 2,
        "divergence_type": "macd",
        "structure_text": "fixture",
    }
    signal.update(signal_overrides)
    return SignalEvent(
        signal_index=signal_index,
        entry_index=signal_index + 1,
        exit_index=signal_index + 1 + bars_held,
        signal=signal,
        entry_bar={"trend": "up"},
        trade=trade(signal_index + 1, return_pct, bars_held),
    )


class CandidateGridTest(unittest.TestCase):
    def test_grid_is_deterministic_and_deduplicates_values(self) -> None:
        candidates = build_candidate_grid(
            min_signal_scores=[0, 0, 8],
            min_structure_scores=[0],
            divergence_filters=["all", "macd"],
            signal_directions=["all"],
            block_local_countertrends=[False, True],
        )

        self.assertEqual(len(candidates), 8)
        self.assertEqual(candidates[0].name, "score0_structure0_divall_directionall_countertrend0")
        self.assertEqual(candidates[-1].name, "score8_structure0_divmacd_directionall_countertrend1")


class ExactSelectionTest(unittest.TestCase):
    def test_filtering_and_non_overlap_match_project_rules(self) -> None:
        events = [
            event(0, 0.10, bars_held=2),
            event(2, 0.30, bars_held=1, signal_score=10),
            event(4, -0.05, bars_held=1, signal_score=10),
        ]
        candidate = ResearchCandidate(
            name="strict",
            min_signal_score=9,
            min_structure_score=0,
            divergence_filter="all",
            signal_direction="all",
            block_local_countertrend=False,
        )

        selected = select_candidate_trades(events, candidate)

        self.assertEqual([item.return_pct for item in selected], [0.30])

    def test_cached_events_preserve_reference_backtest_trade_sequence(self) -> None:
        class FixtureEngine:
            def __init__(self, _config: object) -> None:
                pass

            def detect(self, ready_bars: list[dict[str, object]], clean: bool = True) -> dict[str, object]:
                del clean
                signal_number = {1: 1, 2: 2, 4: 3}.get(len(ready_bars))
                if signal_number is None:
                    return {"signal": "wait"}
                return {
                    "signal": "long",
                    "stop_loss": 95.0,
                    "divergence_time": signal_number,
                    "signal_score": 8,
                    "structure_score": 2,
                    "divergence_type": "macd",
                    "structure_text": "fixture",
                }

        start = pd.Timestamp("2025-01-01T00:00:00Z")
        bars = [
            {
                "time": start + pd.Timedelta(minutes=15 * index),
                "open": 100.0,
                "high": 106.0,
                "low": 99.0,
                "close": 105.0,
                "volume": 1.0,
                "macd": 0.0,
                "trend": "up",
            }
            for index in range(6)
        ]
        baseline = ResearchCandidate(
            name="baseline",
            min_signal_score=0,
            min_structure_score=0,
            divergence_filter="all",
            signal_direction="all",
            block_local_countertrend=False,
        )

        events = extract_signal_events(
            bars,
            reward_risk=2.0,
            max_hold_bars=2,
            fee_rate=0.0,
            engine_factory=FixtureEngine,
        )
        selected = select_candidate_trades(events, baseline)
        with patch("project_signal_backtest.ProjectSignalEngine", FixtureEngine):
            reference, _curve = run_backtest(bars, 2.0, 2, 0.0)

        self.assertEqual([item.entry_time for item in selected], [item.entry_time for item in reference])
        self.assertEqual([item.exit_time for item in selected], [item.exit_time for item in reference])
        self.assertEqual([item.return_pct for item in selected], [item.return_pct for item in reference])


class VectorbtMetricsTest(unittest.TestCase):
    def test_metrics_compound_exact_project_trade_returns(self) -> None:
        result = vectorbt_candidate_metrics(
            {
                "two_trades": [trade(0, 0.10), trade(3, -0.05)],
                "empty": [],
            }
        ).set_index("candidate")

        self.assertEqual(int(result.loc["two_trades", "total_trades"]), 2)
        self.assertAlmostEqual(float(result.loc["two_trades", "expectancy"]), 0.025)
        self.assertAlmostEqual(float(result.loc["two_trades", "total_return"]), 0.045)
        self.assertAlmostEqual(float(result.loc["two_trades", "max_drawdown"]), -0.05)
        self.assertEqual(int(result.loc["empty", "total_trades"]), 0)
        self.assertEqual(float(result.loc["empty", "total_return"]), 0.0)


class CandidatePromotionGateTest(unittest.TestCase):
    def test_pooled_robustness_includes_cost_concentration_and_best_trade_checks(self) -> None:
        metrics = pooled_candidate_robustness(
            {
                "BTCUSDT": [0.010, -0.004],
                "ETHUSDT": [0.006, -0.002],
            },
            extra_round_trip_cost=0.002,
        )

        self.assertEqual(metrics["trades"], 4)
        self.assertEqual(metrics["symbols"], 2)
        self.assertAlmostEqual(metrics["expectancy"], 0.0025)
        self.assertAlmostEqual(metrics["stress_expectancy"], 0.0005)
        self.assertAlmostEqual(metrics["positive_symbol_ratio"], 1.0)
        self.assertAlmostEqual(metrics["max_positive_symbol_share"], 0.6)
        self.assertAlmostEqual(metrics["expectancy_without_best_trade"], 0.0)

    def test_cross_market_gate_fails_closed_when_one_market_loses_cost_headroom(self) -> None:
        okx = {
            "trades": 110,
            "symbols": 19,
            "positive_symbol_ratio": 0.63,
            "expectancy_without_best_trade": 0.0021,
            "profit_factor_without_best_trade": 1.35,
            "stress_expectancy": 0.0009,
            "stress_profit_factor": 1.12,
        }
        binance = {
            "trades": 340,
            "symbols": 42,
            "positive_symbol_ratio": 0.38,
            "expectancy_without_best_trade": 0.00006,
            "profit_factor_without_best_trade": 1.008,
            "stress_expectancy": -0.00169,
            "stress_profit_factor": 0.813,
        }

        gate = cross_market_candidate_gate(
            {"okx": okx, "binance": binance},
            minimum_trades_per_market=100,
            minimum_symbols_per_market=10,
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["status"], "RESEARCH_ONLY")
        self.assertIn("binance:POSITIVE_SYMBOL_RATIO", gate["blockers"])
        self.assertIn("binance:STRESS_EXPECTANCY", gate["blockers"])
        self.assertIn("binance:STRESS_PROFIT_FACTOR", gate["blockers"])

    def test_cross_market_gate_promotes_only_when_every_market_passes(self) -> None:
        passing = {
            "trades": 150,
            "symbols": 20,
            "positive_symbol_ratio": 0.60,
            "expectancy_without_best_trade": 0.001,
            "profit_factor_without_best_trade": 1.20,
            "stress_expectancy": 0.0002,
            "stress_profit_factor": 1.03,
        }

        gate = cross_market_candidate_gate(
            {"okx": passing, "binance": passing},
            minimum_trades_per_market=100,
            minimum_symbols_per_market=10,
        )

        self.assertTrue(gate["passed"])
        self.assertEqual(gate["status"], "PROMOTABLE")
        self.assertEqual(gate["blockers"], [])


class ResearchBoundaryTest(unittest.TestCase):
    def test_default_boundary_keeps_holdout_and_holding_horizon_untouched(self) -> None:
        entry_start, entry_end, holdout_start = research_boundaries(
            pd.Timestamp("2025-01-01T00:00:00Z"),
            pd.Timestamp("2025-01-11T00:00:00Z"),
            interval="1h",
            max_hold_bars=24,
            research_fraction=0.80,
        )

        self.assertEqual(entry_start, pd.Timestamp("2025-01-01T00:00:00Z"))
        self.assertEqual(entry_end, pd.Timestamp("2025-01-08T00:00:00Z"))
        self.assertEqual(holdout_start, pd.Timestamp("2025-01-09T00:00:00Z"))


class PreparedCacheTest(unittest.TestCase):
    def test_cache_loader_restores_timestamps_without_mutating_input(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "prepared.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                json.dump(
                    {
                        "bars": [{"time": "2025-01-01T00:00:00+00:00", "close": 1.0}],
                        "coverage": {"symbol": "BTCUSDT"},
                    },
                    handle,
                )

            bars, coverage = load_prepared_bars(path)

            self.assertEqual(bars[0]["time"], pd.Timestamp("2025-01-01T00:00:00Z"))
            self.assertEqual(coverage, {"symbol": "BTCUSDT"})
            self.assertTrue(path.exists())


class ParityTest(unittest.TestCase):
    def test_parity_reports_the_first_execution_difference(self) -> None:
        reference = [trade(0, 0.10)]
        accelerated = [trade(0, 0.10)]
        accelerated[0].exit_price = 999.0

        parity = trade_sequence_parity(reference, accelerated)

        self.assertFalse(parity["passed"])
        self.assertEqual(parity["first_mismatch_index"], 0)
        self.assertIn("exit_price", parity["differing_fields"])


class ArtifactTest(unittest.TestCase):
    def test_artifacts_are_research_only_and_never_overwrite_a_run(self) -> None:
        metrics = pd.DataFrame(
            [
                {
                    "candidate": "baseline",
                    "total_trades": 1,
                    "win_rate": 1.0,
                    "expectancy": 0.1,
                    "total_return": 0.1,
                    "max_drawdown": 0.0,
                }
            ]
        )
        with TemporaryDirectory() as directory:
            output_dir = Path(directory) / "run"
            write_research_artifacts(
                output_dir,
                metrics=metrics,
                trades_by_candidate={"baseline": [trade(0, 0.1)]},
                manifest={"parity": {"passed": True}},
            )

            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "RESEARCH_ONLY")
            self.assertTrue((output_dir / "candidate_results.csv").exists())
            self.assertTrue((output_dir / "candidate_trades.csv").exists())
            self.assertTrue((output_dir / "report.md").exists())
            with self.assertRaises(FileExistsError):
                write_research_artifacts(
                    output_dir,
                    metrics=metrics,
                    trades_by_candidate={},
                    manifest={},
                )

    def test_offline_poc_uses_cache_and_writes_a_fresh_parity_report(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache_path = root / "prepared.json.gz"
            start = pd.Timestamp("2025-01-01T00:00:00Z")
            bars = [
                {
                    "time": str(start + pd.Timedelta(minutes=15 * index)),
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "volume": 1.0,
                    "macd": None,
                    "trend": "sideways",
                }
                for index in range(50)
            ]
            with gzip.open(cache_path, "wt", encoding="utf-8") as handle:
                json.dump({"bars": bars, "coverage": {"symbol": "BTCUSDT", "exchange": "okx"}}, handle)

            output_dir = run_vectorbt_poc(
                [
                    "--prepared-cache",
                    str(cache_path),
                    "--reports-dir",
                    str(root / "reports"),
                    "--max-hold-bars",
                    "2",
                    "--min-signal-scores",
                    "0",
                    "--min-structure-scores",
                    "0",
                    "--divergence-filters",
                    "all",
                    "--signal-directions",
                    "all",
                    "--countertrend-grid",
                    "off",
                ]
            )

            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["scope"], "POST_SIGNAL_FILTER_ONLY")
            self.assertTrue(manifest["parity"]["passed"])
            self.assertEqual(manifest["candidate_count"], 1)
            self.assertEqual(manifest["status"], "RESEARCH_ONLY")

    def test_direct_script_entrypoint_runs_after_all_helpers_are_defined(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache_path = root / "prepared.json.gz"
            start = pd.Timestamp("2025-01-01T00:00:00Z")
            bars = [
                {
                    "time": str(start + pd.Timedelta(minutes=15 * index)),
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "volume": 1.0,
                    "macd": None,
                    "trend": "sideways",
                }
                for index in range(20)
            ]
            with gzip.open(cache_path, "wt", encoding="utf-8") as handle:
                json.dump({"bars": bars, "coverage": {"symbol": "BTCUSDT", "exchange": "okx"}}, handle)

            completed = subprocess.run(
                [
                    sys.executable,
                    "vectorbt_research.py",
                    "--prepared-cache",
                    str(cache_path),
                    "--reports-dir",
                    str(root / "reports"),
                    "--max-hold-bars",
                    "2",
                    "--min-signal-scores",
                    "0",
                    "--min-structure-scores",
                    "0",
                    "--divergence-filters",
                    "all",
                    "--signal-directions",
                    "all",
                    "--countertrend-grid",
                    "off",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
