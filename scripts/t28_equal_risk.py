"""Causal 60-day forecast-risk targets for fixed T28 portfolios; original engine."""
from __future__ import annotations

import argparse
import bisect
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

from research_projects.trend_portfolio_v1.target_weight_adapter import aggregate_daily  # noqa: E402
from scripts.t28_equal_exposure import MODES  # noqa: E402
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

PROTOCOL = ROOT / "research_projects/t28_equal_risk_20261025/protocol.json"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
DAY, STEP = 86_400_000, 900_000


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def estimate_covariance(closes):
    if set(closes) != set(SYMBOLS) or any(len(closes[s]) != 61 for s in SYMBOLS):
        raise ValueError("61 aligned daily closes are required")
    if any(not _finite(value) or value <= 0 for s in SYMBOLS for value in closes[s]):
        raise ValueError("finite positive daily close required")
    returns = [[math.log(b / a) for a, b in zip(closes[s], closes[s][1:])] for s in SYMBOLS]
    means = [math.fsum(values) / 60 for values in returns]
    centered = [[value - mean for value in values] for values, mean in zip(returns, means)]
    matrix = [[365 / 59 * math.fsum(a * b for a, b in zip(left, right)) for right in centered] for left in centered]
    _validate_covariance(matrix)
    return matrix


def _validate_covariance(matrix):
    if len(matrix) != 2 or any(len(row) != 2 for row in matrix) or any(not _finite(value) for row in matrix for value in row):
        raise ValueError("finite 2x2 covariance required")
    scale = max(abs(value) for row in matrix for value in row)
    if abs(matrix[0][1] - matrix[1][0]) > 1e-10 * scale:
        raise ValueError("covariance must be symmetric")
    if matrix[0][0] < 0 or matrix[1][1] < 0:
        raise ValueError("negative covariance diagonal variance")
    minimum = (matrix[0][0] + matrix[1][1] - math.hypot(matrix[0][0] - matrix[1][1], 2 * matrix[0][1])) / 2
    if minimum < -1e-12 * scale:
        raise ValueError("covariance is materially non-PSD")


def _variance(weights, matrix):
    return math.fsum(weights[left] * matrix[i][j] * weights[right]
                     for i, left in enumerate(SYMBOLS) for j, right in enumerate(SYMBOLS))


def allocate_event(original, covariance):
    _validate_covariance(covariance)
    weights = original["weights"]
    if set(weights) != set(SYMBOLS) or any(not _finite(value) or not 0 <= value <= .5 for value in weights.values()):
        raise ValueError("finite original capped weights required")
    diagonal_errors = {}
    for i, symbol in enumerate(SYMBOLS):
        volatility = original["features"][symbol]["annualized_volatility"]
        if not _finite(volatility) or volatility < 0:
            raise ValueError("finite original volatility required")
        diagonal_errors[symbol] = abs(covariance[i][i] - volatility ** 2)
        if diagonal_errors[symbol] > 1e-12:
            raise ValueError("covariance diagonal differs from original volatility squared")
    details, eligible = {}, []
    for portfolio, symbols in PORTFOLIOS.items():
        active = [symbol for symbol in symbols if weights[symbol] > 0]
        direction = dict.fromkeys(SYMBOLS, 0.)
        if active:
            total = math.fsum(weights[symbol] for symbol in active)
            for symbol in active:
                direction[symbol] = weights[symbol] / total
            variance = _variance(direction, covariance)
            if variance < -1e-12 * max(abs(value) for row in covariance for value in row):
                raise ValueError("materially negative forecast variance")
            if variance <= 0:
                raise ValueError("ZERO_FORECAST_RISK: eligible direction has no positive forecast variance")
            sigma = math.sqrt(variance)
            eligible.append(portfolio)
        else:
            variance = sigma = None
        details[portfolio] = dict(eligible=bool(active), direction=direction, direction_variance=variance,
            direction_sigma=sigma, cap_feasible_risk=.5 * sigma if sigma is not None else None)
    common = min([.1, *[details[p]["cap_feasible_risk"] for p in eligible]]) if eligible else 0.
    events = {}
    for portfolio, detail in details.items():
        event = copy.deepcopy(original)
        target = dict.fromkeys(SYMBOLS, 0.)
        gross = common / detail["direction_sigma"] if detail["eligible"] else 0.
        active = [symbol for symbol in SYMBOLS if detail["direction"][symbol] > 0]
        assigned = 0.
        for index, symbol in enumerate(active):
            target[symbol] = gross - assigned if index == len(active) - 1 else gross * detail["direction"][symbol]
            assigned += target[symbol]
        target_variance = _variance(target, covariance)
        if detail["eligible"] and target_variance <= 0:
            raise ValueError("ZERO_FORECAST_RISK: eligible scaled target variance is not positive")
        forecast = math.sqrt(target_variance) if detail["eligible"] else 0.
        if (math.fsum(target.values()) > .5 + 1e-12 or any(not 0 <= value <= .5 for value in target.values())
                or abs(forecast - (common if detail["eligible"] else 0.)) > 1e-10):
            raise ValueError("forecast risk target or frozen gross/asset cap mismatch")
        event["weights"] = target
        events[portfolio] = event
        detail.update(target_gross=gross, target_weights=target, forecast_risk=forecast)
    joint = all(weights[symbol] > 0 for symbol in SYMBOLS)
    return events, dict(decision_time_ms=original["decision_time_ms"], known_at=original["known_at"],
        symbols=list(SYMBOLS), covariance_annual=covariance, covariance_diagonal_errors=diagonal_errors,
        original_active={symbol: weights[symbol] > 0 for symbol in SYMBOLS},
        A_eligible_portfolios=eligible, common_target_risk=common, B_joint_eligible=joint,
        B_common_target_risk=common if joint else 0., portfolios=details,
        target_risk_attained=bool(eligible) and abs(common - .1) <= 1e-10,
        binding_portfolios=[p for p in eligible if abs(details[p]["cap_feasible_risk"] - common) <= 1e-10],
        status="CAUSAL_RISK_TARGETS" if eligible else "NO_ELIGIBLE_SIGNAL_CASH")


def build_risk_targets(data, original_targets):
    daily = aggregate_daily(data)
    if set(daily) != set(SYMBOLS):
        raise ValueError("complete original BTC ETH data required")
    ends = [row["end_ms"] for row in daily[SYMBOLS[0]]]
    result = {mode: {portfolio: [] for portfolio in PORTFOLIOS} for mode in MODES}
    features = []
    for original in original_targets:
        clock = original["decision_time_ms"]
        known = bisect.bisect_right(ends, clock)
        if known < 61 or ends[known - 1] != clock or original["known_at"] != clock:
            raise ValueError("complete decision-known 61-close covariance history required")
        closes = {symbol: [row["close"] for row in daily[symbol][known - 61:known]] for symbol in SYMBOLS}
        covariance = estimate_covariance(closes)
        events, sidecar = allocate_event(original, covariance)
        sidecar.update(close_end_ms=ends[known - 61:known], n_returns=60, ddof=1, annualization=365)
        features.append(sidecar)
        for portfolio, event in events.items():
            result[MODES[0]][portfolio].append(event)
            joint_event = copy.deepcopy(event if sidecar["B_joint_eligible"] else original)
            if not sidecar["B_joint_eligible"]:
                joint_event["weights"] = dict.fromkeys(SYMBOLS, 0.)
            result[MODES[1]][portfolio].append(joint_event)
    return result, features


def postfill_risk_audit(account, data, events, features, mode, portfolio):
    if len(events) != len(features) or len(events) != len(account["rebalances"]):
        raise ValueError("risk target and rebalance event counts differ")
    opens = {symbol: {row["open_time"]: row["open"] for row in bars} for symbol, bars in data.items()}
    points = {point["time"]: point for point in account["equity"]}
    risk_error = weight_error = gross_error = 0.
    delay = STEP + account["scenario"]["extra_delay_minutes"] * 60_000
    for event, risk, rebalance in zip(events, features, account["rebalances"]):
        clock = event["decision_time_ms"] + delay
        if (rebalance["time"] != clock or rebalance["decision_time_ms"] != event["decision_time_ms"]
                or rebalance["weights"] != event["weights"] or risk["decision_time_ms"] != event["decision_time_ms"]):
            raise ValueError("original execution clock or risk sidecar mismatch")
        point = points[clock + STEP]
        actual = {symbol: point["quantities"][symbol] * opens[symbol][clock] / rebalance["post_equity"] for symbol in SYMBOLS}
        weight_error = max(weight_error, *(abs(actual[s] - event["weights"][s]) for s in SYMBOLS))
        expected = risk["common_target_risk"] if mode == MODES[0] and risk["portfolios"][portfolio]["eligible"] else risk["B_common_target_risk"] if mode == MODES[1] else 0.
        actual_variance = _variance(actual, risk["covariance_annual"])
        if expected > 0 and actual_variance <= 0:
            raise ValueError("ZERO_FORECAST_RISK: eligible postfill variance is not positive")
        forecast = math.sqrt(actual_variance) if expected > 0 else 0.
        risk_error = max(risk_error, abs(forecast - expected))
        gross_error = max(gross_error, max(0., math.fsum(actual.values()) - .5))
    if max(risk_error, weight_error) > 1e-10 or gross_error > 1e-12:
        raise ValueError("postfill forecast-risk or asset-weight mismatch")
    return dict(max_postfill_forecast_risk_error=risk_error, max_postfill_asset_weight_error=weight_error,
                max_postfill_gross_cap_excess=gross_error, rebalances=len(events),
                model="Decision-known covariance applied to raw-open weights after original fill friction")


def prepare(protocol_path, output):
    protocol_path, output = Path(protocol_path).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError("new preparation directory required")
    protocol = read(protocol_path)
    if (protocol["modes"] != list(MODES) or protocol["gross_cap"] != .5 or protocol["per_asset_cap"] != .5
            or protocol["target_annual_volatility"] != .1
            or protocol["portfolios"] != {key: list(value) for key, value in PORTFOLIOS.items()}
            or len(protocol["datasets"]) != 2 or {d["id"] for d in protocol["datasets"]} != {"main", "cross"}
            or [c["id"] for c in protocol["cost_scenarios"]] != ["base", "double_cost", "delay15m"]):
        raise ValueError("fixed36 causal-risk comparison protocol required")
    model = protocol["risk_model"]
    if (model["lookback_returns"], model["close_observations"], model["ddof"], model["annualization_days"]) != (60, 61, 1, 365):
        raise ValueError("frozen60-return sample covariance required")
    expected_tolerances = dict(covariance_diagonal_absolute=1e-12, covariance_relative=1e-10,
                               covariance_psd_relative=1e-12, forecast_risk_absolute=1e-10,
                               raw_open_weight_absolute=1e-10, target_gross_cap_absolute=1e-12)
    if any(protocol["tolerances"][key] != value for key, value in expected_tolerances.items()):
        raise ValueError("frozen numerical risk tolerances changed")
    if read(Path(protocol["costs_path"]))["scenarios"] != protocol["cost_scenarios"]:
        raise ValueError("frozen cost scenario mismatch")
    bindings = dict(protocol["source_bindings"])
    names = ("scripts/t28_equal_risk.py", "tests/test_t28_equal_risk.py", "scripts/t28_equal_exposure.py",
             "scripts/t28_portfolio_compare.py", "scripts/t28_cost_review.py", "scripts/t28_cost_stats.py",
             "scripts/trend_pullback_backtest.py", "scripts/trend_pullback_forward.py", "scripts/v1_forward_observer.py",
             "research_portfolio.py", "position_manager.py", "research_projects/trend_portfolio_v1/target_weight_adapter.py",
             "research_projects/trend_portfolio_v1/run_study.py")
    for path in (protocol_path, Path(protocol["costs_path"]), *[ROOT / name for name in names]):
        bindings.setdefault(str(path), sha(path))
    _verify(bindings)
    targets, features, effective_protocols = {}, {}, {}
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
            raise ValueError("original signal targets differ from frozen old events")
        targets[dataset["id"]], features[dataset["id"]] = build_risk_targets(data, original)
        effective_protocols[dataset["id"]] = effective
    _verify(bindings)
    _immutable(output / "targets_before_pnl.json", _bytes(targets))
    _immutable(output / "risk_features_before_pnl.json", _bytes(features))
    _immutable(output / "effective_protocols.json", _bytes(effective_protocols))
    frozen = dict(status="ALL_CAUSAL_RISK_TARGETS_AND_FEATURES_FROZEN_NO_PNL", protocol=protocol, source_sha256=bindings,
                  targets_sha256=sha(output / "targets_before_pnl.json"), risk_features_sha256=sha(output / "risk_features_before_pnl.json"),
                  effective_protocols_sha256=sha(output / "effective_protocols.json"),
                  target_counts={dataset: {mode: {p: len(events) for p, events in groups.items()} for mode, groups in modes.items()} for dataset, modes in targets.items()},
                  risk_event_counts={dataset: len(rows) for dataset, rows in features.items()}, pnl_computed=False, expected_accounts=36,
                  scope="Shared causal forecast risk among eligible A groups; B joint events reuse A exactly. Invalid or zero eligible risk blocks preparation.")
    _immutable(output / "pre_pnl_manifest.json", _bytes(frozen))
    return frozen


def execute(prepared, output):
    prepared, output = Path(prepared).resolve(), Path(output).resolve()
    frozen = read(prepared / "pre_pnl_manifest.json")
    checks = {**frozen["source_sha256"], str(prepared / "targets_before_pnl.json"): frozen["targets_sha256"],
              str(prepared / "risk_features_before_pnl.json"): frozen["risk_features_sha256"],
              str(prepared / "effective_protocols.json"): frozen["effective_protocols_sha256"]}
    _verify(checks)
    if output.exists():
        raise FileExistsError("new causal-risk replay directory required")
    protocol, targets = frozen["protocol"], read(prepared / "targets_before_pnl.json")
    features, effective_protocols = read(prepared / "risk_features_before_pnl.json"), read(prepared / "effective_protocols.json")
    _immutable(output / "run_manifest.json", _bytes(dict(source_and_input_sha256=checks, preparation_sha256=sha(prepared / "pre_pnl_manifest.json"),
        protocol=protocol, risk_features_sha256=frozen["risk_features_sha256"], account_labels="Wrapper metadata only; original engine account unchanged")))
    (output / "accounts").mkdir()
    cases = []
    for dataset in protocol["datasets"]:
        data, funding = load_data(dataset)
        effective, risk = effective_protocols[dataset["id"]], features[dataset["id"]]
        for mode in MODES:
            for portfolio in PORTFOLIOS:
                events = targets[dataset["id"]][mode][portfolio]
                for source_cost in protocol["cost_scenarios"]:
                    cost = source_cost["id"]
                    original_id = dataset["cost_mapping"][cost]
                    scenario = target_cost(dict(source_cost, id=original_id))
                    account = replay_case(data, funding, events, effective, scenario, portfolio)
                    audit = postfill_risk_audit(account, data, events, risk, mode, portfolio)
                    account_path = output / "accounts" / f"{dataset['id']}__{mode}__{portfolio}__{cost}.json.gz"
                    digest = write_account(account_path, account)
                    cases.append(dict(dataset=dataset["id"], mode=mode, portfolio=portfolio, cost=cost, original_scenario_id=original_id,
                        account_path=str(account_path), account_sha256=digest, scope=dataset.get("scope"), risk_audit=audit,
                        **_full_summary(account, data, effective)))
                    print(json.dumps(dict(dataset=dataset["id"], mode=mode, portfolio=portfolio, cost=cost, status="SAVED_CAUSAL_RISK_ACCOUNT")), flush=True)
                    del account
                    gc.collect()
    if len(cases) != 36:
        raise ValueError("frozen36-account matrix incomplete")
    _verify(checks)
    result = dict(status="COMPLETE_EXPOSED_T28_CAUSAL_RISK_COMPARISON", cases=cases,
                  account_verified=False, statistically_verified=False, promotion_eligible=False,
                  interpretation="Decision-known price covariance forecast-risk matching, not equal realized risk. B joint participation is primary; A matches only nonempty eligible portfolios. Shared-controller reduced ETH is a counterfactual diagnostic, not a standalone ETH rule.")
    _immutable(output / "summary.json", _bytes(result))
    _immutable(output / "artifact_manifest.json", _bytes(dict(source_and_input_sha256=checks,
        artifact_sha256={str(path.relative_to(output)): sha(path) for path in sorted(output.rglob("*")) if path.is_file()})))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "execute"))
    parser.add_argument("--protocol", type=Path, default=PROTOCOL)
    parser.add_argument("--prepared", type=Path, default=ROOT / "reports/t28_equal_risk_20261025/prepared")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        os.nice(10)
    except OSError:
        pass
    result = prepare(args.protocol, args.output or args.prepared) if args.command == "prepare" else execute(
        args.prepared, args.output or ROOT / "reports/t28_equal_risk_20261025/run")
    print(json.dumps(dict(status=result["status"])))


if __name__ == "__main__":
    main()
