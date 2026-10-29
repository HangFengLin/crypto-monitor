"""Frozen equal-budget T28 comparisons; target transforms only, original replay."""
from __future__ import annotations

import argparse
import copy
import gc
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.t28_portfolio_compare import (  # noqa: E402
    PORTFOLIOS,
    STRATEGY,
    _full_summary,
    _verify,
    build_weekly_targets,
    load_data,
    read,
    replay_case,
    sha,
    target_cost,
    write_account,
)
from scripts.v1_forward_observer import _bytes, _immutable  # noqa: E402

PROTOCOL = ROOT / "research_projects/t28_equal_exposure_20261025/protocol.json"
MODES = ("budget_all_signals", "matched_joint_signals")


def transform_targets(targets, mode, portfolio, gross_budget=0.5):
    if mode not in MODES or portfolio not in PORTFOLIOS:
        raise ValueError("unknown frozen mode or portfolio")
    if isinstance(gross_budget, bool) or gross_budget != .5:
        raise ValueError("frozen gross budget must be 0.5")
    result = copy.deepcopy(targets)
    for original, event in zip(targets, result):
        weights = original["weights"]
        if (set(weights) != set(PORTFOLIOS["both"])
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or not 0 <= value <= .5 for value in weights.values())):
            raise ValueError("finite original capped BTC ETH weights required")
        active = [symbol for symbol in PORTFOLIOS[portfolio] if weights[symbol] > 0]
        if mode == "matched_joint_signals" and not all(weights[symbol] > 0 for symbol in PORTFOLIOS["both"]):
            active = []
        event["weights"] = dict.fromkeys(weights, 0.0)
        total = math.fsum(weights[symbol] for symbol in active)
        assigned = 0.0
        for index, symbol in enumerate(active):
            value = gross_budget - assigned if index == len(active) - 1 else gross_budget * weights[symbol] / total
            event["weights"][symbol] = value
            assigned += value
    return result


def exposure_audit(account, targets, tolerance=1e-10):
    if len(account["rebalances"]) != len(targets):
        raise ValueError("original decision event count changed")
    errors, clock_errors = [], 0
    delay_ms = account["scenario"]["extra_delay_minutes"] * 60_000
    for event, rebalance in zip(targets, account["rebalances"]):
        requested = math.fsum(event["weights"].values())
        if abs(requested - (0.5 if requested else 0.)) > 1e-12:
            raise ValueError("requested gross budget differs from frozen half")
        clock_errors += (rebalance["decision_time_ms"] != event["decision_time_ms"]
                         or rebalance["time"] != event["decision_time_ms"] + 900_000 + delay_ms
                         or rebalance["weights"] != event["weights"])
        errors.append(abs(rebalance["post_gross_notional"] / rebalance["post_equity"] - requested))
    worst = max(errors, default=0.)
    if clock_errors or worst > tolerance:
        raise ValueError("post-rebalance exposure or original execution clock mismatch")
    return dict(post_rebalance_max_abs_error=worst, clock_and_weight_mismatches=clock_errors,
                rebalances=len(targets), active_rebalances=sum(any(value > 0 for value in event["weights"].values()) for event in targets),
                mark="Raw open after fills/fees/slippage, before later within-bar funding; close exposure may drift")


def prepare(protocol_path, output):
    protocol_path, output = Path(protocol_path).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError("new preparation directory required")
    protocol = read(protocol_path)
    cost_ids = [scenario["id"] for scenario in protocol["cost_scenarios"]]
    if (protocol["modes"] != list(MODES) or protocol["gross_budget"] != .5
            or protocol["portfolios"] != {key: list(value) for key, value in PORTFOLIOS.items()}
            or len(protocol["datasets"]) != 2 or {item["id"] for item in protocol["datasets"]} != {"main", "cross"}
            or cost_ids != ["base", "double_cost", "delay15m"]):
        raise ValueError("only frozen two modes, three portfolios and costs across two windows allowed")
    if read(Path(protocol["costs_path"]))["scenarios"] != protocol["cost_scenarios"]:
        raise ValueError("fixed cost scenario mismatch")
    bindings = dict(protocol["source_bindings"])
    names = ("scripts/t28_equal_exposure.py", "tests/test_t28_equal_exposure.py", "scripts/t28_portfolio_compare.py",
             "scripts/t28_cost_review.py", "scripts/t28_cost_stats.py", "scripts/trend_pullback_backtest.py",
             "scripts/trend_pullback_forward.py", "scripts/v1_forward_observer.py", "research_portfolio.py", "position_manager.py",
             "research_projects/trend_portfolio_v1/target_weight_adapter.py", "research_projects/trend_portfolio_v1/run_study.py")
    for path in (protocol_path, Path(protocol["costs_path"]), *[ROOT / name for name in names]):
        bindings.setdefault(str(path), sha(path))
    _verify(bindings)
    targets, effective_protocols, counts, participation = {}, {}, {}, {}
    for dataset in protocol["datasets"]:
        previous = Path(dataset["prepared_directory"])
        effective = read(previous / "effective_protocol.json")
        if (effective["scoring"]["start_inclusive_utc"], effective["scoring"]["end_exclusive_utc"]) != (dataset["start"], dataset["end"]):
            raise ValueError("original continuous account window changed")
        for path in (previous / "effective_protocol.json", previous / "targets_before_pnl.json", Path(dataset["input_manifest"])):
            bindings.setdefault(str(path), sha(path))
        source = Path(dataset["input_manifest"])
        for item in read(source)["symbols"].values():
            for key in ("raw15m", "funding"):
                bindings.setdefault(str((source.parent / item[key]["path"]).resolve()), item[key]["sha256"])
        data, _funding = load_data(dataset)
        original = build_weekly_targets(data, effective, STRATEGY)
        if original != read(previous / "targets_before_pnl.json")[STRATEGY]:
            raise ValueError("rebuilt original signal targets differ from old frozen targets")
        identifier = dataset["id"]
        targets[identifier] = {mode: {portfolio: transform_targets(original, mode, portfolio, protocol["gross_budget"])
                                     for portfolio in PORTFOLIOS} for mode in MODES}
        effective_protocols[identifier] = effective
        counts[identifier] = {mode: {portfolio: len(events) for portfolio, events in groups.items()} for mode, groups in targets[identifier].items()}
        participation[identifier] = {mode: {portfolio: sum(any(value > 0 for value in event["weights"].values()) for event in events)
                                         for portfolio, events in groups.items()} for mode, groups in targets[identifier].items()}
        matched = targets[identifier]["matched_joint_signals"]
        schedules = [[math.fsum(event["weights"].values()) for event in events] for events in matched.values()]
        if any(schedule != schedules[0] for schedule in schedules[1:]):
            raise ValueError("matched joint participation differs across portfolios")
    _verify(bindings)
    _immutable(output / "targets_before_pnl.json", _bytes(targets))
    _immutable(output / "effective_protocols.json", _bytes(effective_protocols))
    frozen = dict(status="ALL_EQUAL_BUDGET_TARGETS_FROZEN_NO_PNL", protocol=protocol, source_sha256=bindings,
                  targets_sha256=sha(output / "targets_before_pnl.json"), effective_protocols_sha256=sha(output / "effective_protocols.json"),
                  target_counts=counts, decision_participation_counts=participation, pnl_computed=False, expected_accounts=36,
                  scope="Sizing changes; original momentum/vol features and weekly clocks retained. No future exposure scaling.")
    _immutable(output / "pre_pnl_manifest.json", _bytes(frozen))
    return frozen


def execute(prepared, output):
    prepared, output = Path(prepared).resolve(), Path(output).resolve()
    frozen = read(prepared / "pre_pnl_manifest.json")
    checks = {**frozen["source_sha256"], str(prepared / "targets_before_pnl.json"): frozen["targets_sha256"],
              str(prepared / "effective_protocols.json"): frozen["effective_protocols_sha256"]}
    _verify(checks)
    if output.exists():
        raise FileExistsError("new equal-exposure replay directory required")
    protocol, targets = frozen["protocol"], read(prepared / "targets_before_pnl.json")
    effective_protocols = read(prepared / "effective_protocols.json")
    _immutable(output / "run_manifest.json", _bytes(dict(source_and_input_sha256=checks, preparation_sha256=sha(prepared / "pre_pnl_manifest.json"),
        protocol=protocol, account_labels="Wrapper metadata only; original engine account schema retained")))
    (output / "accounts").mkdir()
    cases = []
    for dataset in protocol["datasets"]:
        data, funding = load_data(dataset)
        effective = effective_protocols[dataset["id"]]
        for mode in MODES:
            for portfolio in PORTFOLIOS:
                events = targets[dataset["id"]][mode][portfolio]
                for source_cost in protocol["cost_scenarios"]:
                    cost = source_cost["id"]
                    original_id = dataset["cost_mapping"][cost]
                    scenario = target_cost(dict(source_cost, id=original_id))
                    account = replay_case(data, funding, events, effective, scenario, portfolio)
                    audit = exposure_audit(account, events, protocol.get("tolerances", {}).get("post_rebalance_gross_over_equity", 1e-10))
                    account_path = output / "accounts" / f"{dataset['id']}__{mode}__{portfolio}__{cost}.json.gz"
                    digest = write_account(account_path, account)
                    cases.append(dict(dataset=dataset["id"], mode=mode, portfolio=portfolio, cost=cost, original_scenario_id=original_id,
                        account_path=str(account_path), account_sha256=digest, scope=dataset.get("scope"), exposure_audit=audit,
                        **_full_summary(account, data, effective)))
                    print(json.dumps(dict(dataset=dataset["id"], mode=mode, portfolio=portfolio, cost=cost, status="SAVED_EQUAL_BUDGET_ACCOUNT")), flush=True)
                    del account
                    gc.collect()
    if len(cases) != 36:
        raise ValueError("frozen 36-account matrix is incomplete")
    _verify(checks)
    result = dict(status="COMPLETE_EXPOSED_T28_EQUAL_BUDGET_COMPARISON", cases=cases,
                  account_verified=False, statistically_verified=False, promotion_eligible=False,
                  interpretation="B is joint-signal matched participation; A retains each portfolio own original signal opportunities. Fixed target exposure is not equal continuous exposure or risk. Both frozen families reported without choosing a winner.",
                  attribution="Both versus single changes retained ETH budget allocation as well as shared equity/fees/funding feedback; not contribution subtraction or pure BTC alpha.")
    _immutable(output / "summary.json", _bytes(result))
    _immutable(output / "artifact_manifest.json", _bytes(dict(source_and_input_sha256=checks,
        artifact_sha256={str(path.relative_to(output)): sha(path) for path in sorted(output.rglob("*")) if path.is_file()})))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "execute"))
    parser.add_argument("--protocol", type=Path, default=PROTOCOL)
    parser.add_argument("--prepared", type=Path, default=ROOT / "reports/t28_equal_exposure_20261025/prepared")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        os.nice(10)
    except OSError:
        pass
    result = prepare(args.protocol, args.output or args.prepared) if args.command == "prepare" else execute(
        args.prepared, args.output or ROOT / "reports/t28_equal_exposure_20261025/run")
    print(json.dumps(dict(status=result["status"])))


if __name__ == "__main__":
    main()
