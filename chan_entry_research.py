"""Two frozen Chan entry diagnostics; offline only, never a promotion gate.

Stages: --prepare binds all inputs/code, --signals generates causal C1 events,
--replay freezes those events before any PnL and audits every shared replay.
Existing parent artifacts are read-only. Each new checkpoint has a SHA sidecar.
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
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from research_portfolio import replay_portfolio
from strategy import DEFAULT_CONFIG, ProjectSignalEngine
from strategy_optimization import HOLD, STEP, causal_events, portfolio_metrics, sha256
from vectorbt_research import load_prepared_bars


def write_once_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def read_json(path):
    return json.loads(Path(path).read_text())


def verify_bindings(bindings):
    for path, expected in bindings.items():
        if not Path(path).is_file() or sha256(path) != expected:
            raise ValueError(f"input/code hash drift: {path}")


def variants():
    risk = dict(max_positions=5, max_risk_fraction=.025,
                direction_risk_fraction={"long": .025, "short": .01},
                allow_overlap=False, stop_mode="structure_atr")
    return {"risk_baseline": dict(signal_variant="baseline", **risk),
            "C1": dict(signal_variant="C1", require_candidate_anchor_at_open=True, **risk),
            "C2": dict(signal_variant="C1", require_candidate_anchor_at_open=True,
                       min_net_reward_risk=.5, **risk)}


def recorded_causal_events(symbol, bars, config, entry_start, entry_end, *, engine_factory=ProjectSignalEngine):
    """Observe the shared detector; do not reimplement signal extraction."""
    records = {}

    class Recorder:
        def __init__(self, configuration):
            self.engine = engine_factory(configuration)

        def detect(self, ready, clean=False):
            signal = self.engine.detect(ready, clean=clean)
            bar = ready[-1]
            close_time = int(bar["close_time"])
            if signal.get("signal") in {"long", "short"} and entry_start <= close_time + 1 < entry_end:
                key = (signal["signal"], signal.get("divergence_time"), close_time)
                records[key] = (copy.deepcopy(signal), bar.get("trend"))
            return signal

    events = causal_events(symbol, bars, config, entry_start, entry_end, engine_factory=Recorder)
    for event in events:
        divergence_time = event["setup_id"].split(":", 1)[1]
        key = (event["direction"], int(divergence_time) if divergence_time != "None" else None,
               event["known_at"])
        signal, local_trend = records[key]
        # New C1 events only: the old baseline's absent score stays absent.
        event["score"] = signal.get("signal_score")
        event["local_trend"] = local_trend
        for field in ("signal_score", "signal_grade", "signal_score_max", "score_text",
                      "structure_factors", "structure_text", "strength", "divergence_type",
                      "signal_ready_time", "confirm_bars", "higher_trend", "filter",
                      "candidate_age_bars", "candidate_anchor_time", "candidate_anchor_price",
                      "candidate_first_ready_time", "candidate_invalidation_reason", "candidate_lifecycle"):
            if field in signal:
                event[field] = copy.deepcopy(signal[field])
    return events


def audit_accounting(result):
    """Reconcile the shared ledger, without calculating alternative exits."""
    if result["open_positions"] or not result["equity"]:
        raise ValueError("cash audit requires finalized positions and equity")
    quantities, fees, gross = defaultdict(lambda: [0., 0.]), defaultdict(float), defaultdict(float)
    for fill in result["fills"]:
        key = (fill["symbol"], fill["setup_id"], fill["direction"])
        if fill["kind"] not in {"entry", "exit"} or not math.isfinite(fill["quantity"]) or fill["quantity"] <= 0:
            raise ValueError("invalid fill quantity")
        quantities[key][fill["kind"] == "exit"] += fill["quantity"]
        fees[key] += fill["fee"]
        gross[key] += fill.get("gross_pnl", 0)
    trade_keys = set()
    for trade in result["trades"]:
        key = (trade["symbol"], trade["setup_id"], trade["direction"])
        if key in trade_keys:
            raise ValueError("duplicate closed trade")
        trade_keys.add(key)
        entry, exit_ = quantities[key]
        if entry <= 0 or not math.isclose(entry, exit_, rel_tol=1e-10, abs_tol=0) or not math.isclose(entry, trade["quantity"], rel_tol=1e-10, abs_tol=0):
            raise ValueError("entry/exit quantity conservation failed")
        for measured, expected in ((fees[key], trade["total_fees"]), (gross[key], trade["gross_pnl"]),
                                   (gross[key] - fees[key] + trade["funding_cashflow"], trade["net_pnl"])):
            if not math.isclose(measured, expected, rel_tol=1e-10, abs_tol=1e-8):
                raise ValueError("closed trade PnL/fees reconciliation failed")
    if set(quantities) != trade_keys:
        raise ValueError("unmatched fill quantity")
    final = result["equity"][-1]
    initial = result["parameters"]["initial_equity"]
    if (not math.isclose(final["cash"], final["equity"], rel_tol=1e-10, abs_tol=1e-8)
            or not math.isclose(final["unrealized_pnl"], 0, abs_tol=1e-8)):
        raise ValueError("final cash/equity reconciliation failed")
    if not math.isclose(sum(t["net_pnl"] for t in result["trades"]), final["equity"] - initial,
                        rel_tol=1e-10, abs_tol=1e-7):
        raise ValueError("closed PnL/equity reconciliation failed")
    return dict(status="PASS", closed_trades=len(trade_keys), final_equity=final["equity"])


def audit_baseline_parity(current, parent):
    def contains(actual, expected):
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(k in actual and contains(actual[k], v) for k, v in expected.items())
        if isinstance(expected, list):
            return isinstance(actual, list) and len(actual) == len(expected) and all(contains(a, b) for a, b in zip(actual, expected))
        return actual == expected

    for field in ("equity", "fills", "trades", "parameters"):
        if not contains(current[field], parent[field]):
            raise ValueError(f"baseline parity failed: {field}")
    return dict(status="PASS", fields=["equity", "fills", "closed trades", "parent parameters"],
                equality="exact parent fields; additional metadata/parameters ignored")


def checkpoint_write(path, value):
    path = Path(path)
    if path.exists() or path.with_suffix(path.suffix + ".sha256.json").exists():
        raise ValueError(f"checkpoint already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False)
    temporary.replace(path)
    write_once_json(path.with_suffix(path.suffix + ".sha256.json"), {str(path.resolve()): sha256(path)})


def checkpoint_read(path):
    path = Path(path)
    sidecar = path.with_suffix(path.suffix + ".sha256.json")
    if not sidecar.exists():
        raise ValueError(f"incomplete checkpoint: {path}")
    binding = read_json(sidecar)
    if set(binding) != {str(path.resolve())}:
        raise ValueError("checkpoint binding path drift")
    verify_bindings(binding)
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def build_registry(protocol_path, output):
    output = Path(output).resolve()
    if (output / "experiment_registry.json").exists():
        raise ValueError("immutable registry already exists")
    protocol_path = Path(protocol_path).resolve()
    protocol = read_json(protocol_path)
    source_report = Path(protocol["source_report"])
    parent_registry = source_report / "experiment_registry.json"
    parent_execution = source_report / "execution_snapshot.json"
    if sha256(parent_registry) != protocol["source_registry_sha256"] or sha256(parent_execution) != protocol["source_final_execution_sha256"]:
        raise ValueError("parent final binding differs from protocol")
    parent = read_json(parent_registry)
    if len(parent["input_sources"]) != 44 or len(protocol["folds"]) != 6 or protocol["folds"] != parent["folds"]:
        raise ValueError("frozen 44-symbol/six-window universe drift")
    if protocol["candidate_count"] != 2 or protocol["max_replays"] != 54 or protocol["grid_search"]:
        raise ValueError("candidate protocol drift")
    expected_risk = dict(risk_fraction=.005, max_positions=5, max_risk_fraction=.025,
                         direction_risk_fraction={"long": .025, "short": .01}, gross_notional_cap=1.)
    if protocol["risk"] != expected_risk or protocol["common"] != parent["common"] or protocol["stress"] != parent["stress"]:
        raise ValueError("risk/cost/common protocol drift")
    base_config = asdict(DEFAULT_CONFIG)
    if any(base_config.get(k) != v for k, v in parent["config"].items()):
        raise ValueError("baseline signal configuration drift")
    c1_config = asdict(replace(DEFAULT_CONFIG, strict_candidate_lifecycle=True))
    bindings = {str(protocol_path): sha256(protocol_path), str(parent_registry): sha256(parent_registry),
                str(parent_execution): sha256(parent_execution)}
    baseline_hashes = read_json(parent_execution)["actual_code_hashes"]
    for name, expected in baseline_hashes.items():
        path = output / "source_before" / name
        if not path.exists() or sha256(path) != expected:
            raise ValueError(f"parent final baseline source backup drift: {name}")
        bindings[str(path)] = expected
    for symbol, source in parent["input_sources"].items():
        bindings[source["path"]] = source["sha256"]
        funding_path = source_report / "funding_evidence" / f"{symbol}.json"
        bindings[str(funding_path)] = sha256(funding_path)
        for fold in protocol["folds"]:
            cache = source_report / "signal_cache" / fold["name"] / f"{symbol}.json.gz"
            bindings[str(cache)] = sha256(cache)
    for fold in protocol["folds"]:
        for stress in protocol["stress"]:
            path = source_report / "portfolio_replays" / fold["name"] / f"risk_only_{stress}.json.gz"
            bindings[str(path)] = sha256(path)
    code_names = ("strategy.py", "position_manager.py", "research_portfolio.py", "strategy_optimization.py",
                  "vectorbt_research.py", "chan_entry_research.py", "tests/test_chan_entry_research.py",
                  "tests/test_candidate_lifecycle.py", "tests/test_fee_aware_admission.py",
                  "config.py", "config.yaml", "indicators.py")
    code_bindings = {str(Path(__file__).resolve().parent / name): sha256(Path(__file__).resolve().parent / name)
                     for name in code_names}
    manifest = source_report / "funding_evidence" / "manifest.json"
    bindings[str(manifest)] = sha256(manifest)
    verify_bindings(bindings)
    registry_path = output / "experiment_registry.json"
    registry = dict(schema_version=1, registry_path=str(registry_path), created_at=datetime.now(timezone.utc).isoformat(),
                    status="RESEARCH_ONLY", selection="NO_SELECTION", promotion_eligible=False,
                    protocol=protocol, source_report=str(source_report), input_sources=parent["input_sources"],
                    folds=protocol["folds"], stress=protocol["stress"], common=protocol["common"],
                    variants=variants(), config_baseline=base_config, config_C1=c1_config,
                    primary_tests={"C1": "risk_baseline", "C2": "C1"},
                    input_bindings=bindings, code_bindings=code_bindings,
                    baseline_actual_code_hashes=baseline_hashes,
                    timing="250-bar warmup; close+1 next-open events; entry_end=end-96*15m; actual prior bar retained",
                    metadata_boundary="only new detector emissions; no backfill of parent baseline metadata",
                    reasons=["HOLDOUT_EXPOSED", "PIT_UNIVERSE_UNVERIFIED", "OI_AVAILABLE_AT_UNVERIFIED", "NO_OKX_CONFIRMATION"])
    write_once_json(registry_path, registry)
    write_once_json(registry_path.with_suffix(".sha256.json"), {str(registry_path): sha256(registry_path)})
    return registry


def verify_registry(registry):
    registry_path = Path(registry["registry_path"])
    registry_binding = read_json(registry_path.with_suffix(".sha256.json"))
    if set(registry_binding) != {str(registry_path)}:
        raise ValueError("registry binding path drift")
    verify_bindings(registry_binding)
    verify_bindings(registry["input_bindings"])
    verify_bindings(registry["code_bindings"])
    if registry["config_C1"] != asdict(replace(DEFAULT_CONFIG, strict_candidate_lifecycle=True)):
        raise ValueError("new signal configuration drift")


def _symbol_job(task):
    symbol, source, registry, output, registry_hash = task
    if sha256(source["path"]) != source["sha256"]:
        raise ValueError("prepared source hash drift")
    rows, _ = load_prepared_bars(source["path"])
    times = [int(row["open_time"]) for row in rows]
    config = replace(DEFAULT_CONFIG, strict_candidate_lifecycle=True)
    inventory = []
    for fold in registry["folds"]:
        path = Path(output) / "signal_cache" / fold["name"] / f"{symbol}.json.gz"
        if path.exists():
            payload = checkpoint_read(path)
            if payload["registry_sha256"] != registry_hash or payload["source_sha256"] != source["sha256"] or payload["window"] != fold:
                raise ValueError("signal checkpoint binding drift")
        else:
            first = bisect.bisect_left(times, fold["start"] - 250 * STEP)
            last = bisect.bisect_left(times, fold["end"])
            events = recorded_causal_events(symbol, rows[first:last], config, fold["start"], fold["end"] - HOLD * STEP)
            payload = dict(symbol=symbol, window=fold, events={"C1": events},
                           source_sha256=source["sha256"], registry_sha256=registry_hash)
            checkpoint_write(path, payload)
        inventory.append(dict(fold=fold["name"], events=len(payload["events"]["C1"])))
    if sha256(source["path"]) != source["sha256"]:
        raise ValueError("prepared source hash drift during signal generation")
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
    destination = output / "signal_inventory.json"
    value = sorted(inventory, key=lambda row: row["symbol"])
    if destination.exists():
        if read_json(destination) != value:
            raise ValueError("signal inventory drift")
    else:
        write_once_json(destination, value)
    return value


def freeze_signal_bindings(registry, output):
    output = Path(output).resolve()
    bindings = {}
    registry_hash = sha256(output / "experiment_registry.json")
    for fold in registry["folds"]:
        for symbol in registry["input_sources"]:
            path = output / "signal_cache" / fold["name"] / f"{symbol}.json.gz"
            payload = checkpoint_read(path)
            if payload["registry_sha256"] != registry_hash:
                raise ValueError("signal registry hash drift")
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
    source_report = Path(registry["source_report"])
    funding = {s: read_json(source_report / "funding_evidence" / f"{s}.json") for s in registry["input_sources"]}
    rows = []
    for fold in registry["folds"]:
        data, events = {}, {"baseline": [], "C1": []}
        for symbol in registry["input_sources"]:
            parent_path = source_report / "signal_cache" / fold["name"] / f"{symbol}.json.gz"
            with gzip.open(parent_path, "rt") as handle:
                parent = json.load(handle)
            if parent["source_sha256"] != registry["input_sources"][symbol]["sha256"] or "prior_bar" not in parent:
                raise ValueError("parent source/preceding bar drift")
            data[symbol] = ([parent["prior_bar"]] if parent["prior_bar"] else []) + parent["bars"]
            events["baseline"].extend(parent["events"]["baseline"])
            cache = checkpoint_read(output / "signal_cache" / fold["name"] / f"{symbol}.json.gz")
            events["C1"].extend(cache["events"]["C1"])
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
                if name == "risk_baseline":
                    with gzip.open(source_report / "portfolio_replays" / fold["name"] / f"risk_only_{stress}.json.gz", "rt") as handle:
                        previous = json.load(handle)
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
            summary.append(dict(variant=name, stress=stress, folds=len(selected), trades=sum(r["trades"] for r in selected),
                                linked_return=math.prod(1 + r["total_return"] for r in selected) - 1,
                                positive_folds=sum(r["total_return"] > 0 for r in selected),
                                worst_fold_drawdown=max(r["max_drawdown"] for r in selected)))
    result = dict(status="RESEARCH_ONLY", selection="NO_SELECTION", promotion_eligible=False,
                  reasons=registry["reasons"], summary=summary, comparisons=registry["primary_tests"],
                  statistical_inference="not calculated here; exactly two predeclared exposed-data comparisons",
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
