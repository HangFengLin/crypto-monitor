"""Explicit, frozen offline account evidence for formal validation.

This module never obtains funding, repairs caches or executes orders. Missing
contract/source evidence raises ValueError; trade CSVs are not account evidence.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from data_client import interval_ms
from project_signal_backtest import SignalTrade, calculate_metrics
from research_portfolio import _finite, _integer, replay_portfolio
from strategy import ProjectSignalEngine, StrategyConfig

SCOPE = "synchronized_linear_perpetual_account_v1"
EXECUTION_KEYS = {
    "initial_equity",
    "risk_fraction",
    "max_positions",
    "max_risk_fraction",
    "direction_risk_fraction",
    "fee_rate",
    "slippage_bps",
    "max_hold_bars",
    "reward_risk",
    "stop_mode",
    "return_model",
}


def fingerprint(value: Any) -> str:
    digest = hashlib.sha256()
    # Stream large multi-symbol inputs instead of retaining a second JSON copy.
    for chunk in json.JSONEncoder(sort_keys=True, default=str, allow_nan=False, separators=(",", ":")).iterencode(
        value
    ):
        digest.update(chunk.encode())
    return digest.hexdigest()


def freeze_coverage(exchange, requested, datasets, rows):
    requested_names = [str(item["symbol"]) for item in requested]
    wanted = set(requested_names)
    evaluated = {str(symbol) for symbol, bars in datasets.items() if bars}
    errors, covered, duplicates = set(), set(), set()
    for row in rows:
        if str(row.get("exchange", exchange)) != exchange:
            continue
        symbol = str(row.get("symbol", ""))
        if symbol in covered:
            duplicates.add(symbol)
        covered.add(symbol)
        error = row.get("error")
        has_error = error is not None and not pd.isna(error) and bool(str(error))
        bars = row.get("bars", 0)
        if has_error or not isinstance(bars, (int, float)) or not math.isfinite(bars) or bars <= 0:
            errors.add(symbol)
    failed = (wanted - evaluated) | (wanted - covered) | ((errors | duplicates) & wanted)
    unexpected = evaluated - wanted
    value = dict(
        exchange=exchange,
        requested=sorted(wanted),
        evaluated=sorted(evaluated),
        failed=sorted(failed),
        unexpected=sorted(unexpected),
        duplicate_requested=len(requested_names) != len(wanted),
        duplicate_coverage=sorted(duplicates),
        complete=bool(wanted) and not failed and not unexpected and len(requested_names) == len(wanted),
    )
    value["sha256"] = fingerprint(value)
    return value


def validate_execution(execution):
    """Apply shared replay numeric bounds before any data preparation."""
    directions = execution["direction_risk_fraction"]
    if not isinstance(directions, dict):
        directions = dict(long=directions, short=directions)
    numeric = (
        execution["initial_equity"],
        execution["risk_fraction"],
        execution["max_risk_fraction"],
        execution["fee_rate"],
        execution["slippage_bps"],
        execution["reward_risk"],
    )
    if (
        not all(_finite(value) for value in numeric)
        or execution["initial_equity"] <= 0
        or not 0 < execution["risk_fraction"] <= 1
        or not 0 < execution["max_risk_fraction"] <= 1
        or not 0 <= execution["fee_rate"] < 1
        or not 0 <= execution["slippage_bps"] < 10000
        or execution["reward_risk"] <= 0
        or not _integer(execution["max_hold_bars"], positive=True)
        or not _integer(execution["max_positions"], positive=True)
        or set(directions) != {"long", "short"}
        or any(not _finite(value) or not 0 < value <= 1 for value in directions.values())
    ):
        raise ValueError("invalid account execution parameters or direction risk limits")


def load_contract(args):
    path = getattr(args, "account_contract", None)
    if not path:
        raise ValueError("explicit account contract required; trade metrics are diagnostic only")
    path = Path(path)
    content = path.read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    expected = getattr(args, "account_contract_sha256", None)
    if expected is not None and sha != expected:
        raise ValueError("frozen account contract hash drift")
    args.account_contract_sha256 = sha
    contract = json.loads(content)
    execution = contract.get("execution", {})
    if (
        contract.get("schema_version") != 1
        or contract.get("research_only") is not True
        or contract.get("mark_frequency") != "bar_close"
        or set(execution) != EXECUTION_KEYS
        or execution.get("return_model") != "linear_usdm_v1"
        or execution.get("stop_mode") != "structure_atr"
        or getattr(args, "return_model", None) != "linear_usdm_v1"
        or getattr(args, "holding_limit_convention", None) != "entry_bar_is_first"
    ):
        raise ValueError("unsupported/incomplete account contract; legacy trade model cannot be certified")
    validate_execution(execution)
    for key in ("fee_rate", "max_hold_bars", "reward_risk"):
        if execution[key] != getattr(args, key, None):
            raise ValueError(f"account contract conflicts with frozen {key}")
    cap = execution["max_positions"]
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        raise ValueError("explicit finite max_positions required")
    if not isinstance(contract.get("funding_evidence"), dict):
        raise ValueError("frozen funding evidence bindings required")
    return contract, sha, path.parent


def load_funding(contract, directory, exchange, symbols):
    bindings = contract["funding_evidence"].get(exchange, {})
    if set(bindings) != set(symbols):
        raise ValueError("funding bindings do not cover the frozen requested universe")
    funding = {}
    source_hashes = {}
    for symbol in symbols:
        binding = bindings[symbol]
        path = Path(binding["path"])
        if not path.is_absolute():
            path = directory / path
        content = path.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        if sha != binding.get("sha256"):
            raise ValueError(f"funding source hash drift: {symbol}")
        payload = json.loads(content)
        if (
            payload.get("symbol") != symbol
            or payload.get("exchange") != exchange
            or payload.get("market") != "linear_perpetual"
            or payload.get("complete") is not True
            or payload.get("gaps") != []
            or exchange not in str(payload.get("source", "")).lower()
            or not isinstance(payload.get("raw_records"), list)
            or not payload["raw_records"]
            or not isinstance(payload.get("records"), list)
        ):
            raise ValueError(f"complete original funding source evidence required: {symbol}")
        # Raw settlement records are retained, not replaced by signal funding-rate inputs.
        raw = sorted(
            [
                dict(time=int(row["fundingTime"]), rate=float(row["fundingRate"]), mark_price=float(row["markPrice"]))
                for row in payload["raw_records"]
            ],
            key=lambda row: row["time"],
        )
        if raw != payload["records"] or len({r["time"] for r in raw}) != len(raw):
            raise ValueError(f"raw/normalized funding evidence mismatch: {symbol}")
        funding[symbol] = payload
        source_hashes[symbol] = sha
    return funding, source_hashes


def account_evidence_valid(metrics):
    numeric = (metrics.get("account_max_drawdown"), metrics.get("account_total_return"))
    return bool(
        metrics.get("account_verified") is True
        and metrics.get("account_scope") == SCOPE
        and metrics.get("funding_complete") is True
        and metrics.get("coverage_complete") is True
        and metrics.get("funding_included") is True
        and metrics.get("notional_fees_included") is True
        and metrics.get("independent_slippage_included") is True
        and all(isinstance(v, (int, float)) and math.isfinite(v) for v in numeric)
        and all(
            isinstance(metrics.get(k), str) and len(metrics[k]) == 64
            for k in ("account_contract_sha256", "account_input_sha256", "account_result_sha256")
        )
    )


def run_account_window(
    datasets: dict[str, list[dict[str, Any]]],
    config: StrategyConfig,
    score_start: pd.Timestamp,
    score_end: pd.Timestamp,
    args: argparse.Namespace,
    *,
    fee_multiplier: int = 1,
    slippage_multiplier: int = 1,
    exchange: str = "binance",
):
    contract, contract_sha, directory = load_contract(args)
    if fee_multiplier not in (1, 2) or slippage_multiplier not in (1, 2):
        raise ValueError("unsupported frozen cost scenario")
    coverage = getattr(args, "_coverage_by_exchange", {}).get(exchange)
    if coverage is not None and (coverage.get("complete") is not True or set(coverage["requested"]) != set(datasets)):
        raise ValueError("incomplete frozen requested/evaluated/failed coverage")
    funding, funding_hashes = load_funding(contract, directory, exchange, sorted(datasets))
    step = interval_ms(args.interval)
    start, end = int(score_start.timestamp() * 1000), int(score_end.timestamp() * 1000)
    entry_end = end - args.max_hold_bars * step
    data, signal_inputs, events, signals = {}, {}, [], {}
    for symbol, rows in datasets.items():
        engine = ProjectSignalEngine(config)
        ready, seen = [], set()
        signal_inputs[symbol] = [bar for bar in rows if start - 250 * step <= int(bar["open_time"]) < end]
        for bar in signal_inputs[symbol]:
            time = int(bar["open_time"])
            if not start - 250 * step <= time < end or bar.get("macd") is None:
                continue
            ready.append(bar)
            signal = engine.detect(ready[-250:], clean=False)
            direction = signal.get("signal")
            identity = direction, signal.get("divergence_time")
            if direction not in {"long", "short"} or identity in seen:
                continue
            seen.add(identity)
            decision = int(bar["close_time"]) + 1
            if not start <= decision < entry_end:
                continue
            setup = f"{direction}:{signal.get('divergence_time')}"
            event = dict(
                symbol=symbol,
                direction=direction,
                setup_id=setup,
                signal_time=decision,
                known_at=int(bar["close_time"]),
                stop_loss=float(signal["stop_loss"]),
            )
            for key in ("candidate_anchor_time", "candidate_anchor_price", "candidate_lifecycle"):
                if key in signal:
                    event[key] = signal[key]
            events.append(event)
            signals[(symbol, setup)] = copy.deepcopy(signal)
        selected = [bar for bar in rows if start <= int(bar["open_time"]) < end]
        if (
            not selected
            or selected[0]["open_time"] != start
            or selected[-1]["close_time"] + 1 != end
            or len(selected) * step != end - start
        ):
            raise ValueError(f"complete per-symbol scoring coverage required: {symbol}")
        preceding = [bar for bar in rows if int(bar["open_time"]) == start - step]
        data[symbol] = preceding + selected
    execution = dict(contract["execution"])
    execution["fee_rate"] *= fee_multiplier
    execution["slippage_bps"] *= slippage_multiplier
    result = replay_portfolio(
        data,
        events,
        funding,
        **execution,
        strict_funding=True,
        finalize=True,
        allow_overlap=False,
        require_candidate_anchor_at_open=config.strict_candidate_lifecycle,
    )
    result["equity"] = [row for row in result["equity"] if row["time"] > start]
    if not result["equity"] or result["open_positions"] or result["funding_status"] != "COMPLETE":
        raise ValueError("complete finalized account/funding evidence required")
    trades = []
    for row in result["trades"]:
        signal = signals[(row["symbol"], row["setup_id"])]
        trade = SignalTrade(
            signal=row["direction"],
            stop_mode=row["stop_mode"],
            entry_time=pd.Timestamp(row["entry_time"], unit="ms", tz="UTC"),
            entry_price=row["entry_price"],
            stop_loss=row["initial_stop"],
            initial_stop_loss=row["initial_stop"],
            initial_risk=row["initial_risk"],
            target_price=row["entry_price"]
            + (1 if row["direction"] == "long" else -1)
            * abs(row["entry_price"] - row["initial_stop"])
            * args.reward_risk,
            exit_time=pd.Timestamp(row["exit_time"], unit="ms", tz="UTC"),
            exit_price=row["exit_price"],
            exit_reason=row["exit_reason"],
            outcome=row["outcome"],
            return_pct=row["net_return"],
            bars_held=row["holding_bars"],
            confirm_bars=int(signal.get("confirm_bars") or 0),
            strength=float(signal.get("strength") or 0),
            divergence_type=str(signal.get("divergence_type") or "unknown"),
            trend=str(signal.get("trend") or "unknown"),
            higher_trend=str(signal.get("higher_trend") or "unknown"),
            signal_grade=str(signal.get("signal_grade") or "unknown"),
            signal_score=int(signal.get("signal_score") or 0),
            signal_score_max=int(signal.get("signal_score_max") or 0),
            structure_score=int(signal.get("structure_score") or 0),
            structure_text=str(signal.get("structure_text") or ""),
            score_text=str(signal.get("score_text") or ""),
            filter_text=str(signal.get("filter") or ""),
            return_model="linear_usdm_v1",
        )
        trade.symbol = row["symbol"]
        trades.append(trade)
    initial = execution["initial_equity"]
    curve = pd.Series([initial] + [row["equity"] for row in result["equity"]])
    metrics = calculate_metrics(trades, curve, args.bootstrap_iterations, getattr(args, "seed", 20260620))
    independent_net = sum(row["net_pnl"] for row in result["trades"])
    if not math.isclose(curve.iloc[-1] - initial, independent_net, rel_tol=1e-10, abs_tol=1e-7):
        raise ValueError("account/trade net cash reconciliation failed")
    input_binding = dict(
        data_sha256=fingerprint(data),
        signal_inputs_sha256=fingerprint(signal_inputs),
        events_sha256=fingerprint(events),
        funding_sha256=funding_hashes,
        strategy_sha256=fingerprint(dataclasses.asdict(config)),
        contract_sha256=contract_sha,
        start=start,
        end=end,
        exchange=exchange,
        fee_multiplier=fee_multiplier,
        slippage_multiplier=slippage_multiplier,
        coverage=coverage,
        code_sha256={
            name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in (
                "validation_account.py",
                "research_portfolio.py",
                "strategy.py",
                "position_manager.py",
                "project_signal_backtest.py",
                "backtest_statistics.py",
                "historical_market_data.py",
                "indicators.py",
            )
        },
    )
    result["validation_binding"] = input_binding
    result_sha = fingerprint(result)
    metrics.update(
        account_verified=True,
        account_scope=SCOPE,
        account_max_drawdown=metrics["max_drawdown"],
        account_total_return=metrics["total_return"],
        account_contract_sha256=contract_sha,
        account_input_sha256=fingerprint(input_binding),
        account_result_sha256=result_sha,
        funding_complete=True,
        coverage_complete=True,
        funding_included=True,
        notional_fees_included=True,
        independent_slippage_included=True,
        funding_evidence_authority="caller_frozen_source_and_coverage_declaration_requires_independent_audit",
        fee_multiplier=fee_multiplier,
        slippage_multiplier=slippage_multiplier,
    )
    target = getattr(args, "_account_output_dir", None)
    if target:
        folder = Path(target) / "account_replays"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{exchange}_{start}_{end}_{metrics['account_input_sha256'][:16]}.json"
        if path.exists():
            if json.loads(path.read_text()) != result:
                raise ValueError("refusing to overwrite frozen account replay")
        else:
            with path.open("x") as handle:
                json.dump(result, handle, ensure_ascii=False, allow_nan=False, default=str)
        metrics["account_artifact_path"] = str(path)
    symbol_rows = []
    for symbol in sorted(datasets):
        retained = [t for t in trades if t.symbol == symbol]
        symbol_rows.append(
            dict(
                symbol=symbol,
                total_trades=len(retained),
                expectancy=sum(t.return_pct for t in retained) / len(retained) if retained else 0,
            )
        )
    return trades, pd.DataFrame(symbol_rows), metrics
