"""Frozen-entry exit/holding exploration through the existing account engine.

All 144 accounts are hypothetical historical research. Entry observations are
copied from their frozen journal, never regenerated or filtered by later prices.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_portfolio import replay_portfolio  # noqa: E402
from scripts.entry_screen import (  # noqa: E402
    CANDIDATES,
    DEFAULT_CONFIG,
    SCOPE,
    STEP,
    SYMBOLS,
    _bytes,
    _immutable,
    _sha,
    _timestamp,
    continuation_screen,
    recent_segment,
    trade_diagnostics,
)
from scripts.trend_pullback_backtest import account_summary, prepare_inputs  # noqa: E402
from scripts.trend_pullback_forward import validate_funding_coverage  # noqa: E402

ENTRIES = CANDIDATES
POLICIES = ("legacy1R", "target2", "target3", "BE_then2")
HOLDS = (96, 288, 672)
PROTOCOL = ROOT / "research_projects/exit_horizon_20261025/protocol.json"


def load_protocol(path=PROTOCOL):
    protocol = json.loads(Path(path).read_bytes())
    specifications = [("legacy1R", "structure_atr", 2),
                      ("target2", "structure_atr_target_only", 2),
                      ("target3", "structure_atr_target_only", 3),
                      ("BE_then2", "structure_atr_breakeven_after_1r", 2)]
    actual = [(policy.get("id"), policy.get("stop_mode"), policy.get("reward_risk"))
              for policy in protocol.get("exit_policies", [])]
    if (protocol.get("schema_version") != 1 or protocol.get("scope") != SCOPE
            or protocol.get("research_only") is not True or protocol.get("entry_ids") != list(ENTRIES)
            or protocol.get("hold_bars") != list(HOLDS) or actual != specifications
            or protocol.get("symbols") != list(SYMBOLS)
            or [item.get("id") for item in protocol.get("scenarios", [])] != ["base", "double_cost", "delay15m"]):
        raise ValueError("fixed four-entry four-exit three-horizon protocol required")
    start, end, recent = (_timestamp(protocol[key]) for key in ("start", "end", "recent_start"))
    if any(t % STEP for t in (start, end, recent)) or not start < recent < end:
        raise ValueError("aligned full/recent scoring boundaries required")
    return protocol


def case_name(entry, policy, hold, cost):
    return f"{entry}-{policy}-h{hold}-{cost}"


def verify_control(reference_path, replay_path):
    original, current = Path(reference_path).read_bytes(), Path(replay_path).read_bytes()
    if original != current:
        raise ValueError(f"legacy1R 24h control bytes differ: {Path(reference_path).name}")
    return dict(identical_bytes=True, reference_sha256=_sha(original), replay_sha256=_sha(current),
                reference_bytes=len(original), reference_path=str(reference_path), replay_path=str(replay_path))


def _mature_trades(trades, cutoff):
    result = {}
    fields = ("symbol", "direction", "setup_id", "signal_time", "entry_time", "entry_price", "initial_stop",
              "net_return", "net_r")
    for trade in trades:
        if trade["signal_time"] > cutoff:
            continue
        key = (trade["symbol"], trade["direction"], trade["setup_id"], trade["signal_time"])
        if key in result:
            raise ValueError("duplicate closed trade identity in paired cohort")
        result[key] = {field: trade[field] for field in fields}
    return result


def _pair_cohorts(baseline, variant, cutoff):
    common = []
    for key in sorted(baseline.keys() & variant.keys()):
        prior, current = baseline[key], variant[key]
        if any(prior[field] != current[field] for field in ("entry_time", "entry_price", "initial_stop")):
            raise ValueError(f"paired entry execution/stop mismatch: {key}")
        common.append(dict(symbol=key[0], direction=key[1], setup_id=key[2], signal_time=key[3],
                           entry_time=current["entry_time"], entry_price=current["entry_price"],
                           initial_stop=current["initial_stop"],
                           net_return_difference=current["net_return"] - prior["net_return"],
                           net_r_difference=current["net_r"] - prior["net_r"]))
    prior_only, current_only = sorted(baseline.keys() - variant.keys()), sorted(variant.keys() - baseline.keys())
    returns = [row["net_return_difference"] for row in common]
    risk_units = [row["net_r_difference"] for row in common]
    return dict(maturity_cutoff=cutoff, cutoff_field="signal_time <= end - (672 + 1) * 15m",
                common_count=len(common), baseline_only_count=len(prior_only), variant_only_count=len(current_only),
                mean_net_return_difference=sum(returns) / len(common) if common else None,
                median_net_return_difference=median(returns) if common else None,
                mean_net_r_difference=sum(risk_units) / len(common) if common else None,
                median_net_r_difference=median(risk_units) if common else None,
                improved_count=sum(value > 1e-12 for value in returns),
                worsened_count=sum(value < -1e-12 for value in returns),
                unchanged_count=sum(abs(value) <= 1e-12 for value in returns),
                mean_positive_return_difference=sum(max(value, 0.) for value in returns) / len(common) if common else None,
                mean_negative_return_difference_magnitude=sum(max(-value, 0.) for value in returns) / len(common) if common else None,
                common=common, baseline_only=prior_only, variant_only=current_only,
                interpretation="Conditional on closed trades admitted by both account paths, with identical entry "
                               "time/price/initial stop. Percentage/net-R differences do not depend on quantity. "
                               "Holding and exits change capacity, sizing and subsequent admissions; this is not "
                               "a pure causal effect over every original opportunity.")


def paired_closed_trades(baseline_trades, variant_trades, cutoff):
    return _pair_cohorts(_mature_trades(baseline_trades, cutoff), _mature_trades(variant_trades, cutoff), cutoff)


def account_deltas(baseline, variant):
    fields = ("net_return", "max_close_drawdown", "fees_usdt", "funding_cashflow_usdt", "closed_trades")
    result = {field: variant[field] - baseline[field] for field in fields}
    result.update(recent_net_return=variant["recent"]["net_return"] - baseline["recent"]["net_return"],
                  recent_max_close_drawdown=variant["recent"]["max_close_drawdown"] - baseline["recent"]["max_close_drawdown"])
    for field in ("net_win_rate", "payoff_ratio", "profit_factor", "mean_net_r"):
        prior, current = baseline["trade_diagnostics"][field], variant["trade_diagnostics"][field]
        result[field] = current - prior if current is not None and prior is not None else None
    return result


def _summarize(account, data, recent_start):
    summary = account_summary(account, data)
    trades = account["trades"]
    summary["trade_diagnostics"] = trade_diagnostics(trades)
    summary["recent"] = recent_segment(account, recent_start)
    summary["exit_reason_counts"] = dict(Counter(trade["exit_reason"] for trade in trades))
    summary["rejection_reason_counts"] = dict(Counter(item["reason"] for item in account["rejections"]))
    summary["average_hold_hours"] = (sum((trade["exit_time"] - trade["entry_time"]) / 3_600_000
                                         for trade in trades) / len(trades) if trades else None)
    summary["average_holding_bars"] = sum(trade["holding_bars"] for trade in trades) / len(trades) if trades else None
    summary["average_hold_scope"] = "Elapsed fill-clock exit_time minus entry_time; open tail positions excluded."
    summary["by_direction"] = {direction: trade_diagnostics([t for t in trades if t["direction"] == direction])
                                for direction in ("long", "short")}
    summary["by_symbol"] = {symbol: trade_diagnostics([t for t in trades if t["symbol"] == symbol]) for symbol in SYMBOLS}
    return summary


def _pair_summary(paired):
    return {key: value for key, value in paired.items() if key not in {"common", "baseline_only", "variant_only"}}


def run_matrix(data, events, funding, protocol, output, entry_run, recent_start, end, *, progress=False):
    output, entry_run = Path(output), Path(entry_run)
    cutoff = end - (max(HOLDS) + max(s["delay_bars"] for s in protocol["scenarios"])) * STEP
    policies = {policy["id"]: policy for policy in protocol["exit_policies"]}
    # Prove all twelve original account controls before exploring any changed exit.
    controls = [(entry, "legacy1R", 96, cost) for entry in ENTRIES for cost in protocol["scenarios"]]
    changed = [(entry, policy, hold, cost) for entry in ENTRIES for policy in POLICIES
               for hold in HOLDS for cost in protocol["scenarios"] if (policy, hold) != ("legacy1R", 96)]
    summaries, control_receipts, cohorts = {}, {}, {}
    for entry, policy_id, hold, cost in [*controls, *changed]:
        name, policy = case_name(entry, policy_id, hold, cost["id"]), policies[policy_id]
        parameters = {**protocol["execution"], "stop_mode": policy["stop_mode"],
                      "reward_risk": policy["reward_risk"], "max_hold_bars": hold,
                      **{key: value for key, value in cost.items() if key != "id"}}
        account = replay_portfolio(data, events[entry], funding, **parameters, strict_funding=True, finalize=False)
        account_path = output / f"accounts/{name}.json"
        _immutable(account_path, _bytes(account))
        if policy_id == "legacy1R" and hold == 96:
            control_receipts[name] = verify_control(entry_run / f"accounts/{entry}-{cost['id']}.json", account_path)
        summary = _summarize(account, data, recent_start)
        summary.update(entry=entry, exit_policy=policy_id, max_hold_bars=hold, maximum_hold_hours=hold / 4,
                       scenario=cost["id"], maturity_cutoff=cutoff)
        current_cohort = _mature_trades(account["trades"], cutoff)
        if hold == 96 or policy_id == "legacy1R":
            cohorts[(entry, policy_id, hold, cost["id"])] = current_cohort
        legacy_name = case_name(entry, "legacy1R", 96, cost["id"])
        legacy_same_hold_name = case_name(entry, "legacy1R", hold, cost["id"])
        same_policy_name = case_name(entry, policy_id, 96, cost["id"])
        legacy = summary if name == legacy_name else summaries[legacy_name]
        legacy_same_hold = summary if name == legacy_same_hold_name else summaries[legacy_same_hold_name]
        same_policy = summary if name == same_policy_name else summaries[same_policy_name]
        summary["delta_vs_legacy_24h"] = account_deltas(legacy, summary)
        summary["delta_vs_legacy_same_hold"] = account_deltas(legacy_same_hold, summary)
        summary["delta_vs_same_policy_24h"] = account_deltas(same_policy, summary)
        paired_legacy = _pair_cohorts(cohorts[(entry, "legacy1R", 96, cost["id"])], current_cohort, cutoff)
        paired_exit = _pair_cohorts(cohorts[(entry, "legacy1R", hold, cost["id"])], current_cohort, cutoff)
        paired_hold = _pair_cohorts(cohorts[(entry, policy_id, 96, cost["id"])], current_cohort, cutoff)
        paired_path = output / f"paired/{name}.json"
        _immutable(paired_path, _bytes(dict(vs_legacy_24h=paired_legacy, vs_legacy_same_hold=paired_exit,
                                           vs_same_policy_24h=paired_hold)))
        summary["paired_vs_legacy_24h"] = _pair_summary(paired_legacy)
        summary["paired_vs_legacy_same_hold"] = _pair_summary(paired_exit)
        summary["paired_vs_same_policy_24h"] = _pair_summary(paired_hold)
        summary["paired_evidence"] = dict(path=f"paired/{name}.json", sha256=_sha(paired_path.read_bytes()))
        summaries[name] = summary
        if progress:
            print(json.dumps(dict(account_completed=name, net_return=summary["net_return"],
                                  recent_net_return=summary["recent"]["net_return"],
                                  closed_trades=summary["closed_trades"],
                                  net_win_rate=summary["trade_diagnostics"]["net_win_rate"],
                                  payoff_ratio=summary["trade_diagnostics"]["payoff_ratio"]), allow_nan=False), flush=True)
        del account, current_cohort, paired_legacy, paired_exit, paired_hold
        gc.collect()
    screens = {}
    for policy in POLICIES:
        for hold in HOLDS:
            mapped = {f"{entry}-{scenario['id']}": summaries[case_name(entry, policy, hold, scenario["id"])]
                      for entry in ENTRIES for scenario in protocol["scenarios"]}
            for entry, screen in continuation_screen(mapped).items():
                screens[f"{entry}-{policy}-h{hold}"] = dict(entry=entry, exit_policy=policy,
                                                           max_hold_bars=hold, **screen)
    return dict(rows=summaries, controls=control_receipts, continuation_screen=screens,
                maturity_cutoff=cutoff, paired_scope="Common closed admitted trades only; account deltas include capacity effects.")


def _bound_json(root, binding):
    path = (root / binding["path"]).resolve()
    raw = path.read_bytes()
    if _sha(raw) != binding["sha256"]:
        raise ValueError(f"input SHA mismatch: {path.name}")
    return json.loads(raw)


def _verify_files(bindings):
    for path, digest in bindings.items():
        if _sha(Path(path).read_bytes()) != digest:
            raise ValueError(f"frozen input/source binding SHA drift: {path}")


def run(entry_run, input_manifest, output, *, protocol_path=PROTOCOL, progress=False):
    entry_run, source, output = Path(entry_run).resolve(), Path(input_manifest).resolve(), Path(output).resolve()
    protocol_path = Path(protocol_path).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("new empty output directory required")
    protocol_bytes, input_bytes = protocol_path.read_bytes(), source.read_bytes()
    protocol = load_protocol(protocol_path)
    start, end, recent = (_timestamp(protocol[key]) for key in ("start", "end", "recent_start"))
    previous_bytes = (entry_run / "run_manifest.json").read_bytes()
    previous = json.loads(previous_bytes)
    old_protocol_bytes = (entry_run / "protocol.json").read_bytes()
    old_protocol = json.loads(old_protocol_bytes)
    if (previous.get("scope") != SCOPE or previous.get("start") != start or previous.get("end") != end
            or previous.get("recent_start") != recent or previous.get("input_manifest_sha256") != _sha(input_bytes)
            or previous.get("protocol_sha256") != _sha(old_protocol_bytes)
            or previous.get("baseline_strategy") != asdict(DEFAULT_CONFIG)
            or old_protocol["execution"] != protocol["execution"]
            or old_protocol["scenarios"] != protocol["scenarios"]):
        raise ValueError("original entry run/protocol/input binding mismatch")
    bindings = {str(ROOT / name): digest for name, digest in previous["source_sha256"].items()}
    bindings.update({str(Path(__file__).resolve()): _sha(Path(__file__).read_bytes()),
                     str(protocol_path): _sha(protocol_bytes), str(source): _sha(input_bytes),
                     str(entry_run / "run_manifest.json"): _sha(previous_bytes),
                     str(entry_run / "protocol.json"): _sha(old_protocol_bytes)})
    artifact_bytes = (entry_run / "artifact_hashes.json").read_bytes()
    artifacts = json.loads(artifact_bytes)
    bindings[str(entry_run / "artifact_hashes.json")] = _sha(artifact_bytes)
    required = ["signals_before_pnl.json", *[f"accounts/{entry}-{cost['id']}.json"
                                               for entry in ENTRIES for cost in protocol["scenarios"]]]
    for name in required:
        if name not in artifacts:
            raise ValueError(f"missing original artifact binding: {name}")
        bindings[str(entry_run / name)] = artifacts[name]
    document = json.loads(input_bytes)
    if document.get("schema_version") != 1 or set(document.get("symbols", {})) != set(SYMBOLS):
        raise ValueError("original two-symbol SHA-bound manifest required")
    for item in document["symbols"].values():
        for binding in item.values():
            bindings[str((source.parent / binding["path"]).resolve())] = binding["sha256"]
    _verify_files(bindings)
    frozen = json.loads((entry_run / "signals_before_pnl.json").read_bytes())
    if any(entry not in frozen.get("events", {}) for entry in ENTRIES):
        raise ValueError("four frozen entry event sets required")
    events = {entry: frozen["events"][entry] for entry in ENTRIES}
    del frozen
    manifest = dict(schema_version=1, scope=SCOPE, status="RESEARCH_ONLY", start=start, end=end,
                    recent_start=recent, frozen_entry_run=str(entry_run), input_manifest=str(source),
                    protocol_sha256=_sha(protocol_bytes), frozen_bindings=bindings,
                    matrix_accounts=144, entry_signals_regenerated=False, single_worker=True, serial_accounts=True,
                    account_verified=False, statistically_verified=False, promotion_eligible=False)
    _immutable(output / "run_manifest.json", _bytes(manifest))
    _immutable(output / "protocol.json", protocol_bytes)
    _immutable(output / "upstream_entry_manifest.json", previous_bytes)
    _immutable(output / "upstream_entry_protocol.json", old_protocol_bytes)
    _immutable(output / "frozen_entry_events.json", _bytes(events))
    data, funding = {}, {}
    fields = ("open_time", "close_time", "open", "high", "low", "close", "volume", "atr")
    for symbol in SYMBOLS:
        item = document["symbols"][symbol]
        lower = _bound_json(source.parent, item["raw15m"])
        higher = _bound_json(source.parent, item["raw4h"])
        prepared = prepare_inputs(lower, higher, start, end)
        data[symbol] = [{key: row[key] for key in fields} for row in prepared if start <= row["open_time"] < end]
        funding[symbol] = _bound_json(source.parent, item["funding"])
        validate_funding_coverage(funding[symbol], symbol, start, end)
        del lower, higher, prepared
        gc.collect()
    _verify_files(bindings)
    matrix = run_matrix(data, events, funding, protocol, output, entry_run, recent, end, progress=progress)
    _verify_files(bindings)
    result = dict(scope=SCOPE, status="RESEARCH_ONLY", **matrix,
                  frozen_event_counts={entry: len(items) for entry, items in events.items()},
                  account_verified=False, statistically_verified=False, promotion_eligible=False,
                  conclusions="All 48 fixed entry/exit/horizon cases and 3 cost scenarios retained. Historical adaptive "
                              "exploration only; no multiple-attempt correction or verified profitability claim. "
                              "Fee-adjusted breakeven protection does not insure funding or slippage.")
    _immutable(output / "summary.json", _bytes(result))
    paths = [output / "run_manifest.json", output / "protocol.json", output / "frozen_entry_events.json",
             output / "summary.json", *(output / "accounts").glob("*.json"), *(output / "paired").glob("*.json")]
    _immutable(output / "artifact_hashes.json", _bytes({str(path.relative_to(output)): _sha(path.read_bytes()) for path in paths}))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry-run", type=Path, default=ROOT / "reports/entry_screen_20261025/run")
    parser.add_argument("--input-manifest", type=Path, default=ROOT / "reports/entry_screen_20261025/inputs/manifest.json")
    parser.add_argument("--protocol", type=Path, default=PROTOCOL)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        os.nice(10)
    except OSError:
        pass
    result = run(args.entry_run, args.input_manifest, args.output, protocol_path=args.protocol, progress=True)
    print(json.dumps(dict(status=result["status"], scope=result["scope"], accounts=len(result["rows"]), output=str(args.output))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
