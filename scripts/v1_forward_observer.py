"""Local, version-frozen prospective signal journal; no orders or scheduler.

The caller supplies prepared closed 15m bars and optional source envelopes.
``observe`` stamps local time itself. Historical inputs only warm the engine;
they are never reconstructed into prospective decisions. A journal records
observations and intended future execution times, not fills, PnL or promotion.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from signal_engine_state import PersistentSignalEngine  # noqa: E402
from strategy import DEFAULT_CONFIG, StrategyConfig  # noqa: E402

STEP = 900_000
SOURCES = ("strategy.py", "signal_engine_state.py", "indicators.py", "config.py", "scripts/v1_forward_observer.py")
MICRO = ("funding_rate", "lastFundingRate", "open_interest_ratio", "oi_ratio")
REQUIRED = (
    "macd",
    "macd_signal",
    "macd_fast",
    "atr",
    "rsi",
    "ema20",
    "ema60",
    "ema200",
    "volume_ratio",
    "obv_slope",
    "chop",
    "bb_width",
    "higher_close",
    "higher_ema20",
    "higher_ema60",
    "higher_ema200",
    "higher_macd",
)


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _envelope(payload):
    return dict(payload=payload, sha256=_sha(_bytes(payload)))


def _read(path):
    try:
        document = json.loads(path.read_bytes())
        if set(document) != {"payload", "sha256"} or document["sha256"] != _sha(_bytes(document["payload"])):
            raise ValueError("digest mismatch")
        return document["payload"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"corrupt or unavailable observer artifact: {path}") from error


def _immutable(path, data):
    """Atomically publish complete bytes without ever replacing prior history."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError(f"artifact drift: {path}")
        return
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != data:
                raise ValueError(f"artifact drift: {path}") from None
    finally:
        temporary.unlink(missing_ok=True)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _clock(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("observer clocks must be nonnegative epoch milliseconds")
    return value


def attach_microstructure(rows, sources, observed_at_ms):
    """Attach first-observed snapshots to the latest bar, never older bars.

    Source event time, actual publication/availability (when supplied), and
    local observation time remain distinct. Unknown publication is kept null.
    Optional invalid or future snapshots remain missing, not invented zeros.
    """
    now = _clock(observed_at_ms)
    enriched = copy.deepcopy(rows)
    for row in enriched:
        for key in MICRO:
            row.pop(key, None)
    audit = {}
    for name in ("premium", "oi"):
        source = sources.get(name) or {}
        local = source.get("observed_at_ms")
        available = source.get("available_at_ms")
        item = dict(
            status="MISSING_OR_INVALID",
            source_event_time_ms=None,
            source_available_at=available,
            local_first_observed_at_ms=local,
            clock_scope="local_observation_is_not_source_publication",
        )
        audit[name] = item
        target = "funding_rate" if name == "premium" else "open_interest_ratio"
        if enriched:
            enriched[-1][target] = None
        try:
            _clock(local)
            if local > now or (available is not None and (_clock(available) > local)):
                continue
            payload = source["payload"]
            if name == "premium":
                event = _clock(payload["time"])
                value = _number(payload.get("lastFundingRate"))
            else:
                if not isinstance(payload, list) or len(payload) < 2:
                    continue
                previous, current = payload[-2:]
                prior_time, event = _clock(previous["timestamp"]), _clock(current["timestamp"])
                left, right = _number(previous.get("sumOpenInterest")), _number(current.get("sumOpenInterest"))
                if prior_time >= event or left is None or right is None or left <= 0 or right < 0:
                    continue
                value = right / left
            if value is None or event > local:
                continue
            if enriched:
                enriched[-1][target] = value
            item.update(status="OBSERVED", source_event_time_ms=event)
        except (KeyError, TypeError, ValueError):
            continue
    return enriched, audit


def _validate(rows, now):
    if not isinstance(rows, list) or len(rows) < 250:
        raise ValueError("at least 250 complete prepared bars are required")
    previous = None
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("bars must be objects")
        start, close = _clock(row.get("open_time")), _clock(row.get("close_time"))
        if (
            start % STEP
            or close != start + STEP - 1
            or close >= now
            or row.get("confirmed") is not True
            or row.get("is_closed") is not True
        ):
            raise ValueError("bar is not a finalized historical 15m interval")
        if previous is not None and start != previous + 1:
            raise ValueError("missing, duplicate or unordered input bars")
        values = [_number(row.get(key)) for key in ("open", "high", "low", "close", "volume")]
        if any(value is None for value in values) or min(values[:4]) <= 0 or values[4] < 0:
            raise ValueError("invalid OHLCV")
        opening, high, low, ending, _volume = values
        if not low <= min(opening, ending) <= max(opening, ending) <= high:
            raise ValueError("invalid OHLC range")
        higher_close = row.get("higher_close_time")
        if isinstance(higher_close, bool) or not isinstance(higher_close, int) or higher_close > close:
            raise ValueError("higher-timeframe input is not closed at decision bar")
        for key in ("higher_available_at_ms", "higher_observed_at_ms"):
            if row.get(key) is not None and _clock(row[key]) > now:
                raise ValueError("higher-timeframe feature not yet available at local decision")
        previous = close
    for row in rows[-250:]:
        if any(_number(row.get(key)) is None for key in REQUIRED):
            raise ValueError("required prepared feature missing or nonfinite")
    _bytes(rows)


def _guard_first_rows(rows, history, *, extend=False):
    """Compare every received raw row with its first committed observation."""
    for row in rows:
        close, digest = row["close_time"], _sha(_bytes(row))
        if close in history and history[close] != digest:
            raise ValueError(f"historical input revision at {close}; preserve first observation")
        if extend:
            history[close] = digest


def _detect(engine, rows, observed, archive_ref, manifest_sha256):
    # PersistentSignalEngine's wall clock is normalized to this observation's
    # recorded clock, so exact same-data replay does not read a later clock.
    response = engine.detect(rows, clean=False)
    response.update(observed_at=observed / 1000, created_at=observed / 1000)
    if response.get("signal") in {"long", "short"}:
        # The engine's own actionable input file lives in temporary work.
        # Rebind public output to the permanent atomic observation commit.
        response.update(
            input_snapshot_ref=archive_ref,
            input_snapshot_payload_path="/after_state/rows",
            input_snapshot_manifest_sha256=manifest_sha256,
        )
    engine._last_response = copy.deepcopy(response)
    return response


class ForwardObserver:
    """One writer, immutable observation commits and recoverable derived views."""

    def __init__(self, directory, universe=None, frozen_at_ms=None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._locked():
            path = self.directory / "manifest.json"
            if path.exists():
                self.manifest = _read(path)
                if universe is not None and universe != self.manifest["universe"]:
                    raise ValueError("frozen universe differs")
                if frozen_at_ms is not None and frozen_at_ms != self.manifest["frozen_at_ms"]:
                    raise ValueError("frozen observation boundary differs")
            else:
                if list(self.directory.glob("observations/*/*.json")):
                    raise ValueError("manifest missing; do not reconstruct a new research version")
                if not isinstance(universe, dict) or universe.get("ready") is not True:
                    raise ValueError("ready immutable universe snapshot required")
                symbols = [item.get("symbol") for item in universe.get("members", [])]
                if (
                    not symbols
                    or len(set(symbols)) != len(symbols)
                    or any(not isinstance(s, str) or not re.fullmatch(r"\w+", s) or s != s.upper() for s in symbols)
                ):
                    raise ValueError("universe must contain unique safe symbol identifiers")
                frozen = _clock(frozen_at_ms if frozen_at_ms is not None else time.time_ns() // 1_000_000)
                configs = dict(
                    baseline=asdict(DEFAULT_CONFIG), V1=asdict(replace(DEFAULT_CONFIG, volume_sequence_enabled=True))
                )
                if configs["baseline"]["volume_sequence_enabled"]:
                    raise ValueError("baseline must keep the candidate disabled")
                bindings = {}
                for source in SOURCES:
                    data = (ROOT / source).read_bytes()
                    _immutable(self.directory / "code" / source, data)
                    bindings[source] = _sha(data)
                # Only strategy settings, never the repository .env or full
                # configuration (which may contain transport secrets).
                sanitized = _bytes({"strategy": configs["baseline"]})
                _immutable(self.directory / "code" / "config.yaml", sanitized)
                bindings["config.yaml"] = _sha(sanitized)
                self.manifest = dict(
                    schema_version=1,
                    mode="RESEARCH_ONLY_SIGNAL_OBSERVER",
                    frozen_at_ms=frozen,
                    freeze_clock_authority="SYSTEM" if frozen_at_ms is None else "CALLER_SUPPLIED",
                    universe=copy.deepcopy(universe),
                    configs=configs,
                    source_sha256=bindings,
                    warmup_bars=250,
                    interval_ms=STEP,
                    forward_rule="one new latest completed bar after warmup; no historical backfill",
                    execution_rule="first 15m boundary strictly after first local observation; intent only",
                    qualification="original ProjectSignalEngine filters; one event per direction/divergence",
                    promotion="NONE; original sample, cost, PIT and cross-market gates remain unchanged",
                    clock_scope="caller prepared inputs observed locally; publication/provenance not certified",
                    raw_source_provenance_verified=False,
                    historical_feature_scope="only observed at current ingestion; warmup is not historical PIT evidence",
                    raw_history_rule="all raw rows compared with their first committed journal hash, including gap warmup",
                )
                _immutable(path, _bytes(_envelope(self.manifest)))
            self._check_sources()
            self._recover()

    @contextmanager
    def _locked(self):
        fd = os.open(self.directory / ".writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def _check_sources(self):
        for source, expected in self.manifest["source_sha256"].items():
            saved = self.directory / "code" / source
            if not saved.is_file() or saved.is_symlink() or _sha(saved.read_bytes()) != expected:
                raise ValueError(f"source snapshot drift: {source}")
            if source != "config.yaml" and _sha((ROOT / source).read_bytes()) != expected:
                raise ValueError(f"running source drift: {source}; use frozen source snapshot")
        if asdict(DEFAULT_CONFIG) != self.manifest["configs"]["baseline"]:
            raise ValueError("running strategy configuration drift")

    def _documents(self):
        return [(path, _read(path)) for path in sorted(self.directory.glob("observations/*/*.json"))]

    def _materialize(self, payload):
        for variant, decision in payload["decisions"].items():
            name = f"{payload['symbol']}-{payload['close_time']:016d}.json"
            _immutable(self.directory / "decisions" / variant / name, _bytes(_envelope(decision)))
        for variant, event in payload["events"].items():
            _immutable(self.directory / "events" / variant / (event["event_id"] + ".json"), _bytes(_envelope(event)))

    def _recover(self):
        symbols = {}
        history = {}
        expected_views = set()
        for path, payload in self._documents():
            symbol = payload["symbol"]
            if payload["manifest_sha256"] != _sha(_bytes(self.manifest)) or payload["before_state"] != symbols.get(
                symbol
            ):
                raise ValueError("observation chain drift")
            _guard_first_rows(payload["raw_rows"], history.setdefault(symbol, {}), extend=True)
            for response in payload["result"]["responses"].values():
                if response.get("signal") in {"long", "short"}:
                    if (
                        response.get("input_snapshot_ref") != str(path.relative_to(self.directory))
                        or response.get("input_snapshot_payload_path") != "/after_state/rows"
                        or response.get("input_snapshot_manifest_sha256") != payload["manifest_sha256"]
                        or response.get("input_sha256") != _sha(_bytes(payload["after_state"]["rows"]))
                    ):
                        raise ValueError("permanent decision input reference drift")
            symbols[symbol] = payload["after_state"]
            self._materialize(payload)
            for variant in payload["decisions"]:
                expected_views.add(
                    self.directory / "decisions" / variant / f"{symbol}-{payload['close_time']:016d}.json"
                )
            for variant, event in payload["events"].items():
                expected_views.add(self.directory / "events" / variant / (event["event_id"] + ".json"))
        actual_views = set(self.directory.glob("decisions/*/*.json")) | set(self.directory.glob("events/*/*.json"))
        if actual_views != expected_views:
            raise ValueError("unexpected derived event or decision artifact")
        self.state = dict(schema_version=1, manifest_sha256=_sha(_bytes(self.manifest)), symbols=symbols)
        self._history_by_symbol = history
        state_path = self.directory / "state.json"
        if state_path.exists():
            saved = _read(state_path)
            # A complete journal commit may precede a crash during mirror
            # update. Permit only a genuine prior prefix, never unknown state.
            for symbol, state in saved.get("symbols", {}).items():
                current = symbols.get(symbol)
                if current is None or state["last_close"] > current["last_close"]:
                    raise ValueError("state is ahead of committed journal")
                options = [p["after_state"] for _, p in self._documents() if p["symbol"] == symbol]
                if state not in options:
                    raise ValueError("state cache drift")
            if saved.get("manifest_sha256") != self.state["manifest_sha256"]:
                raise ValueError("state version drift")
        self._save_state()

    def _save_state(self):
        path = self.directory / "state.json"
        data = _bytes(_envelope(self.state))
        if path.exists() and path.read_bytes() == data:
            return
        fd, name = tempfile.mkstemp(prefix=".state-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            Path(name).replace(path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _compute(self, symbol, rows, now, microstructure, previous, clock_authority):
        enriched, micro_audit = attach_microstructure(rows, microstructure, now)
        if previous:
            old = {row["close_time"]: row for row in previous["rows"]}
            for row in enriched:
                if row["close_time"] in old:
                    historical = old[row["close_time"]]
                    # First local observations of old prices/features prevail.
                    clean_new = {k: v for k, v in row.items() if k not in MICRO}
                    clean_old = {k: v for k, v in historical.items() if k not in MICRO}
                    if clean_new != clean_old:
                        raise ValueError("previously observed input revision; new decision rejected")
                    for key in MICRO:
                        if key in historical:
                            row[key] = historical[key]
        close = rows[-1]["close_time"]
        archive_ref = f"observations/{symbol}/{close:016d}.json"
        manifest_sha256 = _sha(_bytes(self.manifest))
        timely = close == (now // STEP) * STEP - 1
        missed = [] if not previous else list(range(previous["last_close"] + STEP, close, STEP))
        if not previous:
            status = "WARMED"
        elif not timely:
            status = "LATE_REWARMED"
        elif missed:
            status = "GAP_REWARMED"
        else:
            status = "OBSERVED"
        if close < self.manifest["frozen_at_ms"] and status == "OBSERVED":
            status = "LATE_REWARMED"
        warming = status != "OBSERVED"
        states, responses, decisions, events = {}, {}, {}, {}
        seen = copy.deepcopy(previous["seen"]) if previous else {name: [] for name in self.manifest["configs"]}
        generation = (previous["generation"] if previous else 0) + int(warming)
        with tempfile.TemporaryDirectory(prefix=".engine-", dir=self.directory) as work:
            for name, config in self.manifest["configs"].items():
                identity = ("forward-v1", symbol, name, str(generation), _sha(_bytes(self.manifest)))
                engine = PersistentSignalEngine(StrategyConfig(**config), Path(work) / name, identity)
                if not warming:
                    engine._restore(previous["engines"][name])
                before = engine._snapshot()
                if warming:
                    for end in range(100, len(enriched) + 1):
                        response = _detect(engine, enriched[max(0, end - 250) : end], now, archive_ref, manifest_sha256)
                        if response.get("signal") in {"long", "short"}:
                            key = f"{response['signal']}:{response['divergence_time']}"
                            if key not in seen[name]:
                                seen[name].append(key)
                else:
                    response = _detect(engine, enriched[-250:], now, archive_ref, manifest_sha256)
                    decisions[name] = dict(
                        symbol=symbol,
                        variant=name,
                        decision_close=close,
                        observed_at_ms=now,
                        input_sha256=_sha(_bytes(enriched[-250:])),
                        before_state=before,
                        response=response,
                    )
                    if response.get("signal") in {"long", "short"}:
                        key = f"{response['signal']}:{response['divergence_time']}"
                        if key not in seen[name]:
                            seen[name].append(key)
                            event = dict(
                                symbol=symbol,
                                variant=name,
                                direction=response["signal"],
                                setup_id=key,
                                decision_close=close,
                                known_at=now,
                                observed_at_ms=now,
                                clock_authority=clock_authority,
                                raw_source_provenance_verified=False,
                                signal_ready_time=response.get("signal_ready_time"),
                                signal_time=(now // STEP + 1) * STEP,
                                stop_loss=response["stop_loss"],
                                score=response.get("signal_score"),
                                structure_score=response.get("structure_score"),
                                input_sha256=_sha(_bytes(enriched[-250:])),
                                manifest_sha256=_sha(_bytes(self.manifest)),
                                status="FUTURE_EXECUTION_INTENT",
                            )
                            event["event_id"] = _sha(_bytes(event))
                            events[name] = event
                responses[name], states[name] = response, engine._snapshot()
        state = dict(
            last_close=close,
            last_observed_at_ms=now,
            generation=generation,
            rows=enriched[-250:],
            engines=states,
            seen=seen,
        )
        result = dict(
            status=status,
            symbol=symbol,
            decision_close=close,
            observed_at_ms=now,
            missed_close_times=missed,
            responses=responses,
            forward_events=len(events),
        )
        return dict(
            schema_version=1,
            manifest_sha256=_sha(_bytes(self.manifest)),
            symbol=symbol,
            close_time=close,
            raw_rows=rows,
            microstructure=microstructure,
            clock_authority=clock_authority,
            microstructure_audit=micro_audit,
            observed_at_ms=now,
            before_state=previous,
            after_state=state,
            decisions=decisions,
            events=events,
            result=result,
        )

    def accept_inputs(self, symbol, rows, observed_at_ms=None, microstructure=None):
        clock_authority = "SYSTEM" if observed_at_ms is None else "CALLER_SUPPLIED"
        now = _clock(observed_at_ms if observed_at_ms is not None else time.time_ns() // 1_000_000)
        with self._locked():
            self._check_sources()
            self._recover()
            if symbol not in {item["symbol"] for item in self.manifest["universe"]["members"]}:
                raise ValueError("symbol outside frozen universe")
            if now < self.manifest["frozen_at_ms"]:
                raise ValueError("observation precedes version freeze")
            _validate(rows, now)
            previous = self.state["symbols"].get(symbol)
            close = rows[-1]["close_time"]
            if previous and close < previous["last_close"]:
                raise ValueError("observation moved backwards")
            if previous and close == previous["last_close"]:
                path = self.directory / "observations" / symbol / f"{close:016d}.json"
                first = _read(path)
                return dict(
                    status="DUPLICATE",
                    symbol=symbol,
                    first_observed_at_ms=previous["last_observed_at_ms"],
                    input_revision_ignored=_bytes(rows) != _bytes(first["raw_rows"]),
                    responses=copy.deepcopy(first["result"]["responses"]),
                )
            if previous and now <= previous["last_observed_at_ms"]:
                raise ValueError("local observation clock did not advance")
            _guard_first_rows(rows, self._history_by_symbol.get(symbol, {}))
            payload = self._compute(
                symbol, copy.deepcopy(rows), now, copy.deepcopy(microstructure or {}), previous, clock_authority
            )
            path = self.directory / "observations" / symbol / f"{close:016d}.json"
            _immutable(path, _bytes(_envelope(payload)))  # Authoritative atomic commit.
            self._recover()  # Complete or recover derived event/state views.
            return copy.deepcopy(payload["result"])

    def replay(self):
        """Recompute every archived observation with the same real engine/state."""
        with self._locked():
            self._check_sources()
            self._recover()
            count = forward = events = system_clock = 0
            states = {}
            bindings = {}
            for path, saved in self._documents():
                _validate(saved["raw_rows"], saved["observed_at_ms"])
                symbol = saved["symbol"]
                expected = self._compute(
                    symbol,
                    saved["raw_rows"],
                    saved["observed_at_ms"],
                    saved["microstructure"],
                    states.get(symbol),
                    saved["clock_authority"],
                )
                if _bytes(expected) != _bytes(saved):
                    raise ValueError(f"same-data replay differs: {path}")
                states[symbol] = expected["after_state"]
                bindings[str(path.relative_to(self.directory))] = _sha(path.read_bytes())
                count += 1
                forward += saved["result"]["status"] == "OBSERVED"
                system_clock += (
                    saved["result"]["status"] == "OBSERVED"
                    and saved["clock_authority"] == "SYSTEM"
                    and self.manifest["freeze_clock_authority"] == "SYSTEM"
                )
                events += len(saved["events"])
            return dict(
                status="PASS",
                observations=count,
                forward_decisions=forward,
                events=events,
                system_clock_decisions=system_clock,
                raw_source_provenance_verified=False,
                real_forward_decisions=0,
                manifest_sha256=_sha(_bytes(self.manifest)),
                journal_sha256=bindings,
                scope="signal and state parity only; no fill/PnL/promotion authority",
            )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--universe", required=True, type=Path)
    observe = commands.add_parser("observe")
    observe.add_argument("--symbol", required=True)
    observe.add_argument(
        "--input",
        required=True,
        type=Path,
        help="JSON {bars:[prepared closed bars], microstructure:{source envelopes}}",
    )
    commands.add_parser("replay")
    args = parser.parse_args(argv)
    if args.command == "init":
        observer = ForwardObserver(args.directory, json.loads(args.universe.read_bytes()))
        result = dict(
            status="FROZEN",
            manifest_sha256=_sha(_bytes(observer.manifest)),
            frozen_at_ms=observer.manifest["frozen_at_ms"],
        )
    else:
        observer = ForwardObserver(args.directory)
        if args.command == "observe":
            payload = json.loads(args.input.read_bytes())
            result = observer.accept_inputs(args.symbol, payload["bars"], microstructure=payload.get("microstructure"))
        else:
            result = observer.replay()
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
