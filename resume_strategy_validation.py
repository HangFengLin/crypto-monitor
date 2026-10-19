#!/usr/bin/env python3
"""Replay frozen validation inputs into a new synchronized account report.

Existing CSVs remain diagnostic and are never restored as account evidence.
The old window, universe and candidate registry stay frozen; a relocated
account contract must retain its hash. Historical report files are preserved.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd

from historical_market_data import sha256_file
from position_manager import LEGACY_RATIO_V1, RETURN_MODEL_CHOICES
from project_signal_backtest import SignalTrade, calculate_metrics
from strategy import DEFAULT_CONFIG
from strategy_validation import (
    compare_candidates,
    prepare_snapshot_datasets,
    run_validation,
    strategy_hash,
    trade_equity,
    trade_records,
    utc_timestamp,
    validation_execution_metadata,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按冻结快照完整重放账户，写全新报告并保留历史产物")
    parser.add_argument("--report-dir", required=True)
    parser.add_argument(
        "--account-contract", default=None, help="必须显式提供账户/完整资金费合同，不使用旧逐笔指标补证"
    )
    parser.add_argument("--reports-dir", default=None, help="全新重放报告的父目录，默认原报告父目录")
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
    if metadata and metadata.get("execution_scope") == "synchronized_linear_perpetual_account_v1":
        # Resolve the caller's copy before validating metadata; the original
        # location may no longer exist. Content, not its path, freezes authority.
        expected_sha = metadata.get("account_contract_sha256")
        if not isinstance(expected_sha, str) or len(expected_sha) != 64:
            raise ValueError("frozen account contract hash required")
        if getattr(cli, "account_contract", None):
            values["account_contract"] = str(Path(cli.account_contract).resolve())
        values["account_contract_sha256"] = expected_sha
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
            metrics.update(
                account_verified=False,
                account_scope="trade_sequence_diagnostic_only",
                account_blocker="TRADE_CSV_CANNOT_REBUILD_ACCOUNT_PATH",
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
        (str(exchange), str(symbol)) not in keys for exchange, symbol in zip(existing["exchange"], existing["symbol"])
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
    """Replay into a new report; existing trade CSVs cannot restore an account."""
    cli = parse_args()
    if not getattr(cli, "account_contract", None):
        raise ValueError("resume requires a new explicit account contract; legacy trade CSVs cannot certify an account")
    report_dir = Path(cli.report_dir).resolve()
    manifest_path = report_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    args = report_runtime_args(manifest, cli)
    if args.smoke:
        raise ValueError("smoke reports cannot be promoted through the resume path")
    if args.return_model != "linear_usdm_v1" or args.holding_limit_convention != "entry_bar_is_first":
        raise ValueError(
            "legacy accounting remains diagnostic; start a separate explicitly frozen linear account protocol"
        )
    args.account_contract = str(Path(cli.account_contract).resolve())
    production_config = Path("config.yaml")
    before_hash = sha256_file(production_config) if production_config.exists() else None
    if before_hash != manifest.get("production_config_sha256_before"):
        raise RuntimeError("production config hash differs from original run; refusing to mix experiments")
    args.start, args.end = manifest["start"], manifest["end"]
    args.universe_snapshot = str(report_dir / "universe_snapshot.json")
    args.reports_dir = str(Path(cli.reports_dir or report_dir.parent).resolve())
    if Path(args.reports_dir) == report_dir or report_dir in Path(args.reports_dir).parents:
        raise ValueError("new reports directory must be outside the preserved historical report")
    # Reuse only the frozen candidate set, never current defaults or old outcomes.
    registry = json.loads((report_dir / "candidate_registry.json").read_text())
    candidates = registry["candidates"] if isinstance(registry, dict) else registry
    if not isinstance(registry, dict) or registry.get("base_config_sha256") != strategy_hash(DEFAULT_CONFIG):
        raise ValueError("current baseline differs from frozen candidate registry; start a new protocol")
    bindings = json.loads((report_dir / "data_config_hashes.json").read_text())
    candidate_sha = hashlib.sha256(
        json.dumps(candidates, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    artifact_hashes = json.loads((report_dir / "artifact_hashes.json").read_text())
    if (
        candidate_sha != registry.get("candidate_set_sha256")
        or candidate_sha != bindings.get("candidate_set_sha256")
        or sha256_file(report_dir / "universe_snapshot.json") != bindings.get("universe_snapshot_sha256")
        or any(
            sha256_file(report_dir / name) != artifact_hashes.get(name)
            for name in ("candidate_registry.json", "universe_snapshot.json")
        )
    ):
        raise ValueError("frozen candidate/universe hash binding drift; refusing to mix experiments")
    args._frozen_candidates = candidates
    args._prior_report = {
        "path": str(report_dir),
        "manifest_sha256": sha256_file(manifest_path),
        "candidate_registry_sha256": sha256_file(report_dir / "candidate_registry.json"),
        "old_csv_account_authority": False,
        "sample_authority": "exposed_historical_diagnostic",
        "protocol_relation": "same_frozen_account_contract"
        if manifest.get("execution_metadata", {}).get("account_contract_sha256")
        else "new_explicit_account_protocol",
    }
    run_validation(args)


if __name__ == "__main__":
    main()
