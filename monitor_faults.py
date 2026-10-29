"""Persist safe fault evidence while gating noisy paper-market alerts.

This tracks observations only. It never changes quote eligibility, trading state,
notification delivery, or the caller's immediate diagnostic errors.
"""

from __future__ import annotations

import builtins
import copy
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path

_MODULES = frozenset({
    "paper_data", "strategy", "indicators", "support", "paper_pool", "paper_universe",
    "ledger", "events", "market", "market_data", "summary", "site_monitor", "fault_history",
})
_KINDS = frozenset({
    "stale_trade", "quote_request_failed", "invalid_quote", "market_unavailable",
    "quote_unavailable", "missing_quote", "stale_quote", "future_quote",
    "kline_unavailable", "kline_conflict", "invalid_data", "clock_rollback", "request_failed",
})
_EXCEPTIONS = {
    name for name, value in vars(builtins).items()
    if isinstance(value, type) and issubclass(value, BaseException)
} | {
    "UnknownError", "URLError", "HTTPError", "IncompleteRead", "RemoteDisconnected",
    "JSONDecodeError", "ClientError", "ClientResponseError", "ClientConnectionError",
    "ClientConnectorError", "ClientConnectorDNSError", "ClientConnectorCertificateError",
    "ClientConnectorSSLError", "ClientOSError", "ClientPayloadError", "ContentTypeError",
    "ServerDisconnectedError", "ServerTimeoutError", "ConnectionTimeoutError", "SocketTimeoutError",
}
_REFRESH_ENUMS = frozenset({"invalid", "missing", "unknown", "unavailable", "not_attempted", "not_requested", "ok"})
_INTERVALS = frozenset({"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"})
_EVENTS = frozenset({"fault_started", "fault_updated", "alert_activated", "fault_recovered", "alert_cleared"})
_ERROR_OPERATIONS = frozenset({"journal_append", "state_write", "journal_read", "journal_line"})
_STATE_KEYS = frozenset({
    "active", "armed", "consecutive_failures", "first_failure_at", "last_failure_at",
    "last_observed_at", "recovery_since", "details", "last_details", "seen_failure_ids",
    "last_observation_id", "clock_rollback",
})
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and abs(value) <= 2 ** 53 and math.isfinite(value))


def _module(value):
    if not isinstance(value, str) or value not in _MODULES:
        raise ValueError("Unknown fault module")
    return value


def _error_name(value, *, refresh=False):
    if value is None:
        return None
    allowed = _EXCEPTIONS | _REFRESH_ENUMS if refresh else _EXCEPTIONS
    return value if isinstance(value, str) and value in allowed else "UnknownError"


def _safe_details(details):
    if not isinstance(details, list):
        raise ValueError("Fault details must be a structured list")
    result = {}
    for item in details[:200]:
        if not isinstance(item, dict):
            item = {}
        symbol = item.get("symbol")
        symbol = (symbol[:48].upper() if isinstance(symbol, str)
                  and re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff]{1,200}", symbol) else "UNKNOWN")
        interval = item.get("interval")
        interval = interval if isinstance(interval, str) and interval in _INTERVALS else "unknown"
        kind = item.get("kind")
        kind = kind if isinstance(kind, str) and kind in _KINDS else "invalid_data"
        age = item.get("source_age_ms")
        safe = {
            "symbol": symbol, "interval": interval, "kind": kind,
            "source_age_ms": int(age) if _number(age) else None,
            "refresh_error": _error_name(item.get("refresh_error"), refresh=True),
            "error_type": _error_name(item.get("error_type")),
        }
        result[json.dumps(safe, sort_keys=True, ensure_ascii=False)] = safe
    return [result[key] for key in sorted(result)]


def _new_state():
    return {
        "active": False, "armed": False, "consecutive_failures": 0,
        "first_failure_at": None, "last_failure_at": None, "last_observed_at": None,
        "recovery_since": None, "details": [], "last_details": [],
        "seen_failure_ids": [], "last_observation_id": None, "clock_rollback": False,
    }


def _safe_event(record):
    """Validate the event envelope, then rebuild only its safe fields."""
    if (not isinstance(record, dict) or record.get("event") not in _EVENTS
            or not _number(record.get("at"))):
        raise ValueError("Invalid fault event")
    module = _module(record.get("module"))
    count = record.get("consecutive_failures")
    duration = record.get("duration_seconds")
    if (not isinstance(count, int) or isinstance(count, bool) or count < 0
            or not _number(duration) or duration < 0
            or not isinstance(record.get("alert_active"), bool)):
        raise ValueError("Invalid fault event")
    event = {
        "event": record["event"], "module": module, "at": record["at"],
        "details": _safe_details(record.get("details")),
        "duration_seconds": duration, "consecutive_failures": count,
        "alert_active": record["alert_active"],
    }
    for field in ("first_failure_at", "last_failure_at"):
        value = record.get(field)
        if value is not None and not _number(value):
            raise ValueError("Invalid fault event timestamp")
        event[field] = value
    event_id = record.get("event_id")
    if isinstance(event_id, str) and re.fullmatch(r"[0-9a-f]{32}", event_id):
        event["event_id"] = event_id
    return event


class FaultTracker:
    def __init__(self, path):
        self.path = Path(path)
        self.journal_path = self.path.with_suffix(".jsonl")
        if self.journal_path == self.path:
            self.journal_path = self.path.with_name(self.path.name + ".jsonl")
        self._lock = threading.RLock()
        self._modules = {}
        self._pending_events = []
        self._errors = {}
        if self.path.exists():
            try:
                self._load(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError):
                # Existing corrupt state must never silently become an empty ledger.
                raise ValueError("Fault tracker state is unreadable or invalid") from None

    def _load(self, document):
        if (not isinstance(document, dict) or document.get("version") != 1
                or not isinstance(document.get("modules"), dict)
                or not isinstance(document.get("pending_events"), list)
                or not isinstance(document.get("errors"), dict)):
            raise ValueError("Invalid fault tracker state")
        modules = {}
        for module, value in document["modules"].items():
            _module(module)
            if not isinstance(value, dict) or set(value) != _STATE_KEYS:
                raise ValueError("Invalid fault module state")
            state = copy.deepcopy(value)
            if any(not isinstance(state[field], bool) for field in ("active", "armed", "clock_rollback")):
                raise ValueError("Invalid fault flags")
            count = state["consecutive_failures"]
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("Invalid observation count")
            for field in ("first_failure_at", "last_failure_at", "last_observed_at", "recovery_since"):
                if state[field] is not None and not _number(state[field]):
                    raise ValueError("Invalid fault timestamps")
            state["details"] = _safe_details(state["details"])
            state["last_details"] = _safe_details(state["last_details"])
            if (state["active"] != bool(state["details"])
                    or state["active"] and (count < 1 or state["first_failure_at"] is None
                                            or state["last_failure_at"] is None)
                    or not isinstance(state["seen_failure_ids"], list)
                    or any(not isinstance(key, str) or not _HASH.fullmatch(key)
                           for key in state["seen_failure_ids"])
                    or state["last_observation_id"] is not None
                    and (not isinstance(state["last_observation_id"], str)
                         or not _HASH.fullmatch(state["last_observation_id"]))):
                raise ValueError("Inconsistent fault state")
            state["seen_failure_ids"] = state["seen_failure_ids"][-1024:]
            modules[module] = state
        errors = document["errors"]
        if any(key not in _ERROR_OPERATIONS or value not in _EXCEPTIONS for key, value in errors.items()):
            raise ValueError("Invalid persistence errors")
        self._modules = modules
        self._pending_events = [_safe_event(event) for event in document["pending_events"]]
        self._errors = dict(errors)

    def _journal_error(self):
        return "; ".join(f"{operation}_failed:{name}" for operation, name in sorted(self._errors.items())) or None

    def _public(self, module, state):
        result = {key: copy.deepcopy(value) for key, value in state.items()
                  if key not in {"seen_failure_ids", "last_observation_id"}}
        rounds, duration = (3, 60) if module == "paper_data" else (1, 0)
        observed = state["last_observed_at"] if state["active"] else state["last_failure_at"]
        elapsed = (max(0, observed - state["first_failure_at"])
                   if observed is not None and state["first_failure_at"] is not None else 0)
        result.update(
            alert_active=state["active"] and state["armed"],
            threshold_rounds=rounds, threshold_duration_seconds=duration,
            duration_seconds=elapsed, journal_error=self._journal_error(),
        )
        return result

    def _event(self, event, module, state, now, *, details=None):
        start = state["first_failure_at"]
        self._pending_events.append({
            "event": event, "module": module, "at": now, "event_id": uuid.uuid4().hex,
            "details": copy.deepcopy(state["details"] if details is None else details),
            "duration_seconds": max(0, now - start) if start is not None else 0,
            "consecutive_failures": state["consecutive_failures"],
            "alert_active": state["active"] and state["armed"],
            "first_failure_at": start, "last_failure_at": state["last_failure_at"],
        })

    def _persist(self):
        if self._pending_events:
            try:
                self.journal_path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(self.journal_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    for event in self._pending_events:
                        stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                self._pending_events.clear()
                self._errors.pop("journal_append", None)
            except OSError as error:
                self._errors["journal_append"] = _error_name(type(error).__name__)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._errors.pop("state_write", None)
            document = {"version": 1, "modules": self._modules,
                        "pending_events": self._pending_events, "errors": self._errors}
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(document, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            self._errors["state_write"] = _error_name(type(error).__name__)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def observe(self, module, details, now=None, observation_id=None):
        module = _module(module)
        details = _safe_details(details)
        now = time.time() if now is None else now
        if not _number(now):
            raise ValueError("Invalid observation time")
        identity = (hashlib.sha256(str(observation_id).encode()).hexdigest()
                    if observation_id is not None else None)
        with self._lock:
            state = self._modules.setdefault(module, _new_state())
            if identity is not None and (identity == state["last_observation_id"]
                                         or details and identity in state["seen_failure_ids"]):
                return self._public(module, state)
            previous_time = state["last_observed_at"]
            rollback = previous_time is not None and now < previous_time
            observed_at = max(now, previous_time) if previous_time is not None else now
            state["clock_rollback"] = rollback
            state["last_observed_at"] = observed_at
            state["last_observation_id"] = identity
            if details:
                started = not state["active"]
                changed = details != state["details"]
                if started:
                    state["first_failure_at"] = observed_at
                    state["consecutive_failures"] = 0
                    state["seen_failure_ids"] = []
                state["consecutive_failures"] += 1
                if identity is not None:
                    state["seen_failure_ids"].append(identity)
                    # Bound diagnostic state during long incidents. Explicit
                    # observation IDs are deduplicated over this recent window.
                    state["seen_failure_ids"] = state["seen_failure_ids"][-1024:]
                state.update(active=True, details=details, last_details=copy.deepcopy(details),
                             last_failure_at=observed_at, recovery_since=None)
                if started or changed:
                    self._event("fault_started" if started else "fault_updated", module, state, observed_at)
                rounds, duration = (3, 60) if module == "paper_data" else (1, 0)
                elapsed = max(0, now - state["first_failure_at"])
                if (not state["armed"] and state["consecutive_failures"] >= rounds
                        and (module != "paper_data" or not rollback and elapsed >= duration)):
                    state["armed"] = True
                    self._event("alert_activated", module, state, observed_at)
            else:
                was_active = state["active"]
                state.update(active=False, details=[])
                if was_active:
                    state["recovery_since"] = observed_at
                    self._event("fault_recovered", module, state, observed_at, details=state["last_details"])
                elif (state["recovery_since"] is not None
                      and (rollback or previous_time is None or now - previous_time > 30)):
                    # A restart or stalled monitor is not evidence of healthy
                    # observations. Require a fresh sequence of normal samples.
                    state["recovery_since"] = observed_at
                state["consecutive_failures"] = 0
                state["seen_failure_ids"] = []
                if (state["armed"] and state["recovery_since"] is not None and not rollback
                        and now - state["recovery_since"] >= 60):
                    state["armed"] = False
                    self._event("alert_cleared", module, state, observed_at, details=state["last_details"])
            self._persist()
            return self._public(module, state)

    def summary(self):
        with self._lock:
            return {"modules": {module: self._public(module, state)
                                for module, state in self._modules.items()},
                    "journal_error": self._journal_error()}

    def events(self, limit=50):
        """Read only the last 128 KiB; never expose malformed or arbitrary data."""
        limit = max(0, min(200, int(limit)))
        with self._lock:
            if not limit:
                return []
            try:
                with self.journal_path.open("rb") as stream:
                    stream.seek(0, os.SEEK_END)
                    offset = max(0, stream.tell() - 128 * 1024)
                    stream.seek(offset)
                    if offset:
                        stream.readline()  # Discard a possibly partial first line.
                    lines = stream.read(128 * 1024).splitlines()
                events = []
                bad_line = False
                for line in lines:
                    try:
                        events.append(_safe_event(json.loads(line)))
                    except (ValueError, TypeError, KeyError, UnicodeError):
                        bad_line = True
                if bad_line:
                    self._errors["journal_line"] = "ValueError"
                else:
                    self._errors.pop("journal_line", None)
                self._errors.pop("journal_read", None)
                return copy.deepcopy(events[-limit:])
            except FileNotFoundError:
                self._errors.pop("journal_read", None)
                return []
            except OSError as error:
                self._errors["journal_read"] = _error_name(type(error).__name__)
                return []
