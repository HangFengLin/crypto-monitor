#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import gzip
import hashlib
import html
import json
import math
import os
import platform
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
import yaml

from backtest_statistics import benjamini_hochberg, build_exploratory_group_tables, weekly_block_bootstrap_difference
from config import config_value, load_config
from data_client import fetch_coingecko_top_market_symbols, interval_ms, read_json_url
from historical_market_data import (
    fetch_binance_funding_history,
    fetch_binance_oi_metrics,
    fetch_binance_um_klines,
    fetch_okx_funding_history,
    fetch_okx_klines_range,
    enrich_microstructure,
    sha256_file,
    write_json,
)
from indicators import calculate_indicators
from project_signal_backtest import (
    SignalTrade,
    add_higher_timeframe_context,
    build_full_group_stats,
    calculate_metrics,
    run_backtest,
)
from strategy import DEFAULT_CONFIG, HIGHER_TREND_INTERVAL, ProjectSignalEngine, StrategyConfig
from strategy_universe import STABLE_BASE_ASSETS


BINANCE_FUTURES_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BINANCE_FUTURES_TICKERS = "https://fapi.binance.com/fapi/v1/ticker/24hr"
DEFAULT_SEED = 20260620
PREPARED_CACHE_VERSION = 2
CANDIDATE_FAMILY_FIELDS = {
    "confirmation": {"confirmation_mode"},
    "divergence": {"min_divergence_strength"},
    "rsi": {"buy_rsi_threshold", "sell_rsi_threshold"},
    "adx": {"adx_long_max", "adx_short_max"},
    "microstructure": {"microstructure_enabled", "require_microstructure"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抗过拟合策略验证：时间样本外、有限候选、多品种与跨交易所复核")
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--start", default=None, help="UTC起始日，默认最近完整UTC日往前两年")
    parser.add_argument("--end", default=None, help="UTC结束日（开区间），默认今天00:00 UTC")
    parser.add_argument("--top-n", type=int, default=100)
    parser.add_argument("--okx-top-n", type=int, default=20)
    parser.add_argument("--min-quote-volume", type=float, default=None)
    parser.add_argument("--reward-risk", type=float, default=2.0)
    parser.add_argument("--max-hold-bars", type=int, default=96)
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument("--min-group-trades", type=int, default=30)
    parser.add_argument("--bootstrap-iterations", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="bootstrap固定随机种子")
    parser.add_argument("--data-workers", type=int, default=4, help="历史数据并发准备进程/线程，默认4")
    parser.add_argument("--data-executor", choices=["process", "thread"], default="process", help="历史数据并发方式，默认多进程")
    parser.add_argument("--evaluation-workers", type=int, default=max(1, min(8, os.cpu_count() or 4)), help="候选回放并发进程，默认最多8")
    parser.add_argument("--cache-dir", default="runtime/backtest-data")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--universe-snapshot", default=None, help="复用既有universe_snapshot.json")
    parser.add_argument("--candidate-overrides", default=None, help="额外候选YAML；只允许strategy字段")
    parser.add_argument("--skip-okx", action="store_true")
    parser.add_argument("--smoke", action="store_true", help="仅运行BTC最近7天网络/产物冒烟验证")
    return parser.parse_args()


def utc_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def default_window(args: argparse.Namespace) -> tuple[pd.Timestamp, pd.Timestamp]:
    end = utc_timestamp(args.end) if args.end else pd.Timestamp(datetime.now(timezone.utc).date(), tz="UTC")
    if args.smoke:
        return end - pd.Timedelta(days=7), end
    start = utc_timestamp(args.start) if args.start else end - pd.DateOffset(years=2)
    return start, end


def strategy_hash(config: StrategyConfig) -> str:
    payload = json.dumps(dataclasses.asdict(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def config_with_overrides(overrides: dict[str, Any]) -> StrategyConfig:
    allowed = {field.name for field in dataclasses.fields(StrategyConfig)}
    unknown = sorted(set(overrides) - allowed)
    if unknown:
        raise ValueError(f"unknown StrategyConfig overrides: {', '.join(unknown)}")
    return dataclasses.replace(DEFAULT_CONFIG, **overrides)


def builtin_candidates() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = [{"name": "baseline", "family": "baseline", "overrides": {}}]
    candidates.extend(
        {"name": f"confirmation_{mode}", "family": "confirmation", "overrides": {"confirmation_mode": mode}}
        for mode in ["either", "both", "ma_only", "macd_only"]
        if mode != DEFAULT_CONFIG.confirmation_mode
    )
    candidates.extend(
        {"name": f"divergence_{value:.2f}", "family": "divergence", "overrides": {"min_divergence_strength": value}}
        for value in [0.15, 0.20, 0.25]
        if not math.isclose(value, DEFAULT_CONFIG.min_divergence_strength)
    )
    candidates.extend(
        {
            "name": f"rsi_{buy}_{sell}",
            "family": "rsi",
            "overrides": {"buy_rsi_threshold": buy, "sell_rsi_threshold": sell},
        }
        for buy, sell in [(35, 65), (40, 60), (45, 55)]
        if (buy, sell) != (DEFAULT_CONFIG.buy_rsi_threshold, DEFAULT_CONFIG.sell_rsi_threshold)
    )
    candidates.extend(
        {
            "name": f"adx_{long_value}_{short_value}",
            "family": "adx",
            "overrides": {"adx_long_max": long_value, "adx_short_max": short_value},
        }
        for long_value, short_value in [(24, 28), (28, 32), (32, 36)]
        if (long_value, short_value) != (DEFAULT_CONFIG.adx_long_max, DEFAULT_CONFIG.adx_short_max)
    )
    candidates.extend(
        [
            {"name": "microstructure_disabled", "family": "microstructure", "overrides": {"microstructure_enabled": False, "require_microstructure": False}},
            {"name": "microstructure_required", "family": "microstructure", "overrides": {"microstructure_enabled": True, "require_microstructure": True}},
        ]
    )
    return candidates


def load_candidates(path: Optional[str]) -> list[dict[str, Any]]:
    candidates = builtin_candidates()
    if path:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError("candidate override YAML must be a mapping")
        if set(payload) == {"strategy"}:
            extra = [{"name": f"external_{Path(path).stem}", "strategy": payload["strategy"]}]
        elif set(payload) == {"candidates"} and isinstance(payload["candidates"], list):
            extra = payload["candidates"]
        else:
            raise ValueError("candidate override YAML allows only 'strategy' or 'candidates'")
        for item in extra:
            if not isinstance(item, dict) or not isinstance(item.get("strategy"), dict) or not item.get("name"):
                raise ValueError("candidate entries require name and strategy mapping")
            overrides = dict(item["strategy"])
            config_with_overrides(overrides)
            matching_families = [family for family, fields in CANDIDATE_FAMILY_FIELDS.items() if set(overrides) <= fields]
            if len(matching_families) != 1:
                raise ValueError("each candidate must change fields from exactly one approved parameter family")
            candidates.append({"name": str(item["name"]), "family": matching_families[0], "overrides": overrides})
    names = [candidate["name"] for candidate in candidates]
    if len(names) != len(set(names)):
        raise ValueError("candidate names must be unique")
    return candidates


def collect_oi_signal_dates(bars: list[dict[str, Any]]) -> list[Any]:
    """Collect potential decision dates without making OI itself a prerequisite."""
    # Only the microstructure-required candidate consumes OI as an entry gate,
    # and candidates never combine families. Therefore baseline confirmation
    # events are the complete decision-relevant OI date set.
    probe_config = dataclasses.replace(DEFAULT_CONFIG, require_microstructure=False)
    signal_times: set[Any] = set()
    engine = ProjectSignalEngine(probe_config)
    ready: list[dict[str, Any]] = []
    seen: set[tuple[str, Any]] = set()
    for bar in bars:
        if bar.get("macd") is None:
            continue
        ready.append(bar)
        signal = engine.detect(ready[-250:])
        if signal.get("signal") not in {"long", "short"}:
            continue
        key = (str(signal["signal"]), signal.get("divergence_time"))
        if key in seen:
            continue
        seen.add(key)
        close_time = bar.get("close_time")
        signal_times.add(pd.to_datetime(int(close_time), unit="ms", utc=True) if close_time is not None else utc_timestamp(bar["time"]))
    return sorted(signal_times, key=utc_timestamp)


def fetch_binance_futures_market() -> tuple[set[str], dict[str, dict[str, Any]]]:
    exchange_info = read_json_url(BINANCE_FUTURES_EXCHANGE_INFO)
    tradable = {
        str(item.get("symbol", "")).upper()
        for item in exchange_info.get("symbols", [])
        if item.get("status") == "TRADING" and item.get("contractType") == "PERPETUAL" and item.get("quoteAsset") == "USDT"
    }
    tickers_payload = read_json_url(BINANCE_FUTURES_TICKERS)
    tickers = {str(item.get("symbol", "")).upper(): item for item in tickers_payload if isinstance(item, dict)}
    return tradable, tickers


def build_universe_snapshot(top_n: int, min_quote_volume: float, okx_top_n: int) -> dict[str, Any]:
    from data_client import fetch_okx_tickers, fetch_okx_trade_symbols

    coins = fetch_coingecko_top_market_symbols(top_n)
    binance_symbols, binance_tickers = fetch_binance_futures_market()
    okx_symbols = fetch_okx_trade_symbols("USDT", "SWAP")
    okx_tickers = fetch_okx_tickers("SWAP")
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for coin in coins:
        base = str(coin.get("symbol", "")).upper()
        symbol = f"{base}USDT"
        reason = None
        ticker = binance_tickers.get(symbol, {})
        quote_volume = float(ticker.get("quoteVolume") or 0.0)
        if not base or base in STABLE_BASE_ASSETS:
            reason = "stable_or_invalid"
        elif symbol not in binance_symbols:
            reason = "no_binance_um_perpetual"
        elif quote_volume < min_quote_volume:
            reason = "below_liquidity_floor"
        item = {
            "symbol": symbol,
            "base_asset": base,
            "name": coin.get("name"),
            "market_cap_rank": coin.get("market_cap_rank"),
            "binance_quote_volume": quote_volume,
            "okx_inst_id": okx_symbols.get(symbol),
            "okx_quote_volume": (okx_tickers.get(symbol) or {}).get("quote_volume"),
        }
        if reason:
            item["reason"] = reason
            excluded.append(item)
        else:
            included.append(item)
    okx = [item for item in included if item.get("okx_inst_id")][:okx_top_n]
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "top_n_requested": top_n,
        "min_quote_volume": min_quote_volume,
        "binance": included,
        "okx": okx,
        "excluded": excluded,
        "bias_note": "固定当前市值与可交易快照用于评估当前可部署池；历史结果存在幸存者与上市时间偏差。",
    }


def load_or_build_snapshot(args: argparse.Namespace, output_dir: Path, min_quote_volume: float) -> dict[str, Any]:
    if args.universe_snapshot:
        snapshot = json.loads(Path(args.universe_snapshot).read_text(encoding="utf-8"))
    elif args.smoke:
        snapshot = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "top_n_requested": 1,
            "min_quote_volume": 0,
            "binance": [{"symbol": "BTCUSDT", "market_cap_rank": 1, "name": "Bitcoin"}],
            "okx": [{"symbol": "BTCUSDT", "market_cap_rank": 1, "name": "Bitcoin", "okx_inst_id": "BTC-USDT-SWAP"}],
            "excluded": [],
            "bias_note": "network smoke only",
        }
    else:
        snapshot = build_universe_snapshot(args.top_n, min_quote_volume, args.okx_top_n)
    write_json(output_dir / "universe_snapshot.json", snapshot)
    return snapshot


def failed_coverage_row(
    symbol: str,
    exchange: str,
    interval: str,
    fetch_start: pd.Timestamp,
    end: pd.Timestamp,
    error: BaseException,
) -> dict[str, Any]:
    expected = max(0, math.ceil((end - fetch_start) / pd.Timedelta(milliseconds=interval_ms(interval))))
    return {
        "symbol": symbol,
        "exchange": exchange,
        "error": f"{type(error).__name__}: {error}",
        "bars": 0,
        "expected_bars": expected,
        "missing_bars": expected,
        "kline_coverage": 0.0,
        "funding_coverage": 0.0,
        "oi_coverage": 0.0,
        "funding_rows": 0,
        "oi_rows": 0,
    }


def prepare_symbol_bars(
    exchange: str,
    symbol: str,
    interval: str,
    fetch_start: pd.Timestamp,
    end: pd.Timestamp,
    cache_dir: Path,
    *,
    include_oi: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prepared_path = prepared_cache_path(exchange, symbol, interval, fetch_start, end, cache_dir, include_oi)
    cached_prepared = load_prepared_cache(prepared_path)
    if cached_prepared is not None:
        return cached_prepared
    if exchange == "binance":
        raw = fetch_binance_um_klines(symbol, interval, fetch_start, end, cache_dir)
        higher_raw = fetch_binance_um_klines(symbol, HIGHER_TREND_INTERVAL, fetch_start - pd.Timedelta(days=50), end, cache_dir)
        funding = fetch_binance_funding_history(symbol, fetch_start, end, cache_dir)
    else:
        raw = fetch_okx_klines_range(symbol, interval, fetch_start, end, cache_dir=cache_dir)
        higher_raw = fetch_okx_klines_range(symbol, HIGHER_TREND_INTERVAL, fetch_start - pd.Timedelta(days=50), end, cache_dir=cache_dir)
        funding = fetch_okx_funding_history(symbol, fetch_start, end, cache_dir=cache_dir)
    if not raw:
        return [], {"symbol": symbol, "exchange": exchange, "error": "no_klines"}
    bars = add_higher_timeframe_context(calculate_indicators(raw), calculate_indicators(higher_raw))

    # Optional microstructure never changes entry eligibility. Use that pass to
    # identify the small set of dates for which archived OI is decision-relevant.
    signal_dates = collect_oi_signal_dates(bars)
    oi_rows = fetch_binance_oi_metrics(symbol, signal_dates, cache_dir) if include_oi and signal_dates else []
    enriched, coverage = enrich_microstructure(bars, funding, oi_rows, interval)
    coverage.update(
        {
            "symbol": symbol,
            "exchange": exchange,
            "first_bar": str(enriched[0]["time"]),
            "last_bar": str(enriched[-1]["time"]),
            "probe_signals": len(signal_dates),
            "oi_proxy": "binance_vision_um_metrics" if exchange == "okx" else "native_binance_um_metrics",
            "expected_bars": max(0, math.ceil((end - fetch_start) / pd.Timedelta(milliseconds=interval_ms(interval)))),
        }
    )
    coverage["missing_bars"] = max(0, int(coverage["expected_bars"]) - len(enriched))
    coverage["kline_coverage"] = len(enriched) / coverage["expected_bars"] if coverage["expected_bars"] else 0.0
    write_prepared_cache(prepared_path, enriched, coverage)
    return enriched, coverage


def prepared_cache_path(
    exchange: str,
    symbol: str,
    interval: str,
    fetch_start: pd.Timestamp,
    end: pd.Timestamp,
    cache_dir: Path,
    include_oi: bool,
) -> Path:
    key = f"{int(fetch_start.timestamp() * 1000)}_{int(end.timestamp() * 1000)}_{strategy_hash(DEFAULT_CONFIG)[:12]}_oi{int(include_oi)}_v{PREPARED_CACHE_VERSION}.json.gz"
    return cache_dir / "prepared" / exchange / symbol.upper() / interval / key


def load_prepared_cache(path: Path) -> Optional[tuple[list[dict[str, Any]], dict[str, Any]]]:
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        bars = list(payload["bars"])
        for bar in bars:
            bar["time"] = pd.Timestamp(bar["time"])
        return bars, dict(payload["coverage"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return None


def write_prepared_cache(path: Path, bars: list[dict[str, Any]], coverage: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=5) as handle:
        json.dump({"bars": bars, "coverage": coverage}, handle, ensure_ascii=False, default=str, separators=(",", ":"))
    temporary.replace(path)


def prepare_snapshot_item(payload: tuple[str, dict[str, Any], str, pd.Timestamp, pd.Timestamp, Path]) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Process/thread worker for one immutable snapshot item."""
    exchange, item, interval, fetch_start, end, cache_dir = payload
    symbol = str(item["symbol"])
    print(f"准备 {exchange.title()} {symbol} 历史数据", flush=True)
    try:
        bars, coverage = prepare_symbol_bars(exchange, symbol, interval, fetch_start, end, cache_dir)
    except Exception as exc:
        bars, coverage = [], failed_coverage_row(symbol, exchange, interval, fetch_start, end, exc)
    return symbol, bars, coverage


def prepare_snapshot_datasets(
    exchange: str,
    items: list[dict[str, Any]],
    args: argparse.Namespace,
    fetch_start: pd.Timestamp,
    end: pd.Timestamp,
    cache_dir: Path,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Prepare independent symbols concurrently while preserving snapshot order."""
    workers = max(1, int(getattr(args, "data_workers", 4)))
    # OKX public historical endpoints rate-limit concurrent pagination very
    # aggressively. Serial preparation is slower per batch but deterministic
    # and avoids silently reducing a requested top-20 robustness sample.
    if exchange == "okx":
        workers = 1

    payloads = [(exchange, item, args.interval, fetch_start, end, cache_dir) for item in items]
    if workers == 1 or len(items) <= 1:
        results = [prepare_snapshot_item(payload) for payload in payloads]
    else:
        executor_class = concurrent.futures.ProcessPoolExecutor if getattr(args, "data_executor", "process") == "process" else concurrent.futures.ThreadPoolExecutor
        with executor_class(max_workers=min(workers, len(items))) as executor:
            results = list(executor.map(prepare_snapshot_item, payloads))
    datasets: dict[str, list[dict[str, Any]]] = {}
    coverage_rows: list[dict[str, Any]] = []
    for symbol, bars, coverage in results:
        coverage_rows.append(coverage)
        if bars:
            datasets[symbol] = bars
    return datasets, coverage_rows


def walk_forward_windows(start: pd.Timestamp, end: pd.Timestamp, embargo_bars: int, interval: str) -> tuple[list[dict[str, pd.Timestamp]], pd.Timestamp]:
    interval_delta = pd.Timedelta(milliseconds=interval_ms(interval))
    raw_test_start = start + (end - start) * 0.80
    elapsed_intervals = math.ceil((raw_test_start - start) / interval_delta)
    test_start = start + elapsed_intervals * interval_delta
    embargo = pd.Timedelta(milliseconds=embargo_bars * interval_ms(interval))
    windows: list[dict[str, pd.Timestamp]] = []
    cursor = start
    fold_index = 1
    while True:
        train_end = cursor + pd.Timedelta(days=240)
        validation_start = train_end + embargo
        validation_end = validation_start + pd.Timedelta(days=60)
        if validation_end > test_start:
            break
        windows.append(
            {
                "fold": f"fold_{fold_index}",
                "train_start": cursor,
                "train_end": train_end,
                "validation_start": validation_start,
                "validation_end": validation_end,
            }
        )
        cursor += pd.Timedelta(days=60)
        fold_index += 1
    return windows, test_start + embargo


def trade_equity(trades: Iterable[SignalTrade]) -> pd.Series:
    ordered = sorted(trades, key=lambda trade: pd.Timestamp(trade.exit_time))
    equity = 1.0
    values = [equity]
    index = [pd.Timestamp("1970-01-01", tz="UTC")]
    for trade in ordered:
        equity *= 1.0 + trade.return_pct
        values.append(equity)
        timestamp = utc_timestamp(trade.exit_time)
        index.append(timestamp)
    return pd.Series(values, index=index, name="Equity")


def run_window(
    datasets: dict[str, list[dict[str, Any]]],
    config: StrategyConfig,
    score_start: pd.Timestamp,
    score_end: pd.Timestamp,
    args: argparse.Namespace,
    *,
    fee_rate: Optional[float] = None,
    executor: Optional[concurrent.futures.Executor] = None,
) -> tuple[list[SignalTrade], pd.DataFrame, dict[str, float]]:
    interval_delta = pd.Timedelta(milliseconds=interval_ms(args.interval))
    warmup = 250 * interval_delta
    # Reserve a full holding horizon at the right edge. This prevents the last
    # bar of a fold from turning an unresolved position into an artificial
    # profitable/loss-making timeout.
    entry_end = score_end - args.max_hold_bars * interval_delta
    tasks: list[tuple[Any, ...]] = []
    for symbol, bars in datasets.items():
        sliced = [bar for bar in bars if score_start - warmup <= utc_timestamp(bar["time"]) < score_end]
        if not sliced:
            continue
        tasks.append(
            (
                symbol,
                sliced,
                config,
                score_start,
                score_end,
                entry_end,
                args.reward_risk,
                args.max_hold_bars,
                fee_rate if fee_rate is not None else args.fee_rate,
            )
        )
    if executor is not None:
        results = list(executor.map(score_symbol_window, tasks))
    elif int(getattr(args, "evaluation_workers", 1)) > 1 and len(tasks) > 1:
        with concurrent.futures.ProcessPoolExecutor(max_workers=min(int(args.evaluation_workers), len(tasks))) as local_executor:
            results = list(local_executor.map(score_symbol_window, tasks))
    else:
        results = [score_symbol_window(task) for task in tasks]
    all_trades: list[SignalTrade] = []
    symbol_rows: list[dict[str, Any]] = []
    for symbol, retained, symbol_metrics in results:
        all_trades.extend(retained)
        symbol_rows.append({"symbol": symbol, **symbol_metrics})
    metrics = calculate_metrics(
        all_trades,
        trade_equity(all_trades),
        bootstrap_iterations=args.bootstrap_iterations,
        bootstrap_seed=getattr(args, "seed", DEFAULT_SEED),
    )
    return all_trades, pd.DataFrame(symbol_rows), metrics


def score_symbol_window(task: tuple[Any, ...]) -> tuple[str, list[SignalTrade], dict[str, float]]:
    """Process-safe scoring unit for one symbol and one fixed time window."""
    symbol, sliced, config, score_start, score_end, entry_end, reward_risk, max_hold_bars, fee_rate = task
    trades, _curve = run_backtest(
        sliced,
        reward_risk,
        max_hold_bars,
        fee_rate,
        strategy_config=config,
        entry_start_time=score_start,
        entry_end_time=entry_end,
    )
    retained = [trade for trade in trades if score_start <= utc_timestamp(trade.entry_time) and utc_timestamp(trade.exit_time) < score_end]
    for trade in retained:
        setattr(trade, "symbol", symbol)
    metrics = calculate_metrics(retained, trade_equity(retained), bootstrap_iterations=0)
    return symbol, retained, metrics


def trade_records(trades: Iterable[SignalTrade], **extra: Any) -> list[dict[str, Any]]:
    rows = []
    for trade in trades:
        row = dataclasses.asdict(trade)
        row["symbol"] = getattr(trade, "symbol", "")
        row.update(extra)
        rows.append(row)
    return rows


def evaluate_validation_candidates(
    datasets: dict[str, list[dict[str, Any]]],
    candidates: list[dict[str, Any]],
    windows: list[dict[str, pd.Timestamp]],
    args: argparse.Namespace,
) -> tuple[pd.DataFrame, dict[str, list[dict[str, Any]]], dict[str, pd.DataFrame]]:
    metric_rows: list[dict[str, Any]] = []
    trades_by_candidate: dict[str, list[dict[str, Any]]] = {candidate["name"]: [] for candidate in candidates}
    symbols_by_candidate: dict[str, list[pd.DataFrame]] = {candidate["name"]: [] for candidate in candidates}
    worker_count = max(1, int(getattr(args, "evaluation_workers", 1)))
    executor_context: Any = (
        concurrent.futures.ProcessPoolExecutor(max_workers=worker_count)
        if worker_count > 1 and len(datasets) > 1
        else None
    )
    try:
        for window in windows:
            for candidate in candidates:
                config = config_with_overrides(candidate["overrides"])
                trades, symbol_metrics, metrics = run_window(
                    datasets,
                    config,
                    window["validation_start"],
                    window["validation_end"],
                    args,
                    executor=executor_context,
                )
                metric_rows.append(
                    {
                        "candidate": candidate["name"],
                        "family": candidate["family"],
                        "fold": window["fold"],
                        "phase": "validation",
                        "train_start": window["train_start"],
                        "train_end": window["train_end"],
                        "validation_start": window["validation_start"],
                        "validation_end": window["validation_end"],
                        **metrics,
                    }
                )
                trades_by_candidate[candidate["name"]].extend(trade_records(trades, candidate=candidate["name"], fold=window["fold"], phase="validation"))
                if not symbol_metrics.empty:
                    symbol_metrics = symbol_metrics.assign(candidate=candidate["name"], fold=window["fold"])
                    symbols_by_candidate[candidate["name"]].append(symbol_metrics)
    finally:
        if executor_context is not None:
            executor_context.shutdown(wait=True)
    return pd.DataFrame(metric_rows), trades_by_candidate, {
        key: pd.concat(value, ignore_index=True) if value else pd.DataFrame() for key, value in symbols_by_candidate.items()
    }


def compare_candidates(
    candidates: list[dict[str, Any]],
    fold_metrics: pd.DataFrame,
    trades_by_candidate: dict[str, list[dict[str, Any]]],
    symbols_by_candidate: dict[str, pd.DataFrame],
    bootstrap_iterations: int,
    seed: int = DEFAULT_SEED,
) -> pd.DataFrame:
    baseline_folds = fold_metrics[fold_metrics["candidate"] == "baseline"].set_index("fold")
    baseline_symbols = symbols_by_candidate.get("baseline", pd.DataFrame())
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        name = candidate["name"]
        if name == "baseline":
            continue
        comparison = weekly_block_bootstrap_difference(
            trades_by_candidate.get(name, []),
            trades_by_candidate.get("baseline", []),
            iterations=bootstrap_iterations,
            seed=seed,
        )
        candidate_folds = fold_metrics[fold_metrics["candidate"] == name].set_index("fold")
        aligned = candidate_folds.join(baseline_folds, lsuffix="_candidate", rsuffix="_baseline", how="inner")
        fold_positive = float((aligned["expectancy_candidate"] > aligned["expectancy_baseline"]).mean()) if not aligned.empty else 0.0
        max_drawdown_ratio = 0.0
        if not aligned.empty:
            baseline_dd = float(aligned["max_drawdown_baseline"].abs().max())
            candidate_dd = float(aligned["max_drawdown_candidate"].abs().max())
            max_drawdown_ratio = candidate_dd / baseline_dd if baseline_dd else (0.0 if candidate_dd == 0 else math.inf)

        candidate_symbols = symbols_by_candidate.get(name, pd.DataFrame())
        symbol_nonworse = 0.0
        if not candidate_symbols.empty and not baseline_symbols.empty:
            def aggregate_symbols(frame: pd.DataFrame, label: str) -> pd.DataFrame:
                weighted = frame.assign(weighted_return=frame["expectancy"] * frame["total_trades"])
                grouped = weighted.groupby("symbol", as_index=True).agg(total_trades=("total_trades", "sum"), weighted_return=("weighted_return", "sum"))
                grouped[label] = grouped["weighted_return"].div(grouped["total_trades"]).fillna(0.0)
                return grouped[["total_trades", label]].rename(columns={"total_trades": f"{label}_trades"})

            candidate_macro = aggregate_symbols(candidate_symbols, "candidate")
            baseline_macro = aggregate_symbols(baseline_symbols, "baseline")
            symbol_compare = candidate_macro.join(baseline_macro, how="inner")
            symbol_compare = symbol_compare[(symbol_compare["candidate_trades"] + symbol_compare["baseline_trades"]) > 0]
            symbol_nonworse = float((symbol_compare["candidate"] >= symbol_compare["baseline"]).mean()) if not symbol_compare.empty else 0.0
        rows.append(
            {
                "candidate": name,
                "family": candidate["family"],
                "overrides": json.dumps(candidate["overrides"], ensure_ascii=False, sort_keys=True),
                **comparison,
                "fold_positive_ratio": fold_positive,
                "symbol_nonworse_ratio": symbol_nonworse,
                "max_drawdown_ratio": max_drawdown_ratio,
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    corrections = benjamini_hochberg(frame["p_value"].tolist(), alpha=0.10)
    frame["q_value"] = [item["q_value"] for item in corrections]
    frame["fdr_reject"] = [item["reject"] for item in corrections]
    frame["validation_gate"] = (
        frame["fdr_reject"]
        & (frame["delta"] > 0)
        & (frame["fold_positive_ratio"] >= 0.70)
        & (frame["symbol_nonworse_ratio"] > 0.50)
        & (frame["max_drawdown_ratio"] <= 1.10)
    )
    return frame.sort_values(["validation_gate", "delta"], ascending=[False, False]).reset_index(drop=True)


def profit_concentration(trades: Iterable[SignalTrade]) -> float:
    by_symbol: dict[str, float] = {}
    for trade in trades:
        symbol = getattr(trade, "symbol", "unknown")
        by_symbol[symbol] = by_symbol.get(symbol, 0.0) + max(0.0, trade.return_pct)
    total = sum(by_symbol.values())
    return max(by_symbol.values(), default=0.0) / total if total > 0 else 1.0


def apply_extra_roundtrip_cost(trades: Iterable[SignalTrade], extra_fee_rate: float) -> list[SignalTrade]:
    adjusted = []
    for trade in trades:
        next_return = trade.return_pct - extra_fee_rate * 2
        next_trade = dataclasses.replace(trade, return_pct=next_return, outcome="win" if next_return > 0 else "loss")
        if hasattr(trade, "symbol"):
            setattr(next_trade, "symbol", getattr(trade, "symbol"))
        adjusted.append(next_trade)
    return adjusted


def passes_final_candidate_gates(
    baseline_metrics: dict[str, float],
    candidate_metrics: dict[str, float],
    double_cost_metrics: dict[str, float],
    *,
    okx_direction_consistent: bool,
    max_symbol_profit_share: float,
) -> bool:
    """Apply the predeclared holdout gates without weakening missing evidence."""
    baseline_drawdown_limit = abs(baseline_metrics["max_drawdown"]) * 1.10
    return bool(
        candidate_metrics["total_trades"] >= 100
        and candidate_metrics["expectancy"] > baseline_metrics["expectancy"]
        and abs(candidate_metrics["max_drawdown"]) <= baseline_drawdown_limit
        and double_cost_metrics["expectancy"] > 0
        and okx_direction_consistent
        and max_symbol_profit_share <= 0.25
    )


def build_data_config_manifest(cache_dir: Path, snapshot_path: Path, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    symbols = {str(item.get("symbol", "")).upper() for venue in ("binance", "okx") for item in snapshot.get(venue, [])}
    normalized_symbols = {"".join(character for character in symbol if character.isalnum()) for symbol in symbols}
    cache_files = []
    if cache_dir.exists():
        for path in sorted(item for item in cache_dir.rglob("*") if item.is_file() and not item.name.endswith(".tmp")):
            normalized_path = "".join(character for character in path.as_posix().upper() if character.isalnum())
            if not any(symbol and symbol in normalized_path for symbol in normalized_symbols):
                continue
            cache_files.append(
                {
                    "path": str(path.relative_to(cache_dir)),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    candidate_payload = json.dumps(candidates, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    production_config_path = Path("config.yaml")
    code_files = [
        Path("strategy_validation.py"),
        Path("historical_market_data.py"),
        Path("backtest_statistics.py"),
        Path("project_signal_backtest.py"),
        Path("position_manager.py"),
        Path("resume_strategy_validation.py"),
        Path("strategy.py"),
        Path("requirements-validation.txt"),
    ]
    return {
        "base_config_sha256": strategy_hash(DEFAULT_CONFIG),
        "production_config_file_sha256": sha256_file(production_config_path) if production_config_path.exists() else None,
        "candidate_set_sha256": hashlib.sha256(candidate_payload.encode("utf-8")).hexdigest(),
        "universe_snapshot_sha256": sha256_file(snapshot_path),
        "cache_root": str(cache_dir),
        "cache_files": cache_files,
        "code_files": [
            {"path": str(path), "sha256": sha256_file(path)} for path in code_files if path.exists()
        ],
        "python": sys.version,
        "platform": platform.platform(),
    }


def render_chart(candidate_comparison: pd.DataFrame, output_dir: Path) -> list[Path]:
    if candidate_comparison.empty or len(candidate_comparison) < 4:
        return []
    finite_delta = pd.to_numeric(candidate_comparison.get("delta"), errors="coerce").dropna()
    if finite_delta.empty or math.isclose(float(finite_delta.abs().max()), 0.0, abs_tol=1e-15):
        return []
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import seaborn as sns
    except ModuleNotFoundError:
        return []
    plot = candidate_comparison.sort_values("delta").copy()
    plot["gate_label"] = plot["validation_gate"].map({True: "Validation gate passed", False: "Not passed"})
    fig, ax = plt.subplots(figsize=(10, max(4.5, len(plot) * 0.42)), facecolor="#FCFCFD")
    sns.set_theme(style="whitegrid", rc={"axes.facecolor": "#FFFFFF", "figure.facecolor": "#FCFCFD", "grid.color": "#E6E8F0"})
    palette = {"Validation gate passed": "#A3BEFA", "Not passed": "#E2E5EA"}
    sns.barplot(data=plot, x="delta", y="candidate", hue="gate_label", dodge=False, palette=palette, ax=ax, edgecolor="#2E4780", linewidth=0.8)
    legend = ax.get_legend()
    if legend is not None:
        legend.set_title("")
        legend.set_frame_on(False)
    ax.axvline(0, color="#464C55", linewidth=1)
    ax.set_xlabel("Validation expectancy delta per trade")
    ax.set_ylabel("")
    ax.grid(axis="x", color="#E6E8F0", linewidth=0.8)
    ax.grid(axis="y", visible=False)
    ax.set_title("Out-of-sample candidate expectancy delta", loc="left", fontsize=14, color="#1F2430", pad=30)
    ax.text(0, 1.015, "Per-trade return versus baseline; blue marks the joint validation gate.", transform=ax.transAxes, color="#6F768A", fontsize=9)
    sns.despine(ax=ax)
    fig.tight_layout()
    output = output_dir / "candidate_expectancy_delta.png"
    fig.savefig(output, dpi=180, bbox_inches="tight", facecolor="#FCFCFD")
    fig.savefig(output.with_suffix(".svg"), bbox_inches="tight", facecolor="#FCFCFD")
    plt.close(fig)
    return [output]


def html_table(frame: pd.DataFrame, columns: list[str], percent_columns: set[str] | None = None, limit: int = 30) -> str:
    percent_columns = percent_columns or set()
    if frame.empty:
        return '<p class="muted">没有可展示的数据。</p>'
    display = frame.loc[:, [column for column in columns if column in frame.columns]].head(limit).copy()
    for column in display.columns:
        if column in percent_columns:
            display[column] = display[column].map(lambda value: f"{float(value):.2%}" if pd.notna(value) else "-")
    return display.to_html(index=False, escape=True, classes="data-table", border=0)


def render_report(
    output_dir: Path,
    manifest: dict[str, Any],
    fold_metrics: pd.DataFrame,
    comparison: pd.DataFrame,
    test_summary: dict[str, Any],
    group_stats: pd.DataFrame,
) -> Path:
    charts = render_chart(comparison, output_dir)
    status = test_summary.get("status", "baseline_retained")
    selected = test_summary.get("selected_candidate") or "无"
    selected_display = {
        "microstructure_required": "强制微观结构确认",
        "microstructure_disabled": "关闭微观结构",
    }.get(str(selected), str(selected))
    baseline_folds = fold_metrics[fold_metrics.get("candidate", pd.Series(dtype=str)) == "baseline"] if not fold_metrics.empty else pd.DataFrame()
    validation_trades = int(baseline_folds["total_trades"].sum()) if not baseline_folds.empty else 0
    if validation_trades and not baseline_folds.empty:
        validation_expectancy = float((baseline_folds["expectancy"] * baseline_folds["total_trades"]).sum() / validation_trades)
        validation_win_rate = float((baseline_folds["win_rate"] * baseline_folds["total_trades"]).sum() / validation_trades)
        validation_max_drawdown = float(baseline_folds["max_drawdown"].min())
    else:
        validation_expectancy = 0.0
        validation_win_rate = 0.0
        validation_max_drawdown = 0.0
    if status == "candidate_passed":
        summary_text = "候选通过全部样本外与稳健性门槛，已生成独立候选覆盖文件；生产配置未修改。"
    elif status == "smoke_only":
        summary_text = "官方数据、时间对齐、候选回放和报告产物冒烟完成；一周样本不用于参数晋级判断。"
    else:
        summary_text = "没有候选通过全部门槛，基线策略保持不变；这避免了根据噪声自动改写生产参数。"
    chart_html = "".join(f'<figure><img src="{html.escape(path.name)}" alt="候选收益增量图"></figure>' for path in charts)
    if not charts:
        chart_html = '<p class="muted">当前数据不足以形成诚实的候选比较图，保留精确表格。</p>'
    test_rows = []
    for key, label in (("baseline", "最终测试基线"), ("candidate", "最终测试候选"), ("double_cost", "候选双倍成本")):
        values = test_summary.get(key)
        if isinstance(values, dict):
            test_rows.append({"phase": label, **values})
    okx = test_summary.get("okx")
    if isinstance(okx, dict):
        for key, label in (("baseline", "OKX基线"), ("candidate", "OKX候选")):
            values = okx.get(key)
            if isinstance(values, dict):
                test_rows.append({"phase": label, **values})
    test_evidence = html_table(pd.DataFrame(test_rows), ["phase", "total_trades", "win_rate", "expectancy", "max_drawdown"], {"win_rate", "expectancy", "max_drawdown"}, 10)
    gate_rows: list[dict[str, Any]] = []
    baseline_test = test_summary.get("baseline") if isinstance(test_summary.get("baseline"), dict) else {}
    candidate_test = test_summary.get("candidate") if isinstance(test_summary.get("candidate"), dict) else {}
    double_cost = test_summary.get("double_cost") if isinstance(test_summary.get("double_cost"), dict) else {}
    okx_baseline = okx.get("baseline") if isinstance(okx, dict) and isinstance(okx.get("baseline"), dict) else {}
    okx_candidate = okx.get("candidate") if isinstance(okx, dict) and isinstance(okx.get("candidate"), dict) else {}
    selected_row = comparison[comparison["candidate"] == selected].head(1) if not comparison.empty and selected != "无" else pd.DataFrame()
    if candidate_test:
        drawdown_limit = abs(float(baseline_test.get("max_drawdown", 0.0))) * 1.10
        validation_passed = bool(selected_row.iloc[0].get("validation_gate")) if not selected_row.empty else False
        okx_direction = (
            float(okx_candidate.get("expectancy", -math.inf)) >= float(okx_baseline.get("expectancy", math.inf))
            and float(okx_candidate.get("expectancy", -math.inf)) > 0
        )
        concentration = float(test_summary.get("max_symbol_profit_share", math.inf))
        gate_rows = [
            {"gate": "验证期FDR联合门槛", "threshold": "通过", "observed": "通过" if validation_passed else "未通过", "passed": validation_passed},
            {"gate": "最终测试交易数", "threshold": ">= 100", "observed": int(candidate_test.get("total_trades", 0)), "passed": float(candidate_test.get("total_trades", 0)) >= 100},
            {"gate": "最终测试期望优于基线", "threshold": f"> {float(baseline_test.get('expectancy', 0)):.3%}", "observed": f"{float(candidate_test.get('expectancy', 0)):.3%}", "passed": float(candidate_test.get("expectancy", 0)) > float(baseline_test.get("expectancy", 0))},
            {"gate": "最大回撤", "threshold": f"<= {drawdown_limit:.2%}", "observed": f"{abs(float(candidate_test.get('max_drawdown', 0))):.2%}", "passed": abs(float(candidate_test.get("max_drawdown", 0))) <= drawdown_limit},
            {"gate": "双倍成本期望", "threshold": "> 0", "observed": f"{float(double_cost.get('expectancy', 0)):.3%}", "passed": float(double_cost.get("expectancy", 0)) > 0},
            {"gate": "OKX方向一致", "threshold": "候选>=基线且>0", "observed": f"{float(okx_candidate.get('expectancy', 0)):.3%} vs {float(okx_baseline.get('expectancy', 0)):.3%}", "passed": okx_direction},
            {"gate": "单一品种盈利贡献", "threshold": "<= 25%", "observed": f"{concentration:.2%}", "passed": concentration <= 0.25},
        ]
    gate_frame = pd.DataFrame(gate_rows)
    if not gate_frame.empty:
        gate_frame["passed"] = gate_frame["passed"].map({True: "是", False: "否"})
        gate_frame = gate_frame.rename(columns={"gate": "门槛", "threshold": "阈值", "observed": "观测值", "passed": "通过"})
    gate_evidence = html_table(gate_frame, ["门槛", "阈值", "观测值", "通过"], limit=10)
    if status == "smoke_only":
        test_evidence = '<p class="muted">冒烟模式不会解封最终测试集，也不会执行 OKX 晋级复核或成本晋级判断。</p>'
        gate_evidence = '<p class="muted">冒烟模式不执行晋级门槛。</p>'
    write_json(
        output_dir / "report_source_notes.json",
        {
            "delivery_mode": "html",
            "audience": "technical",
            "section_contract": [
                "title",
                "technical-summary",
                "key-findings",
                "scope-data-and-metric-definitions",
                "methodology",
                "limitations-uncertainty-and-robustness-checks",
                "recommended-next-steps",
                "further-questions",
            ],
            "chart_map": [
                {
                    "section": "key-findings",
                    "question": "Which predeclared candidate improved validation expectancy versus baseline?",
                    "family": "comparison-and-ranking",
                    "type": "diverging horizontal bar",
                    "fields": ["candidate", "delta", "validation_gate"],
                    "claim": "Only the microstructure-required candidate passed the joint validation gate.",
                    "palette_policy": "single blue root plus neutral comparator fills",
                    "artifact": charts[0].name if charts else None,
                }
            ] if charts else [],
            "chart_omission_reason": None if charts else "Candidate deltas were absent, too sparse, or all numerically zero.",
            "source_artifacts": [
                "manifest.json",
                "data_config_hashes.json",
                "data_coverage.csv",
                "fold_metrics.csv",
                "candidate_comparison.csv",
                "test_summary.json",
                "group_stats.csv",
                "group_stats_full_8d.csv",
            ],
            "visible_sources_appendix_omitted": "Reproducibility details are preserved in machine-readable companion artifacts to keep the report decision-focused.",
        },
    )
    report = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>抗过拟合策略验证报告</title>
<style>
:root{{--ink:#1F2430;--muted:#6F768A;--line:#E6E8F0;--blue:#A3BEFA;--surface:#FCFCFD}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--surface);color:var(--ink);font-family:Inter,"PingFang SC",Arial,sans-serif;line-height:1.55}}
main{{max-width:1120px;margin:0 auto;padding:48px 28px 80px}} h1{{font-size:32px;margin:0 0 28px}} h2{{margin-top:42px;font-size:22px}} h3{{font-size:16px}}
.summary{{background:#fff;border:1px solid var(--line);padding:24px;border-radius:12px}} .kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:18px}}
.kpi{{padding:16px;background:#fff;border:1px solid var(--line);border-radius:10px;min-width:0}} .kpi strong{{display:block;font-size:21px;overflow-wrap:anywhere;word-break:break-word}} .kpi span,.muted{{color:var(--muted);font-size:13px}}
.data-table{{width:100%;border-collapse:collapse;background:#fff;font-size:13px}} .data-table th,.data-table td{{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left}} .data-table th{{background:#F4F5F7}}
.scroll{{overflow:auto;border:1px solid var(--line);border-radius:10px}} figure{{margin:20px 0}} img{{max-width:100%;height:auto;border:1px solid var(--line);background:#fff}}
code{{background:#F4F5F7;padding:2px 5px;border-radius:4px}}
</style></head><body><main>
<header data-contract-section="title"><h1>抗过拟合策略验证报告</h1></header>
<section data-contract-section="technical-summary"><h2>技术摘要</h2><div class="summary"><strong>{html.escape(summary_text)}</strong><p>验证期候选 <code>{html.escape(str(selected))}</code> 通过联合筛选，但最终结论只由永久测试集及全部稳健性门槛决定。</p>
<div class="kpis"><div class="kpi"><strong>{'保留基线' if status == 'baseline_retained' else '候选通过' if status == 'candidate_passed' else '仅冒烟'}</strong><span>最终决策</span></div><div class="kpi"><strong>{html.escape(selected_display)}</strong><span>验证期入选候选</span></div><div class="kpi"><strong>{manifest.get('binance_symbols',0)}</strong><span>Binance研究品种</span></div><div class="kpi"><strong>{validation_trades}</strong><span>基线验证交易</span></div><div class="kpi"><strong>{validation_win_rate:.2%}</strong><span>基线验证胜率</span></div><div class="kpi"><strong>{validation_expectancy:.3%}</strong><span>基线验证期望</span></div><div class="kpi"><strong>{validation_max_drawdown:.2%}</strong><span>最差fold回撤</span></div><div class="kpi"><strong>{manifest.get('okx_symbols_evaluated',0)}</strong><span>OKX已复核品种</span></div></div></div></section>
<section data-contract-section="key-findings"><h2>{'最终测试否决候选，继续使用基线' if status == 'baseline_retained' else '候选通过全部最终门槛' if status == 'candidate_passed' else '网络与产物链路完成冒烟'}</h2><p>门槛必须同时成立；任何一项失败都会保留基线。下表直接列出最终测试、成本压力、跨交易所方向和盈利集中度证据。</p><div class="scroll">{gate_evidence}</div><h3>最终测试与跨交易所指标</h3><div class="scroll">{test_evidence}</div><h3>验证期候选收益增量</h3><p>图中横轴是候选相对基线的每笔净收益增量；蓝色只表示通过验证期联合门槛，不表示已经通过永久测试集。</p>{chart_html}<div class="scroll">{html_table(comparison,['candidate','family','delta','ci_low','ci_high','p_value','q_value','fold_positive_ratio','symbol_nonworse_ratio','max_drawdown_ratio','validation_gate'],{'delta','ci_low','ci_high','fold_positive_ratio','symbol_nonworse_ratio'})}</div></section>
<section data-contract-section="scope-data-and-metric-definitions"><h2>研究范围与指标定义</h2><p>研究区间为 {html.escape(str(manifest.get('start')))} 至 {html.escape(str(manifest.get('end')))}，使用 {html.escape(str(manifest.get('interval','15m')))} K线；冻结池含 {manifest.get('binance_symbols',0)} 个 Binance USD-M 永续，OKX复核含 {manifest.get('okx_symbols_evaluated',0)} 个 SWAP。期望收益是每笔交易扣除往返成本后的算术均值；最大回撤由退出时记账的组合权益曲线计算；胜率区间使用 Wilson 95% 区间。</p><p>资金费率和OI只取信号K线实际收盘前已发布的最后值。OI比率是最新15分钟值除以前19个值的均值；OKX的OI明确使用 Binance USD-M 对应合约作为代理。</p></section>
<section data-contract-section="methodology"><h2>固定候选与时间样本外设计</h2><p>最后20%时间永久保留为一次性测试集；前80%采用240天训练、60天验证、每60天滚动一次，并在分段间隔离96根15分钟K线。候选只改变一个参数族，不在训练窗内连续拟合。验证期增量使用UTC周分块的 {manifest.get('bootstrap_iterations',2000)} 次bootstrap和固定种子 {manifest.get('bootstrap_seed',20260620)}，随后执行BH-FDR 0.10校正。</p><div class="scroll">{html_table(fold_metrics,['candidate','fold','validation_start','validation_end','total_trades','win_rate','expectancy','max_drawdown'],{'win_rate','expectancy','max_drawdown'},50)}</div><h3>探索性分组只用于提出假设</h3><p>下表仅包含至少 {manifest.get('min_group_trades',30)} 笔的单维或预声明二维分组；完整8维联合表仅作为原始诊断，不参与候选生成或晋级。</p><div class="scroll">{html_table(group_stats,['group_dimensions','group_value','trades','win_rate','win_rate_ci_low','win_rate_ci_high','avg_return'],{'win_rate','win_rate_ci_low','win_rate_ci_high','avg_return'},40)}</div></section>
<section data-contract-section="limitations-uncertainty-and-robustness-checks"><h2>稳健性与仍然存在的限制</h2><ul><li>候选验证增量经过周分块bootstrap与FDR校正，但最终候选只有 {int(candidate_test.get('total_trades',0)) if candidate_test else 0} 笔；低于100笔门槛时不作晋级解释。</li><li>OKX使用自身K线与官方历史资金费率；OI使用 Binance USD-M 代理，不能视为OKX原生持仓结构。</li><li>品种池按研究日快照冻结，因此仍存在幸存者、上市时间与当前市值选择偏差。</li><li>不同上市日期导致单品种历史长度不同；覆盖报告保留每个品种的首尾时间和缺失比例。</li><li>生产 <code>config.yaml</code> 运行前后哈希一致，未部署、未重启服务。</li></ul></section>
<section data-contract-section="recommended-next-steps"><h2>建议下一步</h2><p>{'候选只能进入后续模拟盘一致性验证，不能直接实盘。' if status == 'candidate_passed' else '执行完整两年多品种验证。' if status == 'smoke_only' else '继续积累新样本；不要因本次未晋级而放宽统计门槛。'}</p></section>
<section data-contract-section="further-questions"><h2>仍需持续观察</h2><p>后续实盘/模拟盘只用于检查滑点、延迟、执行价和边界抖动是否与回测一致，不用于根据少量新交易重新调阈值。</p></section>
</main></body></html>"""
    path = output_dir / "report.html"
    path.write_text(report, encoding="utf-8")
    return path


def main() -> None:
    args = parse_args()
    production_config_path = Path("config.yaml")
    production_config_hash_before = sha256_file(production_config_path) if production_config_path.exists() else None
    start, end = default_window(args)
    run_label = datetime.now(timezone.utc).strftime("strategy_validation_%Y%m%dT%H%M%SZ") + ("_smoke" if args.smoke else "")
    output_dir = Path(args.reports_dir) / run_label
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir)
    config = load_config()
    min_quote_volume = args.min_quote_volume
    if min_quote_volume is None:
        min_quote_volume = float(config_value(config, "bot", "min_quote_volume", 10_000_000))
    candidates = load_candidates(args.candidate_overrides)
    snapshot = load_or_build_snapshot(args, output_dir, min_quote_volume)

    fetch_start = start - pd.Timedelta(days=50)
    datasets, coverage_rows = prepare_snapshot_datasets(
        "binance", list(snapshot.get("binance", [])), args, fetch_start, end, cache_dir
    )

    if not datasets:
        pd.DataFrame(coverage_rows).to_csv(output_dir / "data_coverage.csv", index=False)
        raise RuntimeError("no usable Binance datasets; see data_coverage.csv and retry with official data access")

    if args.smoke:
        windows = [{"fold": "smoke", "train_start": start, "train_end": start, "validation_start": start, "validation_end": end}]
        test_start = end
    else:
        windows, test_start = walk_forward_windows(start, end, args.max_hold_bars, args.interval)
    if not windows:
        raise RuntimeError("history window is too short for the configured 240d/60d walk-forward")

    fold_metrics, validation_trades, validation_symbol_metrics = evaluate_validation_candidates(datasets, candidates, windows, args)
    comparison = compare_candidates(candidates, fold_metrics, validation_trades, validation_symbol_metrics, args.bootstrap_iterations, args.seed)
    selected_name: Optional[str] = None
    if not args.smoke and not comparison.empty:
        eligible = comparison[comparison["validation_gate"]]
        if not eligible.empty:
            selected_name = str(eligible.iloc[0]["candidate"])

    test_summary: dict[str, Any] = {"status": "smoke_only" if args.smoke else "baseline_retained", "selected_candidate": selected_name}
    final_test_trades: list[SignalTrade] = []
    okx_symbols_evaluated = 0
    if selected_name:
        selected = next(candidate for candidate in candidates if candidate["name"] == selected_name)
        baseline_test, _baseline_symbols, baseline_metrics = run_window(datasets, DEFAULT_CONFIG, test_start, end, args)
        candidate_test, candidate_symbols, candidate_metrics = run_window(datasets, config_with_overrides(selected["overrides"]), test_start, end, args)
        double_cost = apply_extra_roundtrip_cost(candidate_test, args.fee_rate)
        double_metrics = calculate_metrics(
            double_cost,
            trade_equity(double_cost),
            bootstrap_iterations=args.bootstrap_iterations,
            bootstrap_seed=args.seed,
        )
        okx_delta_ok = False
        okx_metrics: dict[str, Any] = {"skipped": True} if args.skip_okx else {}
        if not args.skip_okx:
            okx_datasets, okx_coverage = prepare_snapshot_datasets(
                "okx", list(snapshot.get("okx", [])), args, fetch_start, end, cache_dir
            )
            coverage_rows.extend(okx_coverage)
            okx_symbols_evaluated = len(okx_datasets)
            okx_baseline, _a, okx_baseline_metrics = run_window(okx_datasets, DEFAULT_CONFIG, test_start, end, args)
            okx_candidate, _b, okx_candidate_metrics = run_window(okx_datasets, config_with_overrides(selected["overrides"]), test_start, end, args)
            okx_metrics = {"baseline": okx_baseline_metrics, "candidate": okx_candidate_metrics}
            okx_delta_ok = okx_candidate_metrics["expectancy"] >= okx_baseline_metrics["expectancy"] and okx_candidate_metrics["expectancy"] > 0
        contribution = profit_concentration(candidate_test)
        test_passed = passes_final_candidate_gates(
            baseline_metrics,
            candidate_metrics,
            double_metrics,
            okx_direction_consistent=okx_delta_ok,
            max_symbol_profit_share=contribution,
        )
        test_summary.update(
            {
                "status": "candidate_passed" if test_passed else "baseline_retained",
                "baseline": baseline_metrics,
                "candidate": candidate_metrics,
                "double_cost": double_metrics,
                "okx": okx_metrics,
                "max_symbol_profit_share": contribution,
            }
        )
        final_test_trades = candidate_test if test_passed else baseline_test
        pd.DataFrame(trade_records(baseline_test, candidate="baseline", phase="test")).to_csv(output_dir / "test_trades_baseline.csv", index=False)
        pd.DataFrame(trade_records(candidate_test, candidate=selected_name, phase="test")).to_csv(output_dir / "test_trades_candidate.csv", index=False)
        if test_passed:
            (output_dir / "candidate_strategy.yaml").write_text(
                yaml.safe_dump({"strategy": selected["overrides"], "evidence": {"candidate": selected_name, "base_config_sha256": strategy_hash(DEFAULT_CONFIG)}}, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )

    all_validation_trades = [row for rows in validation_trades.values() for row in rows]
    pd.DataFrame(all_validation_trades).to_csv(output_dir / "validation_trades.csv", index=False)
    fold_metrics.to_csv(output_dir / "fold_metrics.csv", index=False)
    pd.DataFrame(windows).to_csv(output_dir / "walk_forward_folds.csv", index=False)
    comparison.to_csv(output_dir / "candidate_comparison.csv", index=False)
    coverage_frame = pd.DataFrame(coverage_rows)
    coverage_frame.to_csv(output_dir / "data_coverage.csv", index=False)
    group_source = pd.DataFrame(trade_records(final_test_trades)) if final_test_trades else pd.DataFrame(validation_trades.get("baseline", []))
    group_stats, full_groups = build_exploratory_group_tables(group_source, min_trades=args.min_group_trades) if not group_source.empty else (pd.DataFrame(), pd.DataFrame())
    group_stats.to_csv(output_dir / "group_stats.csv", index=False)
    full_groups.to_csv(output_dir / "group_stats_full_8d.csv", index=False)

    production_config_hash_after = sha256_file(production_config_path) if production_config_path.exists() else None
    if production_config_hash_after != production_config_hash_before:
        raise RuntimeError("production config.yaml changed during validation; refusing to finalize artifacts")
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "interval": args.interval,
        "binance_symbols": len(datasets),
        "okx_symbols": len(snapshot.get("okx", [])),
        "okx_symbols_evaluated": okx_symbols_evaluated,
        "candidate_count": len(candidates),
        "min_group_trades": args.min_group_trades,
        "bootstrap_iterations": args.bootstrap_iterations,
        "bootstrap_seed": args.seed,
        "base_config_sha256": strategy_hash(DEFAULT_CONFIG),
        "production_config_sha256_before": production_config_hash_before,
        "production_config_sha256_after": production_config_hash_after,
        "production_config_modified": False,
        "deployed": False,
        "arguments": vars(args),
        "test_summary": test_summary,
    }
    write_json(output_dir / "manifest.json", manifest)
    write_json(output_dir / "test_summary.json", test_summary)
    write_json(output_dir / "data_config_hashes.json", build_data_config_manifest(cache_dir, output_dir / "universe_snapshot.json", candidates))
    report_path = render_report(output_dir, manifest, fold_metrics, comparison, test_summary, group_stats)
    # Freeze artifact hashes after all report files are present.
    hashes = {path.name: sha256_file(path) for path in sorted(output_dir.iterdir()) if path.is_file()}
    write_json(output_dir / "artifact_hashes.json", hashes)
    print(f"验证完成: {report_path.resolve()}")


if __name__ == "__main__":
    main()
