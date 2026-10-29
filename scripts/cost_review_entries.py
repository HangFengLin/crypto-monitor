"""Reprice seven explicit historical entry expressions without changing rules.

The original four controls and three previously exposed references are fixed.
Costs come from a separately frozen source file; there is no parameter search,
new signal generation, live-account claim or prospective/OOS interpretation.
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_portfolio import replay_portfolio  # noqa: E402
from scripts.entry_screen import SCOPE, SYMBOLS, _bytes, _immutable, _sha, _timestamp  # noqa: E402
from scripts.exit_horizon_screen import (  # noqa: E402
    ENTRIES,
    HOLDS,
    POLICIES,
    _bound_json,
    _summarize,
    _verify_files,
    case_name,
    verify_control,
)
from scripts.trend_pullback_backtest import prepare_inputs  # noqa: E402
from scripts.trend_pullback_forward import validate_funding_coverage  # noqa: E402

FIXED_CASES = [dict(id=f"{entry}-legacy1R-h96", entry=entry, exit_policy="legacy1R", max_hold_bars=96,
                    table="original_controls") for entry in ENTRIES] + [
    dict(id="D48-target3-h672", entry="D48", exit_policy="target3", max_hold_bars=672, table="exposed_references"),
    dict(id="B20-target3-h288", entry="B20", exit_policy="target3", max_hold_bars=288, table="exposed_references"),
    dict(id="S24-target3-h672", entry="S24", exit_policy="target3", max_hold_bars=672, table="exposed_references"),
]


def validate_cases(cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError("explicit frozen case list required")
    identities, names = set(), set()
    for case in cases:
        if (case.get("entry") not in ENTRIES or case.get("exit_policy") not in POLICIES
                or case.get("max_hold_bars") not in HOLDS or not isinstance(case.get("id"), str)
                or re.fullmatch(r"[A-Za-z0-9_-]+", case["id"]) is None):
            raise ValueError("known frozen entry/exit/horizon and safe case id required")
        identity = (case["entry"], case["exit_policy"], case["max_hold_bars"])
        if identity in identities or case["id"] in names:
            raise ValueError("duplicate entry/exit/horizon case")
        identities.add(identity)
        names.add(case["id"])
    return cases


def prior_case_name(case, scenario):
    return case_name(case["entry"], case["exit_policy"], case["max_hold_bars"], scenario)


def pnl_decomposition(account, summary):
    initial = account["parameters"]["initial_equity"]
    final = account["equity"][-1]
    gross = sum(fill.get("gross_pnl", 0.) for fill in account["fills"])
    unrealized, fees, funding = final["unrealized_pnl"], summary["fees_usdt"], summary["funding_cashflow_usdt"]
    net = final["equity"] - initial
    error = net - (gross + unrealized - fees + funding)
    if abs(error) > 1e-7:
        raise ValueError("gross/fees/funding/equity reconciliation failed")
    return dict(gross_realized_pnl_usdt=gross, tail_unrealized_pnl_usdt=unrealized,
                gross_marked_before_fees_and_funding_usdt=gross + unrealized,
                gross_marked_before_fees_and_funding_return=(gross + unrealized) / initial,
                fees_usdt=fees, funding_cashflow_usdt=funding, net_marked_pnl_usdt=net,
                reconciliation_error_usdt=error, zero_cost_replay=False,
                scope="Accounting bridge on this scenario's actually simulated fills and quantities. Gross still "
                      "contains modeled execution slippage. Lower fees can change risk sizing and admissions; "
                      "this decomposition is not a separate zero-cost strategy replay.")


def _parameters(scenario):
    required = ("fee_rate", "slippage_bps", "delay_bars")
    for key in required:
        value = scenario.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("explicit finite cost and delay assumptions required")
    if (not 0 <= scenario["fee_rate"] < 1 or not 0 <= scenario["slippage_bps"] < 10_000
            or not isinstance(scenario["delay_bars"], int) or scenario["delay_bars"] < 0):
        raise ValueError("invalid fixed cost scenario")
    if not isinstance(scenario.get("id"), str) or re.fullmatch(r"[A-Za-z0-9_-]+", scenario["id"]) is None:
        raise ValueError("safe explicit scenario id required")
    return {key: scenario[key] for key in required}


def run_cases(data, events, funding, cases, scenarios, execution, prior_protocol, prior_run, output, recent_start, *, progress=False):
    validate_cases(cases)
    prior_run, output = Path(prior_run), Path(output)
    policies = {policy["id"]: policy for policy in prior_protocol["exit_policies"]}
    rows, controls = {}, {}
    scenario_ids = [scenario["id"] for scenario in scenarios]
    if len(set(scenario_ids)) != len(scenario_ids):
        raise ValueError("duplicate cost scenario id")
    for case in cases:
        policy = policies[case["exit_policy"]]
        for scenario in scenarios:
            name = f"{case['id']}-{scenario['id']}"
            costs = _parameters(scenario)
            parameters = {**execution, "stop_mode": policy["stop_mode"], "reward_risk": policy["reward_risk"],
                          "max_hold_bars": case["max_hold_bars"], **costs}
            account = replay_portfolio(data, events[case["entry"]], funding, **parameters,
                                       strict_funding=True, finalize=False)
            account_path = output / f"accounts/{name}.json"
            _immutable(account_path, _bytes(account))
            for previous in prior_protocol["scenarios"]:
                if costs == {key: previous[key] for key in costs}:
                    reference = prior_run / f"accounts/{prior_case_name(case, previous['id'])}.json"
                    controls[name] = verify_control(reference, account_path)
            summary = _summarize(account, data, recent_start)
            summary.update(case=case, scenario=scenario["id"], cost_assumptions=scenario,
                           account_fee_verified=scenario.get("account_fee_verified") is True,
                           pnl_decomposition=pnl_decomposition(account, summary))
            rows[name] = summary
            if progress:
                print(json.dumps(dict(account_completed=name, net_return=summary["net_return"],
                                      recent_net_return=summary["recent"]["net_return"],
                                      net_win_rate=summary["trade_diagnostics"]["net_win_rate"],
                                      payoff_ratio=summary["trade_diagnostics"]["payoff_ratio"],
                                      closed_trades=summary["closed_trades"]), allow_nan=False), flush=True)
            del account
            gc.collect()
    return dict(rows=rows, controls=controls)


def freeze(entry_run, exit_run, output):
    """Copy only frozen event arrays and the explicit exposed case plan; no PnL."""
    entry_run, exit_run, output = Path(entry_run).resolve(), Path(exit_run).resolve(), Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("new empty frozen-event directory required")
    entry_manifest = json.loads((entry_run / "run_manifest.json").read_bytes())
    exit_manifest = json.loads((exit_run / "run_manifest.json").read_bytes())
    bindings = dict(exit_manifest["frozen_bindings"])
    bindings[str(Path(__file__).resolve())] = _sha(Path(__file__).read_bytes())
    for directory in (entry_run, exit_run):
        for name in ("run_manifest.json", "protocol.json", "artifact_hashes.json"):
            path = directory / name
            bindings[str(path)] = _sha(path.read_bytes())
    entry_artifacts = json.loads((entry_run / "artifact_hashes.json").read_bytes())
    exit_artifacts = json.loads((exit_run / "artifact_hashes.json").read_bytes())
    bindings[str(entry_run / "signals_before_pnl.json")] = entry_artifacts["signals_before_pnl.json"]
    for name in ("frozen_entry_events.json", "summary.json"):
        bindings[str(exit_run / name)] = exit_artifacts[name]
    cases = validate_cases(FIXED_CASES)
    previous_protocol = json.loads((exit_run / "protocol.json").read_bytes())
    for case in cases:
        for scenario in previous_protocol["scenarios"]:
            name = f"accounts/{prior_case_name(case, scenario['id'])}.json"
            bindings[str(exit_run / name)] = exit_artifacts[name]
    _verify_files(bindings)
    original = json.loads((entry_run / "signals_before_pnl.json").read_bytes())
    events = {entry: original["events"][entry] for entry in ENTRIES}
    if events != json.loads((exit_run / "frozen_entry_events.json").read_bytes()):
        raise ValueError("original entry event arrays differ between prior frozen studies")
    frozen = dict(schema_version=1, scope=SCOPE, status="FROZEN_COST_PENDING", entry_run=str(entry_run),
                  exit_run=str(exit_run), start=entry_manifest["start"], end=entry_manifest["end"],
                  recent_start=entry_manifest["recent_start"], frozen_bindings=bindings,
                  input_manifest=exit_manifest["input_manifest"], cases=cases,
                  source_event_counts={entry: len(items) for entry, items in events.items()},
                  event_array_sha256={entry: _sha(_bytes(items)) for entry, items in events.items()},
                  selection_scope="Four original controls plus three references chosen from already inspected "
                                  "prior base-cost losses. No new cost results or parameter search used in this plan.",
                  account_verified=False, statistically_verified=False, promotion_eligible=False)
    _immutable(output / "manifest.json", _bytes(frozen))
    _immutable(output / "events.json", _bytes(events))
    _immutable(output / "cases.json", _bytes(cases))
    return frozen


def run(frozen_directory, protocol_path, costs_path, output, *, progress=False):
    frozen_directory, protocol_path, costs_path = map(lambda p: Path(p).resolve(),
                                                     (frozen_directory, protocol_path, costs_path))
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("new empty output directory required")
    frozen_bytes = (frozen_directory / "manifest.json").read_bytes()
    frozen = json.loads(frozen_bytes)
    bindings = dict(frozen["frozen_bindings"])
    for path in (frozen_directory / "manifest.json", frozen_directory / "events.json",
                 frozen_directory / "cases.json", protocol_path, costs_path, Path(__file__).resolve()):
        bindings[str(path)] = _sha(path.read_bytes())
    _verify_files(bindings)
    protocol, costs = json.loads(protocol_path.read_bytes()), json.loads(costs_path.read_bytes())
    scenarios = costs.get("scenarios", costs.get("cost_scenarios"))
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("separately frozen explicit scenarios required before PnL")
    for scenario in scenarios:
        _parameters(scenario)
    if protocol.get("scope") != SCOPE or protocol.get("research_only") is not True:
        raise ValueError("historical exploratory research-only cost protocol required")
    if any(_timestamp(protocol[key]) != frozen[key] for key in ("start", "end", "recent_start")):
        raise ValueError("dates must match the unchanged original entry study")
    cases = validate_cases(protocol.get("entry_cases", frozen["cases"]))
    if {(c["entry"], c["exit_policy"], c["max_hold_bars"]) for c in cases} != {
            (c["entry"], c["exit_policy"], c["max_hold_bars"]) for c in frozen["cases"]}:
        raise ValueError("cost review must retain the seven frozen cases without new selection")
    entry_run, exit_run = Path(frozen["entry_run"]), Path(frozen["exit_run"])
    previous_protocol = json.loads((exit_run / "protocol.json").read_bytes())
    execution = protocol.get("execution", previous_protocol["execution"])
    if execution != previous_protocol["execution"]:
        raise ValueError("cost review cannot change original risk/execution rules")
    events = json.loads((frozen_directory / "events.json").read_bytes())
    if any(_sha(_bytes(events[entry])) != frozen["event_array_sha256"][entry] for entry in ENTRIES):
        raise ValueError("frozen event array SHA drift")
    if json.loads((frozen_directory / "cases.json").read_bytes()) != frozen["cases"]:
        raise ValueError("frozen case selection drift")
    source = Path(frozen["input_manifest"])
    input_bytes = source.read_bytes()
    entry_manifest = json.loads((entry_run / "run_manifest.json").read_bytes())
    if _sha(input_bytes) != entry_manifest["input_manifest_sha256"]:
        raise ValueError("original input manifest SHA mismatch")
    inputs = json.loads(input_bytes)
    manifest = dict(schema_version=1, scope=SCOPE, status="RESEARCH_ONLY", frozen_bindings=bindings,
                    frozen_event_manifest_sha256=_sha(frozen_bytes), cases=cases, scenarios=scenarios,
                    account_replays=len(cases) * len(scenarios), entry_rules_unchanged=True, single_worker=True,
                    account_verified=False, statistically_verified=False, promotion_eligible=False)
    _immutable(output / "run_manifest.json", _bytes(manifest))
    _immutable(output / "protocol.json", protocol_path.read_bytes())
    _immutable(output / "costs.json", costs_path.read_bytes())
    _immutable(output / "events.json", _bytes(events))
    data, funding = {}, {}
    fields = ("open_time", "close_time", "open", "high", "low", "close", "volume", "atr")
    for symbol in SYMBOLS:
        item = inputs["symbols"][symbol]
        lower = _bound_json(source.parent, item["raw15m"])
        higher = _bound_json(source.parent, item["raw4h"])
        prepared = prepare_inputs(lower, higher, frozen["start"], frozen["end"])
        data[symbol] = [{key: row[key] for key in fields} for row in prepared
                        if frozen["start"] <= row["open_time"] < frozen["end"]]
        funding[symbol] = _bound_json(source.parent, item["funding"])
        validate_funding_coverage(funding[symbol], symbol, frozen["start"], frozen["end"])
        del lower, higher, prepared
        gc.collect()
    _verify_files(bindings)
    results = run_cases(data, events, funding, cases, scenarios, execution, previous_protocol,
                        exit_run, output, frozen["recent_start"], progress=progress)
    _verify_files(bindings)
    result = dict(scope=SCOPE, status="RESEARCH_ONLY", **results,
                  frozen_event_counts=frozen["source_event_counts"],
                  cost_verification={key: costs.get(key) for key in (
                      "status", "actual_account_fee_verified", "slippage_verified", "not_actual_cost_validation")},
                  account_verified=False, statistically_verified=False, promotion_eligible=False,
                  interpretation="Cost sensitivity on frozen, already exposed histories and selected references. "
                                 "Scenario fee verification is separate from real-account/strategy verification. "
                                 "All trade rules, account risk and event timestamps remain unchanged.")
    _immutable(output / "summary.json", _bytes(result))
    paths = [output / "run_manifest.json", output / "protocol.json", output / "costs.json", output / "events.json",
             output / "summary.json", *(output / "accounts").glob("*.json")]
    _immutable(output / "artifact_hashes.json", _bytes({str(path.relative_to(output)): _sha(path.read_bytes()) for path in paths}))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze", "run"))
    parser.add_argument("--entry-run", type=Path, default=ROOT / "reports/entry_screen_20261025/run")
    parser.add_argument("--exit-run", type=Path, default=ROOT / "reports/exit_horizon_20261025/run")
    parser.add_argument("--frozen-directory", type=Path)
    parser.add_argument("--protocol", type=Path, default=ROOT / "research_projects/cost_review_20261025/protocol.json")
    parser.add_argument("--costs", type=Path, default=ROOT / "research_projects/cost_review_20261025/costs.json")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        os.nice(10)
    except OSError:
        pass
    if args.command == "freeze":
        result = freeze(args.entry_run, args.exit_run, args.output)
    else:
        if args.frozen_directory is None:
            parser.error("run requires --frozen-directory")
        result = run(args.frozen_directory, args.protocol, args.costs, args.output, progress=True)
    print(json.dumps(dict(status=result["status"], scope=result["scope"], output=str(args.output))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
