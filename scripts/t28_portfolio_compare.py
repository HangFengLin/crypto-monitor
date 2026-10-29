"""Fixed T28 portfolios: remove excluded target weights into cash, never rescale."""
from __future__ import annotations

import argparse
import copy
import gc
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_projects.trend_portfolio_v1.run_study import calendar_metrics, read, write  # noqa: E402
from research_projects.trend_portfolio_v1.target_weight_adapter import (  # noqa: E402
    build_weekly_targets,
    replay_target_weights,
    utc_ms,
)
from scripts.t28_cost_review import episode_statistics, sha, target_cost  # noqa: E402
from scripts.t28_cost_stats import cash_profit_factor  # noqa: E402
from scripts.trend_pullback_backtest import _load_bound  # noqa: E402
from scripts.trend_pullback_forward import _parse, validate_funding_coverage  # noqa: E402
from scripts.v1_forward_observer import _bytes, _immutable  # noqa: E402

PROTOCOL = ROOT / "research_projects/t28_portfolio_compare_20261025/protocol.json"
PORTFOLIOS = {"both": ("BTCUSDT", "ETHUSDT"), "btc_only": ("BTCUSDT",), "eth_only": ("ETHUSDT",)}
STRATEGY = "T28_long_cash"


def mask_targets(targets, portfolio):
    if portfolio not in PORTFOLIOS:
        raise ValueError("unknown fixed portfolio")
    result = copy.deepcopy(targets)
    for event in result:
        if set(event["weights"]) != set(PORTFOLIOS["both"]):
            raise ValueError("complete original BTC ETH target weights required")
        for symbol in event["weights"]:
            if symbol not in PORTFOLIOS[portfolio]:
                event["weights"][symbol] = 0.0
    return result


def replay_case(data, funding, targets, effective, scenario, portfolio):
    if portfolio not in PORTFOLIOS or set(data) != set(PORTFOLIOS["both"]):
        raise ValueError("retain full BTC ETH source population")
    excluded = set(PORTFOLIOS["both"]) - set(PORTFOLIOS[portfolio])
    if any(event["weights"][symbol] != 0 for event in targets for symbol in excluded):
        raise ValueError("excluded target weights must be exactly zero")
    account = replay_target_weights(data, funding, targets, effective, scenario)
    for symbol in excluded:
        if (any(row["symbol"] == symbol for row in account["fills"])
                or any(row["quantities"][symbol] != 0 for row in account["equity"])
                or any(row["held_quantity"] != 0 or row["cashflow"] != 0 for row in account["funding"] if row["symbol"] == symbol)
                or any(account["by_symbol"][symbol][key] != 0 for key in ("fees", "funding", "net_pnl"))):
            raise ValueError("excluded symbol has nonzero account activity")
    return account


def write_account(path, account, control=None):
    """Use the original deterministic serializer and never add wrapper labels."""
    write(Path(path), account)
    digest = sha(path)
    if control is not None and digest != sha(control):
        raise ValueError("both control account is not byte-exact to original")
    return digest


def _verify(bindings):
    for path, expected in bindings.items():
        if sha(path) != expected:
            raise ValueError(f"frozen source/input hash drift: {path}")


def load_data(dataset):
    source = Path(dataset["input_manifest"])
    document, end = read(source), utc_ms(dataset["end"])
    if set(document["symbols"]) != set(PORTFOLIOS["both"]):
        raise ValueError("complete unchanged BTC ETH input population required")
    data, funding = {}, {}
    for symbol, item in document["symbols"].items():
        data[symbol] = _parse(_load_bound(source.parent, item["raw15m"]), "15m", end)
        funding[symbol] = _load_bound(source.parent, item["funding"])
        if "start" in dataset:
            validate_funding_coverage(funding[symbol], symbol, utc_ms(dataset["start"]), end)
    return data, funding


def prepare(protocol_path, output):
    protocol_path, output = Path(protocol_path).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError("new preparation directory required")
    protocol = read(protocol_path)
    cost_ids = [scenario["id"] for scenario in protocol["cost_scenarios"]]
    if (len(protocol["datasets"]) != 2 or {dataset["id"] for dataset in protocol["datasets"]} != {"main", "cross"}
            or cost_ids != ["base", "double_cost", "delay15m"]):
        raise ValueError("fixed separate main/cross and original three costs required")
    if (protocol.get("portfolios", {key: list(value) for key, value in PORTFOLIOS.items()}) != {key: list(value) for key, value in PORTFOLIOS.items()}
            or protocol.get("strategy_id", STRATEGY) != STRATEGY):
        raise ValueError("only three fixed T28 masks are allowed")
    costs = read(Path(protocol["costs_path"]))
    if costs["scenarios"] != protocol["cost_scenarios"]:
        raise ValueError("frozen cost scenarios differ from cost file")
    bindings = dict(protocol["source_bindings"])
    for path in (protocol_path, Path(protocol["costs_path"]), Path(__file__).resolve(),
                 ROOT / "tests/test_t28_portfolio_compare.py", ROOT / "scripts/t28_cost_review.py",
                 ROOT / "scripts/t28_cost_stats.py", ROOT / "scripts/trend_pullback_backtest.py",
                 ROOT / "scripts/trend_pullback_forward.py", ROOT / "scripts/v1_forward_observer.py",
                 ROOT / "research_projects/trend_portfolio_v1/target_weight_adapter.py",
                 ROOT / "research_projects/trend_portfolio_v1/run_study.py",
                 ROOT / "research_portfolio.py", ROOT / "position_manager.py"):
        bindings.setdefault(str(path), sha(path))
    _verify(bindings)
    targets, effective_protocols, counts = {}, {}, {}
    for dataset in protocol["datasets"]:
        previous = Path(dataset["prepared_directory"])
        effective = read(previous / "effective_protocol.json")
        if (effective["scoring"]["start_inclusive_utc"], effective["scoring"]["end_exclusive_utc"]) != (dataset["start"], dataset["end"]):
            raise ValueError("original continuous account window changed")
        for path in (previous / "effective_protocol.json", previous / "targets_before_pnl.json", Path(dataset["input_manifest"])):
            bindings.setdefault(str(path), sha(path))
        source = Path(dataset["input_manifest"])
        document = read(source)
        for item in document["symbols"].values():
            for key in ("raw15m", "funding"):
                bindings.setdefault(str((source.parent / item[key]["path"]).resolve()), item[key]["sha256"])
        data, _funding = load_data(dataset)
        original_targets = build_weekly_targets(data, effective, STRATEGY)
        if original_targets != read(previous / "targets_before_pnl.json")[STRATEGY]:
            raise ValueError("rebuilt original T28 targets differ from frozen old targets")
        targets[dataset["id"]] = {portfolio: mask_targets(original_targets, portfolio) for portfolio in PORTFOLIOS}
        effective_protocols[dataset["id"]] = effective
        counts[dataset["id"]] = {portfolio: len(events) for portfolio, events in targets[dataset["id"]].items()}
        for cost in cost_ids:
            old = Path(dataset["account_directory"]) / f"{STRATEGY}__{dataset['cost_mapping'][cost]}.json.gz"
            bindings.setdefault(str(old), sha(old))
    _verify(bindings)
    _immutable(output / "targets_before_pnl.json", _bytes(targets))
    _immutable(output / "effective_protocols.json", _bytes(effective_protocols))
    frozen = dict(status="ALL_PORTFOLIO_TARGETS_FROZEN_NO_PNL", protocol=protocol, source_sha256=bindings,
                  targets_sha256=sha(output / "targets_before_pnl.json"),
                  effective_protocols_sha256=sha(output / "effective_protocols.json"), target_counts=counts,
                  pnl_computed=False, expected_accounts=18, mask_scope="Only excluded weights become zero; no allocation transfer, feature or timing change")
    _immutable(output / "pre_pnl_manifest.json", _bytes(frozen))
    return frozen


def _full_summary(account, data, effective):
    curve = account["equity"]
    closes = {symbol: {row["close_time"] + 1: row["close"] for row in bars} for symbol, bars in data.items()}
    exposure = {symbol: sum(point["quantities"][symbol] * closes[symbol][point["time"]] / point["equity"] for point in curve) / len(curve) for symbol in data}
    episodes = episode_statistics(account)
    return dict(net_return=account["net_return"], max_close_drawdown=account["max_drawdown"],
                initial_equity=account["initial_equity"], final_equity=account["final_equity"],
                total_fees=account["total_fees"], funding_cashflow=account["funding_cashflow"],
                realized_gross_pnl=account["realized_gross_pnl"],
                marked_gross_pnl_before_fees_funding=account["final_equity"] - account["initial_equity"] + account["total_fees"] - account["funding_cashflow"],
                by_symbol=account["by_symbol"], final_positions=account["final_positions"], fills=len(account["fills"]),
                calendar=calendar_metrics(curve, account["initial_equity"], effective["scoring"]),
                episode_diagnostics=episodes, cash_profit_factor=cash_profit_factor(episodes["closed"]),
                mean_symbol_notional_over_equity=exposure,
                mean_gross_notional_over_equity=sum(point["gross_over_equity"] for point in curve) / len(curve),
                max_gross_notional_over_equity=account["max_close_gross_over_equity"],
                turnover_over_initial_equity=account["turnover_over_initial_equity"],
                mean_unallocated_nominal_budget=1 - sum(point["gross_over_equity"] for point in curve) / len(curve),
                mean_collateral_cash_over_equity=sum(point["cash"] / point["equity"] for point in curve) / len(curve),
                exposure_scope="Unallocated nominal budget is 1-gross/equity; perpetual collateral cash/equity is distinct and does not subtract notional principal")


def execute(prepared, output):
    prepared, output = Path(prepared).resolve(), Path(output).resolve()
    frozen = read(prepared / "pre_pnl_manifest.json")
    checks = {**frozen["source_sha256"], str(prepared / "targets_before_pnl.json"): frozen["targets_sha256"],
              str(prepared / "effective_protocols.json"): frozen["effective_protocols_sha256"]}
    _verify(checks)
    if output.exists():
        raise FileExistsError("new portfolio replay directory required")
    protocol, targets = frozen["protocol"], read(prepared / "targets_before_pnl.json")
    effective_protocols = read(prepared / "effective_protocols.json")
    source_costs = {scenario["id"]: scenario for scenario in protocol["cost_scenarios"]}
    _immutable(output / "run_manifest.json", _bytes(dict(status="STARTED_FROZEN_PORTFOLIO_COMPARISON", source_and_input_sha256=checks,
        preparation_sha256=sha(prepared / "pre_pnl_manifest.json"), protocol=protocol, account_labels="Wrapper metadata only; original account unchanged")))
    (output / "accounts").mkdir()
    cases, controls = [], {}
    # Complete all six byte controls before any single-symbol account is replayed.
    for portfolio in PORTFOLIOS:
        if portfolio != "both" and len(controls) != 6:
            raise ValueError("all six both control hashes must pass before single portfolios")
        for dataset in protocol["datasets"]:
            data, funding = load_data(dataset)
            effective = effective_protocols[dataset["id"]]
            for cost in source_costs:
                original_id = dataset["cost_mapping"][cost]
                scenario = target_cost(dict(source_costs[cost], id=original_id))
                account = replay_case(data, funding, targets[dataset["id"]][portfolio], effective, scenario, portfolio)
                filename = f"{dataset['id']}__{portfolio}__{cost}.json.gz"
                account_path = output / "accounts" / filename
                control = Path(dataset["account_directory"]) / f"{STRATEGY}__{original_id}.json.gz" if portfolio == "both" else None
                digest = write_account(account_path, account, control)
                if control:
                    controls[f"{dataset['id']}__{cost}"] = digest
                cases.append(dict(dataset=dataset["id"], portfolio=portfolio, cost=cost, original_scenario_id=original_id,
                    account_path=str(account_path), account_sha256=digest, both_control_exact=True if control else None,
                    scope=dataset.get("scope"), **_full_summary(account, data, effective)))
                print(json.dumps(dict(dataset=dataset["id"], portfolio=portfolio, cost=cost, status="SAVED_FROZEN_PORTFOLIO_ACCOUNT")), flush=True)
                del account
                gc.collect()
    _verify(checks)
    result = dict(status="COMPLETE_EXPOSED_T28_PORTFOLIO_COMPARISON", cases=cases, both_control_sha256=controls,
                  account_verified=False, statistically_verified=False, promotion_eligible=False,
                  interpretation="Exposed historical accounts, unchanged single-asset target rules. Removed allocation stays unallocated; independent portfolio replays include equity feedback and cannot equal contribution subtraction.",
                  tail="Open positions marked without artificial liquidation; wrapper labels are outside original account objects")
    _immutable(output / "summary.json", _bytes(result))
    _immutable(output / "artifact_manifest.json", _bytes(dict(source_and_input_sha256=checks,
        artifact_sha256={str(path.relative_to(output)): sha(path) for path in sorted(output.rglob("*")) if path.is_file()})))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "execute"))
    parser.add_argument("--protocol", type=Path, default=PROTOCOL)
    parser.add_argument("--prepared", type=Path, default=ROOT / "reports/t28_portfolio_compare_20261025/prepared")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        os.nice(10)
    except OSError:
        pass
    if args.command == "prepare":
        result = prepare(args.protocol, args.output or args.prepared)
    else:
        result = execute(args.prepared, args.output or ROOT / "reports/t28_portfolio_compare_20261025/run")
    print(json.dumps(dict(status=result["status"])))


if __name__ == "__main__":
    main()
