"""One fixed historical TPR1 diagnostic using existing signals and account replay.

This adapter never changes the frozen prospective study or production defaults.
Historical data, even newly downloaded, is labelled HISTORICAL_DIAGNOSTIC.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from indicators import calculate_indicators  # noqa: E402
from research_portfolio import replay_portfolio  # noqa: E402
from scripts.trend_pullback_forward import (  # noqa: E402
    HIGHER,
    PROTOCOL,
    SOURCES,
    STEP,
    SYMBOLS,
    _parse,
    validate_funding_coverage,
)
from scripts.v1_forward_observer import _bytes, _immutable, _sha  # noqa: E402
from strategy import ProjectSignalEngine  # noqa: E402


def load_protocol():
    return json.loads(PROTOCOL.read_bytes())


def prepare_inputs(raw15m, raw4h, start, end):
    lower = _parse(raw15m, "15m", end)
    higher = _parse(raw4h, "4h", end)
    if (sum(r["close_time"] < start for r in lower) < 300
            or sum(r["close_time"] < start for r in higher) < 300):
        raise ValueError("at least 300 complete pre-score bars in each timeframe required")
    if (lower[-1]["close_time"] != end - 1
            or not any(r["open_time"] == start for r in lower)):
        raise ValueError("complete requested scoring calendar required")
    lower, higher = calculate_indicators(lower), calculate_indicators(higher)
    index = -1
    for row in lower:
        while index + 1 < len(higher) and higher[index + 1]["close_time"] <= row["close_time"]:
            index += 1
        if index < 0:
            continue
        context = higher[index]
        for name in ("close", "ema20", "ema60", "ema200", "macd"):
            row["higher_" + name] = context.get(name)
        row["higher_close_time"] = context["close_time"]
        row["higher_prev_ema20"] = higher[index - 1].get("ema20") if index > 0 else None
        if start <= row["open_time"] < end:
            expected = ((row["close_time"] + 1) // HIGHER) * HIGHER - 1
            if context["close_time"] != expected:
                raise ValueError("latest completed 4h context missing")
    return lower


def historical_event(symbol, variant, identity, close, stop):
    return dict(symbol=symbol, variant=variant, direction="long", setup_id=f"{variant}:{identity}",
                decision_close=close, known_at=close + 1, signal_time=close + 1 + STEP,
                stop_loss=stop, observation_scope="HISTORICAL_MODELED_ARRIVAL")


def generate_events(prepared, start, end):
    events = {"baseline": [], "TPR1": []}
    decisions = []
    for symbol in SYMBOLS:
        rows = prepared[symbol]
        baseline, candidate = ProjectSignalEngine(), ProjectSignalEngine()
        seen = {"baseline": set(), "TPR1": set()}
        for count in range(100, len(rows) + 1):
            row = rows[count - 1]
            original = baseline.detect(rows[max(0, count - 250):count], clean=False)
            responses = {"baseline": original}
            scoring = start <= row["open_time"] < end
            if scoring:
                responses["TPR1"] = candidate.detect_trend_pullback_reclaim(rows[count - 2:count])
            for variant, response in responses.items():
                disposition = "WAIT_OR_FILTERED"
                if response.get("signal") == "long":
                    identity = str(rows[count - 2]["close_time"] if variant == "TPR1" else response["divergence_time"])
                    if identity in seen[variant]:
                        disposition = "DUPLICATE_SETUP"
                    else:
                        seen[variant].add(identity)
                        disposition = "WARMUP_ONLY"
                        if scoring:
                            event = historical_event(symbol, variant, identity, row["close_time"], response["stop_loss"])
                            events[variant].append(event)
                            disposition = "MODELED_FUTURE_EXECUTION_INTENT"
                elif response.get("signal") in {"short", "filtered_sell"}:
                    disposition = "DIRECTION_EXCLUDED"
                if scoring:
                    decisions.append(dict(symbol=symbol, variant=variant, decision_close=row["close_time"],
                                          disposition=disposition, response=response))
    return events, decisions


def account_summary(account, data):
    initial = account["parameters"]["initial_equity"]
    fees = sum(fill.get("fee", 0.) for fill in account["fills"])
    gross = sum(fill.get("gross_pnl", 0.) for fill in account["fills"])
    positions = account["open_positions"]
    funding = sum(row["funding_cashflow"] for row in account["trades"]) + sum(p["funding_cashflow"] for p in positions)
    final = account["equity"][-1]
    error = final["equity"] - (initial + gross + funding - fees + final["unrealized_pnl"])
    if abs(error) > 1e-7:
        raise ValueError("shared account cash/equity reconciliation failed")
    peak, drawdown = initial, 0.
    for row in account["equity"]:
        peak = max(peak, row["equity"])
        drawdown = min(drawdown, row["equity"] / peak - 1)
    returns = [t["net_return"] for t in account["trades"]]
    wins, losses = [x for x in returns if x > 0], [x for x in returns if x < 0]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    contributions = {}
    for symbol in SYMBOLS:
        fills = [f for f in account["fills"] if f["symbol"] == symbol]
        settled = sum(t["funding_cashflow"] for t in account["trades"] if t["symbol"] == symbol)
        open_p = [p for p in positions if p["symbol"] == symbol]
        settled += sum(p["funding_cashflow"] for p in open_p)
        unrealized = sum(p["side"] * p["quantity"] * p["remaining_fraction"]
                         * (data[symbol][-1]["close"] - p["entry_price"]) for p in open_p)
        contributions[symbol] = sum(f.get("gross_pnl", 0.) - f.get("fee", 0.) for f in fills) + settled + unrealized
    weekly, prior = [], initial
    week_ms = 7 * 24 * 60 * 60 * 1000
    start, end = account["equity"][0]["time"] - STEP, final["time"]
    for index in range(math.ceil((end - start) / week_ms)):
        left, right = start + index * week_ms, min(end, start + (index + 1) * week_ms)
        value = next(p["equity"] for p in reversed(account["equity"]) if p["time"] <= right)
        weekly.append(dict(week=index + 1, start=left, end=right, complete=right-left == week_ms,
                           net_return=value / prior - 1))
        prior = value
    return dict(net_return=final["equity"] / initial - 1, final_equity_usdt=final["equity"],
                max_close_drawdown=drawdown, fees_usdt=fees, funding_cashflow_usdt=funding,
                closed_trades=len(returns), open_positions=len(positions), rejections=len(account["rejections"]),
                per_symbol_marked_pnl_usdt=contributions, weekly_returns=weekly,
                cash_equity_reconciliation_error=error,
                trade_diagnostics=dict(scope="equal-trade initial-notional returns; not account returns",
                                       net_win_rate=len(wins)/len(returns) if returns else None,
                                       average_win_return=avg_win, average_loss_return=avg_loss,
                                       payoff_ratio=avg_win/-avg_loss if avg_win is not None and avg_loss is not None else None,
                                       mean_trade_return=sum(returns)/len(returns) if returns else None))


def calculate_accounts(data, events, funding, protocol):
    accounts, summaries = {}, {}
    for variant in ("baseline", "TPR1"):
        for scenario in protocol["scenarios"]:
            params = {key: value for key, value in scenario.items() if key != "id"}
            name = f"{variant}-{scenario['id']}"
            account = replay_portfolio(data, events[variant], funding, **protocol["execution"], **params,
                                       strict_funding=True, finalize=False)
            accounts[name], summaries[name] = account, account_summary(account, data)
    return accounts, summaries


def _load_bound(root, binding):
    path = (root / binding["path"]).resolve()
    raw = path.read_bytes()
    if _sha(raw) != binding["sha256"]:
        raise ValueError(f"input SHA mismatch: {path.name}")
    return json.loads(raw)


def run(input_manifest, output, start, end):
    source = Path(input_manifest).resolve()
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("new empty output directory required")
    if start % STEP or end % STEP or start >= end:
        raise ValueError("aligned increasing UTC scoring interval required")
    document = json.loads(source.read_bytes())
    if document.get("schema_version") != 1 or set(document.get("symbols", {})) != set(SYMBOLS):
        raise ValueError("two-symbol SHA-bound input manifest required")
    protocol = load_protocol()
    bindings = {name: _sha((ROOT / name).read_bytes()) for name in (*SOURCES, "scripts/trend_pullback_backtest.py")}
    manifest = dict(schema_version=1, scope="HISTORICAL_DIAGNOSTIC", status="RESEARCH_ONLY",
                    start=start, end=end, input_manifest_sha256=_sha(source.read_bytes()),
                    inputs=document, source_sha256=bindings, protocol_sha256=_sha(PROTOCOL.read_bytes()),
                    account_verified=False, statistically_verified=False, promotion_eligible=False,
                    execution_time="decision close+1 plus one full 15m bar; scenario delay is additional",
                    data_exposure="Previously observed history; no prospective OOS claim")
    addendum = ROOT / "research_projects/trend_pullback_v1/historical_diagnostic_20261025.json"
    if not addendum.is_file():
        raise ValueError("frozen historical diagnostic addendum required")
    manifest["historical_addendum_sha256"] = _sha(addendum.read_bytes())
    manifest["historical_addendum_path"] = str(addendum.relative_to(ROOT))
    _immutable(output / "run_manifest.json", _bytes(manifest))
    _immutable(output / "protocol.json", PROTOCOL.read_bytes())
    prepared, funding = {}, {}
    for symbol in SYMBOLS:
        item = document["symbols"][symbol]
        lower, higher = _load_bound(source.parent, item["raw15m"]), _load_bound(source.parent, item["raw4h"])
        prepared[symbol] = prepare_inputs(lower, higher, start, end)
        funding[symbol] = _load_bound(source.parent, item["funding"])
        validate_funding_coverage(funding[symbol], symbol, start, end)
    events, decisions = generate_events(prepared, start, end)
    _immutable(output / "signals_before_pnl.json", _bytes(dict(events=events, decisions=decisions)))
    data = {s: [r for r in prepared[s] if start <= r["open_time"] < end] for s in SYMBOLS}
    accounts, summaries = calculate_accounts(data, events, funding, protocol)
    for name, account in accounts.items():
        _immutable(output / f"accounts/{name}.json", _bytes(account))
    result = dict(scope="HISTORICAL_DIAGNOSTIC", status="RESEARCH_ONLY", summaries=summaries,
                  signal_counts={v: len(e) for v, e in events.items()}, decision_records=len(decisions),
                  account_verified=False, statistically_verified=False, promotion_eligible=False,
                  conclusions="Fixed historical diagnostic only; no candidate selection or prospective validation")
    _immutable(output / "summary.json", _bytes(result))
    return result


def utc_ms(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input_manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args(argv)
    result = run(args.input_manifest, args.output, utc_ms(args.start), utc_ms(args.end))
    print(json.dumps(dict(status=result["status"], scope=result["scope"], output=str(args.output))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
