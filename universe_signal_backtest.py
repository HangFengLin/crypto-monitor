#!/usr/bin/env python3
"""Durable, resumable cross-symbol research backtest.

This runner is deliberately exploratory.  It pools ProjectSignalEngine trades
across a frozen market-cap universe and emits candidate hypotheses; promotion
still belongs to strategy_validation.py (bootstrap, BH-FDR, permanent holdout,
and cross-exchange robustness).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import signal
import sys
import time
import traceback
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from backtest_notifier import BacktestNotifier
from backtest_statistics import build_exploratory_group_tables
from data_client import interval_ms
from historical_market_data import fetch_binance_um_klines, fetch_okx_klines_range
from indicators import calculate_indicators
from project_signal_backtest import (
    STOP_MODE_CHOICES,
    add_higher_timeframe_context,
    build_equity_curve,
    calculate_metrics,
    run_backtest,
)
from runtime_utils import append_jsonl, atomic_write_text
from strategy_universe import build_market_cap_universe, build_okx_market_cap_universe

SCHEMA_VERSION = 3
HIGHER_INTERVAL = "4h"
MINIMUM_BARS = 300
INDICATOR_WARMUP_BARS = 250
DEFAULT_REPORTS_DIR = os.getenv("REPORTS_DIR", "reports")
SAFE_SYMBOL = re.compile(r"[^A-Za-z0-9_.-]+")
RESEARCH_ARGUMENTS = {
    "exchange",
    "okx_instrument_type",
    "top_n",
    "quote_asset",
    "min_quote_volume",
    "interval",
    "limit",
    "reward_risk",
    "max_hold_bars",
    "fee_rate",
    "stop_mode",
    "divergence_filter",
    "signal_direction",
    "structure_text_exact",
    "structure_text_contains",
    "symbol_vol_pctile_max",
    "symbol_vol_window_days",
    "symbol_vol_min_history_days",
    "pair_protection_min_fit_trades",
    "pair_protection_max_exclusions",
    "min_group_trades",
    "holdout_fraction",
    "embargo_bars",
    "max_symbols",
    "start",
    "end",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="跨币种池化项目信号回测（可断点续跑）")
    parser.add_argument("--exchange", choices=["binance", "okx"], default="okx")
    parser.add_argument("--okx-instrument-type", choices=["SWAP", "SPOT"], default="SWAP")
    parser.add_argument("--top-n", type=int, default=200)
    parser.add_argument("--quote-asset", default="USDT")
    parser.add_argument("--min-quote-volume", type=float, default=5_000_000)
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--limit", type=int, default=20_000)
    parser.add_argument("--reward-risk", type=float, default=2.0)
    parser.add_argument("--max-hold-bars", type=int, default=96)
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument(
        "--stop-mode",
        choices=STOP_MODE_CHOICES,
        default="structure_atr",
    )
    parser.add_argument(
        "--divergence-filter",
        choices=["all", "macd", "fast_macd"],
        default="all",
        help="只纳入指定背驰类型的信号",
    )
    parser.add_argument(
        "--signal-direction",
        choices=["all", "long", "short"],
        default="all",
        help="只纳入指定方向的信号",
    )
    parser.add_argument("--structure-text-exact", help="只纳入结构文本完全匹配的信号")
    parser.add_argument("--structure-text-contains", help="只纳入结构文本包含该因子的信号")
    parser.add_argument(
        "--symbol-vol-pctile-max",
        type=float,
        default=None,
        help="实验过滤：只纳入入场前已完成日线的个币实现波动历史分位 <= 该值的交易，例如 0.33",
    )
    parser.add_argument("--symbol-vol-window-days", type=int, default=30)
    parser.add_argument("--symbol-vol-min-history-days", type=int, default=60)
    parser.add_argument(
        "--pair-protection-min-fit-trades",
        type=int,
        default=10,
        help="币种保护诊断：只有 fit 期交易数不少于该值的币才可被判定为低收益币",
    )
    parser.add_argument(
        "--pair-protection-max-exclusions",
        type=int,
        default=20,
        help="币种保护诊断：最多评估剔除多少个 fit 期低收益币",
    )
    parser.add_argument("--min-group-trades", type=int, default=30)
    parser.add_argument("--holdout-fraction", type=float, default=0.30)
    parser.add_argument("--embargo-bars", type=int, default=None)
    parser.add_argument("--start", help="UTC inclusive start; must be supplied with --end")
    parser.add_argument("--end", help="UTC exclusive end; must be supplied with --start")
    parser.add_argument("--run-id")
    parser.add_argument("--resume", type=Path, help="Existing universe_backtest_<run-id> directory")
    parser.add_argument("--reports-dir", type=Path, default=Path(DEFAULT_REPORTS_DIR))
    parser.add_argument("--cache-dir", type=Path, default=Path("runtime/backtest-data"))
    parser.add_argument("--max-symbol-attempts", type=int, default=3)
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--sleep-between-symbols", type=float, default=0.3)
    args = parser.parse_args(raw_argv)
    args._provided_options = {
        token.split("=", 1)[0] for token in raw_argv if token.startswith("--")
    }
    return args


def validate_args(args: argparse.Namespace) -> None:
    positive = {
        "top_n": args.top_n,
        "limit": args.limit,
        "reward_risk": args.reward_risk,
        "max_hold_bars": args.max_hold_bars,
        "min_group_trades": args.min_group_trades,
        "max_symbol_attempts": args.max_symbol_attempts,
        "symbol_vol_window_days": args.symbol_vol_window_days,
        "symbol_vol_min_history_days": args.symbol_vol_min_history_days,
        "pair_protection_min_fit_trades": args.pair_protection_min_fit_trades,
    }
    for name, value in positive.items():
        if value <= 0:
            raise ValueError(f"--{name.replace('_', '-')} must be positive")
    if args.min_quote_volume < 0 or args.fee_rate < 0 or args.sleep_between_symbols < 0:
        raise ValueError("volume, fee, and sleep values cannot be negative")
    if args.max_symbols < 0:
        raise ValueError("--max-symbols cannot be negative")
    if args.pair_protection_max_exclusions < 0:
        raise ValueError("--pair-protection-max-exclusions cannot be negative")
    if args.symbol_vol_pctile_max is not None and not 0 <= args.symbol_vol_pctile_max <= 1:
        raise ValueError("--symbol-vol-pctile-max must be between 0 and 1")
    if args.holdout_fraction != 0 and not 0 < args.holdout_fraction < 1:
        raise ValueError("--holdout-fraction must be 0 or between 0 and 1")
    if args.embargo_bars is None:
        args.embargo_bars = args.max_hold_bars
    if args.embargo_bars < 0:
        raise ValueError("--embargo-bars cannot be negative")
    if bool(args.start) != bool(args.end):
        raise ValueError("--start and --end must be supplied together")
    # Fail early for unsupported exchange interval spellings.
    interval_ms(args.interval)


def utc_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def resolve_window(args: argparse.Namespace, now: pd.Timestamp | None = None) -> tuple[pd.Timestamp, pd.Timestamp]:
    width_ms = interval_ms(args.interval)
    if args.start and args.end:
        start, end = utc_timestamp(args.start), utc_timestamp(args.end)
    else:
        current = utc_timestamp(now if now is not None else pd.Timestamp.now(tz="UTC"))
        end_ms = (int(current.timestamp() * 1000) // width_ms) * width_ms
        end = pd.to_datetime(end_ms, unit="ms", utc=True)
        start = pd.to_datetime(end_ms - args.limit * width_ms, unit="ms", utc=True)
    if start >= end:
        raise ValueError("backtest start must be earlier than end")
    return start, end


def split_windows(
    start: pd.Timestamp,
    end: pd.Timestamp,
    holdout_fraction: float,
    embargo_bars: int,
    interval: str,
    max_hold_bars: int,
) -> dict[str, pd.Timestamp | None]:
    """Return leak-resistant fit/holdout entry windows.

    The embargo is one-sided on the right of the split.  Each entry window is
    shortened by the maximum holding horizon so exits cannot cross its end.
    """
    step_ms = interval_ms(interval)
    horizon = pd.Timedelta(milliseconds=step_ms * max_hold_bars)
    if holdout_fraction == 0:
        return {
            "split": None,
            "fit_start": start,
            "fit_entry_end": end - horizon,
            "fit_exit_end": end,
            "holdout_start": None,
            "holdout_entry_end": None,
            "holdout_exit_end": None,
        }
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    raw_split = start_ms + int((end_ms - start_ms) * (1.0 - holdout_fraction))
    split_ms = (raw_split // step_ms) * step_ms
    split = pd.to_datetime(split_ms, unit="ms", utc=True)
    holdout_start = split + pd.Timedelta(milliseconds=step_ms * embargo_bars)
    fit_entry_end = split - horizon
    holdout_entry_end = end - horizon
    if fit_entry_end <= start:
        raise ValueError("fit window is too short for max-hold-bars")
    if holdout_start >= holdout_entry_end:
        raise ValueError("holdout window is too short after embargo and holding horizon")
    return {
        "split": split,
        "fit_start": start,
        "fit_entry_end": fit_entry_end,
        "fit_exit_end": split,
        "holdout_start": holdout_start,
        "holdout_entry_end": holdout_entry_end,
        "holdout_exit_end": end,
    }


def build_universe(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.exchange == "okx":
        universe = build_okx_market_cap_universe(
            args.top_n,
            args.quote_asset,
            args.okx_instrument_type,
            args.min_quote_volume,
        )
    else:
        universe = build_market_cap_universe(
            args.top_n,
            args.quote_asset,
            args.min_quote_volume,
        )
    return universe[: args.max_symbols] if args.max_symbols else universe


def fetch_range(
    args: argparse.Namespace,
    symbol: str,
    interval: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[dict[str, Any]]:
    if args.exchange == "okx":
        return fetch_okx_klines_range(
            symbol,
            interval,
            start,
            end,
            args.okx_instrument_type,
            args.cache_dir,
        )
    return fetch_binance_um_klines(symbol, interval, start, end, args.cache_dir)


def expanding_percentile(series: pd.Series, min_history: int) -> pd.Series:
    history: list[float] = []
    output: list[float] = []
    for value in pd.to_numeric(series, errors="coerce"):
        if pd.isna(value) or len(history) < min_history:
            output.append(math.nan)
        else:
            output.append(sum(item <= float(value) for item in history) / len(history))
        if not pd.isna(value):
            history.append(float(value))
    return pd.Series(output, index=series.index)


def build_symbol_vol_context(
    args: argparse.Namespace,
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if args.symbol_vol_pctile_max is None:
        return pd.DataFrame(), {"enabled": False}
    warmup_days = args.symbol_vol_window_days + args.symbol_vol_min_history_days + 10
    daily_start = start - pd.Timedelta(days=warmup_days)
    daily_rows = fetch_range(args, symbol, "1d", daily_start, end)
    daily = pd.DataFrame(daily_rows)
    if daily.empty:
        return pd.DataFrame(), {
            "enabled": True,
            "threshold": args.symbol_vol_pctile_max,
            "daily_bars": 0,
            "usable_daily_bars": 0,
            "reason": "no daily bars",
        }
    daily["available_at"] = pd.to_datetime(daily["close_time"], unit="ms", utc=True)
    daily["close"] = pd.to_numeric(daily["close"], errors="coerce")
    daily = daily.sort_values("available_at").drop_duplicates("available_at")
    daily["symbol_return_1d"] = daily["close"].pct_change()
    realized_col = f"symbol_realized_vol{args.symbol_vol_window_days}"
    daily[realized_col] = (
        daily["symbol_return_1d"]
        .rolling(args.symbol_vol_window_days, min_periods=args.symbol_vol_window_days)
        .std()
        * math.sqrt(365)
    )
    daily["symbol_vol_pctile"] = expanding_percentile(
        daily[realized_col],
        args.symbol_vol_min_history_days,
    )
    metrics = daily[["available_at", realized_col, "symbol_vol_pctile"]].rename(
        columns={realized_col: "symbol_realized_vol"}
    )
    usable = int(metrics["symbol_vol_pctile"].notna().sum())
    return metrics, {
        "enabled": True,
        "threshold": args.symbol_vol_pctile_max,
        "window_days": args.symbol_vol_window_days,
        "min_history_days": args.symbol_vol_min_history_days,
        "daily_bars": int(len(daily)),
        "usable_daily_bars": usable,
        "first_daily": json_safe(daily["available_at"].min()) if len(daily) else None,
        "last_daily": json_safe(daily["available_at"].max()) if len(daily) else None,
    }


def annotate_symbol_vol_context(
    bars: list[dict[str, Any]],
    metrics: pd.DataFrame,
) -> dict[str, Any]:
    if metrics.empty:
        for bar in bars:
            bar["symbol_realized_vol"] = None
            bar["symbol_vol_pctile"] = None
        return {"bars_with_symbol_vol_pctile": 0, "bar_symbol_vol_coverage": 0.0}
    bar_frame = pd.DataFrame(
        {
            "bar_index": list(range(len(bars))),
            "bar_time": [utc_timestamp(bar["time"]) for bar in bars],
        }
    ).sort_values("bar_time")
    metric_frame = metrics.sort_values("available_at")
    joined = pd.merge_asof(
        bar_frame,
        metric_frame,
        left_on="bar_time",
        right_on="available_at",
        direction="backward",
    ).sort_values("bar_index")
    with_context = 0
    for row in joined.itertuples(index=False):
        realized = row.symbol_realized_vol
        pctile = row.symbol_vol_pctile
        realized_value = float(realized) if pd.notna(realized) else None
        pctile_value = float(pctile) if pd.notna(pctile) else None
        bars[int(row.bar_index)]["symbol_realized_vol"] = realized_value
        bars[int(row.bar_index)]["symbol_vol_pctile"] = pctile_value
        if pctile_value is not None:
            with_context += 1
    return {
        "bars_with_symbol_vol_pctile": with_context,
        "bar_symbol_vol_coverage": with_context / len(bars) if bars else 0.0,
    }


def symbol_vol_entry_filter(args: argparse.Namespace):
    if args.symbol_vol_pctile_max is None:
        return None
    threshold = float(args.symbol_vol_pctile_max)

    def _filter(_signal: dict[str, Any], _signal_bar: dict[str, Any], entry_bar: dict[str, Any]) -> bool:
        value = entry_bar.get("symbol_vol_pctile")
        if value is None:
            return False
        try:
            pctile = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(pctile) and pctile <= threshold

    return _filter


def entry_metric_lookup(bars: list[dict[str, Any]]) -> dict[pd.Timestamp, dict[str, Any]]:
    keys = ("symbol_realized_vol", "symbol_vol_pctile")
    lookup: dict[pd.Timestamp, dict[str, Any]] = {}
    for bar in bars:
        lookup[utc_timestamp(bar["time"])] = {key: json_safe(bar.get(key)) for key in keys}
    return lookup


def _trade_record(
    trade: Any,
    symbol: str,
    sample: str,
    entry_metrics: Mapping[pd.Timestamp, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    raw = asdict(trade) if hasattr(trade, "__dataclass_fields__") else dict(trade.__dict__)
    raw["symbol"] = symbol
    raw["sample"] = sample
    if entry_metrics:
        raw.update(entry_metrics.get(utc_timestamp(raw.get("entry_time")), {}))
    return json_safe(raw)


def run_one_symbol(
    args: argparse.Namespace,
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    windows: Mapping[str, pd.Timestamp | None],
) -> dict[str, Any]:
    base_fetch_start = start - pd.Timedelta(
        milliseconds=interval_ms(args.interval) * INDICATOR_WARMUP_BARS
    )
    raw_bars = fetch_range(args, symbol, args.interval, base_fetch_start, end)
    scoring_bars = [bar for bar in raw_bars if utc_timestamp(bar["time"]) >= start]
    if len(scoring_bars) < MINIMUM_BARS:
        return {
            "status": "skipped",
            "symbol": symbol,
            "reason": f"insufficient scoring bars: {len(scoring_bars)} < {MINIMUM_BARS}",
            "coverage": coverage(raw_bars, scoring_bars=len(scoring_bars)),
            "trades": [],
        }
    bars = calculate_indicators(raw_bars)
    if args.interval in {HIGHER_INTERVAL, "1d"}:
        higher = bars
    else:
        higher_start = start - pd.Timedelta(
            milliseconds=interval_ms(HIGHER_INTERVAL) * INDICATOR_WARMUP_BARS
        )
        raw_higher = fetch_range(args, symbol, HIGHER_INTERVAL, higher_start, end)
        if len(raw_higher) < 60:
            return {
                "status": "skipped",
                "symbol": symbol,
                "reason": f"insufficient higher-timeframe bars: {len(raw_higher)} < 60",
                "coverage": coverage(raw_bars, scoring_bars=len(scoring_bars)),
                "higher_coverage": coverage(raw_higher),
                "trades": [],
            }
        higher = calculate_indicators(raw_higher)
    enriched = add_higher_timeframe_context(bars, higher)
    symbol_vol_metrics, symbol_vol_coverage = build_symbol_vol_context(args, symbol, start, end)
    symbol_vol_bar_coverage = annotate_symbol_vol_context(enriched, symbol_vol_metrics)
    symbol_vol_coverage.update(symbol_vol_bar_coverage)
    entry_filter = symbol_vol_entry_filter(args)

    fit_trades, _fit_equity = run_backtest(
        enriched,
        args.reward_risk,
        args.max_hold_bars,
        args.fee_rate,
        args.stop_mode,
        divergence_filter=args.divergence_filter,
        entry_start_time=windows["fit_start"],
        entry_end_time=windows["fit_entry_end"],
        signal_direction=args.signal_direction,
        structure_text_exact=args.structure_text_exact,
        structure_text_contains=args.structure_text_contains,
        entry_filter=entry_filter,
    )
    fit_exit_end = windows["fit_exit_end"]
    fit_trades = [trade for trade in fit_trades if utc_timestamp(trade.exit_time) < fit_exit_end]
    # Recalculate because filtering a boundary-crossing trade changes the metrics.
    fit_metrics = calculate_metrics(
        fit_trades,
        build_equity_curve(enriched, fit_trades),
        bootstrap_iterations=0,
    )

    holdout_trades: list[Any] = []
    holdout_metrics: dict[str, Any] = {}
    if windows["holdout_start"] is not None:
        holdout_trades, _holdout_equity = run_backtest(
            enriched,
            args.reward_risk,
            args.max_hold_bars,
            args.fee_rate,
            args.stop_mode,
            divergence_filter=args.divergence_filter,
            entry_start_time=windows["holdout_start"],
            entry_end_time=windows["holdout_entry_end"],
            signal_direction=args.signal_direction,
            structure_text_exact=args.structure_text_exact,
            structure_text_contains=args.structure_text_contains,
            entry_filter=entry_filter,
        )
        holdout_exit_end = windows["holdout_exit_end"]
        holdout_trades = [
            trade for trade in holdout_trades if utc_timestamp(trade.exit_time) < holdout_exit_end
        ]
        holdout_metrics = calculate_metrics(
            holdout_trades,
            build_equity_curve(enriched, holdout_trades),
            bootstrap_iterations=0,
        )

    entry_metrics = entry_metric_lookup(enriched)
    trades = [_trade_record(item, symbol, "fit", entry_metrics) for item in fit_trades]
    trades.extend(_trade_record(item, symbol, "holdout", entry_metrics) for item in holdout_trades)
    return {
        "status": "completed",
        "symbol": symbol,
        "coverage": coverage(raw_bars, scoring_bars=len(scoring_bars)),
        "higher_coverage": coverage(higher),
        "symbol_vol_coverage": json_safe(symbol_vol_coverage),
        "fit_metrics": json_safe(fit_metrics),
        "holdout_metrics": json_safe(holdout_metrics),
        "trades": trades,
    }


def coverage(bars: list[dict[str, Any]], *, scoring_bars: int | None = None) -> dict[str, Any]:
    result = {
        "bars": len(bars),
        "first_time": json_safe(bars[0].get("time")) if bars else None,
        "last_time": json_safe(bars[-1].get("time")) if bars else None,
    }
    if scoring_bars is not None:
        result["scoring_bars"] = scoring_bars
        result["warmup_bars"] = max(0, len(bars) - scoring_bars)
    return result


def json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return utc_timestamp(value).isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def canonical_hash(value: Any) -> str:
    body = json.dumps(json_safe(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def strategy_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for name in ("strategy.py", "indicators.py", "project_signal_backtest.py", "position_manager.py"):
        path = root / name
        digest.update(name.encode("utf-8"))
        digest.update(path.read_bytes() if path.exists() else b"missing")
    return digest.hexdigest()


def argument_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {name: json_safe(getattr(args, name)) for name in sorted(RESEARCH_ARGUMENTS)}


def atomic_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(json_safe(value), ensure_ascii=False, indent=2, sort_keys=True))


def atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    atomic_write_text(path, frame.to_csv(index=False))


def symbol_path(run_dir: Path, symbol: str) -> Path:
    return run_dir / "symbols" / f"{SAFE_SYMBOL.sub('_', symbol)}.json"


def initial_checkpoint(
    run_id: str,
    args: argparse.Namespace,
    manifest: Mapping[str, Any],
    total_symbols: int,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "running",
        "created_at": now,
        "updated_at": now,
        "args": manifest["args"],
        "args_sha256": manifest["args_sha256"],
        "strategy_sha256": manifest["strategy_sha256"],
        "universe_sha256": manifest["universe_sha256"],
        "total_symbols": total_symbols,
        "completed_symbols": [],
        "skipped_symbols": [],
        "failures": {},
        "total_trades": 0,
        "symbol_trade_counts": {},
        "next_index": 0,
        "notification_state": {},
    }


def reconcile_checkpoint(run_dir: Path, checkpoint: dict[str, Any]) -> dict[str, Any]:
    completed: list[str] = []
    skipped: list[str] = []
    failures: dict[str, Any] = {}
    total_trades = 0
    symbol_trade_counts: dict[str, int] = {}
    for path in sorted((run_dir / "symbols").glob("*.json")):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        symbol = str(result.get("symbol") or "")
        status = result.get("status")
        if not symbol:
            continue
        if status == "completed":
            completed.append(symbol)
            trade_count = len(result.get("trades") or [])
            symbol_trade_counts[symbol] = trade_count
            total_trades += trade_count
        elif status == "skipped":
            skipped.append(symbol)
        elif status == "failed":
            failures[symbol] = {
                "attempts": int(result.get("attempts") or 0),
                "error": str(result.get("error") or "unknown"),
                "last_at": result.get("finished_at"),
            }
    checkpoint["completed_symbols"] = sorted(set(completed))
    checkpoint["skipped_symbols"] = sorted(set(skipped))
    checkpoint["failures"] = failures
    checkpoint["total_trades"] = total_trades
    checkpoint["symbol_trade_counts"] = symbol_trade_counts
    return checkpoint


def update_checkpoint_with_result(checkpoint: dict[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """Incrementally apply one durable symbol result to the in-memory checkpoint."""
    symbol = str(result.get("symbol") or "")
    if not symbol:
        return checkpoint

    completed = set(checkpoint.get("completed_symbols") or [])
    skipped = set(checkpoint.get("skipped_symbols") or [])
    failures = dict(checkpoint.get("failures") or {})
    trade_counts = {
        str(key): int(value or 0)
        for key, value in (checkpoint.get("symbol_trade_counts") or {}).items()
    }
    total_trades = int(checkpoint.get("total_trades") or 0) - trade_counts.pop(symbol, 0)

    completed.discard(symbol)
    skipped.discard(symbol)
    failures.pop(symbol, None)

    status = str(result.get("status") or "")
    if status == "completed":
        completed.add(symbol)
        trade_count = len(result.get("trades") or [])
        trade_counts[symbol] = trade_count
        total_trades += trade_count
    elif status == "skipped":
        skipped.add(symbol)
    elif status == "failed":
        failures[symbol] = {
            "attempts": int(result.get("attempts") or 0),
            "error": str(result.get("error") or "unknown"),
            "last_at": result.get("finished_at"),
        }

    checkpoint["completed_symbols"] = sorted(completed)
    checkpoint["skipped_symbols"] = sorted(skipped)
    checkpoint["failures"] = failures
    checkpoint["total_trades"] = max(0, total_trades)
    checkpoint["symbol_trade_counts"] = trade_counts
    return checkpoint


def write_checkpoint(run_dir: Path, checkpoint: dict[str, Any], notifier: BacktestNotifier | None = None) -> None:
    checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat()
    if notifier is not None:
        checkpoint["notification_state"] = notifier.snapshot()
    atomic_json(run_dir / "checkpoint.json", checkpoint)


def load_resume_args(parsed: argparse.Namespace) -> tuple[argparse.Namespace, Path, dict[str, Any], list[dict[str, Any]]]:
    supplied_research = {
        f"--{name.replace('_', '-')}"
        for name in RESEARCH_ARGUMENTS
        if f"--{name.replace('_', '-')}" in getattr(parsed, "_provided_options", set())
    }
    if supplied_research:
        names = ", ".join(sorted(supplied_research))
        raise ValueError(f"research parameters cannot override a resumed run: {names}")
    run_dir = parsed.resume.expanduser().resolve()
    manifest_path = run_dir / "manifest.json"
    checkpoint_path = run_dir / "checkpoint.json"
    universe_path = run_dir / "universe_snapshot.json"
    if not manifest_path.exists() or not checkpoint_path.exists() or not universe_path.exists():
        raise FileNotFoundError("resume directory is missing manifest/checkpoint/universe snapshot")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    universe = json.loads(universe_path.read_text(encoding="utf-8"))
    stored = manifest.get("args") or {}
    restored = vars(parsed).copy()
    restored.pop("_provided_options", None)
    restored.update(stored)
    restored["resume"] = run_dir
    restored["run_id"] = manifest["run_id"]
    restored["reports_dir"] = run_dir.parent
    restored["cache_dir"] = Path(restored["cache_dir"])
    args = argparse.Namespace(**restored)
    validate_args(args)
    if canonical_hash(argument_payload(args)) != manifest.get("args_sha256"):
        raise ValueError("resume arguments do not match manifest")
    if canonical_hash(universe) != manifest.get("universe_sha256"):
        raise ValueError("universe snapshot hash mismatch")
    current_strategy = strategy_hash(Path(__file__).resolve().parent)
    if current_strategy != manifest.get("strategy_sha256"):
        raise ValueError("strategy source changed; refusing incompatible resume")
    if checkpoint.get("args_sha256") != manifest.get("args_sha256"):
        raise ValueError("checkpoint/manifest argument hash mismatch")
    if checkpoint.get("strategy_sha256") != manifest.get("strategy_sha256"):
        raise ValueError("checkpoint/manifest strategy hash mismatch")
    if checkpoint.get("universe_sha256") != manifest.get("universe_sha256"):
        raise ValueError("checkpoint/manifest universe hash mismatch")
    if checkpoint.get("status") in {"completed", "no_data"}:
        raise ValueError("run is already completed")
    return args, run_dir, reconcile_checkpoint(run_dir, checkpoint), universe


def _frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records)
    for column in ("entry_time", "exit_time"):
        if column in frame:
            frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    return frame


def build_holdout_comparison(
    fit_stats: pd.DataFrame,
    holdout_frame: pd.DataFrame,
    min_group_trades: int,
) -> pd.DataFrame:
    if fit_stats.empty or holdout_frame.empty:
        return pd.DataFrame()
    holdout_stats, _ = build_exploratory_group_tables(holdout_frame, min_trades=1)
    if holdout_stats.empty:
        return pd.DataFrame()
    keys = ["group_dimensions", "group_value"]
    keep = keys + [
        "trades",
        "win_rate",
        "win_rate_ci_low",
        "win_rate_ci_high",
        "avg_return",
    ]
    holdout_stats = holdout_stats[keep].rename(
        columns={column: f"holdout_{column}" for column in keep if column not in keys}
    )
    merged = fit_stats.merge(holdout_stats, on=keys, how="left")
    merged["holdout_trades"] = merged["holdout_trades"].fillna(0).astype(int)
    minimum_holdout = max(5, min_group_trades // 3)
    merged["candidate_hypothesis"] = (
        (merged["avg_return"] > 0)
        & (merged["win_rate_ci_low"] >= 0.5)
        & (merged["holdout_trades"] >= minimum_holdout)
        & (merged["holdout_avg_return"] > 0)
        & (merged["holdout_win_rate"] >= 0.5)
    )
    return merged.sort_values(
        ["candidate_hypothesis", "avg_return", "trades"], ascending=[False, False, False]
    ).reset_index(drop=True)


def build_pair_protection_comparison(
    fit: pd.DataFrame,
    holdout: pd.DataFrame,
    min_fit_trades: int,
    max_exclusions: int,
) -> pd.DataFrame:
    """Evaluate a LowProfitPairs-style guard using only fit data to rank symbols."""
    if fit.empty or holdout.empty or max_exclusions <= 0:
        return pd.DataFrame()
    by_symbol = (
        fit.groupby("symbol", as_index=True)
        .agg(
            fit_trades=("return_pct", "size"),
            fit_avg_return=("return_pct", "mean"),
            fit_win_rate=("return_pct", lambda values: float((values > 0).mean())),
        )
        .reset_index()
    )
    eligible = by_symbol[by_symbol["fit_trades"] >= min_fit_trades].sort_values(
        ["fit_avg_return", "fit_trades"], ascending=[True, False]
    )
    if eligible.empty:
        return pd.DataFrame()

    baseline_avg = float(holdout["return_pct"].mean()) if not holdout.empty else 0.0
    steps = sorted({0, 1, 3, 5, 10, 15, 20, max_exclusions})
    rows: list[dict[str, Any]] = []
    for count in steps:
        count = min(count, max_exclusions, len(eligible))
        blocked = eligible.head(count)
        blocked_symbols = set(blocked["symbol"].astype(str))
        kept = holdout[~holdout["symbol"].astype(str).isin(blocked_symbols)]
        rows.append(
            {
                "excluded_symbols": count,
                "min_fit_trades": min_fit_trades,
                "blocked_symbols": ",".join(blocked["symbol"].astype(str).tolist()),
                "fit_blocked_avg_return": float(blocked["fit_avg_return"].mean()) if not blocked.empty else 0.0,
                "holdout_trades": int(len(kept)),
                "holdout_win_rate": float((kept["return_pct"] > 0).mean()) if not kept.empty else 0.0,
                "holdout_avg_return": float(kept["return_pct"].mean()) if not kept.empty else 0.0,
                "holdout_delta_vs_baseline": float(kept["return_pct"].mean() - baseline_avg) if not kept.empty else 0.0,
                "candidate_protection": bool(len(kept) >= max(30, len(holdout) // 3) and float(kept["return_pct"].mean()) > 0),
            }
        )
    return pd.DataFrame(rows).drop_duplicates("excluded_symbols").sort_values(
        ["candidate_protection", "holdout_avg_return", "holdout_trades"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def load_symbol_results(run_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    for path in sorted((run_dir / "symbols").glob("*.json")):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        results.append(result)
        trades.extend(result.get("trades") or [])
    return results, trades


def _html_table(frame: pd.DataFrame, limit: int = 50) -> str:
    if frame.empty:
        return "<p>无数据</p>"
    return frame.head(limit).to_html(index=False, border=0, classes="data", escape=True)


def build_report_html(
    summary: Mapping[str, Any],
    fit_stats: pd.DataFrame,
    comparison: pd.DataFrame,
    pair_protection: pd.DataFrame,
    failures: pd.DataFrame,
) -> str:
    summary_rows = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in summary.items()
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Universe Backtest {html.escape(str(summary.get('run_id', '')))}</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem;color:#18212f}}table{{border-collapse:collapse;width:100%;margin:1rem 0;font-size:13px}}th,td{{border:1px solid #dce2ea;padding:6px;text-align:left}}th{{background:#f4f7fa}}.note{{background:#fff7d6;padding:12px;border-radius:8px}}section{{margin-top:2rem;overflow:auto}}</style></head>
<body><h1>Universe 回测报告</h1><p class="note">本报告只生成候选假设；尚未通过正式 bootstrap、BH-FDR、永久留出集或跨交易所验证。</p>
<section><h2>运行摘要</h2><table>{summary_rows}</table></section>
<section><h2>Fit 探索分组</h2>{_html_table(fit_stats)}</section>
<section><h2>Holdout 对比</h2>{_html_table(comparison)}</section>
<section><h2>币种保护层 Holdout 对比</h2><p class="note">只使用 fit 期收益排序剔除低收益币，再评估 holdout；用于模拟 LowProfitPairs 风控，不代表直接晋级生产。</p>{_html_table(pair_protection)}</section>
<section><h2>失败币种</h2>{_html_table(failures)}</section></body></html>"""


def write_final_artifacts(
    run_dir: Path,
    args: argparse.Namespace,
    checkpoint: dict[str, Any],
    started_at: float,
) -> tuple[dict[str, Any], Path, int]:
    results, records = load_symbol_results(run_dir)
    trades = _frame(records)
    fit = trades[trades["sample"] == "fit"].copy() if not trades.empty else pd.DataFrame()
    holdout = trades[trades["sample"] == "holdout"].copy() if not trades.empty else pd.DataFrame()
    fit_stats, full_stats = build_exploratory_group_tables(fit, min_trades=args.min_group_trades)
    if not full_stats.empty:
        full_stats = full_stats.sort_values(["group_dimensions", "group_value"]).reset_index(drop=True)
    comparison = build_holdout_comparison(fit_stats, holdout, args.min_group_trades)
    candidates = comparison[comparison["candidate_hypothesis"]].copy() if not comparison.empty else pd.DataFrame()
    pair_protection = build_pair_protection_comparison(
        fit,
        holdout,
        args.pair_protection_min_fit_trades,
        args.pair_protection_max_exclusions,
    )
    protection_candidates = pair_protection[pair_protection["candidate_protection"]].copy() if not pair_protection.empty else pd.DataFrame()

    coverage_rows = []
    for result in results:
        item = {"symbol": result.get("symbol"), "status": result.get("status")}
        item.update(result.get("coverage") or {})
        for key, value in (result.get("symbol_vol_coverage") or {}).items():
            item[f"symbol_vol_{key}"] = value
        coverage_rows.append(item)
    failures = pd.DataFrame(
        [
            {"symbol": symbol, **details}
            for symbol, details in sorted((checkpoint.get("failures") or {}).items())
        ]
    )
    stable_files: dict[str, pd.DataFrame] = {
        "trades.csv": trades,
        "fit_group_stats.csv": fit_stats,
        "holdout_group_comparison.csv": comparison,
        "pair_protection_comparison.csv": pair_protection,
        "group_stats_full_8d.csv": full_stats,
        "data_coverage.csv": pd.DataFrame(coverage_rows),
        "failures.csv": failures,
    }
    for name, frame in stable_files.items():
        atomic_csv(run_dir / name, frame)

    if checkpoint["completed_symbols"]:
        final_status = "completed"
    elif checkpoint["failures"]:
        final_status = "failed"
    else:
        final_status = "no_data"
    summary = {
        "run_id": checkpoint["run_id"],
        "status": final_status,
        "exchange": args.exchange,
        "instrument_type": args.okx_instrument_type,
        "interval": args.interval,
        "divergence_filter": args.divergence_filter,
        "signal_direction": args.signal_direction,
        "structure_text_exact": args.structure_text_exact,
        "structure_text_contains": args.structure_text_contains,
        "symbol_vol_pctile_max": args.symbol_vol_pctile_max,
        "symbol_vol_window_days": args.symbol_vol_window_days,
        "symbol_vol_min_history_days": args.symbol_vol_min_history_days,
        "pair_protection_min_fit_trades": args.pair_protection_min_fit_trades,
        "pair_protection_max_exclusions": args.pair_protection_max_exclusions,
        "start": args.start,
        "end": args.end,
        "universe_size": checkpoint["total_symbols"],
        "completed_symbols": len(checkpoint["completed_symbols"]),
        "skipped_symbols": len(checkpoint["skipped_symbols"]),
        "failed_symbols": len(checkpoint["failures"]),
        "total_trades": len(trades),
        "fit_trades": len(fit),
        "holdout_trades": len(holdout),
        "overall_win_rate": float((trades["return_pct"] > 0).mean()) if not trades.empty else 0.0,
        "overall_avg_return": float(trades["return_pct"].mean()) if not trades.empty else 0.0,
        "candidate_hypotheses": len(candidates),
        "pair_protection_candidates": len(protection_candidates),
        "best_pair_protection_holdout_avg_return": float(protection_candidates["holdout_avg_return"].max()) if not protection_candidates.empty else 0.0,
        "elapsed_seconds": round(time.time() - started_at, 3),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "validation_status": "exploratory_only_bootstrap_fdr_pending",
    }
    atomic_json(run_dir / "candidate_hypotheses.json", candidates.to_dict("records"))
    atomic_json(run_dir / "summary.json", summary)

    report_path = run_dir / "report.html"
    atomic_write_text(report_path, build_report_html(summary, fit_stats, comparison, pair_protection, failures))
    hash_names = list(stable_files) + [
        "candidate_hypotheses.json",
        "summary.json",
        "report.html",
    ]
    hashes = {
        name: hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
        for name in hash_names
    }
    atomic_json(run_dir / "artifact_hashes.json", hashes)
    return summary, report_path, len(candidates)


def memory_pressure() -> tuple[int, int, float] | None:
    pairs = [
        (Path("/sys/fs/cgroup/memory.current"), Path("/sys/fs/cgroup/memory.max")),
        (Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"), Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")),
    ]
    for usage_path, limit_path in pairs:
        try:
            usage = int(usage_path.read_text().strip())
            limit_text = limit_path.read_text().strip()
            if limit_text == "max":
                continue
            limit = int(limit_text)
            if limit > 0:
                return usage, limit, usage / limit
        except (OSError, ValueError):
            continue
    return None


def _event(run_dir: Path, event: str, **fields: Any) -> None:
    try:
        append_jsonl(
            run_dir / "events.jsonl",
            {
                "time": time.time(),
                "source": "universe_signal_backtest",
                "event": event,
                **json_safe(fields),
            },
        )
    except Exception:
        pass


def _sigterm(_signum: int, _frame_value: Any) -> None:
    raise KeyboardInterrupt


def execute(args: argparse.Namespace) -> int:
    validate_args(args)
    root = Path(__file__).resolve().parent
    started_at = time.time()
    resumed = args.resume is not None
    if resumed:
        args, run_dir, checkpoint, universe = load_resume_args(args)
        start, end = utc_timestamp(args.start), utc_timestamp(args.end)
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    else:
        start, end = resolve_window(args)
        args.start, args.end = start.isoformat(), end.isoformat()
        run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
            raise ValueError("--run-id may contain only letters, numbers, dot, underscore, and dash")
        run_dir = args.reports_dir.expanduser().resolve() / f"universe_backtest_{run_id}"
        if run_dir.exists() and any(run_dir.iterdir()):
            raise FileExistsError(f"run directory already exists: {run_dir}")
        (run_dir / "symbols").mkdir(parents=True, exist_ok=True)
        universe = build_universe(args)
        if not universe:
            raise RuntimeError("universe is empty after market-cap and liquidity filters")
        universe_hash = canonical_hash(universe)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "args": argument_payload(args),
            "args_sha256": canonical_hash(argument_payload(args)),
            "strategy_sha256": strategy_hash(root),
            "universe_sha256": universe_hash,
            "purpose": "exploratory_hypothesis_generation_only",
        }
        atomic_json(run_dir / "manifest.json", manifest)
        atomic_json(run_dir / "universe_snapshot.json", universe)
        checkpoint = initial_checkpoint(run_id, args, manifest, len(universe))
        write_checkpoint(run_dir, checkpoint)

    windows = split_windows(
        start,
        end,
        args.holdout_fraction,
        args.embargo_bars,
        args.interval,
        args.max_hold_bars,
    )
    notifier = BacktestNotifier.from_env(
        manifest["run_id"], run_dir, checkpoint.get("notification_state") if resumed else None
    )
    checkpoint["status"] = "running"
    if resumed:
        notifier.send(
            "RESUMED",
            f"Completed: {len(checkpoint['completed_symbols'])}/{len(universe)}\n"
            f"Remaining: {len(universe) - len(processed_symbols(checkpoint))}\n"
            f"Failures: {len(checkpoint['failures'])}",
            force=True,
            dedupe_key=f"resume:{notifier.resume_count}",
        )
        _event(run_dir, "resumed", resume_count=notifier.resume_count)
    else:
        notifier.send(
            "STARTED",
            f"Symbols: {len(universe)}\nInterval: {args.interval}\n"
            f"Window: {args.start} to {args.end}\nExchange: {args.exchange}",
            force=True,
        )
        _event(run_dir, "started", symbols=len(universe), args=argument_payload(args))
    write_checkpoint(run_dir, checkpoint, notifier)

    consecutive_failures = 0
    try:
        for index, item in enumerate(universe):
            symbol = str(item["symbol"])
            if symbol in processed_symbols(checkpoint):
                continue
            attempts = 0
            result: dict[str, Any] | None = None
            while attempts < args.max_symbol_attempts:
                attempts += 1
                try:
                    result = run_one_symbol(args, symbol, start, end, windows)
                    break
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    _event(
                        run_dir,
                        "symbol_attempt_failed",
                        symbol=symbol,
                        attempt=attempts,
                        error=error,
                        traceback=traceback.format_exc(),
                    )
                    if "429" in error or "rate" in error.lower():
                        notifier.warning("exchange-rate-limit", f"Exchange rate limit detected while processing {symbol}.")
                    if attempts < args.max_symbol_attempts:
                        time.sleep(min(8.0, 2.0 ** (attempts - 1)))
            finished_at = datetime.now(timezone.utc).isoformat()
            if result is None:
                consecutive_failures += 1
                result = {
                    "status": "failed",
                    "symbol": symbol,
                    "attempts": attempts,
                    "error": error,
                    "finished_at": finished_at,
                    "trades": [],
                }
            else:
                consecutive_failures = 0
                result["attempts"] = attempts
                result["finished_at"] = finished_at
            atomic_json(symbol_path(run_dir, symbol), result)
            checkpoint = update_checkpoint_with_result(checkpoint, result)
            checkpoint["next_index"] = index + 1
            write_checkpoint(run_dir, checkpoint, notifier)

            if consecutive_failures >= 5:
                notifier.warning(
                    "five-consecutive-symbol-failures",
                    f"{consecutive_failures} consecutive symbols failed; the run is continuing.",
                )
            pressure = memory_pressure()
            if pressure and pressure[2] >= 0.85:
                notifier.warning(
                    "memory-85-percent",
                    f"Memory usage reached {pressure[2]:.0%} of the container limit.",
                    {"usage_bytes": pressure[0], "limit_bytes": pressure[1]},
                )
            notifier.progress(
                len(processed_symbols(checkpoint)),
                len(universe),
                f"Progress: {len(processed_symbols(checkpoint))}/{len(universe)}\n"
                f"Trades: {checkpoint['total_trades']}\nFailures: {len(checkpoint['failures'])}",
            )
            write_checkpoint(run_dir, checkpoint, notifier)
            print(
                f"[{len(processed_symbols(checkpoint))}/{len(universe)}] {symbol}: "
                f"{result['status']}, trades={len(result.get('trades') or [])}",
                flush=True,
            )
            if args.sleep_between_symbols:
                time.sleep(args.sleep_between_symbols)
    except KeyboardInterrupt:
        checkpoint["status"] = "interrupted"
        write_checkpoint(run_dir, checkpoint, notifier)
        notifier.send(
            "INTERRUPTED",
            f"Completed: {len(processed_symbols(checkpoint))}/{len(universe)}\n"
            f"Checkpoint: {run_dir / 'checkpoint.json'}\nResume with --resume {run_dir}",
            force=True,
            dedupe_key=f"interrupted:{notifier.resume_count}",
        )
        write_checkpoint(run_dir, checkpoint, notifier)
        _event(run_dir, "interrupted", completed=len(processed_symbols(checkpoint)))
        return 130
    except Exception as exc:
        checkpoint["status"] = "failed"
        checkpoint["fatal_error"] = f"{type(exc).__name__}: {exc}"
        write_checkpoint(run_dir, checkpoint, notifier)
        notifier.send("FAILED", checkpoint["fatal_error"], force=True)
        write_checkpoint(run_dir, checkpoint, notifier)
        _event(run_dir, "failed", error=checkpoint["fatal_error"])
        raise

    summary, report_path, candidate_count = write_final_artifacts(run_dir, args, checkpoint, started_at)
    checkpoint["status"] = summary["status"]
    checkpoint["next_index"] = len(universe)
    write_checkpoint(run_dir, checkpoint, notifier)
    report_url = notifier.build_report_url(report_path, args.reports_dir)
    if summary["status"] == "failed":
        notifier.send(
            "FAILED",
            f"No symbol completed successfully. Failures: {summary['failed_symbols']}\n"
            f"Report: {report_url or report_path}",
            force=True,
        )
        write_checkpoint(run_dir, checkpoint, notifier)
        _event(run_dir, "failed", summary=summary, report_url=report_url)
        return 1
    if summary["status"] == "no_data":
        notifier.send(
            "NO_DATA",
            f"All {summary['universe_size']} symbols were skipped; no usable data was available.\n"
            f"Report: {report_url or report_path}",
            force=True,
        )
        write_checkpoint(run_dir, checkpoint, notifier)
        _event(run_dir, "no_data", summary=summary, report_url=report_url)
        print(f"No usable data: {report_path}", flush=True)
        return 0
    if candidate_count:
        notifier.send(
            "VALIDATION",
            f"发现 {candidate_count} 个候选假设，尚需正式 bootstrap/FDR 验证。",
            force=True,
        )
    notifier.send(
        "COMPLETED",
        f"Symbols: {summary['completed_symbols']}/{summary['universe_size']}\n"
        f"Trades: {summary['total_trades']}\nWin rate: {summary['overall_win_rate']:.2%}\n"
        f"Expectancy: {summary['overall_avg_return']:.3%}\nReport: {report_url or report_path}",
        force=True,
    )
    write_checkpoint(run_dir, checkpoint, notifier)
    _event(run_dir, "completed", summary=summary, report_url=report_url)
    print(f"Completed: {report_path}", flush=True)
    return 0


def processed_symbols(checkpoint: Mapping[str, Any]) -> set[str]:
    return (
        set(checkpoint.get("completed_symbols") or [])
        | set(checkpoint.get("skipped_symbols") or [])
        | set((checkpoint.get("failures") or {}).keys())
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    previous = signal.signal(signal.SIGTERM, _sigterm)
    try:
        return execute(args)
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
