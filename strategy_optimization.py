"""Frozen offline diagnostic experiment; no configuration writes or promotion.

Prepared prices are read without cache repair. Signals are generated causally
with the production state machine, before any capacity or lifecycle simulation.
All legacy windows are explicitly exposed, including the old final tail.
"""
from __future__ import annotations

import argparse
import bisect
import concurrent.futures
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from research_portfolio import replay_portfolio
from strategy import DEFAULT_CONFIG, ProjectSignalEngine
from strategy_validation import walk_forward_windows
from vectorbt_research import load_prepared_bars

STEP = 900_000
HOLD = 96
SEED = 20260620


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def causal_events(symbol, bars, config, entry_start, entry_end, *, engine_factory=ProjectSignalEngine):
    engine = engine_factory(config)
    ready, seen, events = [], set(), []
    for bar in bars:
        if bar.get("macd") is None:
            continue
        ready.append(bar)
        signal = engine.detect(ready[-250:], clean=False)
        direction = signal.get("signal")
        if direction not in {"long", "short"}:
            continue
        identity = (direction, signal.get("divergence_time"))
        if identity in seen:
            continue
        seen.add(identity)
        execution_time = int(bar["close_time"]) + 1
        if not entry_start <= execution_time < entry_end:
            continue
        # No future prices, existing positions or exit evaluation here.
        events.append(dict(symbol=symbol, direction=direction, stop_loss=float(signal["stop_loss"]),
                           setup_id=f"{direction}:{signal.get('divergence_time')}",
                           signal_time=execution_time, known_at=int(bar["close_time"]),
                           score=signal.get("score"), structure_score=signal.get("structure_score")))
    return events


def variants():
    loose = dict(max_positions=None, max_risk_fraction=1.0, direction_risk_fraction=1.0,
                 allow_overlap=True)
    risk = dict(max_positions=5, max_risk_fraction=.025,
                direction_risk_fraction={"long": .025, "short": .01}, allow_overlap=False)
    return {
        "baseline": dict(signal_variant="baseline", stop_mode="structure_atr", **loose),
        "risk_only": dict(signal_variant="baseline", stop_mode="structure_atr", **risk),
        "V1_entry_only": dict(signal_variant="V1", stop_mode="structure_atr", **loose),
        "V1_with_risk": dict(signal_variant="V1", stop_mode="structure_atr", **risk),
        "X1_exit_only": dict(signal_variant="baseline", stop_mode="partial_1r_breakeven", **loose),
        "X1_with_risk": dict(signal_variant="baseline", stop_mode="partial_1r_breakeven", **risk),
    }


def portfolio_metrics(result):
    start = result["parameters"]["initial_equity"]
    curve = [start] + [r["equity"] for r in result["equity"]]
    peak, drawdown = start, 0.0
    for value in curve:
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak)
    trades = result["trades"]
    winners = sum(max(0, t["net_pnl"]) for t in trades)
    losers = -sum(min(0, t["net_pnl"]) for t in trades)
    return dict(total_return=curve[-1] / start - 1, max_drawdown=drawdown,
                trades=len(trades), win_rate=sum(t["net_pnl"] > 0 for t in trades) / len(trades) if trades else None,
                mean_net_return=float(np.mean([t["net_return"] for t in trades])) if trades else None,
                mean_net_r=float(np.mean([t["net_r"] for t in trades])) if trades else None,
                profit_factor=winners / losers if losers else None,
                funding=sum(t["funding_cashflow"] for t in trades),
                fees=sum(t["total_fees"] for t in trades),
                max_positions=max((r["positions"] for r in result["equity"]), default=0),
                max_short_positions=max((r.get("short_positions", 0) for r in result["equity"]), default=0),
                max_gross_equity=max((r["gross_notional"] / r["equity"] for r in result["equity"] if r["equity"] > 0), default=0),
                max_stop_risk_equity=max((r["committed_risk"] / r["equity"] for r in result["equity"] if r["equity"] > 0), default=0),
                max_short_risk_equity=max((r["short_risk"] / r["equity"] for r in result["equity"] if r["equity"] > 0), default=0),
                rejections=dict(Counter(r["reason"] for r in result["rejections"])),
                open_positions=len(result["open_positions"]))


def weekly_returns(result):
    previous = result["parameters"]["initial_equity"]
    groups = defaultdict(lambda: 1.0)
    for row in result["equity"]:
        day = pd.Timestamp(row["time"] - 1, unit="ms", tz="UTC")
        iso = day.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        groups[key] *= row["equity"] / previous
        previous = row["equity"]
    return {key: value - 1 for key, value in groups.items()}


def correlation_exposure(data, result):
    """Daily diagnostic: same-direction peers with trailing return corr >= .7.

    Uses only closes known by each sampled UTC boundary, 30 past daily returns,
    minimum 20 pairwise observations. This is not an intrabar maximum or a cap.
    Positions and remaining quantities are reconstructed from actual fills.
    """
    day_ms = 86_400_000
    daily = {}
    for symbol, bars in data.items():
        values = {}
        for bar in bars:
            boundary = int(bar["close_time"]) + 1
            if boundary % day_ms == 0:
                values[boundary] = bar["close"]
        daily[symbol] = pd.Series(values, dtype=float)
    closes = pd.DataFrame(daily).sort_index()
    returns = closes.pct_change(fill_method=None)
    exit_fills = defaultdict(list)
    for fill in result["fills"]:
        if fill["kind"] == "exit":
            exit_fills[(fill["symbol"], fill["setup_id"])].append(fill)
    peak, observations, insufficient = 0.0, 0, 0
    for row in result["equity"]:
        time = row["time"]
        if time % day_ms or time not in closes.index or row["equity"] <= 0:
            continue
        gross = defaultdict(float)
        for trade in result["trades"]:
            held_at_close = (trade["exit_time"] > time or
                             (trade["exit_time"] == time and trade.get("exit_phase") == "open"))
            if not trade["entry_time"] < time or not held_at_close:
                continue
            quantity = trade["quantity"] - sum(f["quantity"] for f in exit_fills[(trade["symbol"], trade["setup_id"])]
                                              if f["time"] < time or
                                              (f["time"] == time and f.get("phase", "bar_end") == "bar_end"))
            price = closes.at[time, trade["symbol"]]
            if pd.notna(price) and quantity > 0:
                gross[(trade["symbol"], trade["direction"])] += quantity * price
        if len(gross) < 2:
            continue
        trailing = returns.loc[:time].tail(30)
        corr = trailing.corr(min_periods=20)
        correlated = set()
        items = list(gross)
        for i, (symbol, direction) in enumerate(items):
            for other, other_direction in items[i + 1:]:
                if direction != other_direction or symbol == other:
                    continue
                coefficient = corr.at[symbol, other]
                if pd.isna(coefficient):
                    insufficient += 1
                elif coefficient >= .7:
                    correlated.update(((symbol, direction), (other, direction)))
        observations += 1
        peak = max(peak, sum(gross[key] for key in correlated) / row["equity"])
    return dict(peak_correlated_gross_equity=peak, observations=observations,
                insufficient_pair_observations=insufficient,
                boundary="UTC daily close; known trailing 30 daily returns, >=20 pairwise obs, corr>=.7; descriptive only")


def paired_weekly_test(candidate, baseline, *, iterations=2000, seed=SEED, segment_lengths=None):
    delta = np.asarray(candidate, dtype=float) - np.asarray(baseline, dtype=float)
    if len(delta) < 4:
        return dict(weeks=len(delta), delta=float(delta.mean()) if len(delta) else None,
                    ci_low=None, ci_high=None, p_value=1.0)
    lengths = list(segment_lengths or [len(delta)])
    if sum(lengths) != len(delta) or any(length < 2 for length in lengths):
        raise ValueError("each contiguous weekly segment needs at least two observations")
    blocks, offset = [], 0
    for length in lengths:
        blocks.extend([[i, i + 1] for i in range(offset, offset + length - 1)])
        offset += length
    # Two-week moving blocks never cross missing weeks or wrap tail to start.
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, len(blocks), size=(iterations, (len(delta) + 1) // 2))
    indices = np.asarray(blocks)[starts].reshape(iterations, -1)[:, :len(delta)]
    boot = delta[indices].mean(axis=1)
    centered = (delta - delta.mean())[indices].mean(axis=1)
    return dict(weeks=len(delta), delta=float(delta.mean()), ci_low=float(np.quantile(boot, .025)),
                ci_high=float(np.quantile(boot, .975)),
                p_value=float((np.count_nonzero(centered >= delta.mean()) + 1) / (iterations + 1)),
                segment_lengths=lengths, block_count=len(blocks),
                method="paired complete UTC-week return; noncircular two-week moving blocks within observed segments; one-sided null-centered bootstrap")


def _symbol_job(task):
    symbol, source, windows, directory = task
    rows, coverage = load_prepared_bars(source)
    times = [int(row["open_time"]) for row in rows]
    source_hash = sha256(source)
    collected = []
    for window in windows:
        destination = Path(directory) / "signal_cache" / window["name"] / f"{symbol}.json.gz"
        cached = None
        if destination.exists():
            try:
                with gzip.open(destination, "rt") as handle:
                    candidate = json.load(handle)
                if candidate["source_sha256"] == source_hash and candidate["window"] == window:
                    cached = candidate
            except (OSError, ValueError, KeyError):
                pass  # Only this experiment's partial output may be regenerated.
        first = bisect.bisect_left(times, window["start"] - 250 * STEP)
        last = bisect.bisect_left(times, window["end"])
        sliced = rows[first:last]
        execution = [{k: row[k] for k in ("open_time", "close_time", "open", "high", "low", "close", "volume", "atr") if k in row}
                     for row in sliced if window["start"] <= int(row["open_time"]) < window["end"]]
        variants_events = cached["events"] if cached else {}
        if not cached:
            for name, config in (("baseline", DEFAULT_CONFIG), ("V1", replace(DEFAULT_CONFIG, volume_sequence_enabled=True))):
                variants_events[name] = causal_events(symbol, sliced, config, window["start"], window["end"] - HOLD * STEP)
        payload = dict(symbol=symbol, window=window, coverage=coverage, bars=execution,
                       events=variants_events, source_sha256=source_hash)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not cached:
            temporary = destination.with_suffix(".part")
            with gzip.open(temporary, "wt", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
            temporary.replace(destination)
        collected.append(dict(window=window["name"], bars=len(execution),
                              events={k: len(v) for k, v in variants_events.items()},
                              first=execution[0]["open_time"] if execution else None,
                              end=execution[-1]["close_time"] + 1 if execution else None))
    return dict(symbol=symbol, windows=collected)


def build_registry(source_root, output):
    start = pd.Timestamp("2024-06-20", tz="UTC")
    end = pd.Timestamp("2026-06-20", tz="UTC")
    windows, tail_start = walk_forward_windows(start, end, HOLD, "15m")
    folds = [dict(name=w["fold"], start=int(w["validation_start"].timestamp() * 1000),
                  end=int(w["validation_end"].timestamp() * 1000),
                  train_start=w["train_start"].isoformat(), train_end=w["train_end"].isoformat(),
                  exposure="DEVELOPMENT_EXPOSED") for w in windows]
    folds.append(dict(name="old_final_tail_exposed", start=int(tail_start.timestamp() * 1000),
                      end=int(end.timestamp() * 1000), exposure="HOLDOUT_EXPOSED"))
    universe_path = source_root / "reports/strategy_validation_20260620T141732Z/universe_snapshot.json"
    universe = json.loads(universe_path.read_text())
    sources = {}
    for item in universe["binance"]:
        symbol = item["symbol"]
        candidates = sorted((source_root / "runtime/backtest-data/prepared/binance" / symbol / "15m").glob(
            "1714521600000_1781913600000_fa0bec8bfe99_oi1_v*.json.gz"))
        if not candidates:
            raise ValueError(f"missing frozen universe cache: {symbol}")
        # v2 is a pre-existing provider correction, never selected by performance.
        path = candidates[-1]
        sources[symbol] = dict(path=str(path), sha256=sha256(path))
    return dict(schema_version=1, created_at=datetime.now(timezone.utc).isoformat(),
                status="RESEARCH_ONLY", selection="NO_SELECTION", promotion_eligible=False,
                input_sources=sources, universe_sha256=sha256(universe_path),
                universe_bias="2026-06-20 current pool; historical survivorship/listing bias; not PIT universe",
                oi_boundary="OI_EVENT_TIME_ASOF_AVAILABLE_AT_UNVERIFIED; score unused for admission",
                folds=folds, variants=variants(), candidate_count=2,
                primary_tests={"V1_with_risk": "risk_only", "X1_with_risk": "risk_only"},
                stress={"base": {"fee_rate": .001, "slippage_bps": 2, "delay_bars": 0},
                        "double_cost": {"fee_rate": .002, "slippage_bps": 4, "delay_bars": 0},
                        "delay_one_bar": {"fee_rate": .001, "slippage_bps": 2, "delay_bars": 1}},
                common=dict(initial_equity=10000, risk_fraction=.005, max_hold_bars=HOLD,
                            reward_risk=2, return_model="linear_usdm_v1", strict_funding=True, finalize=True),
                config=asdict(DEFAULT_CONFIG), config_V1=asdict(replace(DEFAULT_CONFIG, volume_sequence_enabled=True)),
                timing="250-bar warmup; signal close -> next open; open funding/gap exits -> entries -> intrabar lifecycle",
                baseline_note="Includes pending_sell engineering repair; old accounting separately recalculated without history rewrite",
                inference="All results diagnostic; exposed holdout, incomplete PIT and no OKX confirmation forbid promotion",
                code_hashes={p: sha256(Path(__file__).parent / p) for p in
                             ("strategy.py", "position_manager.py", "strategy_optimization.py", "research_portfolio.py")})


def attach_predecessor(task):
    symbol, source, windows, directory = task
    rows, _ = load_prepared_bars(source)
    times = [int(row["open_time"]) for row in rows]
    for window in windows:
        path = Path(directory) / "signal_cache" / window["name"] / f"{symbol}.json.gz"
        with gzip.open(path, "rt") as handle:
            cache = json.load(handle)
        index = bisect.bisect_left(times, window["start"]) - 1
        predecessor = rows[index] if index >= 0 and times[index] == window["start"] - STEP else None
        cache["prior_bar"] = ({k: predecessor[k] for k in
                               ("open_time", "close_time", "open", "high", "low", "close", "volume", "atr") if k in predecessor}
                              if predecessor else None)
        temporary = path.with_suffix(".part")
        with gzip.open(temporary, "wt") as handle:
            json.dump(cache, handle, ensure_ascii=False, allow_nan=False)
        temporary.replace(path)
    return symbol


def run_replays(registry, output):
    actual_code = {p: sha256(Path(__file__).parent / p) for p in registry["code_hashes"]}
    if actual_code["strategy.py"] != registry["code_hashes"]["strategy.py"]:
        raise ValueError("signal engine changed after freeze")
    execution_snapshot = dict(created_at=datetime.now(timezone.utc).isoformat(),
                              registry_sha256=sha256(output / "experiment_registry.json"),
                              actual_code_hashes=actual_code,
                              amendments_before_first_pnl=[
                                  "Signal job resume/cache reuse and worker count only; no candidate changes",
                                  "Ragged/empty symbol calendar audit; no price fill or universe shrink",
                                  "Open-known favorable exit before admissions; profit trigger price conservative; adverse gap actual open",
                                  "Open-known 1R partial arms BE for subsequent range of same bar; range-first partial arms only next bar",
                                  "Funding credit guaranteed surviving quantity; debit prior quantity under uncertain intrabar timing",
                                  "Independent long/short caps and direction position counts",
                                  "Actual preceding bar retained for first-boundary signal/ATR evidence; no synthetic price",
                                  "Fill open/bar_end phase distinguishes next-open events from prior-close equity at identical milliseconds",
                                  "Merge fold fragments of same UTC week; exclude incomplete weeks; bootstrap blocks never cross unobserved gaps",
                                  "Equity/quantity reconciliation, per-direction diagnostics and fixed descriptive daily correlation proxy"],
                              correlation_proxy="30 prior daily returns; >=20 pairwise observations; corr>=.7; descriptive daily sample")
    snapshot_path = output / "execution_snapshot.json"
    if snapshot_path.exists():
        existing = json.loads(snapshot_path.read_text())
        if existing["actual_code_hashes"] != actual_code:
            raise ValueError("execution code changed after first PnL; requires explicit new diagnostic run")
    else:
        write_json(snapshot_path, execution_snapshot)
    funding = {s: json.loads((output / "funding_evidence" / f"{s}.json").read_text())
               for s in registry["input_sources"]}
    metrics_rows, weekly, per_symbol, per_direction = [], {}, [], []
    for fold in registry["folds"]:
        data, events = {}, {"baseline": [], "V1": []}
        for symbol in registry["input_sources"]:
            with gzip.open(output / "signal_cache" / fold["name"] / f"{symbol}.json.gz", "rt") as handle:
                cache = json.load(handle)
            if cache["source_sha256"] != registry["input_sources"][symbol]["sha256"]:
                raise ValueError("signal source hash drift")
            if "prior_bar" not in cache:
                raise ValueError("preceding bar audit missing; run --prepare-predecessors")
            data[symbol] = ([cache["prior_bar"]] if cache["prior_bar"] else []) + cache["bars"]
            for variant in events:
                events[variant].extend(cache["events"][variant])
        for stress, costs in registry["stress"].items():
            for name, definition in registry["variants"].items():
                options = {k: v for k, v in definition.items() if k != "signal_variant"}
                result = replay_portfolio(data, events[definition["signal_variant"]], funding,
                                          **registry["common"], **options, **costs)
                result["scoring_boundary"] = dict(start=fold["start"], end=fold["end"],
                                                  preceding_bar="observed only; not scored")
                result["equity"] = [r for r in result["equity"] if r["time"] > fold["start"]]
                if result["open_positions"]:
                    raise ValueError("fold retained unresolved position")
                difference = result["equity"][-1]["equity"] - registry["common"]["initial_equity"]
                if not np.isclose(difference, sum(t["net_pnl"] for t in result["trades"]), atol=1e-7, rtol=1e-10):
                    raise ValueError("cash/equity/closed trade reconciliation failed")
                quantities = defaultdict(float)
                for fill in result["fills"]:
                    identity = (fill["symbol"], fill["setup_id"])
                    quantities[identity] += fill["quantity"] * (1 if fill["kind"] == "entry" else -1)
                if any(abs(quantity) > 1e-7 for quantity in quantities.values()):
                    raise ValueError("entry/exit quantity conservation failed")
                metric = portfolio_metrics(result)
                if stress == "base":
                    metric["correlation_exposure"] = correlation_exposure(data, result)
                metric.update(fold=fold["name"], variant=name, stress=stress, exposure=fold["exposure"])
                metrics_rows.append(metric)
                weekly[(fold["name"], stress, name)] = weekly_returns(result)
                path = output / "portfolio_replays" / fold["name"] / f"{name}_{stress}.json.gz"
                path.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(path, "wt") as handle:
                    json.dump(result, handle, ensure_ascii=False, allow_nan=False)
                if stress == "base":
                    for direction in ("long", "short"):
                        ts = [t for t in result["trades"] if t["direction"] == direction]
                        per_direction.append(dict(fold=fold["name"], variant=name, direction=direction,
                                                  trades=len(ts), pnl=sum(t["net_pnl"] for t in ts),
                                                  wins=sum(t["net_pnl"] > 0 for t in ts),
                                                  mean_net_return=float(np.mean([t["net_return"] for t in ts])) if ts else None))
                    for symbol in data:
                        ts = [t for t in result["trades"] if t["symbol"] == symbol]
                        per_symbol.append(dict(fold=fold["name"], variant=name, symbol=symbol,
                                               trades=len(ts), pnl=sum(t["net_pnl"] for t in ts),
                                               mean_net_return=float(np.mean([t["net_return"] for t in ts])) if ts else None))
                print(json.dumps({"replay": fold["name"], "variant": name, "stress": stress,
                                  "trades": metric["trades"], "return": round(metric["total_return"], 6)}, ensure_ascii=False), flush=True)
    write_json(output / "fold_metrics.json", metrics_rows)
    pd.DataFrame(metrics_rows).to_csv(output / "fold_metrics.csv", index=False)
    pd.DataFrame(per_symbol).to_csv(output / "per_symbol.csv", index=False)
    pd.DataFrame(per_direction).to_csv(output / "per_direction.csv", index=False)
    write_json(output / "weekly_returns.json", [dict(fold=k[0], stress=k[1], variant=k[2], weekly_returns=v)
                                               for k, v in weekly.items()])
    summary = []
    for stress in registry["stress"]:
        for name in registry["variants"]:
            rows = [r for r in metrics_rows if r["stress"] == stress and r["variant"] == name]
            summary.append(dict(variant=name, stress=stress, folds=len(rows), trades=sum(r["trades"] for r in rows),
                                positive_folds=sum(r["total_return"] > 0 for r in rows),
                                linked_return=float(np.prod([1 + r["total_return"] for r in rows]) - 1),
                                worst_fold_drawdown=max(r["max_drawdown"] for r in rows),
                                max_positions=max(r["max_positions"] for r in rows),
                                max_short_positions=max(r["max_short_positions"] for r in rows),
                                max_short_risk=max(r["max_short_risk_equity"] for r in rows),
                                peak_correlated_gross_equity=max((r.get("correlation_exposure", {}).get("peak_correlated_gross_equity", 0) for r in rows), default=0) if stress == "base" else None))
    comparisons = []
    covered_week_time = defaultdict(int)
    week_ms = 7 * 86_400_000
    for fold in registry["folds"]:
        for key in weekly[(fold["name"], "base", "risk_only")]:
            year, week = map(int, key.replace("-W", " ").split())
            opening = int(datetime.fromisocalendar(year, week, 1).replace(tzinfo=timezone.utc).timestamp() * 1000)
            covered_week_time[key] += max(0, min(fold["end"], opening + week_ms) - max(fold["start"], opening))
    full_weeks = sorted(key for key, covered in covered_week_time.items() if covered == week_ms)
    week_times = [int(datetime.fromisocalendar(*map(int, key.replace("-W", " ").split()), 1)
                      .replace(tzinfo=timezone.utc).timestamp() * 1000) for key in full_weeks]
    segments = []
    for i, time in enumerate(week_times):
        if i == 0 or time - week_times[i - 1] != week_ms:
            segments.append(1)
        else:
            segments[-1] += 1
    # Primary multiplicity family is exactly the two frozen base-cost candidates.
    for name, comparator in registry["primary_tests"].items():
        left, right = [], []
        for week in full_weeks:
            a, b = 1.0, 1.0
            for fold in registry["folds"]:
                a *= 1 + weekly[(fold["name"], "base", name)].get(week, 0)
                b *= 1 + weekly[(fold["name"], "base", comparator)].get(week, 0)
            left.append(a - 1)
            right.append(b - 1)
        comparisons.append(dict(candidate=name, comparator=comparator,
                                excluded_partial_weeks=len(covered_week_time) - len(full_weeks),
                                **paired_weekly_test(left, right, segment_lengths=segments)))
    ranked = sorted(range(len(comparisons)), key=lambda i: comparisons[i]["p_value"])
    previous = 1.0
    for rank, idx in reversed(list(enumerate(ranked, 1))):
        previous = min(previous, comparisons[idx]["p_value"] * len(comparisons) / rank)
        comparisons[idx]["q_value"] = previous
    result = dict(status="RESEARCH_ONLY", selection="NO_SELECTION", promotion_eligible=False,
                  reasons=["HOLDOUT_EXPOSED", "PIT_UNIVERSE_UNVERIFIED", "OI_AVAILABLE_AT_UNVERIFIED", "NO_OKX_CONFIRMATION"],
                  summary=summary, comparisons=comparisons,
                  linked_return_convention="product of independently reset fixed-window returns; gaps uninvested; no account return claim",
                  multiplicity_boundary="BH-FDR covers only two new fixed comparisons; excludes previous strategy/universe selection; descriptive on exposed data",
                  diagnostic_positive=[r["candidate"] for r in comparisons if r["ci_low"] is not None and r["ci_low"] > 0 and r["q_value"] <= .1])
    write_json(output / "comparison.json", result)
    pd.DataFrame(summary).to_csv(output / "comparison.csv", index=False)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--replay-only", action="store_true")
    parser.add_argument("--signals-only", action="store_true")
    parser.add_argument("--resume-signals", action="store_true")
    parser.add_argument("--prepare-predecessors", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    registry_path = args.output / "experiment_registry.json"
    if args.replay_only or args.resume_signals or args.prepare_predecessors:
        registry = json.loads(registry_path.read_text())
    else:
        if registry_path.exists():
            raise ValueError("immutable registry already exists; use --replay-only or a fresh output")
        registry = build_registry(args.source_root, args.output)
        write_json(registry_path, registry)  # Before any new PnL is observed.
    if args.prepare_predecessors:
        tasks = [(s, source["path"], registry["folds"], str(args.output)) for s, source in registry["input_sources"].items()]
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as executor:
            for symbol in executor.map(attach_predecessor, tasks):
                print(json.dumps({"predecessor_audit": symbol}), flush=True)
    elif not args.replay_only:
        if registry["config"] != asdict(DEFAULT_CONFIG) or registry["code_hashes"]["strategy.py"] != sha256(Path(__file__).parent / "strategy.py"):
            raise ValueError("signal configuration or engine differs from frozen registry")
        tasks = [(s, source["path"], registry["folds"], str(args.output)) for s, source in registry["input_sources"].items()]
        inventory = []
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(_symbol_job, task): task[0] for task in tasks}
            for future in concurrent.futures.as_completed(futures):
                inventory.append(future.result())
                print(json.dumps({"signals_complete": futures[future], "completed": len(inventory), "total": len(tasks)}, ensure_ascii=False), flush=True)
        write_json(args.output / "signal_inventory.json", sorted(inventory, key=lambda r: r["symbol"]))
    if not args.signals_only:
        run_replays(registry, args.output)


if __name__ == "__main__":
    main()
