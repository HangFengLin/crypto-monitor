"""Independent, incremental price-pattern observations, without execution clients.

The fixed v1 rules describe chart geometry, not a validated trading advantage.
Past anchors are labelled at their bar times; all events use actual availability.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any

RULE_VERSION = "pattern_observer_v1"
DEFAULT_CONFIG = {
    "history_limit": 240,
    "range_lookback": 32,
    "warmup_bars": 40,
    "atr_period": 14,
    "range_atr_max": 6.0,
    "breakout_buffer_atr": .1,
    "retest_band_atr": .25,
    "breakout_invalid_atr": .5,
    "pivot_left": 2,
    "pivot_right": 2,
    "double_tolerance_atr": .5,
    "double_min_separation": 4,
    "double_max_span": 40,
    "neckline_prominence_atr": 1.0,
    "double_invalid_atr": .25,
    "forming_expiry_bars": 12,
    "confirmed_expiry_bars": 24,
    "max_streams": 64,
    "dedupe_limit": 512,
    "latest_limit": 256,
}
_INTEGER_KEYS = {key for key, value in DEFAULT_CONFIG.items() if isinstance(value, int)}
_PATTERNS = {"breakout_retest", "double_bottom", "double_top"}
_STAGES = {"forming", "confirmed", "invalidated", "expired"}


def _number(value: Any, *, positive: bool = False) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or (value <= 0 if positive else value < 0)):
        raise ValueError("pattern number must be finite and within its allowed range")
    return float(value)


def _millisecond(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("pattern timestamp must be a nonnegative integer millisecond")
    return value


def _bar(value: dict[str, Any], available_at_ms: int) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("confirmed") is False or value.get("is_closed") is False:
        raise ValueError("only finalized OHLCV bars are accepted")
    opened, closed = _millisecond(value.get("open_time")), _millisecond(value.get("close_time"))
    if opened > closed or closed > available_at_ms:
        raise ValueError("bar has not closed before its availability time")
    result: dict[str, Any] = {"open_time": opened, "close_time": closed}
    result.update({name: _number(value.get(name), positive=True) for name in ("open", "high", "low", "close")})
    result["volume"] = _number(value.get("volume"))
    if not result["low"] <= min(result["open"], result["close"]) <= max(result["open"], result["close"]) <= result["high"]:
        raise ValueError("inconsistent OHLC prices")
    return result


def _identity(symbol: str, interval: str) -> str:
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{2,32}", symbol):
        raise ValueError("invalid pattern symbol")
    if not isinstance(interval, str) or not re.fullmatch(r"[1-9][0-9]*[mhdw]", interval):
        raise ValueError("invalid pattern interval")
    return symbol + ":" + interval


def _public(pattern: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy({key: value for key, value in pattern.items() if not key.startswith("_")})


class PatternObserver:
    """Consume one completed bar at a time and emit immutable lifecycle events."""

    def __init__(self, config: dict[str, Any] | None = None):
        if config is not None and not isinstance(config, dict):
            raise ValueError("pattern config must be a mapping")
        if set(config or {}) - DEFAULT_CONFIG.keys():
            raise ValueError("unknown pattern rule setting")
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        for key, value in self.config.items():
            _number(value, positive=True)
            if key in _INTEGER_KEYS and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError("pattern window sizes must be integers")
        if (self.config["warmup_bars"] < max(self.config["range_lookback"], self.config["atr_period"] + 1)
                or self.config["history_limit"] < max(self.config["warmup_bars"] + 1,
                                                       self.config["double_max_span"] + 5,
                                                       self.config["pivot_left"] + self.config["pivot_right"] + 1)
                or self.config["double_max_span"] < self.config["double_min_separation"]
                or self.config["history_limit"] > 1000 or self.config["max_streams"] > 512
                or self.config["latest_limit"] > 2000 or self.config["dedupe_limit"] > 4000):
            raise ValueError("inconsistent or unbounded pattern settings")
        self._streams: dict[str, dict[str, Any]] = {}
        self._latest: dict[str, dict[str, Any]] = {}

    def process_bar(self, symbol: str, interval: str, bar: dict[str, Any], *, available_at_ms: int) -> list[dict[str, Any]]:
        key = _identity(symbol, interval)
        available_at_ms = _millisecond(available_at_ms)
        row = _bar(bar, available_at_ms)
        stream = self._streams.get(key)
        if stream is None:
            if len(self._streams) >= self.config["max_streams"]:
                raise ValueError("pattern stream capacity reached")
            stream = {"symbol": symbol, "interval": interval, "bars": [], "count": 0,
                      "active": {}, "pivots": [], "seen": [], "last_available_at_ms": 0}
        history = stream["bars"]
        if history and row["close_time"] <= history[-1]["close_time"]:
            original = next((item for item in history if item["close_time"] == row["close_time"]), None)
            if original == row:
                return []
            raise ValueError("revised or out-of-order pattern bar")
        if available_at_ms < stream["last_available_at_ms"]:
            raise ValueError("pattern availability time moved backwards")
        if history and row["open_time"] <= history[-1]["close_time"]:
            raise ValueError("overlapping pattern bars")

        self._streams[key] = stream
        past = list(history)
        index = stream["count"]
        history.append(row)
        del history[:-self.config["history_limit"]]
        stream["count"] += 1
        stream["last_available_at_ms"] = available_at_ms
        events = self._advance(stream, row, index, available_at_ms)
        pivot_added = self._pivot(stream, index)
        atr = self._atr(past)
        if len(past) >= self.config["warmup_bars"] and atr and atr > 0:
            events.extend(self._breakout(stream, past, row, index, available_at_ms, atr))
            if pivot_added:
                events.extend(self._double(stream, row, index, available_at_ms, atr))
        return events

    def _atr(self, rows: list[dict[str, Any]]) -> float | None:
        period = self.config["atr_period"]
        if len(rows) < period + 1:
            return None
        recent = rows[-period - 1:]
        return sum(max(row["high"] - row["low"], abs(row["high"] - previous["close"]),
                       abs(row["low"] - previous["close"]))
                   for previous, row in zip(recent, recent[1:])) / period

    def _emit(self, pattern: dict[str, Any], row: dict[str, Any], available: int,
              stage: str, reason: str) -> dict[str, Any]:
        pattern.update(stage=stage, reason=reason, updated_at_ms=available, available_at_ms=available,
                       bar_close_time=row["close_time"], current_price=row["close"])
        event = _public(pattern)
        self._latest[pattern["id"]] = event
        while len(self._latest) > self.config["latest_limit"]:
            oldest = min(self._latest, key=lambda item: self._latest[item]["updated_at_ms"])
            del self._latest[oldest]
        return copy.deepcopy(event)

    def _begin(self, stream: dict[str, Any], row: dict[str, Any], index: int, available: int,
               pattern: str, direction: str, level: float, invalid: float, atr: float,
               points: list[dict[str, Any]], anchors: list[int]) -> list[dict[str, Any]]:
        slot = pattern + ":" + direction
        identity = "|".join(map(str, [RULE_VERSION, stream["symbol"], stream["interval"], pattern, direction, *anchors]))
        ident = hashlib.sha256(identity.encode()).hexdigest()[:24]
        if slot in stream["active"] or ident in stream["seen"]:
            return []
        stream["seen"].append(ident)
        del stream["seen"][:-self.config["dedupe_limit"]]
        current = {"id": ident, "pattern": pattern, "direction": direction,
                   "symbol": stream["symbol"], "interval": stream["interval"], "rule_version": RULE_VERSION,
                   "mode": "observation_only", "detected_at_ms": available, "level": level,
                   "invalid_level": invalid, "atr": atr, "points": points,
                   "_formed_index": index, "_slot": slot}
        stream["active"][slot] = current
        return [self._emit(current, row, available, "forming", "BREAKOUT_DETECTED" if pattern == "breakout_retest"
                           else "NECKLINE_FORMING")]

    def _breakout(self, stream: dict[str, Any], past: list[dict[str, Any]], row: dict[str, Any],
                  index: int, available: int, atr: float) -> list[dict[str, Any]]:
        window = past[-self.config["range_lookback"]:]
        upper = max(window, key=lambda item: item["high"])
        lower = min(window, key=lambda item: item["low"])
        if upper["high"] - lower["low"] > self.config["range_atr_max"] * atr:
            return []
        buffer = self.config["breakout_buffer_atr"] * atr
        if row["close"] > upper["high"] + buffer:
            direction, level, anchor = "long", upper["high"], upper
            invalid = level - self.config["breakout_invalid_atr"] * atr
        elif row["close"] < lower["low"] - buffer:
            direction, level, anchor = "short", lower["low"], lower
            invalid = level + self.config["breakout_invalid_atr"] * atr
        else:
            return []
        if invalid <= 0:
            return []
        return self._begin(stream, row, index, available, "breakout_retest", direction, level, invalid, atr,
                           [{"time_ms": anchor["close_time"], "price": level, "kind": "range_boundary"},
                            {"time_ms": row["close_time"], "price": row["close"], "kind": "breakout"}],
                           [anchor["close_time"], row["close_time"]])

    def _pivot(self, stream: dict[str, Any], index: int) -> bool:
        left, right = self.config["pivot_left"], self.config["pivot_right"]
        window_size = left + right + 1
        if len(stream["bars"]) < window_size:
            return False
        window = stream["bars"][-window_size:]
        candidate = window[left]
        neighbors = window[:left] + window[left + 1:]
        high = all(candidate["high"] > other["high"] for other in neighbors)
        low = all(candidate["low"] < other["low"] for other in neighbors)
        if high == low:  # A double-sided outside bar has ambiguous structure.
            return False
        pivot = {"time_ms": candidate["close_time"], "price": candidate["high"] if high else candidate["low"],
                 "kind": "high" if high else "low", "index": index - right}
        pivots = stream["pivots"]
        if pivots and pivots[-1]["kind"] == pivot["kind"]:
            if ((high and pivot["price"] <= pivots[-1]["price"])
                    or (low and pivot["price"] >= pivots[-1]["price"])):
                return False
            pivots[-1] = pivot
        else:
            pivots.append(pivot)
        stream["pivots"] = [item for item in pivots if item["index"] >= index - self.config["history_limit"]]
        return True

    def _double(self, stream: dict[str, Any], row: dict[str, Any], index: int,
                available: int, atr: float) -> list[dict[str, Any]]:
        if len(stream["pivots"]) < 3:
            return []
        first, neck, second = stream["pivots"][-3:]
        if (first["kind"] != second["kind"] or first["kind"] == neck["kind"]
                or not self.config["double_min_separation"] <= second["index"] - first["index"] <= self.config["double_max_span"]
                or abs(first["price"] - second["price"]) > self.config["double_tolerance_atr"] * atr):
            return []
        if first["kind"] == "low":
            direction, name = "long", "double_bottom"
            prominence = neck["price"] - max(first["price"], second["price"])
            invalid = min(first["price"], second["price"]) - self.config["double_invalid_atr"] * atr
        else:
            direction, name = "short", "double_top"
            prominence = min(first["price"], second["price"]) - neck["price"]
            invalid = max(first["price"], second["price"]) + self.config["double_invalid_atr"] * atr
        if prominence < self.config["neckline_prominence_atr"] * atr or invalid <= 0:
            return []
        labels = ("first_bottom", "neckline", "second_bottom") if direction == "long" else ("first_top", "neckline", "second_top")
        points = [{"time_ms": pivot["time_ms"], "price": pivot["price"], "kind": label}
                  for pivot, label in zip((first, neck, second), labels)]
        return self._begin(stream, row, index, available, name, direction, neck["price"], invalid, atr,
                           points, [point["time_ms"] for point in points])

    def _advance(self, stream: dict[str, Any], row: dict[str, Any], index: int, available: int) -> list[dict[str, Any]]:
        events = []
        for slot, pattern in list(stream["active"].items()):
            long = pattern["direction"] == "long"
            invalid = row["close"] < pattern["invalid_level"] if long else row["close"] > pattern["invalid_level"]
            if invalid:
                events.append(self._emit(pattern, row, available, "invalidated", "CLOSE_BEYOND_INVALIDATION"))
                del stream["active"][slot]
                continue
            if pattern["stage"] == "confirmed":
                if index - pattern["_confirmed_index"] >= self.config["confirmed_expiry_bars"]:
                    events.append(self._emit(pattern, row, available, "expired", "OBSERVATION_ENDED"))
                    del stream["active"][slot]
                continue
            confirmed = False
            if pattern["pattern"] == "breakout_retest":
                retest = pattern.get("_retest")
                if retest is not None:
                    confirmed = row["close"] > retest["high"] if long else row["close"] < retest["low"]
                else:
                    band = self.config["retest_band_atr"] * pattern["atr"]
                    overlaps = row["low"] <= pattern["level"] + band and row["high"] >= pattern["level"] - band
                    on_side = row["close"] >= pattern["level"] if long else row["close"] <= pattern["level"]
                    if overlaps and on_side:
                        pattern["_retest"] = {"high": row["high"], "low": row["low"], "index": index}
                        pattern["retest_at_ms"] = available
                        pattern.update(bar_close_time=row["close_time"], available_at_ms=available,
                                       updated_at_ms=available, current_price=row["close"])
                        pattern["points"].append({"time_ms": row["close_time"], "price": row["low"] if long else row["high"],
                                                  "kind": "retest"})
            else:
                buffer = self.config["breakout_buffer_atr"] * pattern["atr"]
                confirmed = row["close"] > pattern["level"] + buffer if long else row["close"] < pattern["level"] - buffer
            if confirmed:
                pattern["_confirmed_index"] = index
                pattern["confirmed_at_ms"] = available
                events.append(self._emit(pattern, row, available, "confirmed", "RETEST_CONFIRMED" if pattern["pattern"] == "breakout_retest"
                                         else "NECKLINE_CONFIRMED"))
            elif index - pattern["_formed_index"] >= self.config["forming_expiry_bars"]:
                events.append(self._emit(pattern, row, available, "expired", "FORMATION_TIMED_OUT"))
                del stream["active"][slot]
        return events

    def bars_for(self, symbol: str, interval: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self._streams.get(_identity(symbol, interval), {}).get("bars", []))

    def latest(self) -> list[dict[str, Any]]:
        result = copy.deepcopy(self._latest)
        for stream in self._streams.values():
            for pattern in stream["active"].values():
                result[pattern["id"]] = _public(pattern)
        return sorted(result.values(), key=lambda item: (item["updated_at_ms"], item["id"]), reverse=True)[:self.config["latest_limit"]]

    def dump_state(self) -> dict[str, Any]:
        return copy.deepcopy({"schema_version": 1, "rule_version": RULE_VERSION, "config": self.config,
                              "streams": list(self._streams.values()), "latest": list(self._latest.values())})

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> PatternObserver:
        if not isinstance(state, dict) or state.get("schema_version") != 1 or state.get("rule_version") != RULE_VERSION:
            raise ValueError("unsupported pattern checkpoint")
        try:
            json.dumps(state, allow_nan=False)
            observer = cls(state["config"])
            streams, latest = state["streams"], state["latest"]
            if not isinstance(streams, list) or not isinstance(latest, list):
                raise ValueError("invalid pattern checkpoint collections")
            if len(streams) > observer.config["max_streams"] or len(latest) > observer.config["latest_limit"]:
                raise ValueError("oversized pattern checkpoint")
            for stream in streams:
                key = _identity(stream["symbol"], stream["interval"])
                if key in observer._streams:
                    raise ValueError("duplicate pattern stream")
                bars = stream["bars"]
                available = _millisecond(stream["last_available_at_ms"])
                if (not isinstance(bars, list) or len(bars) > observer.config["history_limit"]
                        or not isinstance(stream["count"], int) or stream["count"] < len(bars)
                        or not isinstance(stream["active"], dict) or len(stream["active"]) > 4
                        or not isinstance(stream["pivots"], list) or len(stream["pivots"]) > observer.config["history_limit"]
                        or not isinstance(stream["seen"], list) or len(stream["seen"]) > observer.config["dedupe_limit"]
                        or any(not isinstance(ident, str) for ident in stream["seen"])):
                    raise ValueError("invalid pattern checkpoint stream")
                normalized = [_bar(row, available) for row in bars]
                if any(a["close_time"] >= b["open_time"] for a, b in zip(normalized, normalized[1:])):
                    raise ValueError("unordered pattern checkpoint history")
                for pattern in stream["active"].values():
                    observer._validate_pattern(pattern)
                    if (pattern["symbol"] != stream["symbol"] or pattern["interval"] != stream["interval"]
                            or pattern["stage"] not in {"forming", "confirmed"}
                            or not isinstance(pattern.get("_formed_index"), int)
                            or not 0 <= pattern["_formed_index"] < stream["count"]
                            or (pattern["stage"] == "confirmed" and not isinstance(pattern.get("_confirmed_index"), int))):
                        raise ValueError("invalid active pattern checkpoint")
                    if (pattern.get("_slot") != pattern["pattern"] + ":" + pattern["direction"]
                            or stream["active"].get(pattern["_slot"]) != pattern
                            or pattern["id"] not in stream["seen"]
                            or not bars or pattern["bar_close_time"] > bars[-1]["close_time"]
                            or pattern["available_at_ms"] > available):
                        raise ValueError("inconsistent active pattern checkpoint")
                    if pattern["stage"] == "confirmed" and not pattern["_formed_index"] < pattern["_confirmed_index"] < stream["count"]:
                        raise ValueError("invalid confirmation checkpoint")
                    retest = pattern.get("_retest")
                    if retest is not None:
                        if (not isinstance(retest, dict) or not isinstance(retest.get("index"), int)
                                or not pattern["_formed_index"] < retest["index"] < stream["count"]
                                or _number(retest.get("low"), positive=True) > _number(retest.get("high"), positive=True)):
                            raise ValueError("invalid retest checkpoint")
                for pivot in stream["pivots"]:
                    if (pivot["kind"] not in {"high", "low"} or not isinstance(pivot["index"], int)
                            or not 0 <= pivot["index"] < stream["count"]):
                        raise ValueError("invalid pivot checkpoint")
                    _number(pivot["price"], positive=True)
                    _millisecond(pivot["time_ms"])
                observer._streams[key] = copy.deepcopy({**stream, "bars": normalized})
            for pattern in latest:
                observer._validate_pattern(pattern)
                observer._latest[pattern["id"]] = copy.deepcopy(pattern)
            return observer
        except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as error:
            raise ValueError("invalid pattern checkpoint") from error

    @staticmethod
    def _validate_pattern(pattern: dict[str, Any]) -> None:
        if (not isinstance(pattern, dict) or pattern.get("pattern") not in _PATTERNS
                or pattern.get("stage") not in _STAGES or pattern.get("direction") not in {"long", "short"}
                or not isinstance(pattern.get("id"), str) or pattern.get("rule_version") != RULE_VERSION
                or not isinstance(pattern.get("points"), list) or len(pattern["points"]) > 4):
            raise ValueError("invalid pattern checkpoint record")
        _identity(pattern["symbol"], pattern["interval"])
        for name in ("level", "invalid_level", "atr", "current_price"):
            _number(pattern[name], positive=True)
        for name in ("detected_at_ms", "updated_at_ms", "bar_close_time", "available_at_ms"):
            _millisecond(pattern[name])
        if pattern["bar_close_time"] > pattern["available_at_ms"] or pattern["detected_at_ms"] > pattern["updated_at_ms"]:
            raise ValueError("invalid pattern availability checkpoint")
        for point in pattern["points"]:
            _number(point["price"], positive=True)
            _millisecond(point["time_ms"])
            if point["time_ms"] > pattern["bar_close_time"]:
                raise ValueError("future pattern anchor")
