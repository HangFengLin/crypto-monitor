"""Fixed exploratory entry screen using the existing continuous account engine.

This reads only frozen historical inputs. It neither alters production settings
nor treats a historical continuation screen as a verified trading advantage.
"""
from __future__ import annotations

import argparse
import gc
import importlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_portfolio import replay_portfolio  # noqa: E402
from scripts.trend_pullback_backtest import account_summary, prepare_inputs, utc_ms  # noqa: E402
from scripts.trend_pullback_forward import (  # noqa: E402
    SOURCES,
    STEP,
    SYMBOLS,
    _parse,
    paired_week_intervals,
    validate_funding_coverage,
)
from scripts.v1_forward_observer import _bytes, _immutable, _sha  # noqa: E402
from strategy import DEFAULT_CONFIG, ProjectSignalEngine  # noqa: E402

CANDIDATES = ("D48", "B20", "S24", "EC20_60")
VARIANTS = (*CANDIDATES, "baseline")
PROTOCOL = ROOT / "research_projects/entry_screen_20261025/protocol.json"
SIGNALS_SOURCE = "research_projects/entry_screen_20261025/signals.py"
SCOPE = "HISTORICAL_EXPLORATORY"


def _timestamp(value):
    if isinstance(value, bool):
        raise ValueError("UTC timestamp cannot be boolean")
    return value if isinstance(value, int) else utc_ms(value)


def load_protocol(path=PROTOCOL):
    protocol = json.loads(Path(path).read_bytes())
    if (protocol.get("schema_version") != 1 or protocol.get("scope") != SCOPE
            or protocol.get("research_only") is not True
            or protocol.get("candidate_ids") != list(CANDIDATES)
            or protocol.get("symbols") != list(SYMBOLS) or protocol.get("direction") != "both"
            or [s.get("id") for s in protocol.get("scenarios", [])] != ["base", "double_cost", "delay15m"]):
        raise ValueError("fixed four-candidate exploratory protocol required")
    start, end, recent = (_timestamp(protocol[key]) for key in ("start", "end", "recent_start"))
    if any(value % STEP for value in (start, end, recent)) or not start < recent < end:
        raise ValueError("aligned increasing full/recent scoring boundaries required")
    return protocol


def baseline_events(prepared, start, end):
    """Recalculate the original state machine; retain both directions and warmup."""
    events, decisions = [], []
    for symbol in SYMBOLS:
        rows, engine, seen = prepared[symbol], ProjectSignalEngine(), set()
        for count in range(100, len(rows) + 1):
            row = rows[count - 1]
            response = engine.detect(rows[max(0, count - 250):count], clean=False)
            scoring = start <= row["open_time"] < end
            disposition = "WAIT_OR_FILTERED"
            direction = response.get("signal")
            if direction in {"long", "short"}:
                identity = (direction, response["divergence_time"])
                if identity in seen:
                    disposition = "DUPLICATE_SETUP"
                else:
                    seen.add(identity)
                    disposition = "WARMUP_ONLY"
                    if scoring:
                        close = row["close_time"]
                        events.append(dict(symbol=symbol, variant="baseline", direction=direction,
                                           setup_id=f"baseline:{direction}:{identity[1]}",
                                           decision_close=close, known_at=close + 1,
                                           signal_time=close + 1 + STEP, stop_loss=response["stop_loss"],
                                           observation_scope="HISTORICAL_MODELED_ARRIVAL"))
                        disposition = "MODELED_FUTURE_EXECUTION_INTENT"
            if scoring:
                # Full actionable responses are retained. Waits keep their real
                # reason without duplicating a large feature snapshot per bar.
                detail = response if direction in {"long", "short"} else {
                    key: response[key] for key in ("signal", "signal_name", "filter") if key in response}
                decisions.append(dict(symbol=symbol, variant="baseline", decision_close=row["close_time"],
                                      disposition=disposition, response=detail))
    return events, decisions


def trade_diagnostics(trades):
    """Cash-weighted PF and equal-trade percentage payoff have different units."""
    returns = [trade["net_return"] for trade in trades]
    wins, losses = [r for r in returns if r > 1e-12], [r for r in returns if r < -1e-12]
    cash_wins = sum(max(0., trade["net_pnl"]) for trade in trades)
    cash_losses = -sum(min(0., trade["net_pnl"]) for trade in trades)
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    return dict(closed_trades=len(trades), net_win_rate=len(wins) / len(trades) if trades else None,
                wins=len(wins), losses=len(losses), breakevens=len(trades) - len(wins) - len(losses),
                average_win_return=avg_win, average_loss_return=avg_loss,
                payoff_ratio=avg_win / -avg_loss if avg_win is not None and avg_loss is not None else None,
                profit_factor=cash_wins / cash_losses if cash_losses > 0 else None,
                profit_factor_basis="sum positive net_pnl USDT / abs(sum negative net_pnl USDT)",
                mean_trade_return=sum(returns) / len(returns) if returns else None,
                mean_net_r=sum(trade["net_r"] for trade in trades) / len(trades) if trades else None,
                net_closed_pnl_usdt=sum(trade["net_pnl"] for trade in trades),
                scope="Closed trades only; payoff/mean return are equal-trade initial-notional ratios; "
                      "PF uses net USDT; mean net R uses the engine's recorded initial cash risk. "
                      "None is an account return or an independent-observation count.")


def recent_segment(account, recent_start):
    """Measure a marked segment of the same account, including boundary holdings."""
    equity = account["equity"]
    first_open = equity[0]["time"] - STEP
    if recent_start == first_open:
        opening = dict(equity=account["parameters"]["initial_equity"],
                       cash=account["parameters"]["initial_equity"])
    else:
        opening = next((point for point in equity if point["time"] == recent_start), None)
    if opening is None or not first_open <= recent_start < equity[-1]["time"]:
        raise ValueError("exact recent boundary equity required")
    points = [point for point in equity if point["time"] > recent_start]
    initial = opening["equity"]
    peak, drawdown = initial, 0.
    for point in points:
        peak = max(peak, point["equity"])
        drawdown = min(drawdown, point["equity"] / peak - 1)
    # A bar-end exit stamped at the boundary is already in opening equity;
    # an open-phase fill at that same timestamp belongs to the new segment.
    fills = [fill for fill in account["fills"] if fill["time"] > recent_start
             or (fill["time"] == recent_start and fill.get("phase") == "open")]
    fees = sum(fill.get("fee", 0.) for fill in fills)
    gross = sum(fill.get("gross_pnl", 0.) for fill in fills)
    funding = points[-1]["cash"] - opening["cash"] - gross + fees
    return dict(start=recent_start, end=points[-1]["time"], initial_marked_equity=initial,
                final_marked_equity=points[-1]["equity"],
                net_return=points[-1]["equity"] / initial - 1,
                max_close_drawdown=drawdown, fees_usdt=fees,
                funding_cashflow_usdt=funding, gross_realized_pnl_usdt=gross,
                capital_reset=False,
                scope="Marked return from the continuous account's exact boundary equity; "
                      "positions and capital carry through. Funding reconciles segment cash, gross fills and fees. "
                      "Previously exposed recent history, not a new account or OOS test.")


def run_accounts_serial(data, events, funding, protocol, output, recent_start, *, progress=False):
    """Persist and release one account before replaying the next variant/scenario."""
    summaries = {}
    for variant in VARIANTS:
        for scenario in protocol["scenarios"]:
            name = f"{variant}-{scenario['id']}"
            parameters = {key: value for key, value in scenario.items() if key != "id"}
            account = replay_portfolio(data, events[variant], funding, **protocol["execution"], **parameters,
                                       strict_funding=True, finalize=False)
            _immutable(Path(output) / f"accounts/{name}.json", _bytes(account))
            summary = account_summary(account, data)
            summary["trade_diagnostics"] = trade_diagnostics(account["trades"])
            summary["by_direction"] = {
                direction: trade_diagnostics([trade for trade in account["trades"] if trade["direction"] == direction])
                for direction in ("long", "short")}
            summary["by_symbol"] = {
                symbol: trade_diagnostics([trade for trade in account["trades"] if trade["symbol"] == symbol])
                for symbol in SYMBOLS}
            summary["recent"] = recent_segment(account, recent_start)
            summaries[name] = summary
            if progress:
                print(json.dumps(dict(account_completed=name, net_return=summary["net_return"],
                                      recent_net_return=summary["recent"]["net_return"],
                                      closed_trades=summary["closed_trades"]), allow_nan=False), flush=True)
            del account
            gc.collect()
    return summaries


def continuation_screen(summaries):
    screens = {}
    for variant in CANDIDATES:
        base, pressure = summaries[f"{variant}-base"], summaries[f"{variant}-double_cost"]
        factor = base["trade_diagnostics"]["profit_factor"]
        adequate = base["closed_trades"] > 0 and pressure["closed_trades"] > 0 and factor is not None
        checks = {
            "full_base_positive": base["net_return"] > 0,
            "full_double_cost_positive": pressure["net_return"] > 0,
            "recent_base_positive": base["recent"]["net_return"] > 0,
            "recent_double_cost_positive": pressure["recent"]["net_return"] > 0,
            "both_symbols_base_positive": all(base["per_symbol_marked_pnl_usdt"].get(s, 0.) > 0 for s in SYMBOLS),
            "base_net_cash_profit_factor_above_one": factor is not None and factor > 1,
        }
        status = "INSUFFICIENT_EVIDENCE" if not adequate else "WORTH_FURTHER_VALIDATION" if all(checks.values()) else "NOT_MET"
        screens[variant] = dict(status=status, checks=checks, account_verified=False,
                                statistically_verified=False, promotion_eligible=False,
                                interpretation="Fixed exploratory prioritization only; a pass requires a separately frozen validation.")
    return screens


def _load_bound(root, binding):
    path = (root / binding["path"]).resolve()
    raw = path.read_bytes()
    if _sha(raw) != binding["sha256"]:
        raise ValueError(f"input SHA mismatch: {path.name}")
    return json.loads(raw)


def _source_bindings():
    names = (*SOURCES, "scripts/trend_pullback_backtest.py", "scripts/entry_screen.py", SIGNALS_SOURCE)
    return {name: _sha((ROOT / name).read_bytes()) for name in dict.fromkeys(names)}


def run(input_manifest, output, *, protocol_path=PROTOCOL, start=None, end=None, recent_start=None, progress=False):
    source, output, protocol_path = Path(input_manifest).resolve(), Path(output).resolve(), Path(protocol_path).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("new empty output directory required")
    protocol_bytes, input_bytes = protocol_path.read_bytes(), source.read_bytes()
    protocol = load_protocol(protocol_path)
    boundaries = [_timestamp(protocol[key]) for key in ("start", "end", "recent_start")]
    if any(value is not None and _timestamp(value) != expected
           for value, expected in zip((start, end, recent_start), boundaries, strict=True)):
        raise ValueError("requested dates must match the frozen protocol")
    start, end, recent_start = boundaries
    document = json.loads(input_bytes)
    if document.get("schema_version") != 1 or set(document.get("symbols", {})) != set(SYMBOLS):
        raise ValueError("two-symbol SHA-bound input manifest required")
    declared_protocol = document.get("source_metadata", {}).get("protocol_sha256")
    if declared_protocol is not None and declared_protocol != _sha(protocol_bytes):
        raise ValueError("input protocol SHA mismatch")
    signals = importlib.import_module("research_projects.entry_screen_20261025.signals")
    bindings = _source_bindings()
    manifest = dict(schema_version=1, scope=SCOPE, status="RESEARCH_ONLY", start=start, end=end,
                    recent_start=recent_start, protocol_sha256=_sha(protocol_bytes),
                    input_manifest_sha256=_sha(input_bytes), inputs=document, source_sha256=bindings,
                    baseline_strategy=asdict(DEFAULT_CONFIG), single_worker=True, serial_accounts=True,
                    account_verified=False, statistically_verified=False, promotion_eligible=False,
                    execution_time="closed signal + 1ms modeled knowledge + one full 15m bar; scenario delay additional",
                    data_exposure="Previously or potentially exposed historical research; not prospectively collected OOS")
    _immutable(output / "run_manifest.json", _bytes(manifest))
    _immutable(output / "protocol.json", protocol_bytes)
    _immutable(output / "input_manifest.json", input_bytes)
    prepared, hourly, funding = {}, {}, {}
    for symbol in SYMBOLS:
        item = document["symbols"][symbol]
        lower = _load_bound(source.parent, item["raw15m"])
        higher = _load_bound(source.parent, item["raw4h"])
        prepared[symbol] = prepare_inputs(lower, higher, start, end)
        hourly[symbol] = signals.prepare_hourly(_parse(lower, "15m", end), _parse(higher, "4h", end))
        funding[symbol] = _load_bound(source.parent, item["funding"])
        validate_funding_coverage(funding[symbol], symbol, start, end)
    del lower, higher
    generated = signals.generate_events(hourly, start, end)
    if set(generated["events"]) != set(CANDIDATES):
        raise ValueError("all four fixed candidate event sets required")
    baseline, baseline_decisions = baseline_events(prepared, start, end)
    events = {**generated["events"], "baseline": baseline}
    decisions = [*generated["decisions"], *baseline_decisions]
    evidence = dict(scope=SCOPE, events=events, decisions=decisions, coverage=generated["coverage"],
                    baseline_directions="both", account_verified=False, promotion_eligible=False)
    _immutable(output / "signals_before_pnl.json", _bytes(evidence))
    signal_counts = {name: len(items) for name, items in events.items()}
    decision_count, coverage = len(decisions), generated["coverage"]
    fields = ("open_time", "close_time", "open", "high", "low", "close", "volume", "atr")
    data = {symbol: [{key: row[key] for key in fields} for row in prepared[symbol]
                     if start <= row["open_time"] < end] for symbol in SYMBOLS}
    del prepared, hourly, decisions, evidence, generated, baseline_decisions
    gc.collect()
    if _source_bindings() != bindings or protocol_path.read_bytes() != protocol_bytes:
        raise ValueError("source/protocol drift before account replay")
    summaries = run_accounts_serial(data, events, funding, protocol, output, recent_start, progress=progress)
    baseline_weeks = [week["net_return"] for week in summaries["baseline-base"]["weekly_returns"] if week["complete"]]
    uncertainty = {variant: paired_week_intervals(
        [week["net_return"] for week in summaries[f"{variant}-base"]["weekly_returns"] if week["complete"]],
        baseline_weeks, iterations=protocol["uncertainty"]["iterations"], seed=protocol["uncertainty"]["seed"])
        for variant in CANDIDATES}
    if _source_bindings() != bindings or protocol_path.read_bytes() != protocol_bytes or source.read_bytes() != input_bytes:
        raise ValueError("source/protocol/input manifest drift during account replay")
    for item in document["symbols"].values():
        for binding in item.values():
            path = (source.parent / binding["path"]).resolve()
            if _sha(path.read_bytes()) != binding["sha256"]:
                raise ValueError(f"input SHA drift during account replay: {path.name}")
    result = dict(scope=SCOPE, status="RESEARCH_ONLY", summaries=summaries,
                  continuation_screen=continuation_screen(summaries), descriptive_intervals=uncertainty,
                  signal_counts=signal_counts, decision_records=decision_count, coverage=coverage,
                  account_verified=False, statistically_verified=False, promotion_eligible=False,
                  idle_collateral_reference_return=0.,
                  conclusions="All four fixed entries and both-direction baseline; exposed historical screen only. "
                              "Intervals are descriptive, without correction for all prior adaptive attempts.")
    _immutable(output / "summary.json", _bytes(result))
    artifact_paths = [output / "signals_before_pnl.json", *(output / "accounts").glob("*.json"), output / "summary.json"]
    _immutable(output / "artifact_hashes.json", _bytes({str(p.relative_to(output)): _sha(p.read_bytes()) for p in artifact_paths}))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-manifest", "--input_manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--protocol", type=Path, default=PROTOCOL)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--recent-start")
    args = parser.parse_args(argv)
    try:
        os.nice(10)
    except OSError:
        pass
    result = run(args.input_manifest, args.output, protocol_path=args.protocol,
                 start=args.start, end=args.end, recent_start=args.recent_start, progress=True)
    print(json.dumps(dict(status=result["status"], scope=result["scope"], output=str(args.output))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
