"""Frozen-C1 diagnostics: one structure snapshot and one fixed-target exit.

Offline only. --prepare binds inputs/code, --signals generates S1 causally,
--replay freezes signal hashes then audits shared accounting and C1 parity.
Price/funding, frozen C1 events and the new output have separate locations.
"""
from __future__ import annotations

import argparse
import bisect
import concurrent.futures
import copy
import csv
import gzip
import json
import math
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from chan_entry_research import (
    audit_accounting,
    audit_baseline_parity,
    checkpoint_read,
    checkpoint_write,
    read_json,
    verify_bindings,
    write_once_json,
)
from position_manager import STOP_MODE_CHOICES
from research_portfolio import replay_portfolio
from strategy import DEFAULT_CONFIG, ProjectSignalEngine
from strategy_optimization import HOLD, STEP, causal_events, portfolio_metrics, sha256
from vectorbt_research import load_prepared_bars

CODE_NAMES = (
    "strategy.py", "position_manager.py", "research_portfolio.py", "strategy_optimization.py",
    "vectorbt_research.py", "chan_entry_research.py", "chan_github_research.py",
    "tests/test_chan_entry_research.py", "tests/test_chan_github_research.py",
    "tests/test_candidate_lifecycle.py", "tests/test_fee_aware_admission.py",
    "tests/test_chan_structure_snapshot.py", "tests/test_target_only_exit.py",
    "config.py", "config.yaml", "indicators.py", "project_signal_backtest.py", "data_client.py",
    "requirements.txt", "requirements-validation.txt", "requirements-dev.txt", "pyproject.toml",
)


def variants():
    risk = dict(max_positions=5, max_risk_fraction=.025,
                direction_risk_fraction={"long": .025, "short": .01},
                allow_overlap=False, require_candidate_anchor_at_open=True)
    return {"C1": dict(signal_variant="C1", stop_mode="structure_atr", **risk),
            "S1": dict(signal_variant="S1", stop_mode="structure_atr", **risk),
            "E1": dict(signal_variant="C1", stop_mode="structure_atr_target_only", **risk)}


def configurations():
    return (replace(DEFAULT_CONFIG, strict_candidate_lifecycle=True, chan_structure_snapshot=False),
            replace(DEFAULT_CONFIG, strict_candidate_lifecycle=True, chan_structure_snapshot=True))


def recorded_causal_events(symbol, bars, config, entry_start, entry_end, *, engine_factory=ProjectSignalEngine):
    """Record actual shared detector emissions, including the S1 snapshot."""
    records = {}

    class Recorder:
        def __init__(self, configuration):
            self.engine = engine_factory(configuration)

        def detect(self, ready, clean=False):
            signal = self.engine.detect(ready, clean=clean)
            bar = ready[-1]
            close = int(bar["close_time"])
            if signal.get("signal") in {"long", "short"} and entry_start <= close + 1 < entry_end:
                key = signal["signal"], signal.get("divergence_time"), close
                records[key] = copy.deepcopy(signal), bar.get("trend")
            return signal

    events = causal_events(symbol, bars, config, entry_start, entry_end, engine_factory=Recorder)
    for event in events:
        pivot = event["setup_id"].split(":", 1)[1]
        signal, trend = records[(event["direction"], int(pivot) if pivot != "None" else None, event["known_at"])]
        event["score"] = signal.get("signal_score")
        event["local_trend"] = trend
        for field in ("signal_score", "signal_grade", "signal_score_max", "score_text",
                      "structure_factors", "structure_text", "strength", "divergence_type",
                      "signal_ready_time", "confirm_bars", "higher_trend", "filter",
                      "candidate_age_bars", "candidate_anchor_time", "candidate_anchor_price",
                      "candidate_first_ready_time", "candidate_invalidation_reason",
                      "candidate_lifecycle", "structure_snapshot"):
            if field in signal:
                event[field] = copy.deepcopy(signal[field])
    return events


def build_registry(protocol_path, output):
    output, protocol_path = Path(output).resolve(), Path(protocol_path).resolve()
    if (output / "experiment_registry.json").exists():
        raise ValueError("immutable registry already exists")
    protocol = read_json(protocol_path)
    parent_report, price_report = Path(protocol["parent_report"]).resolve(), Path(protocol["price_report"]).resolve()
    paths = {"parent_completion_sha256": parent_report / "completion.json",
             "parent_registry_sha256": parent_report / "experiment_registry.json",
             "parent_signal_manifest_sha256": parent_report / "signal_bindings_before_pnl.json"}
    for key, path in paths.items():
        if sha256(path) != protocol[key]:
            raise ValueError(f"parent final binding differs from protocol: {key}")
    parent = read_json(paths["parent_registry_sha256"])
    completion = read_json(paths["parent_completion_sha256"])
    if completion["status"] != "COMPLETE_RESEARCH_DIAGNOSTIC" or completion["promotion_eligible"]:
        raise ValueError("parent diagnostic is not completed")
    if Path(parent["source_report"]).resolve() != price_report:
        raise ValueError("price report differs from frozen parent source")
    if len(parent["input_sources"]) != 44 or len(protocol["folds"]) != 6 or protocol["folds"] != parent["folds"]:
        raise ValueError("frozen 44-symbol/six-window universe drift")
    if (protocol["candidate_count"] != 2 or protocol["max_replays"] != 54 or protocol["grid_search"]
            or protocol["primary_tests"] != {"S1": "C1", "E1": "C1"}):
        raise ValueError("candidate protocol drift")
    risk = dict(risk_fraction=.005, max_positions=5, max_risk_fraction=.025,
                direction_risk_fraction={"long": .025, "short": .01}, gross_notional_cap=1.)
    if protocol["risk"] != risk or protocol["common"] != parent["common"] or protocol["stress"] != parent["stress"]:
        raise ValueError("risk/cost/common protocol drift")
    selected = variants()
    if parent["variants"]["C1"] != selected["C1"]:
        raise ValueError("frozen C1 execution parameter drift")
    candidates = protocol["candidates"]
    if (set(candidates) != {"S1", "E1"}
            or candidates["S1"].get("strict_candidate_lifecycle") is not True
            or candidates["S1"].get("chan_structure_snapshot") is not True
            or candidates["S1"].get("stop_mode") != "structure_atr"
            or candidates["E1"].get("stop_mode") != "structure_atr_target_only"
            or candidates["E1"].get("reward_risk") != 2
            or any(candidates[name].get("min_net_reward_risk") is not None for name in candidates)):
        raise ValueError("candidate definitions drift")
    if selected["E1"]["stop_mode"] not in STOP_MODE_CHOICES:
        raise ValueError("target-only lifecycle is unavailable")
    c1, s1 = configurations()
    if any(asdict(c1).get(key) != value for key, value in parent["config_C1"].items()):
        raise ValueError("frozen C1 signal configuration drift")
    bindings = {str(protocol_path): sha256(protocol_path), **{str(p): sha256(p) for p in paths.values()}}
    github_manifest = output / "github_sources" / "manifest.json"
    bindings[str(github_manifest)] = sha256(github_manifest)
    github_status = dict(readable=0, unavailable=0)
    for source in read_json(github_manifest)["sources"]:
        if source["status"] == "READABLE":
            bindings[str(Path(source["snapshot_path"]).resolve())] = source["sha256"]
            github_status["readable"] += 1
        elif source["status"] == "UNAVAILABLE":
            github_status["unavailable"] += 1
        else:
            raise ValueError("unrecognized GitHub reference status")
    bindings.update(parent["input_bindings"])
    bindings.update(parent["code_bindings"])  # Previous worktree remains immutable.
    for name, expected in completion["artifact_sha256"].items():
        bindings[str(parent_report / name)] = expected
    bindings[str(parent_report / "REPORT.md")] = completion["report_sha256"]
    parent_sidecar = paths["parent_registry_sha256"].with_suffix(".sha256.json")
    sidecar = read_json(parent_sidecar)
    if sidecar != {str(paths["parent_registry_sha256"]): protocol["parent_registry_sha256"]}:
        raise ValueError("parent registry sidecar drift")
    bindings[str(parent_sidecar)] = sha256(parent_sidecar)
    bindings.update(read_json(paths["parent_signal_manifest_sha256"]))
    for path, expected in parent["code_bindings"].items():
        old = Path(path)
        relative = Path("tests") / old.name if old.parent.name == "tests" else Path(old.name)
        backup = output / "source_before" / relative
        if not backup.is_file() or sha256(backup) != expected:
            raise ValueError(f"parent C1 source backup drift: {relative}")
        bindings[str(backup)] = expected
    for fold in parent["folds"]:
        for stress in parent["stress"]:
            path = parent_report / "portfolio_replays" / fold["name"] / f"C1_{stress}.json.gz"
            checkpoint_read(path)
            bindings[str(path)] = sha256(path)
            sidecar = path.with_suffix(path.suffix + ".sha256.json")
            bindings[str(sidecar)] = sha256(sidecar)
    code_bindings = {str(Path(__file__).resolve().parent / name): sha256(Path(__file__).resolve().parent / name)
                     for name in CODE_NAMES}
    verify_bindings(bindings)
    registry_path = output / "experiment_registry.json"
    registry = dict(schema_version=2, registry_path=str(registry_path), created_at=datetime.now(timezone.utc).isoformat(),
                    status="RESEARCH_ONLY", selection="NO_SELECTION", promotion_eligible=False, protocol=protocol,
                    parent_report=str(parent_report), price_report=str(price_report),
                    parent_registry_sha256=protocol["parent_registry_sha256"],
                    input_sources=parent["input_sources"], folds=parent["folds"], stress=parent["stress"], common=parent["common"],
                    variants=selected, config_C1=asdict(c1), config_S1=asdict(s1),
                    primary_tests={"S1": "C1", "E1": "C1"}, input_bindings=bindings, code_bindings=code_bindings,
                    timing="250-bar warmup; close+1 next-open; entry_end=end-96*15m; actual prior bar retained",
                    metadata_boundary="S1 actual detector emissions only; frozen C1 events copied without backfill",
                    github_source_status=github_status,
                    reasons=parent["reasons"])
    write_once_json(registry_path, registry)
    write_once_json(registry_path.with_suffix(".sha256.json"), {str(registry_path): sha256(registry_path)})
    return registry


def verify_registry(registry):
    path = Path(registry["registry_path"])
    sidecar = read_json(path.with_suffix(".sha256.json"))
    if set(sidecar) != {str(path)}:
        raise ValueError("registry binding path drift")
    verify_bindings(sidecar)
    verify_bindings(registry["input_bindings"])
    verify_bindings(registry["code_bindings"])
    c1, s1 = configurations()
    if registry["config_C1"] != asdict(c1) or registry["config_S1"] != asdict(s1):
        raise ValueError("new signal configuration drift")


def _symbol_job(task):
    symbol, source, registry, output, registry_hash = task
    verify_bindings({source["path"]: source["sha256"]})
    bars, _ = load_prepared_bars(source["path"])
    times = [int(bar["open_time"]) for bar in bars]
    _, config = configurations()
    inventory = []
    for fold in registry["folds"]:
        path = Path(output) / "signal_cache" / fold["name"] / f"{symbol}.json.gz"
        if path.exists():
            payload = checkpoint_read(path)
            if (payload["registry_sha256"] != registry_hash or payload["source_sha256"] != source["sha256"]
                    or payload["window"] != fold or payload["symbol"] != symbol):
                raise ValueError("signal checkpoint binding drift")
        else:
            first = bisect.bisect_left(times, fold["start"] - 250 * STEP)
            last = bisect.bisect_left(times, fold["end"])
            events = recorded_causal_events(symbol, bars[first:last], config, fold["start"], fold["end"] - HOLD * STEP)
            payload = dict(symbol=symbol, window=fold, events={"S1": events},
                           source_sha256=source["sha256"], registry_sha256=registry_hash)
            checkpoint_write(path, payload)
        inventory.append(dict(fold=fold["name"], events=len(payload["events"]["S1"])))
    verify_bindings({source["path"]: source["sha256"]})
    return dict(symbol=symbol, windows=inventory)


def generate_signals(registry, output, workers=4):
    if not 1 <= workers <= 4:
        raise ValueError("workers must be 1..4")
    verify_registry(registry)
    output = Path(output).resolve()
    registry_hash = sha256(output / "experiment_registry.json")
    tasks = [(symbol, source, registry, str(output), registry_hash) for symbol, source in registry["input_sources"].items()]
    inventory = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_symbol_job, task): task[0] for task in tasks}
        for future in concurrent.futures.as_completed(futures):
            inventory.append(future.result())
            print(json.dumps(dict(signals_complete=futures[future], completed=len(inventory), total=len(tasks))), flush=True)
    verify_registry(registry)
    destination, value = output / "signal_inventory.json", sorted(inventory, key=lambda item: item["symbol"])
    if destination.exists():
        if read_json(destination) != value:
            raise ValueError("signal inventory drift")
    else:
        write_once_json(destination, value)
    return value


def freeze_signal_bindings(registry, output):
    output = Path(output).resolve()
    registry_hash, bindings = sha256(output / "experiment_registry.json"), {}
    for fold in registry["folds"]:
        for symbol, source in registry["input_sources"].items():
            path = output / "signal_cache" / fold["name"] / f"{symbol}.json.gz"
            payload = checkpoint_read(path)
            if (payload["registry_sha256"] != registry_hash or payload["symbol"] != symbol
                    or payload["window"] != fold or payload["source_sha256"] != source["sha256"]):
                raise ValueError("signal registry/source/window drift")
            bindings[str(path)] = sha256(path)
            sidecar = path.with_suffix(path.suffix + ".sha256.json")
            bindings[str(sidecar)] = sha256(sidecar)
    destination = output / "signal_bindings_before_pnl.json"
    if destination.exists():
        if read_json(destination) != bindings:
            raise ValueError("frozen signal hash drift")
    else:
        write_once_json(destination, bindings)
    return bindings


def run_replays(registry, output):
    verify_registry(registry)
    output = Path(output).resolve()
    signal_bindings = freeze_signal_bindings(registry, output)
    prices, parent = Path(registry["price_report"]), Path(registry["parent_report"])
    funding = {symbol: read_json(prices / "funding_evidence" / f"{symbol}.json") for symbol in registry["input_sources"]}
    rows = []
    for fold in registry["folds"]:
        data, events = {}, {"C1": [], "S1": []}
        for symbol, source in registry["input_sources"].items():
            with gzip.open(prices / "signal_cache" / fold["name"] / f"{symbol}.json.gz", "rt") as handle:
                price_cache = json.load(handle)
            if price_cache["source_sha256"] != source["sha256"] or "prior_bar" not in price_cache:
                raise ValueError("price source/preceding bar drift")
            data[symbol] = ([price_cache["prior_bar"]] if price_cache["prior_bar"] else []) + price_cache["bars"]
            c1 = checkpoint_read(parent / "signal_cache" / fold["name"] / f"{symbol}.json.gz")
            if c1["source_sha256"] != source["sha256"] or c1["window"] != fold or c1["symbol"] != symbol:
                raise ValueError("frozen C1 source/window drift")
            if c1["registry_sha256"] != registry["parent_registry_sha256"]:
                raise ValueError("frozen C1 registry drift")
            events["C1"].extend(c1["events"]["C1"])
            events["S1"].extend(checkpoint_read(output / "signal_cache" / fold["name"] / f"{symbol}.json.gz")["events"]["S1"])
        for stress, costs in registry["stress"].items():
            for name, definition in registry["variants"].items():
                binding = dict(registry_sha256=sha256(output / "experiment_registry.json"),
                               signal_manifest_sha256=sha256(output / "signal_bindings_before_pnl.json"),
                               fold=fold["name"], variant=name, stress=stress)
                path = output / "portfolio_replays" / fold["name"] / f"{name}_{stress}.json.gz"
                if path.exists():
                    result = checkpoint_read(path)
                    if result.get("research_binding") != binding:
                        raise ValueError("replay checkpoint binding drift")
                else:
                    options = {key: value for key, value in definition.items() if key != "signal_variant"}
                    result = replay_portfolio(data, events[definition["signal_variant"]], funding,
                                              **registry["common"], **options, **costs)
                    result["equity"] = [row for row in result["equity"] if row["time"] > fold["start"]]
                    result["scoring_boundary"] = dict(start=fold["start"], end=fold["end"], preceding_bar="observed only; not scored")
                    result["research_binding"] = binding
                audit = audit_accounting(result)
                if name == "C1":
                    previous = checkpoint_read(parent / "portfolio_replays" / fold["name"] / f"C1_{stress}.json.gz")
                    audit["baseline_parity"] = audit_baseline_parity(result, previous)
                if not path.exists():
                    result["accounting_audit"] = audit
                    checkpoint_write(path, result)
                metric = portfolio_metrics(result)
                metric.update(fold=fold["name"], variant=name, stress=stress, exposure=fold["exposure"], accounting_audit=audit)
                rows.append(metric)
                print(json.dumps(dict(replay=fold["name"], variant=name, stress=stress, trades=metric["trades"], return_=metric["total_return"])), flush=True)
    verify_registry(registry)
    verify_bindings(signal_bindings)
    summary = []
    for stress in registry["stress"]:
        for name in registry["variants"]:
            selected = [row for row in rows if row["stress"] == stress and row["variant"] == name]
            summary.append(dict(variant=name, stress=stress, folds=len(selected), trades=sum(row["trades"] for row in selected),
                                linked_return=math.prod(1 + row["total_return"] for row in selected) - 1,
                                positive_folds=sum(row["total_return"] > 0 for row in selected),
                                worst_fold_drawdown=max(row["max_drawdown"] for row in selected)))
    result = dict(status="RESEARCH_ONLY", selection="NO_SELECTION", promotion_eligible=False,
                  reasons=registry["reasons"], summary=summary, comparisons=registry["primary_tests"],
                  statistical_inference="not calculated here; two predeclared comparisons on exposed data",
                  linked_return_convention="product of independently reset fixed-window returns; gaps uninvested; no account return claim")
    for name, value in (("fold_metrics.json", rows), ("comparison.json", result)):
        path = output / name
        if path.exists():
            if read_json(path) != value:
                raise ValueError("existing final summary drift")
        else:
            write_once_json(path, value)
    csv_path = output / "comparison.csv"
    if not csv_path.exists():
        with csv_path.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(summary[0]))
            writer.writeheader()
            writer.writerows(summary)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    stages = parser.add_mutually_exclusive_group(required=True)
    stages.add_argument("--prepare", action="store_true")
    stages.add_argument("--signals", action="store_true")
    stages.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        if not args.protocol:
            parser.error("--prepare requires --protocol")
        registry = build_registry(args.protocol, args.output)
        print(json.dumps(dict(prepared=True, inputs=len(registry["input_bindings"]), promotion_eligible=False)))
    else:
        registry = read_json(args.output / "experiment_registry.json")
        if args.protocol and sha256(args.protocol) != registry["input_bindings"].get(str(args.protocol.resolve())):
            raise ValueError("protocol path/hash drift")
        if args.signals:
            generate_signals(registry, args.output, args.workers)
        else:
            run_replays(registry, args.output)


if __name__ == "__main__":
    main()
