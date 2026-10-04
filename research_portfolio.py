"""Offline synchronized portfolio execution using the shared position lifecycle.

This module consumes already-known signal events; it does not generate signals.
Cash is linear USD-M collateral (not cash minus purchased spot notional), and
equity is cash plus remaining-quantity mark-to-market PnL. All numbers and caps
are research parameters, never production settings or a promotion decision.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from math import isfinite, ulp
from typing import Any

from position_manager import (
    LINEAR_USDM_V1,
    STOP_MODE_CHOICES,
    STRUCTURE_ATR_STOP_MODE,
    calculate_return_pct,
    calculate_target_levels,
    evaluate_lifecycle_bar,
)


def _finite(value: Any, *, positive: bool = False) -> bool:
    try:
        return not isinstance(value, bool) and isfinite(value) and (value > 0 if positive else True)
    except (TypeError, OverflowError):
        return False


def _integer(value: Any, *, positive: bool = False) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= (1 if positive else 0)


def _calendar(data: dict[str, list[dict[str, Any]]]):
    if not data or any(not isinstance(s, str) or not s or not isinstance(rows, list)
                       for s, rows in data.items()) or not any(data.values()):
        raise ValueError("at least one nonempty bar calendar required")
    symbols = sorted(data)
    reference = next(data[s] for s in symbols if data[s])
    try:
        duration = reference[0]["close_time"] - reference[0]["open_time"] + 1
        first = min(rows[0]["open_time"] for rows in data.values() if rows)
        last = max(rows[-1]["open_time"] for rows in data.values() if rows)
    except (KeyError, TypeError):
        raise ValueError("bar calendar timestamps required") from None
    if not _integer(duration, positive=True) or not _integer(first) or not _integer(last):
        raise ValueError("invalid bar calendar")
    times = list(range(first, last + duration, duration))
    coverage, offsets, intervals = {}, {}, []
    for symbol in symbols:
        rows = data[symbol]
        if not rows:
            offsets[symbol] = len(times)
            coverage[symbol] = dict(first_open_time=None, end_exclusive=None, observed_bars=0,
                                    union_bars=len(times), missing_prefix_bars=0, missing_suffix_bars=0,
                                    all_missing_bar_count=len(times), internal_gaps=0,
                                    pit_listing_authority=False)
            continue
        row_times = [bar.get("open_time") for bar in rows]
        if (any(not _integer(t) or (t - first) % duration for t in row_times)
                or any(b - a != duration for a, b in zip(row_times, row_times[1:]))):
            raise ValueError(f"symbol bar calendar gap, duplicate or misalignment: {symbol}")
        for bar in rows:
            if (bar.get("is_closed", True) is not True
                    or bar.get("close_time") != bar["open_time"] + duration - 1
                    or not all(_finite(bar.get(k), positive=True) for k in ("open", "high", "low", "close"))
                    or not _finite(bar.get("volume")) or bar["volume"] < 0
                    or not bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]):
                raise ValueError("invalid completed OHLCV bar")
        offset = (row_times[0] - first) // duration
        offsets[symbol] = offset
        intervals.append((offset, offset + len(rows)))
        coverage[symbol] = dict(first_open_time=row_times[0], end_exclusive=row_times[-1] + duration,
                                observed_bars=len(rows), union_bars=len(times), missing_prefix_bars=offset,
                                missing_suffix_bars=len(times) - offset - len(rows), all_missing_bar_count=0,
                                internal_gaps=0, pit_listing_authority=False)
    covered_until = 0
    for start, end in sorted(intervals):
        if start > covered_until:
            raise ValueError("union bar calendar gap")
        covered_until = max(covered_until, end)
    if covered_until != len(times):
        raise ValueError("union bar calendar gap")
    return symbols, times, duration, coverage, offsets


def _funding_calendar(funding, symbols, times, duration, strict, bar_coverage):
    opening = defaultdict(list)
    intrabar = defaultdict(list)
    incomplete = []
    for symbol in symbols:
        coverage = (funding or {}).get(symbol, {})
        start, end = coverage.get("start"), coverage.get("end")
        bars_start = bar_coverage[symbol]["first_open_time"]
        bars_end = bar_coverage[symbol]["end_exclusive"]
        if bars_start is None:
            bars_start, bars_end = times[0], times[-1] + duration
        covered = (coverage.get("complete") is True and _integer(start) and _integer(end)
                   and start <= bars_start and end >= bars_end
                   and isinstance(coverage.get("records"), list))
        if not covered:
            incomplete.append(symbol)
            if strict:
                raise ValueError(f"complete funding coverage required: {symbol}")
        seen = set()
        records = coverage.get("records", [])
        if not isinstance(records, list):
            raise ValueError("invalid funding records")
        for record in records:
            time = record.get("time")
            if (not _integer(time) or time in seen or not _finite(record.get("rate"))
                    or not _finite(record.get("mark_price"), positive=True)):
                raise ValueError("invalid or duplicate funding settlement")
            seen.add(time)
            if times[0] <= time < times[-1] + duration:
                bar_time = times[0] + ((time - times[0]) // duration) * duration
                destination = opening if time == bar_time else intrabar
                destination[bar_time].append((symbol, record))
    for values in (opening, intrabar):
        for key in values:
            values[key].sort(key=lambda x: (x[1]["time"], x[0]))
    return opening, intrabar, incomplete


def replay_portfolio(
    data: dict[str, list[dict[str, Any]]],
    events: list[dict[str, Any]],
    funding: dict[str, dict[str, Any]] | None = None,
    *,
    initial_equity: float = 10_000,
    risk_fraction: float = .005,
    max_positions: int | None = 5,
    max_risk_fraction: float = .025,
    direction_risk_fraction: float | dict[str, float] = .025,
    fee_rate: float = .001,
    slippage_bps: float = 2,
    max_hold_bars: int = 96,
    stop_mode: str = STRUCTURE_ATR_STOP_MODE,
    return_model: str = LINEAR_USDM_V1,
    reward_risk: float = 2,
    delay_bars: int = 0,
    strict_funding: bool = True,
    allow_overlap: bool = False,
    finalize: bool = False,
    min_net_reward_risk: float | None = None,
    require_candidate_anchor_at_open: bool = False,
) -> dict[str, Any]:
    """Execute events at next opens and return trades, fills and per-bar equity.

    ``signal_time`` is the preceding signal bar's close_time + 1, with known_at
    no later than that boundary. A separate delay adds completed bar intervals.
    A complete funding declaration is an input audit contract, not proof from
    this replay. In partial mode, unavailable settlements are not invented.
    The default leaves tail positions open to preserve prefix invariance;
    ``finalize=True`` explicitly liquidates remaining quantities at fold end.
    ``min_net_reward_risk`` optionally filters structure_atr admissions using
    its ordinary full exit at 1R, with actual entry/exit slippage and notional
    fees. It excludes unknown future funding and gaps beyond the planned stop;
    the separate target ratio is diagnostic, never the admission reference.
    ``require_candidate_anchor_at_open`` extends strict_candidate_v1 validity
    through completed delay bars and the actual raw execution open. The
    execution bar's subsequent high/low range is never an admission input.
    """
    if isinstance(direction_risk_fraction, dict):
        direction_caps = deepcopy(direction_risk_fraction)
    else:
        direction_caps = dict(long=direction_risk_fraction, short=direction_risk_fraction)
    if (set(direction_caps) != {"long", "short"}
            or any(not _finite(v) or not 0 < v <= 1 for v in direction_caps.values())):
        raise ValueError("invalid direction risk limits; require long and short")
    numeric = (initial_equity, risk_fraction, max_risk_fraction,
               fee_rate, slippage_bps, reward_risk)
    if (not all(_finite(value) for value in numeric) or initial_equity <= 0
            or not 0 < risk_fraction <= 1 or not 0 < max_risk_fraction <= 1
            or not 0 <= fee_rate < 1
            or not 0 <= slippage_bps < 10_000 or reward_risk <= 0
            or not _integer(max_hold_bars, positive=True) or not _integer(delay_bars)
            or (max_positions is not None and not _integer(max_positions, positive=True))
            or any(not isinstance(flag, bool) for flag in (
                strict_funding, allow_overlap, finalize, require_candidate_anchor_at_open))
            or stop_mode not in STOP_MODE_CHOICES or return_model != LINEAR_USDM_V1):
        raise ValueError("invalid research execution parameters or return model")
    if min_net_reward_risk is not None and (
            not _finite(min_net_reward_risk) or min_net_reward_risk < 0
            or stop_mode != STRUCTURE_ATR_STOP_MODE or reward_risk < 1):
        raise ValueError("fee-aware admission requires a finite nonnegative threshold, "
                         "structure_atr, and a target at or beyond 1R")
    symbols, times, duration, bar_coverage, offsets = _calendar(data)
    time_set = set(times)
    funding_open, funding_inside, incomplete = _funding_calendar(
        funding, symbols, times, duration, strict_funding, bar_coverage)
    scheduled = defaultdict(list)
    rejections, signals, trades, equity, fills = [], [], [], [], []
    seen_events = set()
    positions = []  # Overlap experiments retain each setup separately.
    cash = float(initial_equity)
    slip = slippage_bps / 10_000

    def reject(event, reason, **details):
        rejections.append({"symbol": event["symbol"], "setup_id": event["setup_id"],
                           "signal_time": event["signal_time"], "reason": reason, **details})

    def has_bar(symbol, time):
        coverage = bar_coverage[symbol]
        return (coverage["first_open_time"] is not None
                and coverage["first_open_time"] <= time < coverage["end_exclusive"]
                and (time - times[0]) % duration == 0)

    validated_events = []
    for event in events:
        if (event.get("symbol") not in data or event.get("direction") not in {"long", "short"}
                or not isinstance(event.get("setup_id"), str) or not event["setup_id"]
                or not _integer(event.get("signal_time")) or not _integer(event.get("known_at"))
                or event["known_at"] > event["signal_time"]
                or not _finite(event.get("stop_loss"), positive=True)):
            raise ValueError("invalid event or future known_at")
        if require_candidate_anchor_at_open:
            metadata = event.get("candidate_lifecycle")
            if (not isinstance(metadata, dict) or metadata.get("version") != "strict_candidate_v1"
                    or not _finite(metadata.get("anchor"), positive=True)
                    or any(not _integer(metadata.get(key)) for key in (
                        "anchor_close_time", "first_ready_time", "checked_through_close_time"))
                    or not metadata["anchor_close_time"] <= metadata["first_ready_time"]
                    <= metadata["checked_through_close_time"] == event["signal_time"] - 1 <= event["known_at"]):
                raise ValueError("valid known strict_candidate_v1 lifecycle metadata required")
        validated_events.append(deepcopy(event))
    validated_events.sort(key=lambda e: (e["signal_time"], e["symbol"], e["setup_id"],
                                         e["direction"], e["stop_loss"]))
    for event in validated_events:
        identity = (event["symbol"], event["direction"], event["setup_id"])
        if identity in seen_events:
            reject(event, "duplicate_event")
            continue
        seen_events.add(identity)
        signals.append(event)
        execution_time = event["signal_time"] + delay_bars * duration
        if execution_time not in time_set or not has_bar(event["symbol"], execution_time):
            reject(event, "no_execution_bar")
        elif not has_bar(event["symbol"], event["signal_time"] - duration):
            reject(event, "no_observed_signal_bar")
        else:
            scheduled[execution_time].append(event)

    def mark_equity(current, field):
        unrealized = sum(p["side"] * p["quantity"] * p["remaining_fraction"]
                         * (current[p["symbol"]][field] - p["entry_price"]) for p in positions)
        return cash + unrealized, unrealized

    def risk_occupied(position):
        stop_fill = position["active_stop"] * (1 - position["side"] * slip)
        per_unit = max(0.0, position["side"] * (position["entry_price"] - stop_fill)
                       + fee_rate * (position["entry_price"] + stop_fill))
        return position["quantity"] * position["remaining_fraction"] * per_unit

    def lifecycle(position, row, *, point=False):
        price = row["open"]
        position_bar_index = (row["open_time"] - times[0]) // duration - offsets[position["symbol"]]
        atr = (data[position["symbol"]][position_bar_index - 1].get("atr")
               if point and position_bar_index > 0 else None if point else row.get("atr"))
        return evaluate_lifecycle_bar(
            position["direction"], price if point else row["low"],
            price if point else row["high"], price if point else row["close"],
            position["entry_price"], position["initial_stop"], position["target"], position["protection"],
            active_stop=position["active_stop"], highest_price=position["highest_price"],
            lowest_price=position["lowest_price"], protection_activated=position["protection_activated"],
            protected_stop_price=position["protected_stop_price"], stop_mode=stop_mode,
            fee_rate=fee_rate, atr_value=atr, return_model=return_model,
            remaining_fraction=position["remaining_fraction"], partial_taken=position["partial_taken"],
        )

    def exit_fill(position, fraction, reference_price, reason, time, partial=False, phase="bar_end"):
        nonlocal cash
        quantity = position["quantity"] * fraction
        price = reference_price * (1 - position["side"] * slip)
        gross = quantity * position["entry_price"] * calculate_return_pct(
            position["direction"], position["entry_price"], price, 0,
            return_model=return_model)
        fee = quantity * price * fee_rate
        cash += gross - fee
        position["realized_gross_pnl"] += gross
        position["exit_fees"] += fee
        record = dict(symbol=position["symbol"], setup_id=position["setup_id"],
                      direction=position["direction"], time=time, kind="exit", phase=phase,
                      quantity=quantity, fraction=fraction, exit_price=price,
                      reason=reason, gross_pnl=gross, fee=fee)
        fills.append(record)
        if partial:
            position["partial_exits"].append(dict(record))
        return record

    def close_position(position, fraction, reference, reason, time, holding_bars, phase="bar_end"):
        record = exit_fill(position, fraction, reference, reason, time, phase=phase)
        net = (position["realized_gross_pnl"] - position["entry_fee"] - position["exit_fees"]
               + position["funding_cashflow"])
        trades.append(dict(
            symbol=position["symbol"], setup_id=position["setup_id"], direction=position["direction"],
            signal_time=position["signal_time"], known_at=position["known_at"],
            entry_time=position["entry_time"], exit_time=time, exit_phase=phase, holding_bars=holding_bars,
            entry_price=position["entry_price"], exit_price=record["exit_price"], exit_reason=reason,
            quantity=position["quantity"], exit_quantity=record["quantity"],
            initial_stop=position["initial_stop"], initial_risk=position["initial_risk"],
            entry_fee=position["entry_fee"], exit_fee=position["exit_fees"],
            total_fees=position["entry_fee"] + position["exit_fees"],
            gross_pnl=position["realized_gross_pnl"], net_pnl=net,
            net_return=net / (position["quantity"] * position["entry_price"]),
            net_r=net / position["initial_risk"], outcome="win" if net > 1e-12 else "loss" if net < -1e-12 else "breakeven",
            funding_cashflow=position["funding_cashflow"],
            funding_timing_ambiguous=position["funding_timing_ambiguous"],
            partial_exits=deepcopy(position["partial_exits"]), remaining_fraction=0.0,
            return_model=return_model, stop_mode=stop_mode,
        ))
        positions.remove(position)

    def fund(position, record, ambiguous=False, guaranteed_fraction=None):
        nonlocal cash
        per_fraction = -position["side"] * position["quantity"] * record["mark_price"] * record["rate"]
        amount = per_fraction * position["remaining_fraction"]
        if ambiguous:
            position["funding_timing_ambiguous"] = True
            if amount > 0:
                amount = per_fraction * (guaranteed_fraction or 0.0)
        cash += amount
        position["funding_cashflow"] += amount

    def update_state(position, result, remainder):
        position.update(active_stop=result.active_stop, highest_price=result.highest_price,
                        lowest_price=result.lowest_price, protection_activated=result.protection_activated,
                        protected_stop_price=result.protected_stop_price, remaining_fraction=remainder,
                        partial_taken=getattr(result, "partial_taken", position["partial_taken"]))

    for index, time in enumerate(times):
        current = {s: data[s][index - offsets[s]] for s in symbols
                   if 0 <= index - offsets[s] < len(data[s])}
        if any(p["symbol"] not in current for p in positions):
            raise ValueError("missing bar for an open position; explicit coverage finalization required")
        # A settlement at the open belongs to positions already held then.
        for symbol, record in funding_open[time]:
            for position in positions:
                if position["symbol"] == symbol:
                    fund(position, record)
        # Only outcomes already executable at the open can release capacity.
        # Profit gaps conservatively use shared trigger prices; adverse gaps use
        # the worse actual open. Range-only outcomes remain after admissions.
        for position in list(positions):
            row = current[position["symbol"]]
            result = lifecycle(position, row, point=True)
            partials = getattr(result, "partial_exits", ())
            remainder = position["remaining_fraction"] - sum(p.fraction for p in partials)
            for partial in partials:
                exit_fill(position, partial.fraction, partial.exit_price, partial.reason, time,
                          partial=True, phase="open")
            update_state(position, result, remainder)
            if result.decision:
                is_stop = result.decision.reason in {"stop_loss", "protected_stop", "trailing_stop"}
                close_position(position, remainder, row["open"] if is_stop else result.decision.exit_price,
                               "gap_stop" if is_stop else result.decision.reason, time,
                               index - position["entry_index"] + 1, phase="open")

        for event in scheduled[time]:
            symbol, direction = event["symbol"], event["direction"]
            if not allow_overlap and any(p["symbol"] == symbol for p in positions):
                reject(event, "position_exists")
                continue
            if max_positions is not None and len(positions) >= max_positions:
                reject(event, "max_positions")
                continue
            side = 1 if direction == "long" else -1
            raw_open = current[symbol]["open"]
            candidate_check = {}
            if require_candidate_anchor_at_open:
                metadata = event["candidate_lifecycle"]
                anchor = metadata["anchor"]
                candidate_check = dict(
                    execution_time=time, raw_open=raw_open, candidate_anchor=anchor,
                    candidate_lifecycle=deepcopy(metadata), candidate_anchor_checked_at=time,
                    candidate_delay_bars_checked=delay_bars,
                )
                invalidated = False
                for delayed_time in range(event["signal_time"], time, duration):
                    if not has_bar(symbol, delayed_time):
                        raise ValueError("missing completed delay bar for candidate anchor validation")
                    delayed_index = (delayed_time - times[0]) // duration - offsets[symbol]
                    delayed_bar = data[symbol][delayed_index]
                    extreme = delayed_bar["low" if direction == "long" else "high"]
                    if side * (extreme - anchor) < 0:
                        reject(event, "candidate_invalidated_during_delay", **candidate_check,
                               invalidated_bar_open_time=delayed_time,
                               invalidated_bar_close_time=delayed_bar["close_time"],
                               invalidating_extreme=extreme)
                        invalidated = True
                        break
                if invalidated:
                    continue
                if side * (raw_open - anchor) < 0:
                    reject(event, "candidate_invalidated_at_open", **candidate_check)
                    continue
            entry = raw_open * (1 + side * slip)
            stop = event["stop_loss"]
            levels = calculate_target_levels(direction, entry, stop, reward_risk)
            if levels is None or side * (raw_open - stop) <= 0 or levels["target_price"] <= 0:
                reject(event, "invalid_entry_open")
                continue
            opening_equity, _ = mark_equity(current, "open")
            budget = opening_equity * risk_fraction
            if opening_equity <= 0:
                reject(event, "nonpositive_equity")
                continue
            stop_fill = stop * (1 - side * slip)
            loss_per_unit = side * (entry - stop_fill) + fee_rate * (entry + stop_fill)
            admission = {}
            if min_net_reward_risk is not None:
                reward_fill = levels["protection_price"] * (1 - side * slip)
                target_fill = levels["target_price"] * (1 - side * slip)
                reward_per_unit = entry * calculate_return_pct(
                    direction, entry, reward_fill, fee_rate, return_model=return_model)
                target_reward_per_unit = entry * calculate_return_pct(
                    direction, entry, target_fill, fee_rate, return_model=return_model)
                admission = dict(
                    execution_time=time, entry_price=entry,
                    reward_reference="protection_price_1r",
                    reward_reference_price=levels["protection_price"], reward_fill_price=reward_fill,
                    target_reference_price=levels["target_price"], target_fill_price=target_fill,
                    stop_fill_price=stop_fill, potential_reward_per_unit=reward_per_unit,
                    potential_target_reward_per_unit=target_reward_per_unit,
                    potential_loss_per_unit=loss_per_unit,
                    net_reward_risk=reward_per_unit / loss_per_unit,
                    target_net_reward_risk=target_reward_per_unit / loss_per_unit,
                    min_net_reward_risk=min_net_reward_risk, fee_rate=fee_rate,
                    slippage_bps=slippage_bps, funding_included=False,
                )
                if admission["net_reward_risk"] < min_net_reward_risk:
                    reject(event, "net_reward_risk", **admission)
                    continue
            gross = sum(p["quantity"] * p["remaining_fraction"] * current[p["symbol"]]["open"] for p in positions)
            available_gross = max(0.0, opening_equity - gross)
            # A mathematically exhausted cap can leave a positive cancellation
            # residue. This equity-relative float64 tolerance is arithmetic,
            # not an exchange lot size or a tunable strategy minimum.
            if available_gross <= 16 * ulp(opening_equity):
                reject(event, "notional_budget")
                continue
            # Fees and entry slippage reduce collateral immediately; include
            # that reduction exactly once in the raw-price gross budget.
            quantity = min(budget / loss_per_unit,
                           available_gross / (raw_open + entry * fee_rate + abs(entry - raw_open)))
            initial_risk = quantity * loss_per_unit
            if quantity <= 0:
                reject(event, "notional_budget")
                continue
            if sum(risk_occupied(p) for p in positions) + initial_risk > opening_equity * max_risk_fraction + 1e-9:
                reject(event, "risk_budget")
                continue
            if (sum(risk_occupied(p) for p in positions if p["direction"] == direction) + initial_risk
                    > opening_equity * direction_caps[direction] + 1e-9):
                reject(event, "direction_risk_budget")
                continue
            entry_fee = quantity * entry * fee_rate
            cash -= entry_fee
            positions.append(dict(
                symbol=symbol, setup_id=event["setup_id"], direction=direction, side=side,
                quantity=quantity, remaining_fraction=1.0, partial_taken=False, partial_exits=[],
                signal_time=event["signal_time"], known_at=event["known_at"],
                entry_time=time, entry_index=index, entry_price=entry, initial_stop=stop,
                initial_risk=initial_risk, active_stop=stop, target=levels["target_price"],
                protection=levels["protection_price"], highest_price=entry, lowest_price=entry,
                protection_activated=False, protected_stop_price=0.0, entry_fee=entry_fee,
                exit_fees=0.0, realized_gross_pnl=0.0, funding_cashflow=0.0,
                funding_timing_ambiguous=False,
            ))
            entry_record = dict(symbol=symbol, setup_id=event["setup_id"], direction=direction,
                                time=time, kind="entry", phase="open", quantity=quantity,
                                entry_price=entry, fee=entry_fee)
            entry_record.update(admission)
            entry_record.update(candidate_check)
            fills.append(entry_record)

        for position in list(positions):
            row = current[position["symbol"]]
            previous_fraction = position["remaining_fraction"]
            result = lifecycle(position, row)
            partials = getattr(result, "partial_exits", ())
            remainder = previous_fraction - sum(partial.fraction for partial in partials)
            if remainder <= 0 or remainder > previous_fraction + 1e-12:
                raise ValueError("shared lifecycle returned invalid remaining quantity")
            for symbol, record in funding_inside[time]:
                if position["symbol"] == symbol:
                    fund(position, record, bool(result.decision or partials),
                         0.0 if result.decision else remainder)
            for partial in partials:
                exit_fill(position, partial.fraction, partial.exit_price, partial.reason,
                          time + duration, partial=True)
            update_state(position, result, remainder)
            holding_bars = index - position["entry_index"] + 1
            if result.decision:
                close_position(position, remainder, result.decision.exit_price, result.decision.reason,
                               time + duration, holding_bars)
            elif holding_bars >= max_hold_bars:
                close_position(position, remainder, row["close"], "timeout", time + duration, holding_bars)
            elif finalize and time + duration == bar_coverage[position["symbol"]]["end_exclusive"]:
                close_position(position, remainder, row["close"],
                               "fold_end" if index == len(times) - 1 else "coverage_end",
                               time + duration, holding_bars)
        value, unrealized = mark_equity(current, "close")
        risk = sum(risk_occupied(p) for p in positions)
        gross = sum(p["quantity"] * p["remaining_fraction"] * current[p["symbol"]]["close"] for p in positions)
        signed = sum(p["side"] * p["quantity"] * p["remaining_fraction"] * current[p["symbol"]]["close"] for p in positions)
        equity.append(dict(time=time + duration, equity=value, cash=cash, unrealized_pnl=unrealized,
                           gross_notional=gross, net_notional=signed, committed_risk=risk,
                           long_risk=sum(risk_occupied(p) for p in positions if p["direction"] == "long"),
                           short_risk=sum(risk_occupied(p) for p in positions if p["direction"] == "short"),
                           long_positions=sum(p["direction"] == "long" for p in positions),
                           short_positions=sum(p["direction"] == "short" for p in positions),
                           positions=len(positions)))

    parameters = dict(initial_equity=initial_equity, risk_fraction=risk_fraction,
                      max_positions=max_positions, max_risk_fraction=max_risk_fraction,
                      direction_risk_fraction=deepcopy(direction_risk_fraction), max_gross_multiple=1.0,
                      fee_rate=fee_rate, slippage_bps=slippage_bps, max_hold_bars=max_hold_bars,
                      stop_mode=stop_mode, return_model=return_model, reward_risk=reward_risk,
                      delay_bars=delay_bars, strict_funding=strict_funding,
                      allow_overlap=allow_overlap, finalize=finalize,
                      min_net_reward_risk=min_net_reward_risk,
                      require_candidate_anchor_at_open=require_candidate_anchor_at_open)
    if min_net_reward_risk is not None:
        parameters.update(net_reward_reference="protection_price_1r",
                          net_reward_risk_excludes=["future_funding", "gap_beyond_stop"])
    if require_candidate_anchor_at_open:
        parameters.update(candidate_anchor_reference="divergence_bar_low_or_high",
                          candidate_anchor_boundary=("completed_delay_bars_then_actual_open; "
                                                     "execution_bar_range_excluded"))
    return dict(trades=trades, equity=equity, fills=fills, rejections=rejections, signals=signals,
                open_positions=deepcopy(positions), status="RESEARCH_ONLY", promotion_eligible=False,
                funding_status="PARTIAL" if incomplete else "COMPLETE", funding_incomplete_symbols=incomplete,
                bar_coverage=bar_coverage, pit_listing_authority=False,
                funding_boundary="exact_open_existing_only; intrabar_debit_prior_quantity_credit_guaranteed_remaining",
                open_exit_boundary="adverse_gap_actual_open; profit_gap_shared_trigger; open_state_before_range",
                parameters=parameters)
