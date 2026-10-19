"""Execute exactly twelve exposed-history target-weight diagnostic accounts."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from research_projects.trend_portfolio_v1.target_weight_adapter import (  # noqa: E402
    DAY,
    WEEK,
    build_weekly_targets,
    replay_target_weights,
    utc_ms,
)

PROTOCOL_SHA = "a79cb6df64e0d3fb15af3d48a6501e15518fbe1a88cc20fd111e6f95a49b770d"
ADDENDUM_SHA = "f3fe6672d27b55109aa49b41fb97f37455d39028cfae5920cdb591c8db81bde3"


def read(path):
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(gzip.compress(encoded, mtime=0) if path.suffix == ".gz" else encoded)


def source_binding():
    names = [
        HERE / n
        for n in (
            "protocol.json",
            "PROJECT.md",
            "source_audit.json",
            "execution_addendum_20261019.json",
            "target_weight_adapter.py",
            "test_target_weight_adapter.py",
            "run_study.py",
            "test_run_study.py",
        )
    ]
    names += [ROOT / "position_manager.py", ROOT / "research_portfolio.py"]
    return {str(p): sha(p) for p in names}


def validate_scope(protocol):
    if sha(HERE / "protocol.json") != PROTOCOL_SHA or protocol != read(HERE / "protocol.json"):
        raise ValueError("Original frozen protocol must remain exact")
    if sha(HERE / "execution_addendum_20261019.json") != ADDENDUM_SHA:
        raise ValueError("Execution clarification drift")
    if protocol["research_budget"]["max_scenario_replays_in_first_round"] != 12 or [
        c["id"] for c in protocol["cost_scenarios"]
    ] != ["base", "double_cost", "extra_delay_15m"]:
        raise ValueError("Only the predeclared twelve scenarios are allowed")
    return [c["id"] for c in protocol["candidates"]] + ["vol_scaled_hold", "equal_weight_buy_hold"]


def economic_decisions(results, protocol):
    benchmark = results["vol_scaled_hold"]["base"]
    decisions = {}
    for candidate in protocol["candidates"]:
        name = candidate["id"]
        base, double = results[name]["base"], results[name]["double_cost"]
        checks = dict(
            base_net_positive=base["net_return"] > 0,
            double_net_positive=double["net_return"] > 0,
            base_outperforms_vol_scaled_hold=base["net_return"] > benchmark["net_return"],
            base_drawdown_no_worse=abs(base["max_drawdown"]) <= abs(benchmark["max_drawdown"]),
        )
        passed = all(checks.values())
        decisions[name] = dict(
            checks=checks,
            continue_diagnostic_research=passed,
            action="REVIEW_CONCENTRATION_AND_UNCERTAINTY" if passed else "STOP_FIRST_VERSION_EXPANSION",
            promotion_eligible=False,
            live_authorized=False,
        )
    return decisions


def calendar_metrics(curve, initial, scoring):
    first, last = (utc_ms(scoring[k]) for k in ("start_inclusive_utc", "end_exclusive_utc"))
    points = {row["time"]: row["equity"] for row in curve}
    if len(points) != len(curve) or first in points or last not in points:
        raise ValueError("Continuous ordered close marks with exact boundary required")
    points[first] = initial
    boundaries = [utc_ms(x) for x in scoring["predeclared_segment_boundaries_utc"]]
    segments = [
        dict(start_ms=a, end_ms=b, start_equity=points[a], end_equity=points[b], net_return=points[b] / points[a] - 1)
        for a, b in zip(boundaries, boundaries[1:])
    ]
    monday = first + ((4 - (first // DAY) % 7) % 7) * DAY
    weeks = [
        dict(start_ms=a, end_ms=a + WEEK, net_return=points[a + WEEK] / points[a] - 1)
        for a in range(monday, last - WEEK + 1, WEEK)
    ]
    return dict(
        segments=segments,
        full_weeks=weeks,
        partial_weeks_excluded_from_weekly_only=True,
        boundary_semantics="previous15m close before next open settlements; no cash or position reset",
    )


def load_inputs(protocol):
    audit = read(HERE / "source_audit.json")
    if audit["protocol_sha256"] != PROTOCOL_SHA or [x["symbol"] for x in audit["inputs"]] != protocol["symbols"]:
        raise ValueError("Original approved source identity differs")
    data, funding, bindings = {}, {}, {}
    for item in audit["inputs"]:
        symbol = item["symbol"]
        for kind in ("prepared", "funding"):
            path = ROOT / item[kind]["path"]
            if sha(path) != item[kind]["sha256"]:
                raise ValueError("Frozen source drift: " + str(path))
            bindings[str(path)] = item[kind]["sha256"]
            value = read(path)
            if kind == "prepared":
                if len(value["bars"]) != item[kind]["bars"]:
                    raise ValueError("Prepared source coverage changed")
                data[symbol] = value["bars"]
            else:
                if value["symbol"] != symbol or not value["complete"] or value["gaps"]:
                    raise ValueError("Complete original funding evidence required")
                normalized = sorted(value["records"], key=lambda r: r["time"])
                raw = sorted(value["raw_records"], key=lambda r: int(r["fundingTime"]))
                if len(raw) != len(normalized) or len(raw) != item[kind]["records"]:
                    raise ValueError("Raw funding record count differs")
                for a, b in zip(normalized, raw):
                    if (
                        a["time"] != int(b["fundingTime"])
                        or a["rate"] != float(b["fundingRate"])
                        or a["mark_price"] != float(b["markPrice"])
                    ):
                        raise ValueError("Funding normalization differs from immutable raw record")
                funding[symbol] = value
    return data, funding, bindings


def execute(output):
    protocol = read(HERE / "protocol.json")
    strategies = validate_scope(protocol)
    output = output.resolve()
    if output.exists():
        raise FileExistsError("Never replace a previous diagnostic directory")
    before = source_binding()
    unit_path = HERE / "pre_pnl_tests_20261019.json"
    units = read(unit_path)
    if units["status"] != "PASS" or units["tests_run"] != 11 or units["source_bindings"] != before:
        raise ValueError("Exact current source requires passing pre-PnL engineering tests")
    data, funding, inputs = load_inputs(protocol)
    targets = {name: build_weekly_targets(data, protocol, name) for name in strategies}
    output.mkdir(parents=True, exist_ok=False)
    write(output / "targets_before_pnl.json", targets)
    write(
        output / "pre_pnl_manifest.json",
        dict(
            status="FROZEN_BEFORE_TWELVE_SCENARIO_PNL",
            created_at_utc=datetime.now(timezone.utc).isoformat(),
            protocol_sha256=PROTOCOL_SHA,
            addendum_sha256=ADDENDUM_SHA,
            source_bindings=before,
            input_bindings=inputs,
            targets_sha256=sha(output / "targets_before_pnl.json"),
            unit_receipt_sha256=sha(unit_path),
            planned_replays=12,
            input_history="EXPOSED_DIAGNOSTIC_ONLY",
            promotion_eligible=False,
        ),
    )
    (output / "replays").mkdir()
    summaries, replay_bindings = {}, {}
    for name in strategies:
        summaries[name] = {}
        for scenario in protocol["cost_scenarios"]:
            result = replay_target_weights(data, funding, targets[name], protocol, scenario)
            result["strategy_id"] = name
            result["calendar"] = calendar_metrics(result["equity"], result["initial_equity"], protocol["scoring"])
            path = output / "replays" / (name + "__" + scenario["id"] + ".json.gz")
            write(path, result)
            replay_bindings[str(path)] = sha(path)
            fields = [
                "status",
                "initial_equity",
                "final_equity",
                "final_cash",
                "net_return",
                "max_drawdown",
                "total_fees",
                "funding_cashflow",
                "realized_gross_pnl",
                "turnover_over_initial_equity",
                "max_close_gross_over_equity",
                "close_marks_above_one_gross",
                "final_positions",
                "by_symbol",
                "calendar",
                "tail",
            ]
            summaries[name][scenario["id"]] = dict(
                {k: result[k] for k in fields},
                fills=len(result["fills"]),
                funding_events=len(result["funding"]),
                equity_points=len(result["equity"]),
                rebalances=len(result["rebalances"]),
            )
            print("TREND_COMPLETED", name, scenario["id"], "net_return", result["net_return"], flush=True)
    if source_binding() != before or any(sha(Path(p)) != h for p, h in inputs.items()):
        raise ValueError("Frozen inputs or code drifted during twelve replays")
    comparisons = {}
    for name in [c["id"] for c in protocol["candidates"]]:
        comparisons[name] = {}
        for scenario in protocol["cost_scenarios"]:
            key = scenario["id"]
            own = summaries[name][key]["calendar"]["full_weeks"]
            comparisons[name][key] = {}
            for benchmark in ("vol_scaled_hold", "equal_weight_buy_hold"):
                reference = summaries[benchmark][key]["calendar"]["full_weeks"]
                if [(w["start_ms"], w["end_ms"]) for w in own] != [(w["start_ms"], w["end_ms"]) for w in reference]:
                    raise ValueError("Weekly comparator calendars must be identical")
                pairs = [
                    dict(start_ms=a["start_ms"], end_ms=a["end_ms"], difference=a["net_return"] - b["net_return"])
                    for a, b in zip(own, reference)
                ]
                differences = [x["difference"] for x in pairs]
                comparisons[name][key][benchmark] = dict(
                    weeks=pairs,
                    count=len(pairs),
                    mean_difference=statistics.fmean(differences),
                    sample_std=statistics.stdev(differences),
                    minimum=min(differences),
                    maximum=max(differences),
                    inference="DESCRIPTIVE_ONLY_NO_NEW_STATISTICAL_CONFIRMATION_PROTOCOL",
                )
    decisions = economic_decisions(summaries, protocol)
    write(output / "summary.json", summaries)
    write(output / "weekly_comparisons.json", comparisons)
    write(output / "economic_decisions.json", decisions)
    write(
        output / "idle_collateral.json",
        dict(
            initial_equity=10000.0,
            final_equity=10000.0,
            net_return=0.0,
            max_drawdown=0.0,
            fees=0.0,
            funding=0.0,
            fills=0,
            method="ANALYTICAL_CONSTANT_CASH_FOR_ENTIRE_SHARED_CALENDAR",
        ),
    )
    artifacts = dict(replay_bindings)
    for path in output.glob("*.json"):
        artifacts[str(path)] = sha(path)
    write(
        output / "completion.json",
        dict(
            status="COMPLETE_EXPOSED_DIAGNOSTIC_SCREEN",
            completed_at_utc=datetime.now(timezone.utc).isoformat(),
            completed_replays=12,
            source_bindings=before,
            input_bindings=inputs,
            artifact_bindings=artifacts,
            decisions=decisions,
            original_protocol_unchanged=True,
            primary_tail_positions_not_liquidated=True,
            promotion_eligible=False,
            live_authorized=False,
            independent_review="SEPARATE_ROOT_OWNED_MARKET_TO_LEDGER_AUDIT",
            limitations=[
                "All data are exposed; source collector coverage does not prove historical publication times",
                "Continuous quantities; no venue lot/min notional or order-book execution validation",
                "15m close MTM is not intrabar worst drawdown; no margin or liquidation simulation",
                "No formal uncertainty-confirmation protocol or new holdout; screening pass is only permission to review",
            ],
        ),
    )
    print("TREND_TWELVE_SCENARIOS_COMPLETE", json.dumps(decisions), flush=True)
    return decisions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/trend_portfolio_completion_20261019")
    execute(parser.parse_args().output)


if __name__ == "__main__":
    main()
