"""Offline linear-perpetual target weights; no stop lifecycle or order API.

Shared calendar/funding validation and the production linear return primitive
are reused. Weekly targets use only completed daily bars. Quantity targets are
solved against raw-open marked equity after actual fees and adverse slippage.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from datetime import datetime
from statistics import stdev

from position_manager import LINEAR_USDM_V1, calculate_return_pct
from research_portfolio import _calendar, _funding_calendar

STEP = 900000
DAY = 86400000
WEEK = 7 * DAY


def utc_ms(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def finite(value, positive=False):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and (value > 0 if positive else True)
    )


def daily_weight(closes, lookback_days):
    needed = max(60, lookback_days or 0) + 1
    if len(closes) < needed or not all(finite(x, True) for x in closes):
        raise ValueError("Complete finite positive daily history required")
    returns = [math.log(b / a) for a, b in zip(closes[-61:-1], closes[-60:])]
    volatility = stdev(returns) * math.sqrt(365)
    momentum = closes[-1] / closes[-1 - lookback_days] - 1 if lookback_days else None
    on = lookback_days is None or momentum > 0
    weight = min(0.5, 0.1 / volatility) if on and volatility else 0.5 if on else 0.0
    return dict(
        weight=weight,
        momentum=momentum,
        annualized_volatility=volatility,
        volatility_returns=60,
        sample_ddof=1,
        daily_observations_used=needed,
    )


def aggregate_daily(data):
    symbols, times, duration, coverage, offsets = _calendar(data)
    if (
        duration != STEP
        or times[0] % DAY
        or (times[-1] + STEP) % DAY
        or any(offsets[s] or coverage[s]["missing_suffix_bars"] for s in symbols)
    ):
        raise ValueError("Identical complete UTC daily grids are required")
    return {
        s: [dict(end_ms=rows[i + 95]["close_time"] + 1, close=rows[i + 95]["close"]) for i in range(0, len(rows), 96)]
        for s, rows in data.items()
    }


def build_weekly_targets(data, protocol, strategy_id):
    symbols = protocol["symbols"]
    if set(data) != set(symbols):
        raise ValueError("All frozen symbols required")
    start, end = (utc_ms(protocol["scoring"][k]) for k in ("start_inclusive_utc", "end_exclusive_utc"))
    if strategy_id == "equal_weight_buy_hold":
        return [
            dict(
                decision_time_ms=start,
                known_at=start,
                weights={s: 0.5 for s in symbols},
                features={},
                strategy_id=strategy_id,
                decision_kind="initial_net_allocation",
            )
        ]
    lookbacks = {c["id"]: c["lookback_days"] for c in protocol["candidates"]}
    if strategy_id == "vol_scaled_hold":
        lookback = None
    elif strategy_id in lookbacks:
        lookback = lookbacks[strategy_id]
    else:
        raise ValueError("Unregistered strategy")
    daily = aggregate_daily(data)
    ends = [x["end_ms"] for x in daily[symbols[0]]]
    first = start + ((4 - (start // DAY) % 7) % 7) * DAY
    result = []
    for decision in range(first, end, WEEK):
        features = {}
        for symbol in symbols:
            known = bisect_right(ends, decision)
            if not known or ends[known - 1] != decision:
                raise ValueError("Complete daily history through decision boundary required")
            features[symbol] = dict(
                daily_weight([x["close"] for x in daily[symbol][:known]], lookback), last_daily_end_ms=ends[known - 1]
            )
        result.append(
            dict(
                decision_time_ms=decision,
                known_at=decision,
                weights={s: features[s]["weight"] for s in symbols},
                features=features,
                strategy_id=strategy_id,
                decision_kind="weekly_closed_daily",
            )
        )
    return result


def solve_target_quantities(equity, prices, quantities, weights, fee_rate, slippage_bps):
    """Solve E_after + sum(actual friction on delta q) = E_before.

    For buys friction per raw dollar is slip+fee*(1+slip); for sells it is
    slip+fee*(1-slip). Marking is at the simultaneous raw-open snapshot.
    The function is strictly increasing for the registered cost/weight bounds.
    """
    symbols = sorted(prices)
    if (
        not finite(equity, True)
        or set(quantities) != set(symbols)
        or set(weights) != set(symbols)
        or not all(
            finite(prices[s], True)
            and finite(quantities[s])
            and quantities[s] >= 0
            and finite(weights[s])
            and 0 <= weights[s] <= 0.5
            for s in symbols
        )
        or sum(weights.values()) > 1 + 1e-12
        or not finite(fee_rate)
        or not 0 <= fee_rate < 1
        or not finite(slippage_bps)
        or not 0 <= slippage_bps < 10000
    ):
        raise ValueError("Invalid equity, price, current quantity, target weight or friction")
    slip = slippage_bps / 10000

    def cost(value):
        total = 0.0
        for symbol in symbols:
            delta = weights[symbol] * value / prices[symbol] - quantities[symbol]
            side = 1 if delta >= 0 else -1
            total += abs(delta) * prices[symbol] * (slip + fee_rate * (1 + side * slip))
        return total

    if cost(0) >= equity:
        raise ValueError("Nonpositive equity after required target friction")
    if cost(equity) == 0:
        after = equity
    else:
        lower, upper = 0.0, equity
        for _ in range(80):
            middle = (lower + upper) / 2
            if middle + cost(middle) > equity:
                upper = middle
            else:
                lower = middle
        after = (lower + upper) / 2
    if not math.isclose(after + cost(after), equity, rel_tol=1e-12, abs_tol=1e-8):
        raise ValueError("Self-financing solution did not converge")
    return after, {s: weights[s] * after / prices[s] for s in symbols}


def replay_target_weights(data, funding, targets, protocol, scenario):
    start, end = (utc_ms(protocol["scoring"][k]) for k in ("start_inclusive_utc", "end_exclusive_utc"))
    symbols = sorted(protocol["symbols"])
    if set(data) != set(symbols):
        raise ValueError("Frozen complete symbol population required")
    score = {s: [b for b in data[s] if start <= b["open_time"] < end] for s in symbols}
    checked, times, duration, coverage, offsets = _calendar(score)
    if (
        checked != symbols
        or duration != STEP
        or times[0] != start
        or times[-1] + STEP != end
        or any(offsets[s] or coverage[s]["missing_suffix_bars"] for s in symbols)
    ):
        raise ValueError("Complete synchronized scoring calendar required")
    opening, intrabar, incomplete = _funding_calendar(funding, symbols, times, STEP, True, coverage)
    if incomplete:
        raise ValueError("Missing funding cannot be cash")
    fee, slip_bps = scenario["fee_rate_per_fill"], scenario["adverse_slippage_bps"]
    delay = scenario["extra_delay_minutes"]
    if not isinstance(delay, int) or isinstance(delay, bool) or delay < 0 or delay % 15:
        raise ValueError("Delay must be complete 15m bars")
    scheduled = {}
    for event in targets:
        clock = event["decision_time_ms"]
        if (
            not isinstance(clock, int)
            or isinstance(clock, bool)
            or event["known_at"] != clock
            or clock < start
            or clock >= end
        ):
            raise ValueError("Invalid preknown decision clock")
        execution = clock + STEP + delay * 60000
        if execution >= end or execution in scheduled:
            raise ValueError("Unavailable or duplicate planned execution")
        scheduled[execution] = event
    initial = float(protocol["accounting"]["initial_equity_usdt"])
    if not finite(initial, True):
        raise ValueError("Positive collateral required")
    cash = initial
    positions = {s: dict(quantity=0.0, average_entry=0.0) for s in symbols}
    fills, funding_rows, curve, rebalances = [], [], [], []
    fees = realized = funded = 0.0
    contributions = {s: dict(realized_gross=0.0, fees=0.0, funding=0.0) for s in symbols}
    slip = slip_bps / 10000
    peak, max_dd = initial, 0.0

    def equity(prices):
        return cash + math.fsum(p["quantity"] * (prices[s] - p["average_entry"]) for s, p in positions.items())

    def settle(symbol, record, phase):
        nonlocal cash, funded
        quantity = positions[symbol]["quantity"]
        value = -quantity * record["mark_price"] * record["rate"]
        cash += value
        funded += value
        contributions[symbol]["funding"] += value
        funding_rows.append(
            dict(
                symbol=symbol,
                time=record["time"],
                held_quantity=quantity,
                mark_price=record["mark_price"],
                rate=record["rate"],
                cashflow=value,
                phase=phase,
            )
        )

    for index, time in enumerate(times):
        for symbol, record in opening.get(time, []):
            settle(symbol, record, "before_open_fills")
        prices = {s: score[s][index]["open"] for s in symbols}
        if time in scheduled:
            event = scheduled[time]
            before = equity(prices)
            expected, target = solve_target_quantities(
                before, prices, {s: positions[s]["quantity"] for s in symbols}, event["weights"], fee, slip_bps
            )
            deltas = {s: target[s] - positions[s]["quantity"] for s in symbols}
            fill_start = len(fills)
            for symbol in sorted(symbols, key=lambda s: (deltas[s] >= 0, s)):
                delta = deltas[symbol]
                if delta == 0:
                    continue
                position = positions[symbol]
                old = position["quantity"]
                side = 1 if delta > 0 else -1
                price = prices[symbol] * (1 + side * slip)
                quantity = abs(delta)
                charge = quantity * price * fee
                gross = 0.0
                if side == 1:
                    position["average_entry"] = (old * position["average_entry"] + quantity * price) / (old + quantity)
                else:
                    gross = (
                        quantity
                        * position["average_entry"]
                        * calculate_return_pct(
                            "long", position["average_entry"], price, 0.0, return_model=LINEAR_USDM_V1
                        )
                    )
                    cash += gross
                    realized += gross
                    contributions[symbol]["realized_gross"] += gross
                cash -= charge
                fees += charge
                contributions[symbol]["fees"] += charge
                position["quantity"] = target[symbol]
                if target[symbol] == 0:
                    position["average_entry"] = 0.0
                fills.append(
                    dict(
                        symbol=symbol,
                        time=time,
                        decision_time_ms=event["decision_time_ms"],
                        side="buy" if side == 1 else "sell",
                        signed_quantity=delta,
                        quantity=quantity,
                        raw_price=prices[symbol],
                        price=price,
                        notional=quantity * price,
                        fee=charge,
                        realized_gross_pnl=gross,
                        quantity_after=target[symbol],
                    )
                )
            after = equity(prices)
            gross = math.fsum(target[s] * prices[s] for s in symbols)
            if not math.isclose(after, expected, rel_tol=1e-12, abs_tol=1e-8) or gross > after + 1e-8:
                raise ValueError("Post-fill equity conservation or gross cap failed")
            rebalances.append(
                dict(
                    time=time,
                    decision_time_ms=event["decision_time_ms"],
                    weights=dict(event["weights"]),
                    pre_equity=before,
                    post_equity=after,
                    post_gross_notional=gross,
                    target_quantities=target,
                    fills=len(fills) - fill_start,
                )
            )
        for symbol, record in intrabar.get(time, []):
            settle(symbol, record, "after_open_fills")
        marks = {s: score[s][index]["close"] for s in symbols}
        marked = equity(marks)
        if not finite(marked, True):
            raise ValueError("ECONOMIC_OR_RISK_FAIL: nonpositive marked equity")
        gross = math.fsum(positions[s]["quantity"] * marks[s] for s in symbols)
        peak = max(peak, marked)
        max_dd = max(max_dd, 1 - marked / peak)
        curve.append(
            dict(
                time=time + STEP,
                cash=cash,
                equity=marked,
                unrealized_pnl=marked - cash,
                gross_notional=gross,
                gross_over_equity=gross / marked,
                quantities={s: positions[s]["quantity"] for s in symbols},
                cumulative_fees=fees,
                cumulative_funding=funded,
                cumulative_realized_gross=realized,
            )
        )
    final = curve[-1]["equity"]
    by_symbol = {
        s: dict(
            contributions[s],
            unrealized_pnl=positions[s]["quantity"] * (score[s][-1]["close"] - positions[s]["average_entry"]),
        )
        for s in symbols
    }
    for values in by_symbol.values():
        values["net_pnl"] = values["realized_gross"] - values["fees"] + values["funding"] + values["unrealized_pnl"]
    if not math.isclose(cash, initial + realized - fees + funded, abs_tol=1e-8, rel_tol=1e-12) or not math.isclose(
        final - initial, math.fsum(x["net_pnl"] for x in by_symbol.values()), abs_tol=1e-8, rel_tol=1e-12
    ):
        raise ValueError("Cash, terminal MTM or symbol contribution conservation failed")
    return dict(
        status="COMPLETE_TARGET_WEIGHT_REPLAY",
        initial_equity=initial,
        final_equity=final,
        final_cash=cash,
        net_return=final / initial - 1,
        max_drawdown=max_dd,
        final_positions=positions,
        total_fees=fees,
        funding_cashflow=funded,
        realized_gross_pnl=realized,
        turnover_over_initial_equity=math.fsum(x["notional"] for x in fills) / initial,
        max_close_gross_over_equity=max(x["gross_over_equity"] for x in curve),
        close_marks_above_one_gross=sum(x["gross_over_equity"] > 1 + 1e-10 for x in curve),
        fills=fills,
        funding=funding_rows,
        equity=curve,
        rebalances=rebalances,
        by_symbol=by_symbol,
        scenario=dict(scenario),
        promotion_eligible=False,
        live_authorized=False,
        tail="OPEN_POSITIONS_MARKED_NO_ARTIFICIAL_TERMINAL_LIQUIDATION",
        shared_accounting_sources=[
            "research_portfolio._calendar",
            "research_portfolio._funding_calendar",
            "position_manager.calculate_return_pct:linear_usdm_v1",
        ],
    )
