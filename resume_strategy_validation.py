#!/usr/bin/env python3
"""Resume a completed validation report after recoverable data-source failures.

The resume path is intentionally conservative: it reuses only immutable trade
records, replays every previously missing Binance symbol, rebuilds all
candidate/fold statistics, and then reruns the complete OKX robustness gate.
Production config.yaml remains read-only.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from backtest_statistics import build_exploratory_group_tables
from historical_market_data import sha256_file, write_json
from position_manager import LEGACY_RATIO_V1, RETURN_MODEL_CHOICES
from project_signal_backtest import SignalTrade, calculate_metrics
from strategy import DEFAULT_CONFIG
from strategy_validation import (
    apply_extra_roundtrip_cost,
    build_data_config_manifest,
    compare_candidates,
    config_with_overrides,
    evaluate_validation_candidates,
    load_candidates,
    passes_final_candidate_gates,
    prepare_snapshot_datasets,
    profit_concentration,
    render_report,
    run_window,
    strategy_hash,
    trade_equity,
    trade_records,
    utc_timestamp,
    validation_execution_metadata,
    walk_forward_windows,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="补齐失败数据并重建抗过拟合验证报告")
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--data-workers", type=int, default=8)
    parser.add_argument("--data-executor", choices=["process", "thread"], default="process")
    parser.add_argument("--evaluation-workers", type=int, default=8)
    return parser.parse_args()


def report_runtime_args(manifest: dict[str, Any], cli: argparse.Namespace) -> argparse.Namespace:
    values = dict(manifest.get("arguments") or {})
    values["cache_dir"] = cli.cache_dir or values.get("cache_dir") or "runtime/backtest-data"
    values["data_workers"] = cli.data_workers
    values["data_executor"] = cli.data_executor
    values["evaluation_workers"] = cli.evaluation_workers
    values.setdefault("candidate_overrides", None)
    values.setdefault("skip_okx", False)
    values.setdefault("smoke", False)
    values.setdefault("seed", int(manifest.get("bootstrap_seed", 20260620)))
    values.setdefault("bootstrap_iterations", int(manifest.get("bootstrap_iterations", 2000)))
    values.setdefault("min_group_trades", int(manifest.get("min_group_trades", 30)))
    accounting_keys = ("return_model", "holding_limit_convention")
    metadata = manifest.get("execution_metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("invalid frozen execution metadata")
    argument_keys = [key for key in accounting_keys if key in values]
    if argument_keys and len(argument_keys) != len(accounting_keys):
        raise ValueError("incomplete frozen accounting arguments")
    if metadata is not None:
        if any(key not in metadata for key in accounting_keys):
            raise ValueError("incomplete frozen execution metadata")
        for key in accounting_keys:
            if key in values and values[key] != metadata[key]:
                raise ValueError("frozen accounting arguments conflict with execution metadata")
            values[key] = metadata[key]
    elif not argument_keys:
        # Pre-version manifests were produced with these two legacy defaults.
        values.update(return_model="legacy_ratio_v1", holding_limit_convention="legacy_inclusive_end")
    args = argparse.Namespace(**values)
    expected = validation_execution_metadata(args)
    if metadata is not None and any(metadata[key] != value for key, value in expected.items() if key in metadata):
        raise ValueError("frozen execution metadata conflicts with replay arguments")
    return args


def frame_to_trades(frame: pd.DataFrame, *, expected_return_model: str | None = None) -> list[SignalTrade]:
    if frame.empty:
        return []
    fields = list(dataclasses.fields(SignalTrade))
    trades: list[SignalTrade] = []
    for row in frame.to_dict("records"):
        values: dict[str, Any] = {}
        for field in fields:
            if field.name in row:
                values[field.name] = row[field.name]
            elif field.default is not dataclasses.MISSING:
                values[field.name] = field.default
            elif field.default_factory is not dataclasses.MISSING:
                values[field.name] = field.default_factory()
            else:
                raise KeyError(field.name)
        values["entry_time"] = utc_timestamp(values["entry_time"])
        values["exit_time"] = utc_timestamp(values["exit_time"])
        model = values["return_model"]
        if pd.isna(model):
            model = LEGACY_RATIO_V1
        if model not in RETURN_MODEL_CHOICES or (expected_return_model is not None and model != expected_return_model):
            raise ValueError("trade accounting version differs from frozen replay arguments")
        values["return_model"] = model
        item = SignalTrade(**values)
        item.symbol = str(row.get("symbol") or "")
        trades.append(item)
    return trades


def rebuild_validation_statistics(
    records: pd.DataFrame,
    candidates: list[dict[str, Any]],
    windows: list[dict[str, pd.Timestamp]],
    args: argparse.Namespace,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    expected_model = validation_execution_metadata(args)["return_model"]
    fold_rows: list[dict[str, Any]] = []
    symbols_by_candidate: dict[str, list[dict[str, Any]]] = {item["name"]: [] for item in candidates}
    records_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        name = candidate["name"]
        candidate_records = records[records["candidate"] == name].copy()
        records_by_candidate[name] = candidate_records.to_dict("records")
        for window in windows:
            fold = window["fold"]
            fold_frame = candidate_records[candidate_records["fold"] == fold]
            trades = frame_to_trades(fold_frame, expected_return_model=expected_model)
            metrics = calculate_metrics(
                trades,
                trade_equity(trades),
                bootstrap_iterations=args.bootstrap_iterations,
                bootstrap_seed=args.seed,
            )
            fold_rows.append(
                {
                    "candidate": name,
                    "family": candidate["family"],
                    "fold": fold,
                    "phase": "validation",
                    "train_start": window["train_start"],
                    "train_end": window["train_end"],
                    "validation_start": window["validation_start"],
                    "validation_end": window["validation_end"],
                    **metrics,
                }
            )
            for symbol, symbol_frame in fold_frame.groupby("symbol", sort=False):
                symbol_trades = frame_to_trades(symbol_frame, expected_return_model=expected_model)
                symbol_metrics = calculate_metrics(symbol_trades, trade_equity(symbol_trades), bootstrap_iterations=0)
                symbols_by_candidate[name].append({"symbol": symbol, "candidate": name, "fold": fold, **symbol_metrics})
    fold_metrics = pd.DataFrame(fold_rows)
    comparison = compare_candidates(
        candidates,
        fold_metrics,
        records_by_candidate,
        {name: pd.DataFrame(rows) for name, rows in symbols_by_candidate.items()},
        args.bootstrap_iterations,
        args.seed,
    )
    return fold_metrics, comparison


def replace_coverage_rows(existing: pd.DataFrame, rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    incoming = pd.DataFrame(list(rows))
    if incoming.empty:
        return existing
    keys = set(zip(incoming["exchange"].astype(str), incoming["symbol"].astype(str)))
    if existing.empty:
        return incoming
    keep = [
        (str(exchange), str(symbol)) not in keys
        for exchange, symbol in zip(existing["exchange"], existing["symbol"])
    ]
    return pd.concat([existing.loc[keep], incoming], ignore_index=True, sort=False)


def append_symbol_trades(existing: pd.DataFrame, new_trades: list[SignalTrade], **extra: Any) -> pd.DataFrame:
    symbols = {str(getattr(trade, "symbol", "")) for trade in new_trades}
    retained = existing[~existing["symbol"].astype(str).isin(symbols)] if not existing.empty and symbols else existing
    incoming = pd.DataFrame(trade_records(new_trades, **extra))
    merged = pd.concat([retained, incoming], ignore_index=True, sort=False)
    dedupe = [column for column in ["candidate", "symbol", "signal", "entry_time", "exit_time"] if column in merged]
    return merged.drop_duplicates(dedupe, keep="last") if dedupe else merged


def prepare_all_binance_if_needed(
    snapshot: dict[str, Any],
    args: argparse.Namespace,
    fetch_start: pd.Timestamp,
    end: pd.Timestamp,
    cache_dir: Path,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    return prepare_snapshot_datasets("binance", list(snapshot.get("binance", [])), args, fetch_start, end, cache_dir)


def main() -> None:
    cli = parse_args()
    report_dir = Path(cli.report_dir)
    manifest_path = report_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing report manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    args = report_runtime_args(manifest, cli)
    if args.smoke:
        raise ValueError("smoke reports cannot be promoted through the resume path")
    production_config = Path("config.yaml")
    before_hash = sha256_file(production_config)
    expected_hash = manifest.get("production_config_sha256_before")
    if before_hash != expected_hash:
        raise RuntimeError("production config hash differs from the original run; refusing to mix experiments")

    snapshot = json.loads((report_dir / "universe_snapshot.json").read_text(encoding="utf-8"))
    candidates = load_candidates(args.candidate_overrides)
    start = utc_timestamp(manifest["start"])
    end = utc_timestamp(manifest["end"])
    fetch_start = start - pd.Timedelta(days=50)
    windows, test_start = walk_forward_windows(start, end, args.max_hold_bars, args.interval)
    cache_dir = Path(args.cache_dir)
    coverage = pd.read_csv(report_dir / "data_coverage.csv")
    failed_binance = set(
        coverage.loc[(coverage["exchange"] == "binance") & (coverage["bars"].fillna(0) <= 0), "symbol"].astype(str)
    )
    known_binance = set(coverage.loc[coverage["exchange"] == "binance", "symbol"].astype(str))
    missing_items = [
        item for item in snapshot.get("binance", [])
        if str(item["symbol"]) in failed_binance or str(item["symbol"]) not in known_binance
    ]
    repaired_datasets, repaired_coverage = prepare_snapshot_datasets(
        "binance", missing_items, args, fetch_start, end, cache_dir
    ) if missing_items else ({}, [])
    coverage = replace_coverage_rows(coverage, repaired_coverage)
    unresolved = coverage[(coverage["exchange"] == "binance") & (coverage["bars"].fillna(0) <= 0)]
    if not unresolved.empty:
        raise RuntimeError(f"unresolved Binance datasets: {', '.join(unresolved['symbol'].astype(str))}")

    validation_records = pd.read_csv(report_dir / "validation_trades.csv")
    if repaired_datasets:
        _metrics, repaired_records, _symbols = evaluate_validation_candidates(repaired_datasets, candidates, windows, args)
        repaired_symbols = set(repaired_datasets)
        validation_records = validation_records[~validation_records["symbol"].astype(str).isin(repaired_symbols)]
        additions = [row for values in repaired_records.values() for row in values]
        validation_records = pd.concat([validation_records, pd.DataFrame(additions)], ignore_index=True, sort=False)
    fold_metrics, comparison = rebuild_validation_statistics(validation_records, candidates, windows, args)
    eligible = comparison[comparison["validation_gate"]] if not comparison.empty else pd.DataFrame()
    selected_name: str | None = str(eligible.iloc[0]["candidate"]) if not eligible.empty else None
    old_summary = json.loads((report_dir / "test_summary.json").read_text(encoding="utf-8"))
    old_selected = old_summary.get("selected_candidate")

    baseline_frame = pd.read_csv(report_dir / "test_trades_baseline.csv") if (report_dir / "test_trades_baseline.csv").exists() else pd.DataFrame()
    candidate_frame = pd.read_csv(report_dir / "test_trades_candidate.csv") if (report_dir / "test_trades_candidate.csv").exists() else pd.DataFrame()
    test_summary: dict[str, Any] = {"status": "baseline_retained", "selected_candidate": selected_name}
    final_test_trades: list[SignalTrade] = []
    okx_symbols_evaluated = 0
    if selected_name:
        selected = next(item for item in candidates if item["name"] == selected_name)
        if selected_name == old_selected and repaired_datasets and not baseline_frame.empty and not candidate_frame.empty:
            baseline_new, _rows, _metrics = run_window(repaired_datasets, DEFAULT_CONFIG, test_start, end, args)
            candidate_new, _rows, _metrics = run_window(
                repaired_datasets, config_with_overrides(selected["overrides"]), test_start, end, args
            )
            baseline_frame = append_symbol_trades(baseline_frame, baseline_new, candidate="baseline", phase="test")
            candidate_frame = append_symbol_trades(candidate_frame, candidate_new, candidate=selected_name, phase="test")
        elif selected_name != old_selected or baseline_frame.empty or candidate_frame.empty:
            all_binance, binance_coverage = prepare_all_binance_if_needed(snapshot, args, fetch_start, end, cache_dir)
            coverage = replace_coverage_rows(coverage, binance_coverage)
            baseline_test, _rows, _metrics = run_window(all_binance, DEFAULT_CONFIG, test_start, end, args)
            candidate_test, _rows, _metrics = run_window(
                all_binance, config_with_overrides(selected["overrides"]), test_start, end, args
            )
            baseline_frame = pd.DataFrame(trade_records(baseline_test, candidate="baseline", phase="test"))
            candidate_frame = pd.DataFrame(trade_records(candidate_test, candidate=selected_name, phase="test"))

        baseline_test = frame_to_trades(baseline_frame, expected_return_model=args.return_model)
        candidate_test = frame_to_trades(candidate_frame, expected_return_model=args.return_model)
        baseline_metrics = calculate_metrics(
            baseline_test, trade_equity(baseline_test), args.bootstrap_iterations, args.seed
        )
        candidate_metrics = calculate_metrics(
            candidate_test, trade_equity(candidate_test), args.bootstrap_iterations, args.seed
        )
        double_cost = apply_extra_roundtrip_cost(candidate_test, args.fee_rate)
        double_metrics = calculate_metrics(double_cost, trade_equity(double_cost), args.bootstrap_iterations, args.seed)

        okx_datasets, okx_coverage = prepare_snapshot_datasets(
            "okx", list(snapshot.get("okx", [])), args, fetch_start, end, cache_dir
        )
        coverage = replace_coverage_rows(coverage, okx_coverage)
        okx_symbols_evaluated = len(okx_datasets)
        expected_okx = len(snapshot.get("okx", []))
        if okx_symbols_evaluated != expected_okx:
            raise RuntimeError(f"OKX robustness sample incomplete: {okx_symbols_evaluated}/{expected_okx}")
        okx_baseline, _rows, okx_baseline_metrics = run_window(okx_datasets, DEFAULT_CONFIG, test_start, end, args)
        okx_candidate, _rows, okx_candidate_metrics = run_window(
            okx_datasets, config_with_overrides(selected["overrides"]), test_start, end, args
        )
        okx_delta_ok = (
            okx_candidate_metrics["expectancy"] >= okx_baseline_metrics["expectancy"]
            and okx_candidate_metrics["expectancy"] > 0
        )
        concentration = profit_concentration(candidate_test)
        passed = passes_final_candidate_gates(
            baseline_metrics,
            candidate_metrics,
            double_metrics,
            okx_direction_consistent=okx_delta_ok,
            max_symbol_profit_share=concentration,
        )
        test_summary.update(
            {
                "status": "candidate_passed" if passed else "baseline_retained",
                "baseline": baseline_metrics,
                "candidate": candidate_metrics,
                "double_cost": double_metrics,
                "okx": {"baseline": okx_baseline_metrics, "candidate": okx_candidate_metrics},
                "max_symbol_profit_share": concentration,
            }
        )
        final_test_trades = candidate_test if passed else baseline_test
        baseline_frame.to_csv(report_dir / "test_trades_baseline.csv", index=False)
        candidate_frame.to_csv(report_dir / "test_trades_candidate.csv", index=False)
        candidate_yaml = report_dir / "candidate_strategy.yaml"
        if passed:
            candidate_yaml.write_text(
                yaml.safe_dump(
                    {
                        "strategy": selected["overrides"],
                        "evidence": {"candidate": selected_name, "base_config_sha256": strategy_hash(DEFAULT_CONFIG)},
                    },
                    sort_keys=False,
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
        else:
            candidate_yaml.unlink(missing_ok=True)

    validation_records.to_csv(report_dir / "validation_trades.csv", index=False)
    fold_metrics.to_csv(report_dir / "fold_metrics.csv", index=False)
    comparison.to_csv(report_dir / "candidate_comparison.csv", index=False)
    coverage.to_csv(report_dir / "data_coverage.csv", index=False)
    group_source = pd.DataFrame(trade_records(final_test_trades)) if final_test_trades else validation_records[validation_records["candidate"] == "baseline"]
    group_stats, full_groups = build_exploratory_group_tables(group_source, min_trades=args.min_group_trades)
    group_stats.to_csv(report_dir / "group_stats.csv", index=False)
    full_groups.to_csv(report_dir / "group_stats_full_8d.csv", index=False)

    after_hash = sha256_file(production_config)
    if after_hash != before_hash:
        raise RuntimeError("production config changed during report resume")
    manifest.update(
        {
            "binance_symbols": int(((coverage["exchange"] == "binance") & (coverage["bars"].fillna(0) > 0)).sum()),
            "okx_symbols_evaluated": okx_symbols_evaluated,
            "production_config_sha256_after": after_hash,
            "production_config_modified": False,
            "deployed": False,
            "test_summary": test_summary,
            "resumed": True,
            "arguments": vars(args),
            "execution_metadata": validation_execution_metadata(args),
        }
    )
    write_json(report_dir / "manifest.json", manifest)
    write_json(report_dir / "test_summary.json", test_summary)
    write_json(report_dir / "data_config_hashes.json", build_data_config_manifest(cache_dir, report_dir / "universe_snapshot.json", candidates))
    for stale in (report_dir / "candidate_expectancy_delta.png", report_dir / "candidate_expectancy_delta.svg"):
        stale.unlink(missing_ok=True)
    report_path = render_report(report_dir, manifest, fold_metrics, comparison, test_summary, group_stats)
    hashes = {
        path.name: sha256_file(path)
        for path in sorted(report_dir.iterdir())
        if path.is_file() and path.name != "artifact_hashes.json"
    }
    write_json(report_dir / "artifact_hashes.json", hashes)
    print(f"断点续跑完成: {report_path.resolve()}")


if __name__ == "__main__":
    main()
