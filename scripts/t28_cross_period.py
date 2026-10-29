"""Small frozen-rule T28 cross-period wrapper; other-research-exposed, not blind."""
from __future__ import annotations

import argparse
import copy
import gc
import json
import os
import sys
from datetime import datetime, timezone
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
from scripts.t28_cost_review import episode_statistics, target_cost  # noqa: E402
from scripts.t28_cost_stats import cash_profit_factor  # noqa: E402
from scripts.trend_pullback_backtest import _load_bound  # noqa: E402
from scripts.trend_pullback_forward import _parse, validate_funding_coverage  # noqa: E402
from scripts.v1_forward_observer import _bytes, _immutable, _sha  # noqa: E402

EXPOSURE = "NOT_USED_IN_T28_SCREEN_BUT_OTHER_RESEARCH_EXPOSED"
PROTOCOL = ROOT / "research_projects/t28_independent_20261025/cross_period_protocol.json"
STRATEGIES = ("T28_long_cash", "vol_scaled_hold")


def _verify(bindings):
    for path, digest in bindings.items():
        if _sha(Path(path).read_bytes()) != digest:
            raise ValueError(f"frozen source/input SHA drift: {path}")


def _load_data(source, protocol):
    document = read(source)
    if document.get("exposure_label") != EXPOSURE or set(document["symbols"]) != set(protocol["symbols"]):
        raise ValueError("complete two-symbol exposed cross-period manifest required")
    start, end, warmup = (utc_ms(protocol[key]) for key in ("start", "end", "warmup_start"))
    data, funding, bindings = {}, {}, {str(source): _sha(source.read_bytes())}
    for symbol in protocol["symbols"]:
        item = document["symbols"][symbol]
        data[symbol] = _parse(_load_bound(source.parent, item["raw15m"]), "15m", end)
        if data[symbol][0]["open_time"] != warmup or data[symbol][-1]["close_time"] != end - 1:
            raise ValueError("complete frozen warmup/scoring price calendar required")
        if sum(row["close_time"] < start for row in data[symbol]) < 61 * 96:
            raise ValueError("61 complete daily warmup closes required")
        funding[symbol] = _load_bound(source.parent, item["funding"])
        validate_funding_coverage(funding[symbol], symbol, start, end)
        for key in ("raw15m", "funding"):
            bindings[str((source.parent / item[key]["path"]).resolve())] = item[key]["sha256"]
    return data, funding, bindings


def prepare(input_manifest, protocol_path, output):
    source, protocol_path, output = (Path(path).resolve() for path in (input_manifest, protocol_path, output))
    if output.exists() and any(output.iterdir()):
        raise ValueError("new empty preparation directory required")
    protocol = read(protocol_path)
    if (protocol.get("scope") != EXPOSURE or protocol.get("fresh_blind_holdout") is not False
            or protocol.get("prospective_oos") is not False or protocol.get("strategies") != list(STRATEGIES)
            or (protocol.get("start"), protocol.get("end"), protocol.get("warmup_start"))
            != ("2024-01-08T00:00:00Z", "2024-05-01T00:00:00Z", "2023-11-01T00:00:00Z")):
        raise ValueError("fixed exposed cross-period protocol required; no forward-study fallback")
    sources = dict(protocol["original_source_sha256"])
    for path in (protocol_path, Path(__file__).resolve(), ROOT / "tests/test_t28_cross_period.py",
                 ROOT / "scripts/trend_pullback_backtest.py", ROOT / "scripts/trend_pullback_forward.py",
                 ROOT / "scripts/v1_forward_observer.py"):
        sources[str(path)] = _sha(path.read_bytes())
    _verify(sources)
    costs = read(Path(protocol["costs_path"]))
    if costs["scenarios"] != protocol["cost_scenarios"]:
        raise ValueError("frozen cost scenario mismatch")
    effective = copy.deepcopy(read(Path(protocol["original_protocol_path"])))
    first, last = utc_ms(protocol["start"]), utc_ms(protocol["end"])
    years = [datetime(year, 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
             for year in range(datetime.fromtimestamp(first / 1000, timezone.utc).year + 1,
                               datetime.fromtimestamp(last / 1000, timezone.utc).year + 1)]
    boundaries = [protocol["start"], *[value for value in years if first < utc_ms(value) < last], protocol["end"]]
    effective["scoring"].update(start_inclusive_utc=protocol["start"], end_exclusive_utc=protocol["end"],
                                 predeclared_segment_boundaries_utc=boundaries)
    if effective["accounting"]["initial_equity_usdt"] != protocol["initial_equity"]:
        raise ValueError("original starting collateral must remain unchanged")
    data, _funding, inputs = _load_data(source, protocol)
    targets = {name: build_weekly_targets(data, effective, name) for name in STRATEGIES}
    _immutable(output / "effective_protocol.json", _bytes(effective))
    _immutable(output / "targets_before_pnl.json", _bytes(targets))
    _immutable(output / "cross_period_protocol.json", protocol_path.read_bytes())
    frozen = dict(status="TARGETS_AND_SOURCES_FROZEN_NO_PNL", scope=EXPOSURE,
                  input_manifest=str(source), protocol=protocol, source_sha256=sources, input_sha256=inputs,
                  effective_protocol_sha256=_sha((output / "effective_protocol.json").read_bytes()),
                  targets_sha256=_sha((output / "targets_before_pnl.json").read_bytes()),
                  target_counts={name: len(events) for name, events in targets.items()},
                  account_verified=False, statistically_verified=False, promotion_eligible=False)
    _verify({**sources, **inputs})
    _immutable(output / "pre_pnl_manifest.json", _bytes(frozen))
    return frozen


def economic_screen(summaries):
    base, pressure, hold = summaries["T28_long_cash"]["base"], summaries["T28_long_cash"]["double_cost"], summaries["vol_scaled_hold"]["base"]
    checks = dict(base_net_positive=base["net_return"] > 0, double_net_positive=pressure["net_return"] > 0,
                  base_outperforms_vol_scaled_hold=base["net_return"] > hold["net_return"],
                  base_drawdown_no_worse=base["max_close_drawdown"] <= hold["max_close_drawdown"])
    return dict(status="CROSS_PERIOD_DIAGNOSTIC_SUPPORT_ONLY" if all(checks.values()) else "ECONOMIC_SCREEN_NOT_REPLICATED",
                checks=checks, strategy_confirmed=False, promotion_eligible=False)


def execute(prepared, output):
    prepared, output = Path(prepared).resolve(), Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("new empty account output required")
    frozen = read(prepared / "pre_pnl_manifest.json")
    checks = {**frozen["source_sha256"], **frozen["input_sha256"],
              str(prepared / "effective_protocol.json"): frozen["effective_protocol_sha256"],
              str(prepared / "targets_before_pnl.json"): frozen["targets_sha256"]}
    _verify(checks)
    protocol, effective, targets = frozen["protocol"], read(prepared / "effective_protocol.json"), read(prepared / "targets_before_pnl.json")
    data, funding, _inputs = _load_data(Path(frozen["input_manifest"]), protocol)
    _immutable(output / "run_manifest.json", _bytes(dict(scope=EXPOSURE, preparation=str(prepared),
                                                          source_and_input_sha256=checks, protocol=protocol,
                                                          preparation_sha256=_sha((prepared / "pre_pnl_manifest.json").read_bytes()),
                                                          account_verified=False, statistically_verified=False, promotion_eligible=False)))
    (output / "accounts").mkdir(exist_ok=True)
    summaries = {name: {} for name in STRATEGIES}
    for name in STRATEGIES:
        for original_scenario in protocol["cost_scenarios"]:
            scenario = target_cost(original_scenario)
            account = replay_target_weights(data, funding, targets[name], effective, scenario)
            path = output / "accounts" / f"{name}__{scenario['id']}.json.gz"
            write(path, account)
            calendar = calendar_metrics(account["equity"], account["initial_equity"], effective["scoring"])
            episodes = episode_statistics(account)
            summaries[name][scenario["id"]] = dict(net_return=account["net_return"], max_close_drawdown=account["max_drawdown"],
                final_equity=account["final_equity"], total_fees=account["total_fees"], funding_cashflow=account["funding_cashflow"],
                by_symbol=account["by_symbol"], calendar=calendar, complete_weeks=len(calendar["full_weeks"]),
                episode_diagnostics=episodes, cash_profit_factor=cash_profit_factor(episodes["closed"]),
                account_path=str(path), account_sha256=_sha(path.read_bytes()), account_fee_verified=False,
                cost_source=original_scenario["source"], scope=EXPOSURE)
            print("CROSS_PERIOD_COMPLETED", name, scenario["id"], "net_return", account["net_return"], flush=True)
            del account
            gc.collect()
    _verify(checks)
    result = dict(status="COMPLETE_CROSS_PERIOD_DIAGNOSTIC", scope=EXPOSURE, summaries=summaries,
                  economic_screen=economic_screen(summaries), cash_benchmark=dict(net_return=0., max_close_drawdown=0.),
                  statistical_interpretation="Short, other-research-exposed window; descriptive only, no formal CI confirmation.",
                  account_verified=False, statistically_verified=False, promotion_eligible=False, strategy_confirmed=False,
                  tail="Open positions marked, not liquidated; complete episodes only in win/payoff statistics.")
    _immutable(output / "summary.json", _bytes(result))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "execute"))
    parser.add_argument("--input-manifest", type=Path)
    parser.add_argument("--protocol", type=Path, default=PROTOCOL)
    parser.add_argument("--prepared", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        os.nice(10)
    except OSError:
        pass
    if args.command == "prepare":
        if args.input_manifest is None:
            parser.error("prepare requires --input-manifest")
        result = prepare(args.input_manifest, args.protocol, args.output)
    else:
        if args.prepared is None:
            parser.error("execute requires --prepared")
        result = execute(args.prepared, args.output)
    print(json.dumps(dict(status=result["status"], scope=result["scope"], output=str(args.output))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
