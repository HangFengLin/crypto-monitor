"""Offline VectorBT research accelerator for cached ProjectSignalEngine outcomes."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import inspect
import itertools
import json
import math
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from data_client import interval_ms
from project_signal_backtest import SignalTrade, evaluate_trade, run_backtest, signal_passes_trade_filters
from strategy import DEFAULT_CONFIG, ProjectSignalEngine, StrategyConfig


@dataclass(frozen=True)
class ResearchCandidate:
    name: str
    min_signal_score: int
    min_structure_score: int
    divergence_filter: str
    signal_direction: str
    block_local_countertrend: bool


@dataclass(frozen=True)
class SignalEvent:
    signal_index: int
    entry_index: int
    exit_index: int
    signal: dict[str, Any]
    entry_bar: dict[str, Any]
    trade: SignalTrade


def load_prepared_bars(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read an immutable prepared-cache artifact without repairing or deleting it."""
    cache_path = Path(path)
    try:
        with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        bars = list(payload["bars"])
        coverage = dict(payload["coverage"])
        for bar in bars:
            bar["time"] = pd.Timestamp(bar["time"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid prepared cache: {cache_path}") from exc
    return bars, coverage


def research_boundaries(
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    interval: str,
    max_hold_bars: int,
    research_fraction: float,
) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    """Reserve the final time slice and a full holding horizon from research."""
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    if end <= start:
        raise ValueError("research end must be after start")
    if not 0 < research_fraction < 1:
        raise ValueError("research_fraction must be between 0 and 1")
    if max_hold_bars < 0:
        raise ValueError("max_hold_bars must be non-negative")

    step = pd.Timedelta(milliseconds=interval_ms(interval))
    raw_holdout_start = start + (end - start) * research_fraction
    elapsed_steps = math.ceil((raw_holdout_start - start) / step)
    holdout_start = start + elapsed_steps * step
    entry_end = holdout_start - max_hold_bars * step
    if entry_end <= start:
        raise ValueError("research window is shorter than the holding-horizon embargo")
    return start, entry_end, holdout_start


def trade_sequence_parity(
    reference: list[SignalTrade],
    accelerated: list[SignalTrade],
) -> dict[str, Any]:
    """Compare the execution fields that define project backtest parity."""
    comparable_fields = (
        "signal",
        "entry_time",
        "entry_price",
        "initial_stop_loss",
        "target_price",
        "exit_time",
        "exit_price",
        "exit_reason",
        "return_pct",
        "bars_held",
    )
    first_mismatch: int | None = None
    differing_fields: list[str] = []
    for index, (expected, observed) in enumerate(zip(reference, accelerated)):
        differences = []
        for field in comparable_fields:
            expected_value = getattr(expected, field)
            observed_value = getattr(observed, field)
            if isinstance(expected_value, float) or isinstance(observed_value, float):
                try:
                    equal = math.isclose(float(expected_value), float(observed_value), rel_tol=1e-12, abs_tol=1e-12)
                except (TypeError, ValueError):
                    equal = expected_value == observed_value
            else:
                equal = expected_value == observed_value
            if not equal:
                differences.append(field)
        if differences:
            first_mismatch = index
            differing_fields = differences
            break
    if first_mismatch is None and len(reference) != len(accelerated):
        first_mismatch = min(len(reference), len(accelerated))
        differing_fields = ["trade_count"]
    return {
        "passed": first_mismatch is None,
        "reference_count": len(reference),
        "accelerated_count": len(accelerated),
        "first_mismatch_index": first_mismatch,
        "differing_fields": differing_fields,
    }


def write_research_artifacts(
    output_dir: str | Path,
    *,
    metrics: pd.DataFrame,
    trades_by_candidate: dict[str, list[SignalTrade]],
    manifest: dict[str, Any],
) -> Path:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=False)
    payload = dict(manifest)
    payload["status"] = "RESEARCH_ONLY"
    (path / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    metrics.to_csv(path / "candidate_results.csv", index=False)

    trade_rows = []
    for candidate, trades in trades_by_candidate.items():
        for item in trades:
            trade_rows.append({"candidate": candidate, **asdict(item)})
    pd.DataFrame(trade_rows, columns=["candidate", *SignalTrade.__dataclass_fields__]).to_csv(
        path / "candidate_trades.csv",
        index=False,
    )

    parity = payload.get("parity", {})
    minimum = int(payload.get("minimum_ranking_trades", 30))
    eligible = metrics[metrics["total_trades"] >= minimum] if not metrics.empty else metrics
    ranked = eligible.sort_values(["expectancy", "total_return"], ascending=False).head(10)
    lines = [
        "# VectorBT 高速研究 PoC",
        "",
        "> 状态：仅限研究。结果不能修改生产配置、授权下单或替代永久留出集验证。",
        "",
        f"- 逐笔一致性：{'PASS' if parity.get('passed') else 'FAIL'}",
        f"- 候选数量：{len(metrics)}",
        f"- 信号事件：{payload.get('signal_events', 0)}",
        f"- 排名最低交易数：{minimum}",
        "",
        "## 候选摘要",
        "",
    ]
    if ranked.empty:
        lines.append("没有候选达到最低交易数，只保留完整 CSV 供检查。")
    else:
        lines.extend(
            [
                "| candidate | trades | win_rate | expectancy | total_return | max_drawdown |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in ranked.itertuples(index=False):
            lines.append(
                f"| {row.candidate} | {int(row.total_trades)} | {float(row.win_rate):.2%} | "
                f"{float(row.expectancy):.3%} | {float(row.total_return):.3%} | {float(row.max_drawdown):.3%} |"
            )
    lines.extend(
        [
            "",
            "## 性能",
            "",
            f"- 生产基线单次回放：{float(payload.get('timings_seconds', {}).get('reference', 0.0)):.3f} 秒",
            f"- 信号与生命周期缓存：{float(payload.get('timings_seconds', {}).get('signal_cache', 0.0)):.3f} 秒",
            f"- 全部候选选择：{float(payload.get('timings_seconds', {}).get('candidate_selection', 0.0)):.3f} 秒",
            f"- VectorBT 批量统计：{float(payload.get('timings_seconds', {}).get('vectorbt', 0.0)):.3f} 秒",
            "",
        ]
    )
    (path / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return path


def run_vectorbt_poc(argv: list[str] | None = None) -> Path:
    """Run an offline, post-signal VectorBT screen against one prepared cache."""
    parser = argparse.ArgumentParser(description="VectorBT PoC：缓存一次生产信号，再批量筛选不改变信号生成的研究过滤器")
    parser.add_argument("--prepared-cache", required=True, help="strategy_validation 生成的 prepared .json.gz")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--reward-risk", type=float, default=2.0)
    parser.add_argument("--max-hold-bars", type=int, default=96)
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument(
        "--stop-mode",
        choices=["structure_atr", "structure_atr_breakeven_after_1r", "atr_trailing_after_1r"],
        default="structure_atr",
    )
    parser.add_argument("--research-fraction", type=float, default=0.80)
    parser.add_argument("--minimum-ranking-trades", type=int, default=30)
    parser.add_argument("--min-signal-scores", default="0,7,10")
    parser.add_argument("--min-structure-scores", default="0,1,2")
    parser.add_argument("--divergence-filters", default="all,macd,fast_macd")
    parser.add_argument("--signal-directions", default="all,long,short")
    parser.add_argument("--countertrend-grid", choices=["off", "on", "both"], default="both")
    args = parser.parse_args(argv)

    def csv_values(raw: str) -> list[str]:
        values = [value.strip() for value in raw.split(",") if value.strip()]
        if not values:
            parser.error("candidate grids cannot be empty")
        return values

    signal_scores = [int(value) for value in csv_values(args.min_signal_scores)]
    structure_scores = [int(value) for value in csv_values(args.min_structure_scores)]
    divergences = csv_values(args.divergence_filters)
    directions = csv_values(args.signal_directions)
    if not set(divergences) <= {"all", "macd", "fast_macd"}:
        parser.error("divergence filters must be all, macd, or fast_macd")
    if not set(directions) <= {"all", "long", "short"}:
        parser.error("signal directions must be all, long, or short")
    countertrends = {
        "off": [False],
        "on": [True],
        "both": [False, True],
    }[args.countertrend_grid]
    candidates = build_candidate_grid(
        min_signal_scores=signal_scores,
        min_structure_scores=structure_scores,
        divergence_filters=divergences,
        signal_directions=directions,
        block_local_countertrends=countertrends,
    )

    cache_path = Path(args.prepared_cache).resolve()
    bars, coverage = load_prepared_bars(cache_path)
    if len(bars) < 2:
        raise ValueError("prepared cache must contain at least two bars")
    times = [pd.Timestamp(bar["time"]) for bar in bars]
    data_start = min(times)
    data_end = max(times) + pd.Timedelta(milliseconds=interval_ms(args.interval))
    entry_start, entry_end, holdout_start = research_boundaries(
        data_start,
        data_end,
        interval=args.interval,
        max_hold_bars=args.max_hold_bars,
        research_fraction=args.research_fraction,
    )

    reference_started = time.perf_counter()
    reference_trades, _curve = run_backtest(
        bars,
        args.reward_risk,
        args.max_hold_bars,
        args.fee_rate,
        args.stop_mode,
        entry_start_time=entry_start,
        entry_end_time=entry_end,
    )
    reference_seconds = time.perf_counter() - reference_started

    cache_started = time.perf_counter()
    events = extract_signal_events(
        bars,
        reward_risk=args.reward_risk,
        max_hold_bars=args.max_hold_bars,
        fee_rate=args.fee_rate,
        stop_mode=args.stop_mode,
        entry_start_time=entry_start,
        entry_end_time=entry_end,
    )
    signal_cache_seconds = time.perf_counter() - cache_started

    baseline = ResearchCandidate(
        name="baseline",
        min_signal_score=0,
        min_structure_score=0,
        divergence_filter="all",
        signal_direction="all",
        block_local_countertrend=False,
    )
    accelerated_baseline = select_candidate_trades(events, baseline)
    parity = trade_sequence_parity(reference_trades, accelerated_baseline)

    selection_started = time.perf_counter()
    trades_by_candidate = {candidate.name: select_candidate_trades(events, candidate) for candidate in candidates}
    selection_seconds = time.perf_counter() - selection_started

    vectorbt_started = time.perf_counter()
    metrics = vectorbt_candidate_metrics(trades_by_candidate)
    vectorbt_seconds = time.perf_counter() - vectorbt_started
    candidate_config = pd.DataFrame(
        [
            {"candidate": candidate.name, **{key: value for key, value in asdict(candidate).items() if key != "name"}}
            for candidate in candidates
        ]
    )
    metrics = candidate_config.merge(metrics, on="candidate", how="left")

    digest = hashlib.sha256()
    with cache_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    accelerated_seconds = signal_cache_seconds + selection_seconds + vectorbt_seconds
    projected_legacy_seconds = reference_seconds * len(candidates)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "POST_SIGNAL_FILTER_ONLY",
        "methodology": "production signal detection and lifecycle cached once; VectorBT compounds exact net trade returns",
        "prepared_cache": str(cache_path),
        "prepared_cache_sha256": digest.hexdigest(),
        "coverage": coverage,
        "bars": len(bars),
        "signal_events": len(events),
        "candidate_count": len(candidates),
        "minimum_ranking_trades": args.minimum_ranking_trades,
        "research_window": {
            "entry_start": str(entry_start),
            "entry_end_exclusive": str(entry_end),
            "untouched_holdout_start": str(holdout_start),
            "data_end_exclusive": str(data_end),
            "research_fraction": args.research_fraction,
            "holding_horizon_embargo_bars": args.max_hold_bars,
        },
        "execution": {
            "interval": args.interval,
            "reward_risk": args.reward_risk,
            "max_hold_bars": args.max_hold_bars,
            "fee_rate": args.fee_rate,
            "stop_mode": args.stop_mode,
        },
        "parity": parity,
        "timings_seconds": {
            "reference": reference_seconds,
            "signal_cache": signal_cache_seconds,
            "candidate_selection": selection_seconds,
            "vectorbt": vectorbt_seconds,
            "accelerated_total": accelerated_seconds,
            "projected_legacy_all_candidates": projected_legacy_seconds,
        },
        "estimated_speedup_vs_repeated_reference": (
            projected_legacy_seconds / accelerated_seconds if accelerated_seconds > 0 else None
        ),
        "speedup_is_projection": True,
        "vectorbt_version": metadata.version("vectorbt"),
        "python_version": sys.version.split()[0],
    }
    symbol = str(coverage.get("symbol") or "unknown").lower()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = Path(args.reports_dir) / f"vectorbt_poc_{symbol}_{timestamp}"
    write_research_artifacts(
        output_dir,
        metrics=metrics,
        trades_by_candidate=trades_by_candidate,
        manifest=manifest,
    )
    if not parity["passed"]:
        raise RuntimeError(f"VectorBT PoC parity failed; inspect {output_dir}")
    return output_dir


def main(argv: list[str] | None = None) -> int:
    try:
        output_dir = run_vectorbt_poc(argv)
    except Exception as exc:
        print(f"VectorBT PoC failed: {exc}", file=sys.stderr)
        return 2
    print(f"VectorBT PoC completed: {output_dir}")
    return 0


def extract_signal_events(
    bars: list[dict[str, Any]],
    *,
    reward_risk: float,
    max_hold_bars: int,
    fee_rate: float,
    stop_mode: str = "structure_atr",
    strategy_config: StrategyConfig | None = None,
    entry_start_time: Any | None = None,
    entry_end_time: Any | None = None,
    engine_factory: Any = ProjectSignalEngine,
) -> list[SignalEvent]:
    """Detect each unique production signal once and cache its exact lifecycle."""
    engine = engine_factory(strategy_config or DEFAULT_CONFIG)
    detect_fn = engine.detect
    accepts_clean = "clean" in inspect.signature(detect_fn).parameters
    ready_bars: list[dict[str, Any]] = []
    seen_signal_keys: set[tuple[str, Any]] = set()
    events: list[SignalEvent] = []

    for index, bar in enumerate(bars):
        if bar.get("macd") is not None:
            ready_bars.append(bar)
        window = ready_bars[-250:]
        signal = detect_fn(window, clean=False) if accepts_clean else detect_fn(window)
        if signal.get("signal") not in {"long", "short"}:
            continue

        signal_key = (str(signal["signal"]), signal.get("divergence_time"))
        if signal_key in seen_signal_keys:
            continue
        seen_signal_keys.add(signal_key)

        entry_index = index + 1
        if entry_index >= len(bars):
            continue
        entry_time = pd.Timestamp(bars[entry_index]["time"])
        if entry_start_time is not None and entry_time < pd.Timestamp(entry_start_time):
            continue
        if entry_end_time is not None and entry_time >= pd.Timestamp(entry_end_time):
            continue

        item = evaluate_trade(
            signal,
            bars,
            entry_index,
            reward_risk,
            max_hold_bars,
            fee_rate,
            stop_mode,
        )
        if item is None:
            continue
        events.append(
            SignalEvent(
                signal_index=index,
                entry_index=entry_index,
                exit_index=min(len(bars) - 1, entry_index + item.bars_held),
                signal=dict(signal),
                entry_bar=dict(bar),
                trade=item,
            )
        )
    return events


def _unique(values: Iterable[Any]) -> list[Any]:
    return list(dict.fromkeys(values))


def build_candidate_grid(
    *,
    min_signal_scores: Iterable[int],
    min_structure_scores: Iterable[int],
    divergence_filters: Iterable[str],
    signal_directions: Iterable[str],
    block_local_countertrends: Iterable[bool],
) -> list[ResearchCandidate]:
    """Build a stable Cartesian product for post-signal research filters."""
    candidates = []
    for score, structure, divergence, direction, countertrend in itertools.product(
        _unique(int(value) for value in min_signal_scores),
        _unique(int(value) for value in min_structure_scores),
        _unique(str(value) for value in divergence_filters),
        _unique(str(value) for value in signal_directions),
        _unique(bool(value) for value in block_local_countertrends),
    ):
        name = (
            f"score{score}_structure{structure}_div{divergence}_"
            f"direction{direction}_countertrend{int(countertrend)}"
        )
        candidates.append(
            ResearchCandidate(
                name=name,
                min_signal_score=score,
                min_structure_score=structure,
                divergence_filter=divergence,
                signal_direction=direction,
                block_local_countertrend=countertrend,
            )
        )
    return candidates


def select_candidate_trades(events: list[SignalEvent], candidate: ResearchCandidate) -> list[SignalTrade]:
    """Apply the project's filter and single-position rules to cached events."""
    selected: list[SignalTrade] = []
    used_exit_until = -1
    for event in events:
        if event.signal_index <= used_exit_until:
            continue
        if not signal_passes_trade_filters(
            event.signal,
            event.entry_bar,
            min_signal_score=candidate.min_signal_score,
            min_structure_score=candidate.min_structure_score,
            divergence_filter=candidate.divergence_filter,
            block_local_countertrend=candidate.block_local_countertrend,
            signal_direction=candidate.signal_direction,
        ):
            continue
        selected.append(event.trade)
        used_exit_until = event.exit_index
    return selected


def vectorbt_candidate_metrics(trades_by_candidate: dict[str, list[SignalTrade]]) -> pd.DataFrame:
    """Use VectorBT to compound exact project trade returns for many candidates."""
    try:
        import vectorbt as vbt
    except ImportError as exc:  # pragma: no cover - exercised by the CLI boundary
        raise RuntimeError(
            "VectorBT is required; install requirements-validation.txt in the research environment"
        ) from exc

    names = list(trades_by_candidate)
    if not names:
        return pd.DataFrame(
            columns=["candidate", "total_trades", "win_rate", "expectancy", "total_return", "max_drawdown"]
        )

    max_trades = max((len(trades) for trades in trades_by_candidate.values()), default=0)
    slots = max(2, max_trades * 2)
    close = pd.DataFrame(1.0, index=pd.RangeIndex(slots), columns=names)
    entries = pd.DataFrame(False, index=close.index, columns=names)
    exits = pd.DataFrame(False, index=close.index, columns=names)
    for name, trades in trades_by_candidate.items():
        for index, item in enumerate(trades):
            entry_slot = index * 2
            exit_slot = entry_slot + 1
            entries.at[entry_slot, name] = True
            exits.at[exit_slot, name] = True
            close.at[exit_slot, name] = 1.0 + float(item.return_pct)

    portfolio = vbt.Portfolio.from_signals(
        close,
        entries,
        exits,
        init_cash=1.0,
        size=np.inf,
        fees=0.0,
        freq="1D",
    )
    total_returns = portfolio.total_return()
    max_drawdowns = portfolio.max_drawdown()

    rows = []
    for name in names:
        trades = trades_by_candidate[name]
        returns = [float(item.return_pct) for item in trades]
        drawdown = float(max_drawdowns[name])
        if drawdown > 0:
            drawdown = -drawdown
        rows.append(
            {
                "candidate": name,
                "total_trades": len(returns),
                "win_rate": sum(value > 0 for value in returns) / len(returns) if returns else 0.0,
                "expectancy": float(np.mean(returns)) if returns else 0.0,
                "total_return": float(total_returns[name]),
                "max_drawdown": drawdown,
            }
        )
    return pd.DataFrame(rows)


def pooled_candidate_robustness(
    returns_by_symbol: Mapping[str, Iterable[float]],
    *,
    extra_round_trip_cost: float = 0.002,
) -> dict[str, Any]:
    """Summarize whether pooled returns are broad and retain cost headroom."""
    if extra_round_trip_cost < 0:
        raise ValueError("extra_round_trip_cost cannot be negative")

    normalized = {
        str(symbol): [float(value) for value in values]
        for symbol, values in returns_by_symbol.items()
    }
    normalized = {symbol: values for symbol, values in normalized.items() if values}
    returns = [value for values in normalized.values() for value in values]

    def profit_factor(values: list[float]) -> float:
        gross_profit = sum(value for value in values if value > 0)
        gross_loss = -sum(value for value in values if value < 0)
        if gross_loss == 0:
            return math.inf if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    if not returns:
        return {
            "trades": 0,
            "symbols": 0,
            "expectancy": 0.0,
            "profit_factor": 0.0,
            "stress_expectancy": 0.0,
            "stress_profit_factor": 0.0,
            "positive_symbol_ratio": 0.0,
            "median_symbol_expectancy": 0.0,
            "max_positive_symbol_share": 1.0,
            "expectancy_without_best_trade": 0.0,
            "profit_factor_without_best_trade": 0.0,
        }

    stressed = [value - extra_round_trip_cost for value in returns]
    without_best = list(returns)
    without_best.remove(max(without_best))
    symbol_expectancies = [float(np.mean(values)) for values in normalized.values()]
    positive_symbol_totals = [max(float(np.sum(values)), 0.0) for values in normalized.values()]
    total_positive_symbol_return = sum(positive_symbol_totals)
    return {
        "trades": len(returns),
        "symbols": len(normalized),
        "expectancy": float(np.mean(returns)),
        "profit_factor": profit_factor(returns),
        "stress_expectancy": float(np.mean(stressed)),
        "stress_profit_factor": profit_factor(stressed),
        "positive_symbol_ratio": sum(value > 0 for value in symbol_expectancies) / len(symbol_expectancies),
        "median_symbol_expectancy": float(np.median(symbol_expectancies)),
        "max_positive_symbol_share": (
            max(positive_symbol_totals) / total_positive_symbol_return
            if total_positive_symbol_return > 0
            else 1.0
        ),
        "expectancy_without_best_trade": float(np.mean(without_best)) if without_best else 0.0,
        "profit_factor_without_best_trade": profit_factor(without_best),
    }


def cross_market_candidate_gate(
    metrics_by_market: Mapping[str, Mapping[str, Any]],
    *,
    minimum_trades_per_market: int = 100,
    minimum_symbols_per_market: int = 10,
    minimum_positive_symbol_ratio: float = 0.50,
) -> dict[str, Any]:
    """Fail closed unless every market has breadth, robustness, and cost headroom."""
    if minimum_trades_per_market <= 0 or minimum_symbols_per_market <= 0:
        raise ValueError("minimum trade and symbol counts must be positive")
    if not 0 <= minimum_positive_symbol_ratio <= 1:
        raise ValueError("minimum_positive_symbol_ratio must be between 0 and 1")

    blockers: list[str] = []
    if not metrics_by_market:
        blockers.append("NO_MARKETS")
    checks = (
        ("trades", minimum_trades_per_market, "MIN_TRADES", lambda value, threshold: value >= threshold),
        ("symbols", minimum_symbols_per_market, "MIN_SYMBOLS", lambda value, threshold: value >= threshold),
        (
            "positive_symbol_ratio",
            minimum_positive_symbol_ratio,
            "POSITIVE_SYMBOL_RATIO",
            lambda value, threshold: value >= threshold,
        ),
        ("expectancy_without_best_trade", 0.0, "EXPECTANCY_WITHOUT_BEST", lambda value, threshold: value > threshold),
        (
            "profit_factor_without_best_trade",
            1.0,
            "PROFIT_FACTOR_WITHOUT_BEST",
            lambda value, threshold: value > threshold,
        ),
        ("stress_expectancy", 0.0, "STRESS_EXPECTANCY", lambda value, threshold: value > threshold),
        ("stress_profit_factor", 1.0, "STRESS_PROFIT_FACTOR", lambda value, threshold: value > threshold),
    )
    for market in sorted(metrics_by_market):
        metrics = metrics_by_market[market]
        for field, threshold, code, predicate in checks:
            try:
                value = float(metrics.get(field, 0.0))
            except (TypeError, ValueError):
                value = 0.0
            if not predicate(value, float(threshold)):
                blockers.append(f"{market}:{code}")
    passed = not blockers
    return {
        "passed": passed,
        "status": "PROMOTABLE" if passed else "RESEARCH_ONLY",
        "markets": sorted(metrics_by_market),
        "blockers": blockers,
    }


if __name__ == "__main__":
    raise SystemExit(main())
