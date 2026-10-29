"""Bounded, read-only public projections of the isolated Jindan observer.

This module has no market, account, settings, notification or order clients.
Never return raw snapshots or exception messages to a public endpoint.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from datetime import datetime
from pathlib import Path
from typing import Any

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
STALE_AFTER_SECONDS = 120
DATE_KEYS = ("activated_at_utc", "capture_start_utc", "warmup_start_utc", "feature_initialization_end_utc",
             "warmup_end_utc", "score_start_utc", "score_end_utc", "dataset_close_utc")
COUNT_KEYS = ("raw_messages", "closed_bars", "bbo_health_samples", "price_events", "flow_pass", "flow_fail",
              "flow_unknown", "tvp_pass", "joint_pass", "on_time_events", "late_events", "reference_captured",
              "reference_missing", "price", "flow", "unknown", "reconnect_count")
PHASES = {"WAITING_CAPTURE", "COMMISSIONING", "FEATURE_INITIALIZATION", "CAUSAL_HISTORY_WARMUP",
          "AWAITING_SCORE_PREFLIGHT", "OBSERVATION", "LABEL_MATURATION", "COMPLETED", "ENGINEERING_HALT"}
BOUNDARY_KEYS = ("orders_enabled", "final_period_access", "final_2026_read", "trading_integration",
                 "account_access", "trading_authorized")
CAPABILITY_KEYS = ("raw_closed_1m", "actual_receive_timestamps", "incremental_price_flow_events", "causal_history",
                   "journal_checkpoint_recovery", "bbo_reference_observation", "e2_labels", "origin_labels",
                   "funding_cashflow_labels", "formal_scoring", "order_execution")
READINESS_GAPS = {"E2_LABELS_NOT_IMPLEMENTED", "ORIGIN_LABELS_NOT_IMPLEMENTED", "FUNDING_LABELS_NOT_IMPLEMENTED",
                  "SCORE_PREFLIGHT_REQUIRED"}
METRICS = ("closed_trades", "active_iso_weeks", "total_net_return", "annualized_net_return", "mean_net_r",
           "minute_mdd", "net_profit_factor", "profitable_symbols", "positive_quarters")


def _number(value: Any) -> float | int | None:
    if type(value) is int:  # noqa: E721 - bool must not be accepted as a number
        return value if abs(value) <= 9007199254740991 else None
    return value if type(value) is float and math.isfinite(value) else None  # noqa: E721


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 9007199254740991 else None  # noqa: E721


def _code(value: Any, limit: int = 128) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit and re.fullmatch(r"[A-Za-z0-9_.:=-]+", value) else None


def _date(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.isoformat() if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _counts(value: Any) -> dict[str, int | None] | None:
    return {key: _count(value.get(key)) for key in COUNT_KEYS if key in value} if isinstance(value, dict) else None


def _read(root: Path, name: str, max_bytes: int) -> tuple[dict[str, Any] | None, str]:
    try:
        fd = os.open(root / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None, "unavailable"
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None, "invalid"
        def reject_constant(_value):
            raise ValueError("Nonfinite JSON")
        value = json.loads(raw, parse_constant=reject_constant)
        return (value, "available") if isinstance(value, dict) else (None, "invalid")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, PermissionError):
        return None, "unavailable"
    except (ValueError, UnicodeError, RecursionError):
        return None, "invalid"


def _empty(availability: str) -> dict[str, Any]:
    return dict(availability=availability, schema_version=1, display_name="金丹v1.0", mode="observation_only",
                updated_at=None, age_seconds=None, stale_after_seconds=STALE_AFTER_SECONDS,
                phase=None, counts=None, per_symbol=None, last_receive_ms=None, last_closed_bar_ms=None,
                errors=[], events=[], gap_count=None, revision_count=None, reconnect_count=None, boundaries=None,
                capabilities=None, readiness_gaps=[], unresolved_gap_count=None, storage=None, clock=None, reference_clock_usable=None,
                **dict.fromkeys(DATE_KEYS))


def _events(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for row in value[-50:]:
        if not isinstance(row, dict) or row.get("symbol") not in SYMBOLS:
            continue
        out = {"event_id": _code(row.get("event_id")), "symbol": row["symbol"]}
        for key in ("flow_status", "tvp_status", "joint_status", "on_time_status", "reference_status"):
            out[key] = _code(row.get(key))
        for key in ("measurement_minutes", "side", "initial_direction", "start_time_ms", "confirmed_time_ms",
                    "available_time_ms", "decision_committed_at_ms"):
            out[key] = _number(row.get(key))
        result.append(out)
    return result


def read_status(root: Path | str, *, now: float | None = None) -> dict[str, Any]:
    raw, availability = _read(Path(root), "status.json", 512 * 1024)
    out = _empty(availability)
    if raw is None:
        return out
    clock = time.time() if now is None else now
    updated = _number(raw.get("updated_at"))
    if (type(raw.get("schema_version")) is not int or raw["schema_version"] != 1  # noqa: E721
            or raw.get("mode") != "observation_only" or updated is None or updated < 0 or updated > clock + 60):
        return _empty("invalid")
    age = max(0, clock - updated)
    out.update(availability="stale" if age > STALE_AFTER_SECONDS else "available", updated_at=updated,
               age_seconds=round(age, 1), counts=_counts(raw.get("counts")), events=_events(raw.get("events")))
    for key in ("phase", "calendar_phase"):
        out[key] = raw.get(key) if raw.get(key) in PHASES else None
    out["operational_status"] = raw.get("operational_status") if raw.get("operational_status") in {"WAITING", "RUNNING", "DEGRADED", "HALTED", "STOPPED"} else None
    for key in DATE_KEYS:
        out[key] = _date(raw.get(key))
    for key in ("last_receive_ms", "last_closed_bar_ms", "gap_count", "unresolved_gap_count", "revision_count", "reconnect_count"):
        out[key] = _number(raw.get(key)) if key.endswith("_ms") else _count(raw.get(key))
    if "reconnect_count" not in raw and isinstance(raw.get("counts"), dict):
        out["reconnect_count"] = _count(raw["counts"].get("reconnect_count"))
    storage = raw.get("storage")
    if isinstance(storage, dict):
        out["storage"] = {key: _count(storage.get(key)) for key in ("used_bytes", "limit_bytes", "free_bytes", "min_free_bytes")}
        out["storage"]["ok"] = storage.get("ok") if type(storage.get("ok")) is bool else None  # noqa: E721
    clock_record = raw.get("clock")
    if isinstance(clock_record, dict):
        out["clock"] = {key: _number(clock_record.get(key)) for key in ("local_receive_at_ms", "utc_offset_estimate_ms", "offset_uncertainty_ms", "estimated_utc_ms")}
        uncertainty = out["clock"]["offset_uncertainty_ms"]
        if uncertainty is not None and out["clock"]["utc_offset_estimate_ms"] is not None:
            out["reference_clock_usable"] = 0 <= uncertainty <= 100
    symbols = raw.get("per_symbol")
    if isinstance(symbols, dict):
        out["per_symbol"] = {}
        for symbol in SYMBOLS:
            row = symbols.get(symbol)
            if isinstance(row, dict):
                out["per_symbol"][symbol] = {key: _number(row.get(key)) for key in ("last_receive_ms", "last_closed_bar_ms", "processed_bar_ms")}
                out["per_symbol"][symbol].update({key: _count(row.get(key)) for key in ("closed_bars", "gap_count", "revision_count", "reconnect_count")})
    capabilities = raw.get("capabilities")
    if isinstance(capabilities, dict):
        out["capabilities"] = {key: capabilities.get(key) if type(capabilities.get(key)) is bool else None for key in CAPABILITY_KEYS}  # noqa: E721
    gaps = raw.get("readiness_gaps")
    if isinstance(gaps, list):
        out["readiness_gaps"] = [code for code in gaps[:20] if isinstance(code, str) and code in READINESS_GAPS]
    boundaries = raw.get("boundaries")
    if isinstance(boundaries, dict):
        out["boundaries"] = {key: boundaries.get(key) if type(boundaries.get(key)) is bool else None for key in BOUNDARY_KEYS}  # noqa: E721
    errors = raw.get("errors")
    if isinstance(errors, list):
        for row in errors[-20:]:
            if isinstance(row, dict):
                out["errors"].append(dict(code=_code(row.get("code"), 80) or "COLLECTOR_ERROR",
                                          at_ms=_number(row.get("at_ms")), count=_count(row.get("count"))))
            else:
                out["errors"].append(dict(code="COLLECTOR_ERROR", at_ms=None, count=None))
    return out


def read_research(root: Path | str) -> dict[str, Any]:
    raw, availability = _read(Path(root), "research_public.json", 2 * 1024 * 1024)
    out: dict[str, Any] = dict(availability=availability, scope="EXPOSED_2024_2025_DEVELOPMENT",
                               status=None, main_metrics=[], strategy_validated=False,
                               eligible_for_original_acceptance=False, final_2026_read=False, orders_enabled=False)
    if raw is None:
        return out
    out["status"] = _code(raw.get("status"))
    out["completed_at_utc"] = _date(raw.get("completed_at_utc"))
    reproduction = raw.get("reproduction")
    out["reproduction"] = ({"status": _code(reproduction.get("status")),
                            "calendar_days": _count(reproduction.get("calendar_days")),
                            "cost_replays": _count(reproduction.get("cost_replays"))} if isinstance(reproduction, dict) else None)
    for key in ("main_policy_count", "control_policy_count", "cost_replays", "calendar_days"):
        out[key] = _count(raw.get(key))
    bounds = raw.get("bounds")
    if isinstance(bounds, dict):
        out["bounds"] = {key: _date(bounds.get(key)) for key in ("start", "end", "start_utc", "end_exclusive_utc")}
        out["bounds"]["calendar_days"] = _count(bounds.get("calendar_days"))
    rows = raw.get("main_metrics")
    if isinstance(rows, list):
        for row in rows[:64]:
            if not isinstance(row, dict):
                continue
            projected = {key: _code(row.get(key)) for key in ("policy_id", "variant", "arm")}
            projected.update({key: _number(row.get(key)) for key in ("flow_threshold", "hold_minutes")})
            for cost in ("BASE", "DOUBLE"):
                values = row.get(cost)
                projected[cost] = {key: _number(values.get(key)) for key in METRICS} if isinstance(values, dict) else None
            out["main_metrics"].append(projected)
    return out


def register_jindan_routes(app) -> None:
    from fastapi.responses import JSONResponse

    def state_dir():
        return Path(os.environ.get("JINDAN_STATE_DIR") or Path(__file__).parent / "runtime" / "jindan-v1")

    @app.get("/api/jindan/status")
    def jindan_status():
        return JSONResponse(read_status(state_dir()), headers={"Cache-Control": "no-store"})

    @app.get("/api/jindan/events")
    def jindan_events():
        status = read_status(state_dir())
        return JSONResponse({key: status[key] for key in ("availability", "updated_at", "age_seconds", "events")},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/jindan/research")
    def jindan_research():
        return JSONResponse(read_research(state_dir()), headers={"Cache-Control": "no-store"})
