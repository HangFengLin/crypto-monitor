from __future__ import annotations

import unittest
import argparse
import csv
import io
import zipfile
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from backtest_statistics import (
    benjamini_hochberg,
    build_exploratory_group_tables,
    weekly_block_bootstrap_difference,
    weekly_block_bootstrap_mean,
    wilson_interval,
)
from historical_market_data import _ascii_url, _parse_okx_funding_archive, enrich_microstructure
from project_signal_backtest import SignalTrade, add_higher_timeframe_context, build_equity_curve, evaluate_trade, run_backtest
from strategy import DEFAULT_CONFIG
from strategy_validation import (
    apply_extra_roundtrip_cost,
    compare_candidates,
    config_with_overrides,
    load_candidates,
    load_prepared_cache,
    passes_final_candidate_gates,
    prepare_snapshot_datasets,
    profit_concentration,
    run_window,
    walk_forward_windows,
    write_prepared_cache,
)


def trade(index: int, return_pct: float = 0.01) -> SignalTrade:
    timestamp = pd.Timestamp("2025-01-01", tz="UTC") + pd.Timedelta(days=index)
    return SignalTrade(
        signal="long" if index % 2 == 0 else "short",
        stop_mode="structure_atr",
        entry_time=timestamp,
        entry_price=100,
        stop_loss=95,
        target_price=110,
        exit_time=timestamp + pd.Timedelta(hours=1),
        exit_price=101,
        exit_reason="protection_reached" if return_pct > 0 else "stop_loss",
        outcome="win" if return_pct > 0 else "loss",
        return_pct=return_pct,
        bars_held=4,
        confirm_bars=1,
        strength=0.5,
        divergence_type="macd",
        trend="up",
        higher_trend="up",
        signal_grade="normal",
        signal_score=8,
        signal_score_max=20,
        structure_score=1,
        structure_text="test",
        score_text="test",
        filter_text="test",
    )


class BacktestStatisticsTest(unittest.TestCase):
    def test_wilson_interval_exposes_small_sample_uncertainty(self) -> None:
        low, high = wilson_interval(2, 3)
        self.assertLess(low, 0.25)
        self.assertGreater(high, 0.90)

    def test_weekly_bootstrap_is_reproducible(self) -> None:
        records = [item.__dict__ for item in [trade(index, 0.01 if index % 2 else -0.005) for index in range(35)]]
        first = weekly_block_bootstrap_mean(records, iterations=200, seed=7)
        second = weekly_block_bootstrap_mean(records, iterations=200, seed=7)
        self.assertEqual(first, second)

    def test_candidate_difference_uses_a_95_percent_interval(self) -> None:
        baseline = [item.__dict__ for item in [trade(index, 0.001 * (index % 5 - 2)) for index in range(70)]]
        candidate = [item.__dict__ for item in [trade(index, 0.003 + 0.001 * (index % 7 - 3)) for index in range(70)]]
        interval_90 = weekly_block_bootstrap_difference(candidate, baseline, iterations=400, seed=9, confidence=0.90)
        interval_95 = weekly_block_bootstrap_difference(candidate, baseline, iterations=400, seed=9, confidence=0.95)
        self.assertLessEqual(interval_95["ci_low"], interval_90["ci_low"])
        self.assertGreaterEqual(interval_95["ci_high"], interval_90["ci_high"])

    def test_bh_adjustment_preserves_input_order(self) -> None:
        adjusted = benjamini_hochberg([0.04, 0.001, 0.20], alpha=0.10)
        self.assertTrue(adjusted[1]["reject"])
        self.assertFalse(adjusted[2]["reject"])

    def test_group_report_applies_hard_minimum_and_keeps_full_diagnostic(self) -> None:
        frame = pd.DataFrame([item.__dict__ for item in [trade(index) for index in range(29)]])
        report, full = build_exploratory_group_tables(frame, min_trades=30)
        self.assertTrue(report.empty)
        self.assertFalse(full.empty)
        frame = pd.DataFrame([item.__dict__ for item in [trade(index) for index in range(30)]])
        report, _full = build_exploratory_group_tables(frame, min_trades=30)
        self.assertFalse(report.empty)
        self.assertTrue((report["trades"] >= 30).all())


class HistoricalAlignmentTest(unittest.TestCase):
    def test_archive_urls_percent_encode_unicode_contracts(self) -> None:
        encoded = _ascii_url("https://data.binance.vision/path/币安人生USDT/file.zip?x=1")
        self.assertIn("%E5%B8%81%E5%AE%89%E4%BA%BA%E7%94%9FUSDT", encoded)
        self.assertTrue(encoded.endswith("?x=1"))

    def test_okx_funding_archive_parser_filters_range(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "funding.zip"
            payload = io.StringIO()
            writer = csv.writer(payload)
            writer.writerow(["instrument_name", "funding_rate", "funding_time"])
            writer.writerow(["BTC-USDT-SWAP", "0.001", "100"])
            writer.writerow(["BTC-USDT-SWAP", "0.002", "200"])
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("funding.csv", payload.getvalue())

            rows = _parse_okx_funding_archive(path, 150, 250)

        self.assertEqual(rows, [{"timestamp": 200, "funding_rate": 0.002, "source": "okx_historical_archive"}])

    def test_prepared_cache_round_trip_restores_timestamps(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "prepared.json.gz"
            timestamp = pd.Timestamp("2025-01-01T00:00:00Z")
            write_prepared_cache(path, [{"time": timestamp, "open": 1.0}], {"bars": 1})
            loaded = load_prepared_cache(path)
            self.assertIsNotNone(loaded)
            bars, coverage = loaded
            self.assertEqual(bars[0]["time"], timestamp)
            self.assertEqual(coverage["bars"], 1)

    def test_microstructure_alignment_never_uses_future_rows(self) -> None:
        start = pd.Timestamp("2025-01-01T00:00:00Z")
        bars = []
        oi_rows = []
        for index in range(25):
            timestamp = start + pd.Timedelta(minutes=15 * index)
            bars.append({"open_time": int(timestamp.timestamp() * 1000), "close_time": int((timestamp + pd.Timedelta(minutes=15)).timestamp() * 1000) - 1})
            oi_time = timestamp + pd.Timedelta(minutes=15)
            oi_rows.append({"timestamp": int(oi_time.timestamp() * 1000), "open_interest": 100 + index})
        future_funding = int((start + pd.Timedelta(hours=10)).timestamp() * 1000)
        enriched, coverage = enrich_microstructure(bars, [{"timestamp": future_funding, "funding_rate": 0.1}], oi_rows, "15m")
        self.assertNotIn("funding_rate", enriched[0])
        self.assertNotIn("open_interest_ratio", enriched[18])
        self.assertNotIn("open_interest_ratio", enriched[19])
        self.assertIn("open_interest_ratio", enriched[20])
        boundary_funding = int((start + pd.Timedelta(minutes=15)).timestamp() * 1000)
        boundary_enriched, _ = enrich_microstructure(bars[:2], [{"timestamp": boundary_funding, "funding_rate": 0.1}], [], "15m")
        self.assertNotIn("funding_rate", boundary_enriched[0])
        self.assertIn("funding_rate", boundary_enriched[1])
        stale_bar = {"open_time": int((start + pd.Timedelta(days=2)).timestamp() * 1000)}
        stale_bar["close_time"] = stale_bar["open_time"] + 15 * 60 * 1000 - 1
        stale_enriched, _ = enrich_microstructure(bars + [stale_bar], [], oi_rows, "15m")
        self.assertNotIn("open_interest_ratio", stale_enriched[-1])
        self.assertEqual(coverage["funding_coverage"], 0.0)

    def test_higher_timeframe_alignment_uses_only_closed_bars(self) -> None:
        lower = [{"open_time": value - 10, "close_time": value} for value in (100, 200, 300)]
        higher = [
            {"open_time": 50, "close_time": 150, "trend": "old", "close": 1},
            {"open_time": 151, "close_time": 250, "trend": "new", "close": 2},
        ]
        aligned = add_higher_timeframe_context(lower, higher)
        self.assertEqual([bar["higher_trend"] for bar in aligned], ["unknown", "old", "new"])


class BacktestConsistencyTest(unittest.TestCase):
    def test_long_backtest_caps_signal_context_at_250_bars(self) -> None:
        observed_lengths: list[int] = []

        class RecordingEngine:
            def __init__(self, _config: object) -> None:
                pass

            def detect(self, bars: list[dict[str, object]]) -> dict[str, str]:
                observed_lengths.append(len(bars))
                return {"signal": "wait"}

        bars = [
            {
                "time": pd.Timestamp("2025-01-01", tz="UTC") + pd.Timedelta(minutes=15 * index),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1.0,
                "macd": 0.0,
            }
            for index in range(400)
        ]
        with patch("project_signal_backtest.ProjectSignalEngine", RecordingEngine):
            run_backtest(bars, 2.0, 96, 0.001)
        self.assertEqual(max(observed_lengths), 250)
        self.assertEqual(observed_lengths[-1], 250)

    def test_equity_books_return_at_exit_not_entry(self) -> None:
        bars = [
            {"time": pd.Timestamp("2025-01-01T00:00:00Z")},
            {"time": pd.Timestamp("2025-01-01T01:00:00Z")},
            {"time": pd.Timestamp("2025-01-01T02:00:00Z")},
        ]
        item = trade(0, 0.10)
        item.entry_time = bars[0]["time"]
        item.exit_time = bars[2]["time"]
        curve = build_equity_curve(bars, [item])
        self.assertEqual(curve.iloc[0], 1.0)
        self.assertEqual(curve.iloc[1], 1.0)
        self.assertAlmostEqual(curve.iloc[2], 1.1)

    def test_structure_mode_matches_shared_one_r_exit(self) -> None:
        entry_time = pd.Timestamp("2025-01-01T00:00:00Z")
        bars = [
            {"time": entry_time, "open": 100.0, "high": 106.0, "low": 99.0, "close": 105.0, "atr": 2.0},
        ]
        signal = {"signal": "long", "stop_loss": 95.0}
        result = evaluate_trade(signal, bars, 0, 2.0, 96, 0.0, "structure_atr")
        self.assertIsNotNone(result)
        self.assertEqual(result.exit_reason, "protection_reached")
        self.assertEqual(result.exit_price, 105.0)

    def test_positive_timeout_is_counted_as_win(self) -> None:
        entry_time = pd.Timestamp("2025-01-01T00:00:00Z")
        bars = [{"time": entry_time, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "atr": 2.0}]
        result = evaluate_trade({"signal": "long", "stop_loss": 95.0}, bars, 0, 2.0, 0, 0.0, "structure_atr")
        self.assertIsNotNone(result)
        self.assertEqual(result.exit_reason, "timeout")
        self.assertEqual(result.outcome, "win")


class ValidationWindowTest(unittest.TestCase):
    def test_parallel_data_preparation_preserves_snapshot_order(self) -> None:
        args = argparse.Namespace(interval="15m", data_workers=3, data_executor="thread")
        items = [{"symbol": symbol} for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]]
        start = pd.Timestamp("2025-01-01", tz="UTC")
        end = pd.Timestamp("2025-01-02", tz="UTC")

        def prepared(_exchange: str, symbol: str, *_args: object, **_kwargs: object) -> tuple[list[dict[str, object]], dict[str, object]]:
            return [{"time": start, "symbol": symbol}], {"symbol": symbol, "bars": 1}

        with patch("strategy_validation.prepare_symbol_bars", side_effect=prepared):
            datasets, coverage = prepare_snapshot_datasets("binance", items, args, start, end, Path("unused"))
        self.assertEqual(list(datasets), ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        self.assertEqual([row["symbol"] for row in coverage], ["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    def test_okx_data_preparation_is_serial_to_respect_rate_limits(self) -> None:
        args = argparse.Namespace(interval="15m", data_workers=8, data_executor="thread")
        items = [{"symbol": symbol} for symbol in ["BTCUSDT", "ETHUSDT"]]
        start = pd.Timestamp("2025-01-01", tz="UTC")
        end = pd.Timestamp("2025-01-02", tz="UTC")

        with patch("strategy_validation.prepare_snapshot_item", side_effect=[
            ("BTCUSDT", [{"time": start}], {"symbol": "BTCUSDT", "bars": 1}),
            ("ETHUSDT", [{"time": start}], {"symbol": "ETHUSDT", "bars": 1}),
        ]) as prepared, patch("strategy_validation.concurrent.futures.ThreadPoolExecutor") as executor:
            datasets, _coverage = prepare_snapshot_datasets("okx", items, args, start, end, Path("unused"))

        self.assertEqual(list(datasets), ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(prepared.call_count, 2)
        executor.assert_not_called()

    def test_walk_forward_reserves_final_holdout_and_embargo(self) -> None:
        start = pd.Timestamp("2024-01-01", tz="UTC")
        end = pd.Timestamp("2026-01-01", tz="UTC")
        windows, test_start = walk_forward_windows(start, end, 96, "15m")
        self.assertGreaterEqual(len(windows), 4)
        self.assertEqual(windows[0]["validation_start"] - windows[0]["train_end"], pd.Timedelta(days=1))
        self.assertGreater(test_start, start + (end - start) * 0.80)
        odd_end = end + pd.Timedelta(days=1)
        _windows, aligned_test_start = walk_forward_windows(start, odd_end, 96, "15m")
        self.assertEqual(int((aligned_test_start - start).total_seconds()) % (15 * 60), 0)

    def test_candidate_overrides_are_strict_and_do_not_mutate_baseline(self) -> None:
        candidate = config_with_overrides({"confirmation_mode": "both"})
        self.assertEqual(candidate.confirmation_mode, "both")
        self.assertEqual(DEFAULT_CONFIG.confirmation_mode, "either")
        with self.assertRaises(ValueError):
            config_with_overrides({"not_a_parameter": 1})

    def test_external_candidate_yaml_is_single_family_and_read_only(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "rsi.yaml"
            path.write_text("strategy:\n  buy_rsi_threshold: 35\n  sell_rsi_threshold: 65\n", encoding="utf-8")
            candidates = load_candidates(str(path))
            self.assertEqual(candidates[-1]["family"], "rsi")
            path.write_text("strategy:\n  buy_rsi_threshold: 35\n  adx_long_max: 24\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_candidates(str(path))

    def test_window_excludes_trades_that_cross_the_segment_boundary(self) -> None:
        start = pd.Timestamp("2025-01-02", tz="UTC")
        end = pd.Timestamp("2025-01-03", tz="UTC")
        valid = trade(1)
        valid.entry_time = start + pd.Timedelta(hours=1)
        valid.exit_time = start + pd.Timedelta(hours=2)
        crossing = trade(2)
        crossing.entry_time = end - pd.Timedelta(hours=1)
        crossing.exit_time = end
        bars = [{"time": start - pd.Timedelta(days=1), "macd": 0.0}]
        args = argparse.Namespace(interval="15m", reward_risk=2.0, max_hold_bars=96, fee_rate=0.001, bootstrap_iterations=20)
        with patch("strategy_validation.run_backtest", return_value=([valid, crossing], pd.Series(dtype=float))) as mocked:
            retained, _symbols, metrics = run_window({"BTCUSDT": bars}, DEFAULT_CONFIG, start, end, args)
        self.assertEqual(retained, [valid])
        self.assertEqual(metrics["total_trades"], 1)
        self.assertEqual(mocked.call_args.kwargs["entry_end_time"], end - pd.Timedelta(days=1))

    def test_profit_concentration_enforces_multi_symbol_weight(self) -> None:
        trades = [trade(index, 0.01) for index in range(4)]
        for index, item in enumerate(trades):
            setattr(item, "symbol", f"S{index}")
        self.assertAlmostEqual(profit_concentration(trades), 0.25)

    def test_symbol_nonworse_ratio_ignores_symbols_with_no_trades(self) -> None:
        candidates = [
            {"name": "baseline", "family": "baseline", "overrides": {}},
            {"name": "candidate", "family": "rsi", "overrides": {"buy_rsi_threshold": 35, "sell_rsi_threshold": 65}},
        ]
        folds = pd.DataFrame(
            [
                {"candidate": "baseline", "fold": "f1", "expectancy": 0.01, "max_drawdown": -0.02},
                {"candidate": "candidate", "fold": "f1", "expectancy": 0.02, "max_drawdown": -0.02},
            ]
        )
        baseline_symbols = pd.DataFrame(
            [
                {"symbol": "ACTIVE", "total_trades": 1, "expectancy": 0.02},
                {"symbol": "IDLE", "total_trades": 0, "expectancy": 0.0},
            ]
        )
        candidate_symbols = pd.DataFrame(
            [
                {"symbol": "ACTIVE", "total_trades": 1, "expectancy": -0.01},
                {"symbol": "IDLE", "total_trades": 0, "expectancy": 0.0},
            ]
        )
        comparison = compare_candidates(
            candidates,
            folds,
            {"baseline": [], "candidate": []},
            {"baseline": baseline_symbols, "candidate": candidate_symbols},
            20,
        )
        self.assertEqual(comparison.iloc[0]["symbol_nonworse_ratio"], 0.0)

    def test_double_cost_reuses_trades_and_only_adjusts_roundtrip_return(self) -> None:
        original = trade(0, 0.01)
        setattr(original, "symbol", "BTCUSDT")
        adjusted = apply_extra_roundtrip_cost([original], 0.001)
        self.assertAlmostEqual(adjusted[0].return_pct, 0.008)
        self.assertEqual(getattr(adjusted[0], "symbol"), "BTCUSDT")
        self.assertAlmostEqual(original.return_pct, 0.01)

    def test_missing_okx_evidence_can_never_promote_a_candidate(self) -> None:
        baseline = {"expectancy": 0.001, "max_drawdown": -0.10}
        candidate = {"total_trades": 150, "expectancy": 0.005, "max_drawdown": -0.10}
        double_cost = {"expectancy": 0.002}
        self.assertFalse(
            passes_final_candidate_gates(
                baseline,
                candidate,
                double_cost,
                okx_direction_consistent=False,
                max_symbol_profit_share=0.20,
            )
        )


if __name__ == "__main__":
    unittest.main()
