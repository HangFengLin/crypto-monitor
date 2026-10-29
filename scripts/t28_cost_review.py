"""Fixed T28 cost review and historical extension; no production mutations.

Preparation freezes sources/inputs/weekly target prefixes without computing new
PnL. Execution is a separate command after the review's cost file is frozen.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_projects.trend_portfolio_v1.run_study import calendar_metrics, load_inputs, read, write  # noqa: E402
from research_projects.trend_portfolio_v1.target_weight_adapter import (  # noqa: E402
    build_weekly_targets,
    replay_target_weights,
    utc_ms,
)
from scripts.trend_pullback_backtest import _load_bound  # noqa: E402
from scripts.trend_pullback_forward import _parse, validate_funding_coverage  # noqa: E402
from scripts.v1_forward_observer import _bytes, _immutable, _sha  # noqa: E402

ORIGINAL = ROOT / "research_projects/trend_portfolio_v1/protocol.json"
ORIGINAL_TARGETS = ROOT / "reports/trend_portfolio_completion_20261019/targets_before_pnl.json"
COSTS = ROOT / "research_projects/cost_review_20261025/costs.json"
REVIEW = ROOT / "research_projects/cost_review_20261025/protocol.json"
STRATEGIES = ("T28_long_cash", "vol_scaled_hold")
SOURCES = ("scripts/t28_cost_review.py", "position_manager.py", "research_portfolio.py",
           "research_projects/trend_portfolio_v1/target_weight_adapter.py",
           "research_projects/trend_portfolio_v1/run_study.py", "scripts/trend_pullback_backtest.py",
           "scripts/trend_pullback_forward.py", "scripts/v1_forward_observer.py")


def sha(path):
    return _sha(Path(path).read_bytes())


def extend_protocol(original, end):
    result = copy.deepcopy(original)
    old_end = result["scoring"]["end_exclusive_utc"]
    if utc_ms(end) <= utc_ms(old_end):
        raise ValueError("extension end must follow original scoring end")
    result["scoring"]["end_exclusive_utc"] = end
    result["scoring"]["predeclared_segment_boundaries_utc"].append(end)
    return result


def target_cost(scenario):
    if "fee_rate_per_fill" in scenario:
        result = {key: scenario[key] for key in ("id", "fee_rate_per_fill", "adverse_slippage_bps", "extra_delay_minutes")}
    else:
        delay = scenario["delay_bars"]
        if isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
            raise ValueError("nonnegative integer delay bars required")
        result = dict(id=scenario["id"], fee_rate_per_fill=scenario["fee_rate"],
                      adverse_slippage_bps=scenario["slippage_bps"], extra_delay_minutes=15*delay)
    if (not isinstance(result["id"], str) or not result["id"] or "/" in result["id"] or ".." in result["id"]
            or any(isinstance(result[k], bool) or not isinstance(result[k], (int, float))
                   or not math.isfinite(result[k]) for k in ("fee_rate_per_fill", "adverse_slippage_bps"))
            or not 0 <= result["fee_rate_per_fill"] < 1 or not 0 <= result["adverse_slippage_bps"] < 10000
            or not isinstance(result["extra_delay_minutes"], int) or isinstance(result["extra_delay_minutes"], bool)
            or result["extra_delay_minutes"] < 0 or result["extra_delay_minutes"] % 15):
        raise ValueError("invalid fixed target-weight cost scenario")
    return result


def _episode_summary(episodes):
    wins = [x for x in episodes if x["net_cash_pnl"] > 1e-10]
    losses = [x for x in episodes if x["net_cash_pnl"] < -1e-10]
    cash_win = sum(x["net_cash_pnl"] for x in wins)/len(wins) if wins else None
    cash_loss = sum(x["net_cash_pnl"] for x in losses)/len(losses) if losses else None
    ratio_win = sum(x["net_return_on_cumulative_buy_notional"] for x in wins)/len(wins) if wins else None
    ratio_loss = sum(x["net_return_on_cumulative_buy_notional"] for x in losses)/len(losses) if losses else None
    return dict(closed_episodes=len(episodes), wins=len(wins), losses=len(losses),
                breakevens=len(episodes)-len(wins)-len(losses),
                net_cash_win_rate=len(wins)/len(episodes) if episodes else None,
                average_cash_win=cash_win, average_cash_loss=cash_loss,
                cash_payoff_ratio=cash_win/-cash_loss if cash_win is not None and cash_loss is not None else None,
                normalized_episode_payoff_ratio=ratio_win/-ratio_loss if ratio_win is not None and ratio_loss is not None else None)


def episode_statistics(result, recent_start=None):
    """Zero-to-positive-to-zero per-symbol episodes; no fill-as-trade shortcut."""
    active, closed = {}, []
    timeline = [(row["time"], 1, index, "fill", row) for index, row in enumerate(result["fills"])]
    timeline += [(row["time"], 0 if row["phase"] == "before_open_fills" else 2, index, "funding", row)
                 for index, row in enumerate(result["funding"])]
    for stamp, _phase, _index, kind, row in sorted(timeline):
        symbol = row["symbol"]
        if kind == "funding":
            if row["held_quantity"] != 0:
                if symbol not in active:
                    raise ValueError("nonzero funding holding without an episode")
                active[symbol]["funding_cashflow"] += row["cashflow"]
            elif abs(row["cashflow"]) > 1e-10:
                raise ValueError("flat position has nonzero funding")
            continue
        if symbol not in active:
            if row["signed_quantity"] <= 0:
                raise ValueError("episode starts without a positive opening fill")
            active[symbol] = dict(symbol=symbol, opened_at=stamp, closed_at=None, cumulative_buy_notional=0.,
                                  realized_gross_pnl=0., fees=0., funding_cashflow=0., fills=0, quantity=0.)
        episode = active[symbol]
        if not math.isclose(episode["quantity"]+row["signed_quantity"], row["quantity_after"], rel_tol=1e-12, abs_tol=1e-10):
            raise ValueError("episode quantity conservation failed")
        episode["quantity"] = row["quantity_after"]
        episode["fills"] += 1
        episode["fees"] += row["fee"]
        episode["realized_gross_pnl"] += row["realized_gross_pnl"]
        if row["signed_quantity"] > 0:
            episode["cumulative_buy_notional"] += row["notional"]
        if episode["quantity"] == 0:
            episode["closed_at"] = stamp
            episode["net_cash_pnl"] = episode["realized_gross_pnl"]-episode["fees"]+episode["funding_cashflow"]
            episode["net_return_on_cumulative_buy_notional"] = episode["net_cash_pnl"]/episode["cumulative_buy_notional"]
            closed.append(active.pop(symbol))
    tails = []
    for symbol, episode in sorted(active.items()):
        unrealized = result["by_symbol"][symbol]["unrealized_pnl"]
        episode["net_cash_pnl"] = episode["realized_gross_pnl"]-episode["fees"]+episode["funding_cashflow"]
        episode.update(unrealized_pnl=unrealized, marked_net_pnl=episode["net_cash_pnl"]+unrealized)
        tails.append(episode)
    error = (result["final_equity"]-result["initial_equity"]
             -sum(e["net_cash_pnl"] for e in closed)-sum(e["marked_net_pnl"] for e in tails))
    if abs(error) > 1e-7:
        raise ValueError("episode net PnL and continuous account fail reconciliation")
    stats = dict(_episode_summary(closed), fills=len(result["fills"]), closed=closed, open=tails,
                 equity_reconciliation_error=error,
                 scope="Per-asset complete nonzero-position episodes; scale-ins/reductions stay in one episode. Cash payoff depends on size; normalized payoff uses cumulative buy notional, not account return. Episodes are dependent; open tails excluded from win rates.")
    if recent_start is not None:
        recent = [e for e in closed if e["opened_at"] >= recent_start]
        crossing = [e for e in closed if e["opened_at"] < recent_start <= e["closed_at"]]
        crossing += [e for e in tails if e["opened_at"] < recent_start]
        stats["recent_complete_opened_episodes"] = dict(_episode_summary(recent), episodes=recent,
                                                       scope="Opened at or after suffix boundary and fully closed; not suffix account return")
        stats["cross_boundary_episodes"] = crossing
    return stats


def prepare(input_manifest, output, end="2026-10-25T00:00:00Z"):
    output, source = Path(output).resolve(), Path(input_manifest).resolve()
    if output.exists():
        raise FileExistsError("new preparation directory required")
    original = read(ORIGINAL)
    effective = extend_protocol(original, end)
    old_data, old_funding, old_bindings = load_inputs(original)
    document = json.loads(source.read_bytes())
    if set(document["symbols"]) != set(original["symbols"]):
        raise ValueError("frozen BTC ETH coverage required")
    start = utc_ms(original["scoring"]["start_inclusive_utc"])
    old_end = utc_ms(original["scoring"]["end_exclusive_utc"])
    ending = utc_ms(end)
    data, funding, bindings, audit = {}, {}, dict(old_bindings), {}
    for symbol in original["symbols"]:
        item = document["symbols"][symbol]
        raw = _load_bound(source.parent, item["raw15m"])
        data[symbol] = _parse(raw, "15m", ending)
        funding[symbol] = _load_bound(source.parent, item["funding"])
        validate_funding_coverage(funding[symbol], symbol, start, ending)
        old_map = {b["close_time"]: b for b in old_data[symbol]}
        new_map = {b["close_time"]: b for b in data[symbol]}
        fields = ("open_time", "close_time", "open", "high", "low", "close", "volume")
        if any(t not in new_map or any(new_map[t][k] != b[k] for k in fields) for t, b in old_map.items()):
            raise ValueError("old/new raw price prefix differs")
        old_records = [r for r in old_funding[symbol]["records"] if start <= r["time"] < old_end]
        new_records = [r for r in funding[symbol]["records"] if start <= r["time"] < old_end]
        if old_records != new_records:
            raise ValueError("old/new scored funding prefix differs")
        audit[symbol] = dict(old_price_rows_exact=len(old_map), scored_funding_rows_exact=len(old_records),
                             extended_price_rows=len(data[symbol]), funding_records=len(funding[symbol]["records"]))
        for key in ("raw15m", "funding"):
            bindings[str((source.parent/item[key]["path"]).resolve())] = item[key]["sha256"]
    targets = {name: build_weekly_targets(data, effective, name) for name in STRATEGIES}
    old_targets = read(ORIGINAL_TARGETS)
    for name in STRATEGIES:
        prefix = [event for event in targets[name] if event["decision_time_ms"] < old_end]
        if prefix != old_targets[name]:
            raise ValueError("original weekly target prefix changed")
        audit[name] = dict(original_target_prefix_exact=len(prefix), full_targets=len(targets[name]))
    output.mkdir(parents=True, exist_ok=False)
    _immutable(output/"effective_protocol.json", _bytes(effective))
    _immutable(output/"targets_before_pnl.json", _bytes(targets))
    sources = {str(ROOT/name): sha(ROOT/name) for name in SOURCES}
    sources[str(ORIGINAL)], sources[str(ORIGINAL_TARGETS)] = sha(ORIGINAL), sha(ORIGINAL_TARGETS)
    bindings[str(source)] = sha(source)
    manifest = dict(status="INPUTS_AND_TARGETS_FROZEN_NO_NEW_PNL", scope="HISTORICAL_DIAGNOSTIC",
                    input_manifest=str(source), source_sha256=sources, input_sha256=bindings,
                    effective_protocol_sha256=sha(output/"effective_protocol.json"),
                    targets_sha256=sha(output/"targets_before_pnl.json"), prefix_audit=audit,
                    old_scoring_end=old_end, extended_end=ending,
                    account_verified=False, statistically_verified=False, promotion_eligible=False)
    _immutable(output/"pre_pnl_manifest.json", _bytes(manifest))
    return manifest


def execute(prepared, output, costs=COSTS, review_protocol=REVIEW):
    prepared, output, costs, review_protocol = map(lambda p: Path(p).resolve(), (prepared, output, costs, review_protocol))
    if output.exists():
        raise FileExistsError("new cost replay output directory required")
    frozen = read(prepared/"pre_pnl_manifest.json")
    for path, digest in {**frozen["source_sha256"], **frozen["input_sha256"]}.items():
        if sha(path) != digest:
            raise ValueError("prepared source or input drift")
    if (sha(prepared/"effective_protocol.json") != frozen["effective_protocol_sha256"]
            or sha(prepared/"targets_before_pnl.json") != frozen["targets_sha256"]):
        raise ValueError("prepared target or protocol drift")
    effective, targets = read(prepared/"effective_protocol.json"), read(prepared/"targets_before_pnl.json")
    document = read(Path(frozen["input_manifest"]))
    root = Path(frozen["input_manifest"]).parent
    data, funding = {}, {}
    for symbol in effective["symbols"]:
        item = document["symbols"][symbol]
        data[symbol] = _parse(_load_bound(root, item["raw15m"]), "15m", frozen["extended_end"])
        funding[symbol] = _load_bound(root, item["funding"])
    cost_document = read(costs)
    scenarios = cost_document["scenarios"]
    if not scenarios or len({s["id"] for s in scenarios}) != len(scenarios):
        raise ValueError("unique frozen cost scenarios required")
    mapped = [target_cost(s) for s in scenarios]
    before = dict(costs_sha256=sha(costs), review_protocol_sha256=sha(review_protocol),
                  prepared_manifest_sha256=sha(prepared/"pre_pnl_manifest.json"))
    output.mkdir(parents=True, exist_ok=False)
    write(output/"run_manifest.json", dict(before, costs=cost_document, review_protocol=read(review_protocol),
                                          scope="HISTORICAL_DIAGNOSTIC", prefix_source=str(prepared),
                                          original_strategy_parameters_unchanged=True, strategies=list(STRATEGIES)))
    (output/"accounts").mkdir()
    summaries = {}
    for name in STRATEGIES:
        summaries[name] = {}
        for source_scenario, scenario in zip(scenarios, mapped):
            result = replay_target_weights(data, funding, targets[name], effective, scenario)
            calendar = calendar_metrics(result["equity"], result["initial_equity"], effective["scoring"])
            suffix = next(s for s in calendar["segments"] if s["start_ms"] == frozen["old_scoring_end"])
            episodes = episode_statistics(result, frozen["old_scoring_end"])
            write(output/"accounts"/f"{name}__{scenario['id']}.json.gz", result)
            summaries[name][scenario["id"]] = dict(
                net_return=result["net_return"], max_close_drawdown=result["max_drawdown"],
                final_equity=result["final_equity"], total_fees=result["total_fees"], funding_cashflow=result["funding_cashflow"],
                by_symbol=result["by_symbol"], calendar=calendar, recent_account=suffix, episode_diagnostics=episodes,
                fills=len(result["fills"]), account_fee_verified=source_scenario.get("account_fee_verified", False),
                cost_source=source_scenario.get("source"), scope="Current-cost assumptions applied uniformly to exposed historical prices; not historical actual-account statements")
    if before != dict(costs_sha256=sha(costs), review_protocol_sha256=sha(review_protocol),
                      prepared_manifest_sha256=sha(prepared/"pre_pnl_manifest.json")):
        raise ValueError("cost or prepared protocol changed during replay")
    for path, digest in {**frozen["source_sha256"], **frozen["input_sha256"]}.items():
        if sha(path) != digest:
            raise ValueError("source or input changed during cost replay")
    result = dict(status="COMPLETE_HISTORICAL_COST_REVIEW", scope="HISTORICAL_DIAGNOSTIC", summaries=summaries,
                  account_verified=False, statistically_verified=False, promotion_eligible=False,
                  tail="Unclosed positions marked; not artificially closed or counted in episode win rates")
    write(output/"summary.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--input_manifest", type=Path)
    parser.add_argument("--prepared", type=Path)
    parser.add_argument("--costs", type=Path, default=COSTS)
    parser.add_argument("--review-protocol", type=Path, default=REVIEW)
    args = parser.parse_args(argv)
    if bool(args.input_manifest) == bool(args.prepared):
        parser.error("provide exactly one of --input_manifest (prepare only) or --prepared (execute frozen costs)")
    result = prepare(args.input_manifest, args.output) if args.input_manifest else execute(args.prepared, args.output, args.costs, args.review_protocol)
    print(json.dumps(dict(status=result["status"], output=str(args.output))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
