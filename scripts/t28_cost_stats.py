"""Add episode cash PF and an evidence bridge without rerunning frozen PnL."""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.t28_cost_review import ORIGINAL, STRATEGIES, sha, target_cost  # noqa: E402
from scripts.v1_forward_observer import _bytes, _immutable  # noqa: E402


def read(path):
    path = Path(path)
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)


def cash_profit_factor(episodes):
    values = [float(e["net_cash_pnl"]) for e in episodes]
    if any(not math.isfinite(v) for v in values):
        raise ValueError("finite episode net cash PnL required")
    positive = math.fsum(v for v in values if v > 0)
    negative = -math.fsum(v for v in values if v < 0)
    return dict(cash_profit_factor=positive/negative if negative > 0 else None,
                positive_net_pnl_usdt=positive, negative_net_pnl_abs_usdt=negative,
                closed_episodes=len(values), basis="sum positive complete-episode net cash PnL / abs(sum negative); excludes open tails")


def signature(scenario):
    mapped = target_cost(scenario)
    return mapped["fee_rate_per_fill"], mapped["adverse_slippage_bps"], mapped["extra_delay_minutes"]


def scenario_equivalence(old, new):
    if len(old["scenarios"]) != len(new["scenarios"]):
        raise ValueError("complete scenario sets must be equivalent")
    old_values = {}
    for item in old["scenarios"]:
        value = signature(item)
        if value in old_values:
            raise ValueError("ambiguous duplicate control cost values")
        old_values[value] = item["id"]
    mapping = {}
    for item in new["scenarios"]:
        value = signature(item)
        if value not in old_values:
            raise ValueError("new account cost assumptions differ; fresh replay required")
        mapping[item["id"]] = old_values[value]
    if len(set(mapping.values())) != len(old_values):
        raise ValueError("scenario mapping is not one to one")
    return mapping


def execute(run, costs, output):
    run, costs, output = map(lambda p: Path(p).resolve(), (run, costs, output))
    if output.exists():
        raise FileExistsError("new statistics/evidence bridge directory required")
    frozen_run, summaries, current_costs = read(run/"run_manifest.json"), read(run/"summary.json"), read(costs)
    if current_costs.get("user_confirmed_fee") is not True:
        raise ValueError("user-reported fee evidence required before changing evidence classification")
    mapping = scenario_equivalence(frozen_run["costs"], current_costs)
    prepared = Path(frozen_run["prefix_source"])
    preparation = read(prepared/"pre_pnl_manifest.json")
    checks = {**preparation["source_sha256"], **preparation["input_sha256"]}
    for path, digest in checks.items():
        if sha(path) != digest:
            raise ValueError("frozen replay source/input drift")
    bindings = {str(run/"run_manifest.json"):sha(run/"run_manifest.json"),
                str(run/"summary.json"):sha(run/"summary.json"), str(costs):sha(costs),
                str(prepared/"pre_pnl_manifest.json"):sha(prepared/"pre_pnl_manifest.json"),
                str(Path(__file__).resolve()):sha(Path(__file__).resolve()),
                str(ROOT/"tests/test_t28_cost_stats.py"):sha(ROOT/"tests/test_t28_cost_stats.py")}
    stats, prefixes = {}, {}
    original_protocol = read(ORIGINAL)
    original_by_value = {signature(s):s["id"] for s in original_protocol["cost_scenarios"]}
    for strategy in STRATEGIES:
        stats[strategy] = {}
        for new_id, old_id in mapping.items():
            source_scenario = next(s for s in frozen_run["costs"]["scenarios"] if s["id"] == old_id)
            row = summaries["summaries"][strategy][old_id]
            episodes = row["episode_diagnostics"]
            stats[strategy][new_id] = dict(
                source_scenario=old_id, full=cash_profit_factor(episodes["closed"]),
                recent_complete_opened_episodes=cash_profit_factor(episodes["recent_complete_opened_episodes"]["episodes"]),
                open_episodes=episodes["open"], cross_boundary_episodes=episodes["cross_boundary_episodes"],
                net_cash_win_rate=episodes["net_cash_win_rate"], cash_payoff_ratio=episodes["cash_payoff_ratio"],
                normalized_episode_payoff_ratio=episodes["normalized_episode_payoff_ratio"],
                fills=episodes["fills"], scope=episodes["scope"])
            path = run/"accounts"/f"{strategy}__{old_id}.json.gz"
            bindings[str(path)] = sha(path)
            account = read(path)
            original_id = original_by_value[signature(source_scenario)]
            original_path = ROOT/"reports/trend_portfolio_completion_20261019/replays"/f"{strategy}__{original_id}.json.gz"
            original = read(original_path)
            bindings[str(original_path)] = sha(original_path)
            old_end = original["equity"][-1]["time"]
            prefix = dict(equity_points=len(original["equity"]),
                          equity_prefix_exact=account["equity"][:len(original["equity"])] == original["equity"],
                          fills_prefix_exact=[x for x in account["fills"] if x["time"]<old_end] == original["fills"],
                          funding_prefix_exact=[x for x in account["funding"] if x["time"]<old_end] == original["funding"],
                          rebalances_prefix_exact=[x for x in account["rebalances"] if x["time"]<old_end] == original["rebalances"])
            if not all(v for k,v in prefix.items() if k.endswith("exact")):
                raise ValueError("original prefix control reproduction differs")
            prefixes[f"{strategy}__{new_id}"] = prefix
    for path, digest in {**bindings, **checks}.items():
        if sha(path) != digest:
            raise ValueError("bound evidence changed during postprocessing")
    output.mkdir(parents=True, exist_ok=False)
    _immutable(output/"derived_statistics.json", _bytes(dict(
        status="COMPLETE_POSTPROCESSING_NO_REPLAY", scope="HISTORICAL_DIAGNOSTIC", statistics=stats,
        input_bindings=bindings, account_verified=False, statistically_verified=False, promotion_eligible=False)))
    bridge = dict(status="NUMERICALLY_IDENTICAL_SCENARIOS_EVIDENCE_BRIDGED_NO_REPLAY",
                  scenario_mapping=mapping, control_costs=frozen_run["costs"], current_cost_evidence=current_costs,
                  input_bindings=bindings, original_prefix_reproduction=prefixes,
                  statistics_sha256=sha(output/"derived_statistics.json"),
                  fee_evidence="USER_REPORTED", independently_verified_account_fee=False,
                  actual_historical_fee_verified=False, slippage_verified=False,
                  account_verified=False, statistically_verified=False, promotion_eligible=False,
                  interpretation="Reuse freshly calculated accounts because all fee/slippage/delay values are identical. This adds user-reported fee evidence; it does not convert historical research into actual-account or independently verified execution evidence.")
    _immutable(output/"cost_evidence_bridge.json", _bytes(bridge))
    return bridge


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--costs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    result = execute(args.run,args.costs,args.output)
    print(json.dumps(dict(status=result["status"], output=str(args.output))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
