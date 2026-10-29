"""Four fixed hourly entry hypotheses; no accounts, orders or parameter search."""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any

from indicators import calculate_indicators, rolling_average, rolling_std

STEP, HOUR, FOUR = 900_000, 3_600_000, 14_400_000
CANDIDATES = ("D48", "B20", "S24", "EC20_60")


def _clock(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("nonnegative integer UTC millisecond clock required")
    return value


def _decimal(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not an OHLCV value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("finite decimal OHLCV value required") from exc
    if not result.is_finite():
        raise ValueError("finite decimal OHLCV value required")
    return result


def _canonical(rows, duration):
    if not isinstance(rows, list) or not rows:
        raise ValueError("nonempty finalized bar list required")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("standard OHLCV dictionary bars required")
        opening, close = _clock(row.get("open_time")), _clock(row.get("close_time"))
        values = {key: _decimal(row.get(key)) for key in ("open", "high", "low", "close", "volume")}
        if (opening % duration or close != opening + duration - 1
                or row.get("confirmed", True) is not True or row.get("is_closed", True) is not True
                or any(values[k] <= 0 for k in ("open", "high", "low", "close")) or values["volume"] < 0
                or not values["low"] <= min(values["open"], values["close"])
                <= max(values["open"], values["close"]) <= values["high"]):
            raise ValueError("invalid, misaligned or unclosed OHLCV bar")
        if result and opening != result[-1]["close_time"] + 1:
            raise ValueError("missing, duplicate or unordered source bar")
        result.append(dict(open_time=opening, close_time=close, **values, confirmed=True, is_closed=True))
    return result


def _floats(rows):
    return [{key: float(value) if isinstance(value, Decimal) else value for key, value in row.items()}
            for row in rows]


def prepare_hourly(rows15m, rows4h):
    """Aggregate exactly four UTC quarters using Decimal, then existing indicators.

    Existing caller-supplied indicator values are ignored. Every feature is
    recomputed from its own raw history; future 4h closes never enter past H1.
    """
    quarters, higher_raw = _canonical(rows15m, STEP), _canonical(rows4h, FOUR)
    if quarters[0]["open_time"] % HOUR or len(quarters) % 4:
        raise ValueError("each hourly aggregate requires all four UTC quarter hours")
    hourly = []
    for index in range(0, len(quarters), 4):
        group = quarters[index:index + 4]
        hourly.append(dict(open_time=group[0]["open_time"], close_time=group[-1]["close_time"],
                           open=group[0]["open"], close=group[-1]["close"],
                           high=max(row["high"] for row in group), low=min(row["low"] for row in group),
                           volume=sum((row["volume"] for row in group), Decimal(0)),
                           confirmed=True, is_closed=True, source_15m_count=4))
    hourly = calculate_indicators(_floats(hourly))
    higher = calculate_indicators(_floats(higher_raw))
    closes = [row["close"] for row in hourly]
    averages, deviations = rolling_average(closes, 20), rolling_std(closes, 20)
    higher_index = -1
    for index, row in enumerate(hourly):
        average, deviation = averages[index], deviations[index]
        row["bb_lower"] = average - 2 * deviation if average is not None and deviation is not None else None
        row["bb_upper"] = average + 2 * deviation if average is not None and deviation is not None else None
        while higher_index + 1 < len(higher) and higher[higher_index + 1]["close_time"] <= row["close_time"]:
            higher_index += 1
        if higher_index >= 0:
            context = higher[higher_index]
            row.update(higher_close_time=context["close_time"], higher_ema20=context.get("ema20"),
                       higher_ema60=context.get("ema60"), higher_adx=context.get("adx"))
        else:
            row.update(higher_close_time=None, higher_ema20=None, higher_ema60=None, higher_adx=None)
    return hourly


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _direction(candidate, rows, index):
    row, prior = rows[index], rows[index - 1] if index else None
    close = _number(row.get("close"))
    if candidate in {"D48", "EC20_60"}:
        fast, slow = _number(row.get("higher_ema20")), _number(row.get("higher_ema60"))
        if fast is None or slow is None:
            return "wait", "MISSING_4H_EMA"
    if candidate in {"D48", "S24"}:
        window = 48 if candidate == "D48" else 24
        if index < window:
            return "wait", "INSUFFICIENT_PRIOR_HOURS"
        prior_highs = [_number(r.get("high")) for r in rows[index-window:index]]
        prior_lows = [_number(r.get("low")) for r in rows[index-window:index]]
        if any(v is None for v in prior_highs + prior_lows):
            return "wait", "MISSING_CHANNEL_PRICE"
        highest, lowest = max(prior_highs), min(prior_lows)
        if candidate == "D48":
            if close > highest and fast > slow:
                return "long", "PRIOR_48H_HIGH_BREAKOUT"
            if close < lowest and fast < slow:
                return "short", "PRIOR_48H_LOW_BREAKOUT"
        else:
            if row["low"] < lowest < close and close > row["open"]:
                return "long", "PRIOR_24H_LOW_SWEEP_RECLAIM"
            if row["high"] > highest > close and close < row["open"]:
                return "short", "PRIOR_24H_HIGH_SWEEP_RECLAIM"
    elif candidate == "B20":
        if prior is None:
            return "wait", "MISSING_PRIOR_HOUR"
        lower, upper = _number(row.get("bb_lower")), _number(row.get("bb_upper"))
        old_lower, old_upper = _number(prior.get("bb_lower")), _number(prior.get("bb_upper"))
        old_close, adx = _number(prior.get("close")), _number(row.get("higher_adx"))
        if any(v is None for v in (lower, upper, old_lower, old_upper, old_close, adx)):
            return "wait", "MISSING_BAND_OR_4H_ADX"
        if not lower < upper or not old_lower < old_upper:
            return "wait", "ZERO_OR_INVALID_BANDWIDTH"
        if not adx < 20:
            return "wait", "FILTERED_4H_ADX"
        if lower < close < upper:
            if old_close < old_lower:
                return "long", "LOWER_BAND_REENTRY"
            if old_close > old_upper:
                return "short", "UPPER_BAND_REENTRY"
    else:
        if prior is None:
            return "wait", "MISSING_PRIOR_HOUR"
        now_fast, now_slow = _number(row.get("ema20")), _number(row.get("ema60"))
        old_fast, old_slow = _number(prior.get("ema20")), _number(prior.get("ema60"))
        if any(v is None for v in (now_fast, now_slow, old_fast, old_slow)):
            return "wait", "MISSING_HOURLY_EMA"
        if old_fast <= old_slow and now_fast > now_slow and fast > slow:
            return "long", "HOURLY_EMA_BULL_CROSS"
        if old_fast >= old_slow and now_fast < now_slow and fast < slow:
            return "short", "HOURLY_EMA_BEAR_CROSS"
    return "wait", "TRIGGER_NOT_PRESENT"


def generate_events(hourly_by_symbol, start, end):
    """Return every candidate decision and hypothetical next-boundary intent."""
    _clock(start)
    _clock(end)
    if start >= end or start % HOUR or end % HOUR:
        raise ValueError("increasing whole-hour UTC scoring boundaries required")
    events = {candidate: [] for candidate in CANDIDATES}
    decisions = []
    coverage: dict[str, Any] = {}
    for symbol, rows in sorted(hourly_by_symbol.items()):
        # Validate price clocks without discarding the prepared feature columns.
        _canonical(rows, HOUR)
        counts = {candidate: dict(long=0, short=0, wait=0) for candidate in CANDIDATES}
        for index, row in enumerate(rows):
            if not start <= row["open_time"] < end:
                continue
            for candidate in CANDIDATES:
                atr, close = _number(row.get("atr")), _number(row.get("close"))
                higher_close = row.get("higher_close_time")
                expected = ((row["close_time"] + 1)//FOUR)*FOUR - 1
                if (atr is None or atr <= 0 or close is None
                        or not isinstance(higher_close, int) or isinstance(higher_close, bool)
                        or higher_close < 0 or higher_close != expected):
                    direction, reason = "wait", "MISSING_RISK_OR_FINALIZED_4H_CONTEXT"
                else:
                    direction, reason = _direction(candidate, rows, index)
                stop = None
                if direction != "wait":
                    extremes = [float(row["low"] if direction == "long" else row["high"])]
                    if candidate == "B20":
                        extremes.append(float(rows[index-1]["low"] if direction == "long" else rows[index-1]["high"]))
                    extremes.append(close - 1.5*atr if direction == "long" else close + 1.5*atr)
                    stop = min(extremes) if direction == "long" else max(extremes)
                    if not math.isfinite(stop) or stop <= 0:
                        direction, reason, stop = "wait", "INVALID_INITIAL_STOP", None
                counts[candidate][direction] += 1
                decision = dict(symbol=symbol, candidate=candidate, variant=candidate, signal=direction,
                                reason=reason, decision_close=row["close_time"],
                                higher_close_time=higher_close, price=close, atr=atr, stop_loss=stop)
                decisions.append(decision)
                if direction != "wait":
                    identity = f"{candidate}:{symbol}:{direction}:{row['close_time']}"
                    events[candidate].append(dict(symbol=symbol, candidate=candidate, variant=candidate,
                                                  direction=direction, setup_id=identity,
                                                  decision_close=row["close_time"], known_at=row["close_time"]+1,
                                                  signal_time=row["close_time"]+1+STEP, stop_loss=stop,
                                                  higher_close_time=higher_close,
                                                  entry_reason=reason, scope="HISTORICAL_MODELED_ARRIVAL"))
        coverage[symbol] = dict(prepared_hours=len(rows), scoring_hours=sum(start <= r["open_time"] < end for r in rows),
                                candidate_decisions=counts)
    return dict(events=events, decisions=decisions, coverage=coverage)
