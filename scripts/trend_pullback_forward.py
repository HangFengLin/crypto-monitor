"""Frozen local TPR1 collection; public data only, no orders or early PnL.

The journal is the authority. Source responses and observation commits are
immutable; gaps are never reconstructed into prospective decisions. The fixed
12-week evaluation is a research screen, never account verification/promotion.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import math
import os
import random
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_client import read_json_url  # noqa: E402
from indicators import calculate_indicators  # noqa: E402
from research_funding_data import collect_funding  # noqa: E402
from research_portfolio import replay_portfolio  # noqa: E402
from scripts.v1_forward_observer import _bytes, _envelope, _immutable, _read, _sha  # noqa: E402
from signal_engine_state import PersistentSignalEngine  # noqa: E402
from strategy import DEFAULT_CONFIG, ProjectSignalEngine, StrategyConfig  # noqa: E402

STEP, HIGHER = 900_000, 14_400_000
SYMBOLS = ("BTCUSDT", "ETHUSDT")
SOURCES = ("strategy.py", "indicators.py", "config.py", "data_client.py", "position_manager.py",
           "research_portfolio.py", "research_funding_data.py", "signal_engine_state.py",
           "scripts/v1_forward_observer.py", "scripts/trend_pullback_forward.py")
PROTOCOL = ROOT / "research_projects/trend_pullback_v1/protocol.json"


def _ms(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("nonnegative integer millisecond clock required")
    return value


def _parse(raw, interval, received):
    step = STEP if interval == "15m" else HIGHER
    if not isinstance(raw, list) or not raw:
        raise ValueError("nonempty Binance UM kline response required")
    rows = []
    for item in raw:
        if not isinstance(item, list) or len(item) < 7:
            raise ValueError("invalid Binance kline row")
        opening, close = _ms(item[0]), _ms(item[6])
        prices = [float(item[index]) for index in (1, 2, 3, 4, 5)]
        if (opening % step or close != opening + step - 1 or close >= received
                or any(not math.isfinite(x) for x in prices)
                or min(prices[:4]) <= 0 or prices[4] < 0
                or not prices[2] <= min(prices[0], prices[3]) <= max(prices[0], prices[3]) <= prices[1]):
            raise ValueError("incomplete, misaligned or invalid Binance OHLCV")
        if rows and opening != rows[-1]["close_time"] + 1:
            raise ValueError("response contains missing or duplicate bars")
        rows.append(dict(open_time=opening, close_time=close, open=prices[0], high=prices[1],
                         low=prices[2], close=prices[3], volume=prices[4], confirmed=True, is_closed=True))
    return rows


def _suffix(rows, step):
    start = len(rows) - 1
    while start > 0 and rows[start]["open_time"] == rows[start - 1]["open_time"] + step:
        start -= 1
    return rows[start:]


def paired_week_intervals(candidate, baseline, *, iterations=2000, seed=20261025):
    """Descriptive paired two-week moving-block mean intervals, no significance gate."""
    if len(candidate) != len(baseline) or len(candidate) < 2:
        return dict(status="INSUFFICIENT_COMPLETE_WEEKS", complete_weeks=len(candidate))
    rng, n = random.Random(seed), len(candidate)
    absolute, differences = [], []
    for _ in range(iterations):
        indexes = []
        while len(indexes) < n:
            start = rng.randrange(n - 1)
            indexes.extend((start, start + 1))
        indexes = indexes[:n]
        absolute.append(sum(candidate[i] for i in indexes) / n)
        differences.append(sum(candidate[i] - baseline[i] for i in indexes) / n)

    def interval(values):
        ordered = sorted(values)
        output = []
        for fraction in (.025, .975):
            index = (len(ordered) - 1) * fraction
            left, right = math.floor(index), math.ceil(index)
            output.append(ordered[left] + (ordered[right] - ordered[left]) * (index - left))
        return output

    return dict(status="DESCRIPTIVE_ONLY", complete_weeks=n, iterations=iterations, seed=seed,
                block_weeks=2, circular=False, candidate_weekly_mean_interval=interval(absolute),
                paired_weekly_difference_interval=interval(differences), statistically_verified=False)


def validate_funding_coverage(evidence, symbol, start, end, *, tolerance_ms=60_000):
    """Require each frozen UTC eight-hour slot; retain actual settlement clocks.

    Eight-hour cadence is a conservative frozen research assumption. A future
    venue interval change is inconclusive and is never silently filled with zero.
    The slot tolerance is used only for coverage, not for cash-flow timestamps.
    """
    interval = 28_800_000
    if (evidence.get("symbol") != symbol or evidence.get("exchange") != "binance"
            or evidence.get("market") != "linear_perpetual"
            or evidence.get("source") != "Binance /fapi/v1/fundingRate"
            or evidence.get("complete") is not True or evidence.get("gaps") != []
            or evidence.get("start", end) > start or evidence.get("end", start) < end):
        raise ValueError("official complete funding identity/interval required")
    expected = set(range(((start + interval - 1) // interval) * interval, end, interval))
    records, raw = evidence.get("records"), evidence.get("raw_records")
    if not expected or not isinstance(records, list) or not isinstance(raw, list):
        raise ValueError("scheduled funding observations required")
    actual = {}
    for record in records:
        stamp = _ms(record.get("time"))
        slot = ((stamp + interval // 2) // interval) * interval
        if (slot not in expected or slot in actual or abs(stamp - slot) > tolerance_ms
                or not start <= stamp < end or not math.isfinite(float(record["rate"]))
                or not math.isfinite(float(record["mark_price"])) or record["mark_price"] <= 0):
            raise ValueError("missing, duplicate or unexpected scheduled funding settlement")
        actual[slot] = record
    if set(actual) != expected:
        raise ValueError("missing scheduled funding settlement")
    raw_map = {}
    for row in raw:
        stamp = _ms(row.get("fundingTime"))
        if start <= stamp < end:
            if row.get("symbol") != symbol:
                raise ValueError("raw funding symbol mismatch")
            if stamp in raw_map:
                raise ValueError("duplicate raw scheduled funding record")
            raw_map[stamp] = row
    if set(raw_map) != {r["time"] for r in records}:
        raise ValueError("raw funding timestamp binding mismatch")
    for record in records:
        row = raw_map[record["time"]]
        if float(row["fundingRate"]) != record["rate"] or float(row["markPrice"]) != record["mark_price"]:
            raise ValueError("raw funding values do not match normalized settlements")


class ForwardStudy:
    def __init__(self, directory, *, protocol_path=None, provider=None, clock=None):
        self.directory = Path(directory).resolve()
        self.protocol_path = Path(protocol_path or PROTOCOL).resolve()
        self.provider = provider
        self.clock = clock
        self.authority = "SYSTEM_PUBLIC" if provider is None and clock is None else "TEST"

    def now(self):
        return _ms(self.clock() if self.clock else time.time_ns() // 1_000_000)

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.directory / ".writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def init(self):
        with self.locked():
            path = self.directory / "manifest.json"
            if path.exists():
                return self._manifest()
            if any(self.directory.glob("observations/*/*.json")):
                raise ValueError("missing manifest; preserve original research version")
            raw = self.protocol_path.read_bytes()
            protocol = json.loads(raw)
            if (protocol.get("schema_version") != 1 or protocol.get("hypothesis") != "TPR1"
                    or protocol.get("research_only") is not True or protocol.get("symbols") != list(SYMBOLS)
                    or protocol.get("duration_weeks") != 12 or protocol.get("warmup_bars") != 300
                    or protocol.get("direction") != "long"):
                raise ValueError("fixed TPR1 protocol required")
            _immutable(self.directory / "protocol.json", raw)
            bindings = {}
            for name in SOURCES:
                data = (ROOT / name).read_bytes()
                _immutable(self.directory / "code" / name, data)
                bindings[name] = _sha(data)
            sanitized = _bytes(asdict(DEFAULT_CONFIG))
            _immutable(self.directory / "baseline_strategy.json", sanitized)
            now = self.now()
            manifest = dict(schema_version=1, hypothesis="TPR1", research_only=True,
                            frozen_at_ms=now, deadline_ms=now + 12 * 7 * 24 * 60 * 60 * 1000,
                            data_authority=self.authority, protocol_sha256=_sha(raw),
                            baseline_config_sha256=_sha(sanitized), source_sha256=bindings,
                            account_verified=False, promotion_eligible=False)
            _immutable(path, _bytes(_envelope(manifest)))
            return manifest

    def _manifest(self):
        manifest = _read(self.directory / "manifest.json")
        for name, digest in manifest["source_sha256"].items():
            if (_sha((self.directory / "code" / name).read_bytes()) != digest
                    or _sha((ROOT / name).read_bytes()) != digest):
                raise ValueError(f"frozen or running source drift: {name}")
        if (_sha((self.directory / "protocol.json").read_bytes()) != manifest["protocol_sha256"]
                or _sha(self.protocol_path.read_bytes()) != manifest["protocol_sha256"]
                or _sha((self.directory / "baseline_strategy.json").read_bytes()) != manifest["baseline_config_sha256"]
                or _sha(_bytes(asdict(DEFAULT_CONFIG))) != manifest["baseline_config_sha256"]):
            raise ValueError("frozen protocol or baseline configuration drift")
        if manifest["data_authority"] != self.authority:
            raise ValueError("test and actual public source authority cannot be mixed")
        self.manifest = manifest
        self.protocol = json.loads((self.directory / "protocol.json").read_bytes())
        return manifest

    def _journal(self):
        histories = {s: {"15m": {}, "4h": {}} for s in SYMBOLS}
        observations, last = [], {}
        for path in sorted(self.directory.glob("observations/*/*.json")):
            item = _read(path)
            symbol = item["symbol"]
            previous = last.get(symbol)
            if (symbol not in histories or item["manifest_sha256"] != _sha(_bytes(self.manifest))
                    or item["previous_commit"] != (previous["commit_sha256"] if previous else None)):
                raise ValueError("observation chain drift")
            for interval, rows in item["raw_rows"].items():
                for row in rows:
                    key = row["close_time"]
                    original = histories[symbol][interval].get(key)
                    if original is not None and original != row:
                        raise ValueError("committed historical price drift")
                    histories[symbol][interval][key] = row
            for reference in item["request_refs"]:
                request = _read(self.directory / reference)
                interval = request["interval"]
                if (request["symbol"] != symbol or request["error"] is not None
                        or request["data_authority"] != self.authority
                        or request["requested_at_ms"] > request["received_at_ms"]
                        or request["received_at_ms"] > item["observed_at_ms"]
                        or _parse(request["payload"], interval, request["received_at_ms"]) != item["raw_rows"][interval]):
                    raise ValueError("raw source to observation binding drift")
            item["commit_sha256"] = _sha(path.read_bytes())
            observations.append(item)
            last[symbol] = item
        return histories, observations, last

    def _request(self, symbol, interval, limit, end):
        url = "https://fapi.binance.com/fapi/v1/klines?" + urlencode(
            dict(symbol=symbol, interval=interval, limit=limit, endTime=end))
        requested = self.now()
        payload = None
        error = None
        try:
            payload = self.provider(url) if self.provider else read_json_url(url, timeout=12, attempts=2, max_elapsed=30)
        except Exception as exc:
            error = type(exc).__name__  # Public endpoint only; do not copy arbitrary exception text.
        received = self.now()
        if received < requested:
            raise ValueError("local receive clock moved backwards")
        request = dict(url=url, symbol=symbol, interval=interval, requested_at_ms=requested,
                       received_at_ms=received, data_authority=self.authority,
                       payload=payload, error=error)
        name = f"requests/{len(list(self.directory.glob('requests/*.json'))):09d}.json"
        _immutable(self.directory / name, _bytes(_envelope(request)))
        if error:
            raise OSError(f"public request failed: {error}")
        return _parse(payload, interval, received), name, received

    def _record_failure(self, symbol, reason):
        name = f"failures/{len(list(self.directory.glob('failures/*.json'))):09d}.json"
        _immutable(self.directory / name, _bytes(_envelope(dict(
            symbol=symbol, observed_at_ms=self.now(), reason=reason, data_authority=self.authority))))

    def _symbol(self, symbol, histories, previous, target):
        previous_close = previous["decision_close"] if previous else None
        gap = previous_close is not None and target != previous_close + STEP
        references, incoming, received = [], {}, []
        for interval, step in (("15m", STEP), ("4h", HIGHER)):
            end = ((target + 1) // step) * step - 1
            old = histories[symbol][interval]
            if interval == "4h" and old and max(old) == end:
                incoming[interval] = []
                continue
            rows, ref, stamp = self._request(symbol, interval, 300 if previous is None or gap else 3, end)
            for row in rows:
                original = old.get(row["close_time"])
                if original is not None and original != row:
                    raise ValueError("previously committed raw bar revised")
            incoming[interval] = rows
            references.append(ref)
            received.append(stamp)
        observed = max([self.now(), *received])
        if previous and observed <= previous["observed_at_ms"]:
            raise ValueError("observation clock did not advance")
        merged = {}
        for interval, step in (("15m", STEP), ("4h", HIGHER)):
            all_rows = dict(histories[symbol][interval])
            all_rows.update({row["close_time"]: row for row in incoming[interval]})
            merged[interval] = _suffix([all_rows[key] for key in sorted(all_rows)], step)
            if len(merged[interval]) < 300:
                raise ValueError("300 continuous complete warmup bars required")
        if merged["15m"][-1]["close_time"] != target:
            raise ValueError("latest requested 15m close missing")
        timely = (target == (observed // STEP) * STEP - 1 and target < observed <= target + 90_000
                  and observed < self.manifest["deadline_ms"])
        status = "WARMED" if previous is None else "GAP_REWARMED" if gap else "OBSERVED" if timely else "LATE_REWARMED"
        if target <= self.manifest["frozen_at_ms"] and status == "OBSERVED":
            status = "LATE_REWARMED"
        lower = calculate_indicators(merged["15m"])
        higher = calculate_indicators(merged["4h"])
        h_index = -1
        for row in lower:
            # Warmup rows before the earliest 4h source remain unavailable.
            while h_index + 1 < len(higher) and higher[h_index + 1]["close_time"] <= row["close_time"]:
                h_index += 1
            if h_index < 0:
                continue
            context = higher[h_index]
            for name in ("close", "ema20", "ema60", "ema200", "macd"):
                row["higher_" + name] = context.get(name)
            row["higher_close_time"] = context["close_time"]
            row["higher_prev_ema20"] = higher[h_index - 1].get("ema20") if h_index > 0 else None
        candidate = ProjectSignalEngine().detect_trend_pullback_reclaim(lower[-2:])
        historical_seen = []
        with tempfile.TemporaryDirectory(prefix=".baseline-", dir=self.directory) as work:
            config = StrategyConfig(**json.loads((self.directory / "baseline_strategy.json").read_bytes()))
            engine = PersistentSignalEngine(config, Path(work), ("TPR1-baseline", symbol))
            if previous and status == "OBSERVED":
                engine._restore(previous["baseline_state"])
                baseline = ProjectSignalEngine.detect(engine, lower[-250:], clean=False)
            else:
                baseline = {"signal": "wait", "reason": "historical warmup only"}
                for end in range(100, len(lower) + 1):
                    warm_response = ProjectSignalEngine.detect(engine, lower[max(0, end - 250):end], clean=False)
                    if warm_response.get("signal") == "long":
                        historical_seen.append(str(warm_response.get("divergence_time")))
            baseline_state = engine._snapshot()
        seen = copy.deepcopy(previous["seen"]) if previous else {"baseline": [], "TPR1": []}
        seen["baseline"] = sorted(set(seen["baseline"] + historical_seen))
        decisions, events = {}, []
        for variant, response in (("baseline", baseline), ("TPR1", candidate)):
            decision = dict(response=response, disposition="WARMUP_ONLY")
            if status == "OBSERVED":
                direction = response.get("signal")
                if direction == "long":
                    identity = str(lower[-2]["close_time"] if variant == "TPR1" else response.get("divergence_time"))
                    if identity in seen[variant]:
                        decision["disposition"] = "REJECTED_DUPLICATE_SETUP"
                    else:
                        seen[variant].append(identity)
                        event = dict(symbol=symbol, variant=variant, direction="long", setup_id=f"{variant}:{identity}",
                                     decision_close=target, known_at=observed, observed_at_ms=observed,
                                     signal_time=(observed // STEP + 1) * STEP, stop_loss=response["stop_loss"],
                                     entry_hypothesis=variant, data_authority=self.authority)
                        events.append(event)
                        decision["disposition"] = "FUTURE_EXECUTION_INTENT"
                else:
                    decision["disposition"] = "REJECTED_DIRECTION" if direction in {"short", "filtered_sell"} else "WAIT_OR_FILTERED"
                decisions[variant] = decision
        item = dict(schema_version=1, symbol=symbol, status=status, decision_close=target,
                    observed_at_ms=observed, data_authority=self.authority,
                    manifest_sha256=_sha(_bytes(self.manifest)),
                    previous_commit=previous["commit_sha256"] if previous else None,
                    request_refs=references, raw_rows=incoming, decisions=decisions, events=events,
                    prepared_input_sha256=_sha(_bytes(lower[-250:])), baseline_state=baseline_state, seen=seen)
        _immutable(self.directory / f"observations/{symbol}/{target:016d}.json", _bytes(_envelope(item)))
        return status

    def _collect_locked(self):
        self._manifest()
        now = self.now()
        if now >= self.manifest["deadline_ms"]:
            return dict(status="COLLECTION_HORIZON_ENDED", deadline_ms=self.manifest["deadline_ms"])
        histories, _observations, last = self._journal()
        target = (now // STEP) * STEP - 1
        statuses, errors = [], []
        for symbol in SYMBOLS:
            previous = last.get(symbol)
            if previous and previous["decision_close"] == target:
                statuses.append("DUPLICATE")
                continue
            try:
                statuses.append(self._symbol(symbol, histories, previous, target))
            except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                self._record_failure(symbol, reason)
                errors.append(dict(symbol=symbol, reason=reason))
        status = "COLLECTION_FAILED" if errors else "OBSERVED" if "OBSERVED" in statuses else statuses[0]
        return dict(status=status, symbols=list(SYMBOLS), results=statuses, failures=errors,
                    account_verified=False, promotion_eligible=False)

    def collect(self):
        with self.locked():
            return self._collect_locked()

    def status(self):
        self._manifest()
        _histories, observations, last = self._journal()
        now = self.now()
        first = (self.manifest["frozen_at_ms"] // STEP + 1) * STEP - 1
        end = (min(now, self.manifest["deadline_ms"]) // STEP) * STEP - 1
        expected = max(0, (end - first) // STEP + 1)
        forward = [row for row in observations if row["status"] == "OBSERVED"]
        counts = {s: sum(row["symbol"] == s for row in forward) for s in SYMBOLS}
        return dict(status="OBSERVATION_WINDOW_OPEN" if now < self.manifest["deadline_ms"] else "EVALUATION_DUE",
                    data_authority=self.authority, frozen_at_ms=self.manifest["frozen_at_ms"],
                    deadline_ms=self.manifest["deadline_ms"], forward_observations=len(forward),
                    decision_records=sum(len(row["decisions"]) for row in forward),
                    signal_counts={v: sum(e["variant"] == v for row in forward for e in row["events"])
                                   for v in ("baseline", "TPR1")},
                    expected_observations_per_symbol=expected, observed_per_symbol=counts,
                    missing_observations=sum(max(0, expected - counts[s]) for s in SYMBOLS),
                    last_close_per_symbol={s: last[s]["decision_close"] if s in last else None for s in SYMBOLS},
                    collection_failures=len(list(self.directory.glob("failures/*.json"))),
                    account_verified=False, promotion_eligible=False,
                    scope="sample coverage and intent only; PnL blinded until fixed deadline")

    def _funding(self, symbol, start, end):
        selected = self.directory / f"funding/{symbol}.json"
        if selected.exists():
            evidence = _read(selected)
            validate_funding_coverage(evidence, symbol, start, end)
            return evidence
        requested = self.now()
        evidence = collect_funding(symbol, start, end)
        attempt = dict(requested_at_ms=requested, received_at_ms=self.now(), evidence=evidence)
        name = f"funding_attempts/{len(list(self.directory.glob('funding_attempts/*.json'))):06d}-{symbol}.json"
        _immutable(self.directory / name, _bytes(_envelope(attempt)))
        validate_funding_coverage(evidence, symbol, start, end)
        evidence["selected_attempt"] = name
        _immutable(selected, _bytes(_envelope(evidence)))
        return evidence

    def _evaluation_bindings(self):
        paths = []
        for pattern in ("observations/*/*.json", "requests/*.json", "funding/*.json", "accounts/*.json"):
            paths.extend(self.directory.glob(pattern))
        return {str(p.relative_to(self.directory)): _sha(p.read_bytes()) for p in sorted(paths)}

    def evaluate(self):
        with self.locked():
            self._manifest()
            now = self.now()
            if now < self.manifest["deadline_ms"]:
                return dict(status="WAITING_FOR_NEW_DATA", deadline_ms=self.manifest["deadline_ms"],
                            account_verified=False, promotion_eligible=False)
            if (self.directory / "evaluation.json").exists():
                result = _read(self.directory / "evaluation.json")
                if (result.get("manifest_sha256") != _sha(_bytes(self.manifest))
                        or result.get("input_sha256") != self._evaluation_bindings()):
                    raise ValueError("final evaluation input binding drift")
                self._journal()
                return result
            summary = self.status()
            blockers = []
            if self.authority != "SYSTEM_PUBLIC":
                blockers.append("TEST_SOURCE_OR_CLOCK")
            if summary["missing_observations"]:
                blockers.append("MISSING_TIMELY_OBSERVATIONS")
            if any(summary["signal_counts"][v] == 0 for v in ("baseline", "TPR1")):
                blockers.append("INSUFFICIENT_SIGNALS")
            if blockers:
                return dict(status="INCONCLUSIVE", blockers=blockers, coverage=summary,
                            account_verified=False, promotion_eligible=False)
            histories, observations, _last = self._journal()
            start = (self.manifest["frozen_at_ms"] // STEP + 1) * STEP
            end = self.manifest["deadline_ms"] // STEP * STEP
            data = {s: [row for key, row in sorted(histories[s]["15m"].items())
                        if start <= row["open_time"] < end] for s in SYMBOLS}
            funding = {}
            try:
                for symbol in SYMBOLS:
                    if len(data[symbol]) != (end - start) // STEP:
                        raise ValueError("complete common scoring price calendar required")
                    funding[symbol] = self._funding(symbol, start, end)
                results, weekly = {}, {}
                for variant in ("baseline", "TPR1"):
                    events = [event for row in observations for event in row["events"]
                              if event["variant"] == variant and start <= event["signal_time"] < end]
                    for scenario in self.protocol["scenarios"]:
                        params = {k: v for k, v in scenario.items() if k != "id"}
                        account = replay_portfolio(data, events, funding, **self.protocol["execution"], **params,
                                                   strict_funding=True, finalize=False)
                        if not account["trades"]:
                            raise ValueError("no mature trades in a required variant/scenario")
                        name = f"{variant}-{scenario['id']}"
                        _immutable(self.directory / f"accounts/{name}.json", _bytes(_envelope(account)))
                        initial = self.protocol["execution"]["initial_equity"]
                        peak, drawdown = initial, 0.0
                        for point in account["equity"]:
                            peak = max(peak, point["equity"])
                            drawdown = min(drawdown, point["equity"] / peak - 1)
                        results[name] = dict(net_return=account["equity"][-1]["equity"] / initial - 1,
                                             max_drawdown=drawdown, closed_trades=len(account["trades"]))
                        weekly[name] = []
                        week_ms, prior_value = 7 * 24 * 60 * 60 * 1000, initial
                        for week in range(12):
                            left, right = start + week * week_ms, min(end, start + (week + 1) * week_ms)
                            points = [p for p in account["equity"] if left < p["time"] <= right]
                            if not points:
                                raise ValueError("weekly equity coverage missing")
                            value = points[-1]["equity"]
                            weekly[name].append(dict(week=week + 1, start=left, end=right,
                                                     complete=right - left == week_ms,
                                                     net_return=value / prior_value - 1))
                            prior_value = value
            except (OSError, ValueError, TypeError, KeyError) as exc:
                return dict(status="INCONCLUSIVE", blockers=[type(exc).__name__ + ": " + str(exc)],
                            account_verified=False, promotion_eligible=False)
            passed = (results["TPR1-base"]["net_return"] > 0
                      and results["TPR1-double_cost"]["net_return"] > 0
                      and results["TPR1-base"]["net_return"] > results["baseline-base"]["net_return"])
            absolute = [w["net_return"] for w in weekly["TPR1-base"] if w["complete"]]
            baseline = [w["net_return"] for w in weekly["baseline-base"] if w["complete"]]
            result = dict(status="RESEARCH_SCREEN_PASSED" if passed else "RESEARCH_SCREEN_NOT_MET", results=results,
                          weekly_returns=weekly, descriptive_intervals=paired_week_intervals(absolute, baseline),
                          statistical_advantage="NOT_CONFIRMED", statistically_verified=False,
                          account_verified=False, promotion_eligible=False,
                          manifest_sha256=_sha(_bytes(self.manifest)), input_sha256=self._evaluation_bindings())
            _immutable(self.directory / "evaluation.json", _bytes(_envelope(result)))
            return result

    def follow(self, *, deadline_ms=None, poll_seconds=30):
        if not 1 <= poll_seconds <= 60:
            raise ValueError("poll interval must be between one and sixty seconds")
        with self.locked():
            self._manifest()
            end = min(self.manifest["deadline_ms"], deadline_ms if deadline_ms is not None else self.manifest["deadline_ms"])
            last_complete_target = None
            while self.now() < end:
                target = (self.now() // STEP) * STEP - 1
                if target != last_complete_target:
                    result = self._collect_locked()
                    if result["status"] != "COLLECTION_FAILED":
                        last_complete_target = target
                    if result["status"] != "DUPLICATE":
                        print(json.dumps(result, sort_keys=True), flush=True)
                time.sleep(min(poll_seconds, max(0, (end - self.now()) / 1000)))
        if self.now() >= self.manifest["deadline_ms"]:
            return self.evaluate()  # Evaluation acquires its own writer lock after collection releases it.
        return dict(status="FOLLOW_DEADLINE_REACHED", deadline_ms=end)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--protocol", type=Path, default=PROTOCOL)
    parser.add_argument("command", choices=("init", "collect", "status", "evaluate", "follow"))
    parser.add_argument("--follow-seconds", type=float, default=None)
    args = parser.parse_args(argv)
    study = ForwardStudy(args.directory, protocol_path=args.protocol)
    if args.command == "follow":
        deadline = None if args.follow_seconds is None else study.now() + int(args.follow_seconds * 1000)
        result = study.follow(deadline_ms=deadline)
    else:
        result = getattr(study, args.command)()
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
