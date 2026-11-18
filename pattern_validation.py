"""Validate public pattern projections and durable notification replay payloads."""

from __future__ import annotations

import math
import re
from typing import Any

RULE_VERSION = "pattern_observer_v1"
_PATTERNS = {"breakout_retest", "double_bottom", "double_top"}
_STAGES = {"forming", "confirmed", "invalidated", "expired"}
_REASONS = {"BREAKOUT_DETECTED", "NECKLINE_FORMING", "RETEST_CONFIRMED", "NECKLINE_CONFIRMED",
            "CLOSE_BEYOND_INVALIDATION", "FORMATION_TIMED_OUT", "OBSERVATION_ENDED"}
_POINT_KINDS = {"range_boundary", "breakout", "retest", "first_bottom", "neckline", "second_bottom",
                "first_top", "second_top", "confirmation", "high", "low"}
_TIMES = {"detected_at_ms", "updated_at_ms", "bar_close_time", "available_at_ms"}
_EVENT_REQUIRED = {"id", "symbol", "interval", "pattern", "direction", "stage", "rule_version",
                   "level", "invalid_level", "origin", "source", "points", *_TIMES}
_EVENT_ALLOWED = _EVENT_REQUIRED | {"mode", "atr", "current_price", "reason", "chart", "retest_at_ms",
                                    "confirmed_at_ms", "chart_observed_at_ms", "in_scope"}
_WRAPPER_ALLOWED = {"mode", "rule_version", "engine", "latest", "events", "pending", "updated_at", "symbols",
                    "schema_version", "last_scan_at", "errors", "recoveries"}
_SAFE_INTEGER = 9007199254740991


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("InvalidPatternCheckpoint")


def _mapping(value: Any, allowed: set[str], required: set[str]) -> None:
    _require(isinstance(value, dict) and required <= value.keys() and value.keys() <= allowed)


def _number(value: Any, *, positive: bool = False) -> None:
    _require(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
             and (value > 0 if positive else value >= 0))


def _time(value: Any) -> None:
    _require(not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= _SAFE_INTEGER)


def _seconds(value: Any) -> None:
    _number(value)
    _require(value <= _SAFE_INTEGER / 1000)


def _code(value: Any, *, limit: int = 256) -> None:
    _require(isinstance(value, str) and 0 < len(value) <= limit
             and re.fullmatch(r"[A-Za-z0-9_.:-]+", value) is not None)


def _symbol(value: Any) -> None:
    _require(isinstance(value, str) and re.fullmatch(r"[A-Z0-9]{2,32}", value) is not None)


def _interval(value: Any) -> None:
    _require(value in {"15m", "1h"})


def _chart(rows: Any) -> None:
    _require(isinstance(rows, list) and len(rows) <= 80)
    previous = None
    required = {"open_time", "close_time", "open", "high", "low", "close", "volume"}
    for row in rows:
        _mapping(row, required | {"confirmed", "is_closed"}, required)
        for key in ("open_time", "close_time"):
            _time(row[key])
        for key in ("open", "high", "low", "close"):
            _number(row[key], positive=True)
        _number(row["volume"])
        _require(row["open_time"] <= row["close_time"]
                 and row["low"] <= min(row["open"], row["close"])
                 and max(row["open"], row["close"]) <= row["high"]
                 and row.get("confirmed", True) is True and row.get("is_closed", True) is True)
        if previous is not None:
            _require(previous < row["open_time"])
        previous = row["close_time"]


def _event(row: Any, *, live: bool = False) -> None:
    _mapping(row, _EVENT_ALLOWED, _EVENT_REQUIRED)
    _require(isinstance(row["id"], str) and re.fullmatch(r"[a-f0-9]{24}", row["id"]) is not None)
    _symbol(row["symbol"])
    _interval(row["interval"])
    _require(row["pattern"] in _PATTERNS and row["stage"] in _STAGES
             and row["direction"] in {"long", "short"} and row["rule_version"] == RULE_VERSION
             and row.get("mode", "observation_only") == "observation_only"
             and row["source"] == "binance_usdm" and row["origin"] in {"live", "warmup", "backfill"})
    _require(not live or row["origin"] == "live")
    _require((row["pattern"] != "double_bottom" or row["direction"] == "long")
             and (row["pattern"] != "double_top" or row["direction"] == "short"))
    for key in ("level", "invalid_level", "atr", "current_price"):
        if key in row:
            _number(row[key], positive=True)
    for key in _TIMES:
        _time(row[key])
    _require(row["bar_close_time"] <= row["available_at_ms"]
             and row["detected_at_ms"] <= row["updated_at_ms"] == row["available_at_ms"])
    for key in ("retest_at_ms", "confirmed_at_ms"):
        if key in row:
            _time(row[key])
            _require(row["detected_at_ms"] <= row[key] <= row["updated_at_ms"])
    if "chart_observed_at_ms" in row:
        _time(row["chart_observed_at_ms"])
        _require(row["chart_observed_at_ms"] >= row["updated_at_ms"])
    if "in_scope" in row:
        _require(isinstance(row["in_scope"], bool))
    if "reason" in row:
        _require(row["reason"] in _REASONS)
    _require(isinstance(row["points"], list) and 0 < len(row["points"]) <= 8)
    for point in row["points"]:
        _mapping(point, {"time_ms", "price", "kind"}, {"time_ms", "price", "kind"})
        _time(point["time_ms"])
        _number(point["price"], positive=True)
        _require(point["kind"] in _POINT_KINDS and point["time_ms"] <= row["bar_close_time"])
    if "chart" in row:
        _chart(row["chart"])
        if row["chart"] and "chart_observed_at_ms" in row:
            _require(row["chart"][-1]["close_time"] <= row["chart_observed_at_ms"])


def _batch(batch: Any) -> None:
    required = {"type", "event_key", "created_at", "raw_events", "alerts"}
    _mapping(batch, required | {"scan_id", "sealed"}, required)
    _require(batch["type"] == "observation_batch")
    _code(batch["event_key"])
    _seconds(batch["created_at"])
    if "scan_id" in batch:
        _code(batch["scan_id"], limit=128)
    if "sealed" in batch:
        _require(isinstance(batch["sealed"], bool))
    _require(isinstance(batch["raw_events"], list) and 0 < len(batch["raw_events"]) <= 256
             and isinstance(batch["alerts"], list) and 0 < len(batch["alerts"]) <= 64)
    for row in batch["raw_events"]:
        _event(row, live=True)
    for alert in batch["alerts"]:
        _mapping(alert, {"key", "title", "body", "created_at"}, {"key", "title", "body", "created_at"})
        _code(alert["key"])
        _require(isinstance(alert["title"], str) and 0 < len(alert["title"]) <= 160
                 and isinstance(alert["body"], str) and 0 < len(alert["body"]) <= 24000
                 and "\x00" not in alert["title"] and "\x00" not in alert["body"])
        _seconds(alert["created_at"])


def validate_runtime_payload(saved: Any) -> None:
    """Raise a safe ValueError before a malformed wrapper can reach public views.

    The owning runtime separately validates engine state with from_state().
    This function performs no I/O and never includes checkpoint content in errors.
    """
    try:
        required = {"mode", "rule_version", "engine", "latest", "events", "pending"}
        _mapping(saved, _WRAPPER_ALLOWED, required)
        _require(saved["mode"] == "observation_only" and saved["rule_version"] == RULE_VERSION
                 and isinstance(saved["engine"], dict)
                 and isinstance(saved["latest"], dict) and len(saved["latest"]) <= 256
                 and isinstance(saved["events"], list) and len(saved["events"]) <= 100
                 and isinstance(saved["pending"], list) and len(saved["pending"]) <= 256)
        if "schema_version" in saved:
            _require(not isinstance(saved["schema_version"], bool) and saved["schema_version"] == 1)
        for key, row in saved["latest"].items():
            _event(row)
            _require(key == row["id"])
        for row in saved["events"]:
            _event(row)
        for batch in saved["pending"]:
            _batch(batch)
        for field in ("updated_at", "last_scan_at"):
            if saved.get(field) is not None:
                _seconds(saved[field])
        if "symbols" in saved:
            _require(isinstance(saved["symbols"], list) and len(saved["symbols"]) <= 512)
            for symbol in saved["symbols"]:
                _symbol(symbol)
            _require(len(set(saved["symbols"])) == len(saved["symbols"]))
        if "errors" in saved:
            _require(isinstance(saved["errors"], list) and len(saved["errors"]) <= 64)
            for error in saved["errors"]:
                _mapping(error, {"symbol", "interval", "error_class"}, {"error_class"})
                _code(error["error_class"], limit=128)
                if "symbol" in error:
                    _symbol(error["symbol"])
                if "interval" in error:
                    _interval(error["interval"])
        if "recoveries" in saved:
            _require(isinstance(saved["recoveries"], list) and len(saved["recoveries"]) <= 32)
            fields = {"symbol", "interval", "reason", "recovered_at_ms", "archive_sha256",
                      "previous_close_time", "new_close_time"}
            for recovery in saved["recoveries"]:
                _mapping(recovery, fields, fields)
                _symbol(recovery["symbol"])
                _interval(recovery["interval"])
                _require(recovery["reason"] == "PatternHistoryRevision"
                         and isinstance(recovery["archive_sha256"], str)
                         and re.fullmatch(r"[a-f0-9]{64}", recovery["archive_sha256"]) is not None)
                for name in ("recovered_at_ms", "previous_close_time", "new_close_time"):
                    _time(recovery[name])
                _require(recovery["previous_close_time"] <= recovery["new_close_time"]
                         < recovery["recovered_at_ms"])
    except (TypeError, KeyError, AttributeError, OverflowError, RecursionError) as error:
        raise ValueError("InvalidPatternCheckpoint") from error
