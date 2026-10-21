"""Version-bound, atomic checkpoints for the existing signal state machine."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from strategy import ProjectSignalEngine, StrategyConfig


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _timestamp(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid checkpoint timestamp")
    return value


def _identity(value: Any) -> tuple[str, int]:
    if not isinstance(value, list) or len(value) != 2 or value[0] not in {"long", "short"}:
        raise ValueError("invalid checkpoint candidate identity")
    return value[0], _timestamp(value[1])


class PersistentSignalEngine(ProjectSignalEngine):
    """Persist pending candidates and consumed decisions before returning output.

    A failed checkpoint never yields a fresh signal, and its in-memory mutation
    is rolled back. A repeated closed bar returns its already recorded decision;
    late observations do not rewrite the first decision for that bar.
    """
    def __init__(self, config: StrategyConfig, directory: Path, identity: tuple[str, ...]):
        super().__init__(config)
        self.identity = identity
        self.path = Path(directory) / (_digest(list(identity)) + ".json")
        self._lock = threading.RLock()
        self._last_close: int | None = None
        self._last_response: dict[str, Any] | None = None
        self._last_input_sha256: str | None = None
        if self.path.exists():
            try:
                document = json.loads(self.path.read_text(), parse_constant=lambda value: _invalid_constant(value))
                payload = document["payload"]
                if document["sha256"] != _digest(payload):
                    raise ValueError("checkpoint digest mismatch")
                if (payload["schema_version"] != 1 or payload["identity"] != list(identity)
                        or payload["strategy_config"] != asdict(config)):
                    raise ValueError("checkpoint version mismatch")
                self._restore(payload["state"])
            except (OSError, KeyError, TypeError, ValueError) as error:
                raise ValueError("signal checkpoint unavailable or corrupt; restore valid state") from error

    def _snapshot(self) -> dict[str, Any]:
        return dict(pending_buy=copy.deepcopy(self.pending_buy), pending_sell=copy.deepcopy(self.pending_sell),
            used_volume_sequence_candidates=[list(key) for key in sorted(self.used_volume_sequence_candidates)],
            used_strict_candidates=[list(key) for key in sorted(self.used_strict_candidates)],
            strict_candidate_ready_times=[[list(key), value] for key, value in sorted(self.strict_candidate_ready_times.items())],
            last_close=self._last_close, last_response=copy.deepcopy(self._last_response),
            last_input_sha256=self._last_input_sha256)

    def _restore(self, state: dict[str, Any]) -> None:
        expected = {"pending_buy", "pending_sell", "used_volume_sequence_candidates", "used_strict_candidates",
                    "strict_candidate_ready_times", "last_close", "last_response", "last_input_sha256"}
        if not isinstance(state, dict) or set(state) != expected:
            raise ValueError("invalid checkpoint state fields")
        _digest(state)  # Reject non-finite values before assigning any field.
        for field in ("pending_buy", "pending_sell"):
            candidate = state[field]
            if candidate is not None:
                if not isinstance(candidate, dict) or not {"divergence_time", "strength"} <= candidate.keys():
                    raise ValueError("invalid checkpoint pending candidate")
                _timestamp(candidate["divergence_time"])
        used_volume = {_identity(value) for value in state["used_volume_sequence_candidates"]}
        used_strict = {_identity(value) for value in state["used_strict_candidates"]}
        ready_times = {_identity(key): _timestamp(value) for key, value in state["strict_candidate_ready_times"]}
        close = state["last_close"]
        response = state["last_response"]
        input_hash = state["last_input_sha256"]
        if close is not None:
            _timestamp(close)
            if not isinstance(response, dict) or not isinstance(input_hash, str) or len(input_hash) != 64:
                raise ValueError("invalid checkpoint recorded decision")
        elif response is not None or input_hash is not None:
            raise ValueError("invalid empty checkpoint decision")
        self.pending_buy, self.pending_sell = copy.deepcopy(state["pending_buy"]), copy.deepcopy(state["pending_sell"])
        self.used_volume_sequence_candidates, self.used_strict_candidates = used_volume, used_strict
        self.strict_candidate_ready_times = ready_times
        self._last_close, self._last_response, self._last_input_sha256 = close, copy.deepcopy(response), input_hash

    def _save(self) -> None:
        payload = dict(schema_version=1, identity=list(self.identity), strategy_config=asdict(self.config),
                       state=self._snapshot())
        data = json.dumps(dict(payload=payload, sha256=_digest(payload)), allow_nan=False, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _save_decision_input(self, before: dict[str, Any], ready_bars: list[dict[str, Any]],
                             response: dict[str, Any]) -> tuple[Path, bool]:
        """Keep original input only for actionable decisions, never every wait."""
        path = self.path.parent / "decisions" / f"{self.path.stem}-{self._last_close}.json"
        payload = dict(schema_version=1, identity=list(self.identity), strategy_config=asdict(self.config),
            before_state=before, ready_bars=ready_bars, response=response,
            clock_scope="local_observation_seconds_not_source_publication_time")
        text = json.dumps(dict(payload=payload, sha256=_digest(payload)), sort_keys=True, allow_nan=False)
        if path.exists():
            if path.read_text() != text:
                raise ValueError("recorded decision input differs; preserve original artifact")
            return path, False
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return path, True

    def _read_pending_decision(
        self, before: dict[str, Any], ready_bars: list[dict[str, Any]], close: int,
    ) -> tuple[Path, dict[str, Any]] | None:
        """Validate an input commit left behind before its checkpoint commit.

        The original observation clock belongs to the immutable input commit.
        Its complete response is still checked against a real state-machine
        replay before any recovered checkpoint can be written.
        """
        path = self.path.parent / "decisions" / f"{self.path.stem}-{close}.json"
        if not path.exists():
            return None
        try:
            document = json.loads(path.read_text(), parse_constant=_invalid_constant)
            payload = document["payload"]
            if document["sha256"] != _digest(payload):
                raise ValueError("decision input digest mismatch")
            expected = {"schema_version", "identity", "strategy_config", "before_state", "ready_bars",
                        "response", "clock_scope"}
            if (set(payload) != expected or not isinstance(payload["schema_version"], int)
                    or isinstance(payload["schema_version"], bool)
                    or payload["schema_version"] != 1 or payload["identity"] != list(self.identity)
                    or _digest(payload["strategy_config"]) != _digest(asdict(self.config))
                    or _digest(payload["before_state"]) != _digest(before)
                    or _digest(payload["ready_bars"]) != _digest(ready_bars)
                    or payload["clock_scope"] != "local_observation_seconds_not_source_publication_time"):
                raise ValueError("decision input context mismatch")
            response = payload["response"]
            observed = response["observed_at"]
            if (not isinstance(observed, (int, float)) or isinstance(observed, bool)
                    or not math.isfinite(observed) or observed < 0
                    or _digest(response["created_at"]) != _digest(observed)
                    or response["observed_at_unit"] != "unix_seconds"
                    or _timestamp(response["decision_close_time"]) != close
                    or response["input_sha256"] != _digest(ready_bars)
                    or response["signal"] not in {"long", "short"}):
                raise ValueError("decision input response metadata mismatch")
            return path, copy.deepcopy(response)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError("recorded decision input invalid or conflicting; preserve original artifact") from error

    def detect(self, ready_bars: list[dict[str, Any]], clean: bool = True) -> dict[str, Any]:
        with self._lock:
            if not ready_bars:
                return super().detect(ready_bars, clean=clean)
            close = _timestamp(ready_bars[-1].get("close_time"))
            if self._last_close is not None and close < self._last_close:
                raise ValueError("signal input moved backwards behind its checkpoint")
            if close == self._last_close:
                return copy.deepcopy(self._last_response)
            before = self._snapshot()
            created_artifact: Path | None = None
            try:
                pending = self._read_pending_decision(before, ready_bars, close)
                response = super().detect(ready_bars, clean=clean)
                response["observed_at"] = pending[1]["observed_at"] if pending else time.time()
                response["observed_at_unit"] = "unix_seconds"
                response["created_at"] = response["observed_at"]
                response["decision_close_time"] = close
                self._last_input_sha256 = _digest(ready_bars)
                response["input_sha256"] = self._last_input_sha256
                if pending is not None and _digest(response) != _digest(pending[1]):
                    raise ValueError("recorded decision differs from replay; preserve original artifact")
                self._last_close, self._last_response = close, copy.deepcopy(response)
                if response.get("signal") in {"long", "short"}:
                    if pending is None:
                        path, created = self._save_decision_input(before, ready_bars, response)
                        if created:
                            created_artifact = path
                    else:
                        path = pending[0]
                    response["input_snapshot_ref"] = str(path.relative_to(self.path.parent))
                    self._last_response = copy.deepcopy(response)
                self._save()
                return response
            except (OSError, TypeError, ValueError):
                self._restore(before)
                if created_artifact is not None:
                    created_artifact.unlink(missing_ok=True)
                raise


def _invalid_constant(value: str):
    raise ValueError(f"non-finite checkpoint value: {value}")
