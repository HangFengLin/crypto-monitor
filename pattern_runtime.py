"""Durable observation state; no order, account, strategy or private API clients."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

from pattern_observer import PatternObserver
from pattern_validation import validate_runtime_payload

RULE_VERSION = "pattern_observer_v1"
INTERVALS = {"15m": 900_000, "1h": 3_600_000}
PATTERN_NAMES = {"breakout_retest": "突破回踩", "double_bottom": "双底颈线", "double_top": "双顶颈线"}
STAGE_NAMES = {"forming": "待确认", "confirmed": "已确认", "invalidated": "已失效", "expired": "已到期"}


class PatternHistoryRevision(ValueError):
    """An exchange changed a candle already used by this observation stream."""


class PatternRuntime:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.engine = PatternObserver()
        self.latest = {}
        self.events = []
        self.pending = []
        self.errors = []
        self.started_at = time.time()
        self.updated_at = None
        self.last_scan_at = None
        self.symbols = []
        self.blocked = False
        self.delivery_error = None
        self.recoveries = []
        try:
            if self.path.exists():
                if self.path.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError("oversized checkpoint")
                saved = json.loads(self.path.read_text(), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                validate_runtime_payload(saved)
                self.engine = PatternObserver.from_state(saved["engine"])
                self.latest, self.events, self.pending = saved["latest"], saved["events"], saved["pending"]
                self.updated_at = saved.get("updated_at")
                self.symbols = saved.get("symbols", [])
                self.recoveries = saved.get("recoveries", [])
                for scan_id in {row.get("scan_id") for row in self.pending if row.get("sealed") is False}:
                    self.finalize_scan(scan_id, interrupted=True)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.blocked = True
            self.errors = [{"error_class": "PatternCheckpointUnavailable"}]

    def last_close(self, symbol, interval):
        with self.lock:
            bars = self.engine.bars_for(symbol, interval)
            return bars[-1]["close_time"] if bars else None

    def _persist(self, engine, latest, events, pending, updated_at, *, recoveries=None):
        payload = {"mode": "observation_only", "rule_version": RULE_VERSION, "engine": engine.dump_state(),
                   "latest": latest, "events": events, "pending": pending, "updated_at": updated_at,
                   "symbols": self.symbols, "recoveries": self.recoveries if recoveries is None else recoveries}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w") as stream:
                json.dump(payload, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def ingest(self, symbol, interval, bars, *, observed_at_ms, scan_id=None):
        with self.lock:
            if self.blocked:
                raise ValueError("PatternCheckpointUnavailable")
            if interval not in INTERVALS or not isinstance(bars, list) or not bars:
                raise ValueError("InvalidPatternInput")
            closed = [bar for bar in bars if isinstance(bar, dict) and bar.get("confirmed") is not False
                      and isinstance(bar.get("close_time"), int) and bar["close_time"] < observed_at_ms]
            if not closed:
                raise ValueError("NoClosedPatternBars")
            known = self.engine.bars_for(symbol, interval)
            previous = known[-1]["close_time"] if known else None
            known_by_time = {bar["close_time"]: bar for bar in known}
            candidate = PatternObserver.from_state(self.engine.dump_state())
            generated = []
            last = previous
            for bar in closed:
                stamp = bar["close_time"]
                if previous is not None and stamp <= previous:
                    old = known_by_time.get(stamp)
                    if old is not None and any(bar.get(key) != old.get(key) for key in (
                        "open_time", "open", "high", "low", "close", "volume",
                    )):
                        raise PatternHistoryRevision("PatternHistoryRevision")
                    continue
                if last is not None and stamp != last + INTERVALS[interval]:
                    raise ValueError("PatternHistoryGap")
                origin = "warmup" if previous is None else "backfill" if observed_at_ms - stamp > 90_000 else "live"
                for event in candidate.process_bar(symbol, interval, bar, available_at_ms=observed_at_ms):
                    generated.append({**event, "origin": origin, "source": "binance_usdm",
                                      "chart": candidate.bars_for(symbol, interval)[-80:]})
                last = stamp
            if last == previous:
                return []
            latest = dict(self.latest)
            events = [*self.events]
            for event in generated:
                latest[event["id"]] = event
                events.append({key: value for key, value in event.items() if key != "chart"})
            chart = candidate.bars_for(symbol, interval)[-80:]
            for event in candidate.latest():
                key = event["id"]
                if (key in latest and event.get("symbol") == symbol and event.get("interval") == interval
                        and event["stage"] in {"forming", "confirmed"}):
                    latest[key] = {**latest[key], **event, "chart": chart}
            latest = dict(sorted(latest.items(), key=lambda pair: (
                pair[1]["stage"] in {"forming", "confirmed"}, pair[1]["bar_close_time"],
                pair[1]["updated_at_ms"], pair[0]), reverse=True)[:60])
            events = events[-100:]
            live = [event for event in generated if event["origin"] == "live"]
            pending = [*self.pending]
            if live:
                batch = self._batch(live, observed_at_ms)
                if scan_id is not None:
                    batch.update(scan_id=scan_id, sealed=False)
                pending.append(batch)
            updated = observed_at_ms / 1000
            self._persist(candidate, latest, events, pending, updated)
            self.engine, self.latest, self.events, self.pending = candidate, latest, events, pending
            self.updated_at = updated
            return generated

    def recover_stream(self, symbol, interval, bars, *, observed_at_ms):
        """Archive original evidence, then rewarm a revised observation stream.

        Caller supplies a fresh, mature history after catching a revision. Historical
        events stay in the archive; rebuilt history never enters the live outbox.
        Both replacement state and audit metadata commit before memory advances.
        """
        with self.lock:
            if self.blocked:
                raise ValueError("PatternCheckpointUnavailable")
            if (interval not in INTERVALS or not isinstance(bars, list)
                    or len(bars) < self.engine.config["warmup_bars"] + 1
                    or len(bars) > 1000):
                raise ValueError("PatternRecoveryWarmupInsufficient")
            previous = self.last_close(symbol, interval)
            if previous is None:
                raise ValueError("PatternRecoveryWarmupInsufficient")
            if any(event.get("symbol") == symbol and event.get("interval") == interval
                   for batch in self.pending for event in batch["raw_events"]):
                raise ValueError("PatternRecoveryPendingNotifications")
            saved = self.engine.dump_state()
            saved["streams"] = [row for row in saved["streams"]
                                if (row["symbol"], row["interval"]) != (symbol, interval)]
            saved["latest"] = [row for row in saved["latest"]
                               if (row["symbol"], row["interval"]) != (symbol, interval)]
            candidate = PatternObserver.from_state(saved)
            generated = []
            last = None
            for bar in bars:
                if (not isinstance(bar, dict) or not isinstance(bar.get("close_time"), int)
                        or bar["close_time"] >= observed_at_ms):
                    raise ValueError("InvalidPatternInput")
                if last is not None and bar["close_time"] != last + INTERVALS[interval]:
                    raise ValueError("PatternHistoryGap")
                for event in candidate.process_bar(symbol, interval, bar, available_at_ms=observed_at_ms):
                    generated.append({**event, "origin": "warmup", "source": "binance_usdm",
                                      "chart": candidate.bars_for(symbol, interval)[-80:]})
                last = bar["close_time"]
            if last < previous:
                raise ValueError("PatternRecoveryWarmupInsufficient")
            latest = {key: row for key, row in self.latest.items()
                      if (row["symbol"], row["interval"]) != (symbol, interval)}
            for event in generated:
                latest[event["id"]] = event
            chart = candidate.bars_for(symbol, interval)[-80:]
            for event in candidate.latest():
                if (event.get("symbol"), event.get("interval")) == (symbol, interval) and event["id"] in latest:
                    if event["stage"] in {"forming", "confirmed"}:
                        latest[event["id"]] = {**latest[event["id"]], **event, "chart": chart}
            latest = dict(sorted(latest.items(), key=lambda pair: (
                pair[1]["stage"] in {"forming", "confirmed"}, pair[1]["bar_close_time"],
                pair[1]["updated_at_ms"], pair[0]), reverse=True)[:60])
            events = [*self.events, *({key: value for key, value in event.items() if key != "chart"}
                                      for event in generated)][-100:]
            original = self.path.read_bytes()
            digest = hashlib.sha256(original).hexdigest()
            archive_dir = self.path.with_name(f"{self.path.stem}-revisions")
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path = archive_dir / f"{digest}.json"
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=archive_dir, prefix=".", delete=False) as archive:
                    temporary = Path(archive.name)
                    archive.write(original)
                    archive.flush()
                    os.fsync(archive.fileno())
                temporary.chmod(0o444)
                try:
                    os.link(temporary, archive_path)
                except FileExistsError:
                    if archive_path.read_bytes() != original:
                        raise ValueError("PatternRecoveryArchiveConflict") from None
                directory_fd = os.open(archive_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            recoveries = [*self.recoveries, {
                "symbol": symbol, "interval": interval, "reason": "PatternHistoryRevision",
                "recovered_at_ms": observed_at_ms, "archive_sha256": digest,
                "previous_close_time": previous, "new_close_time": last,
            }][-32:]
            updated = observed_at_ms / 1000
            self._persist(candidate, latest, events, self.pending, updated, recoveries=recoveries)
            self.engine, self.latest, self.events = candidate, latest, events
            self.updated_at, self.recoveries = updated, recoveries

    def _batch(self, events, observed_at_ms):
        events = sorted(events, key=lambda row: (row["symbol"], row["interval"], row["bar_close_time"], row["id"], row["stage"]))
        identities = [f"{event['id']}:{event['stage']}:{event['bar_close_time']}" for event in events]
        digest = hashlib.sha256("|".join(identities).encode()).hexdigest()[:24]
        symbol = events[0]["symbol"]
        base = os.getenv("FEISHU_SITE_URL", "http://localhost:3000/").rstrip("/")
        lines = []
        for event in events:
            name = PATTERN_NAMES.get(event["pattern"], event["pattern"])
            stage = STAGE_NAMES.get(event["stage"], event["stage"])
            direction = "向上" if event["direction"] == "long" else "向下"
            lines.extend([
                f"{event['interval']} · {name} · {direction} · {stage}",
                f"关键位：{event['level']:.8g}；失效位：{event['invalid_level']:.8g}",
                f"查看形态图：{base}/?pattern={quote(event['id'], safe='')}#patterns",
            ])
        lines.append("形态观察，不自动开仓；形态确认不代表已验证盈利。")
        return {"type": "observation_batch", "event_key": f"pattern-round:{digest}",
                "created_at": observed_at_ms / 1000, "raw_events": events,
                "alerts": [{"key": f"pattern:{digest}:{symbol}", "title": f"{symbol} 形态观察",
                            "body": "\n".join(lines), "created_at": observed_at_ms / 1000}]}

    def finalize_scan(self, scan_id, *, interrupted=False):
        with self.lock:
            groups = {}
            for batch in self.pending:
                if batch.get("scan_id") == scan_id and batch.get("sealed") is False:
                    for event in batch["raw_events"]:
                        groups.setdefault(event["symbol"], []).append(event)
            if not groups:
                return
            remaining = [batch for batch in self.pending if not (
                batch.get("scan_id") == scan_id and batch.get("sealed") is False)]
            for events in groups.values():
                batch = self._batch(events, max(event["available_at_ms"] for event in events))
                batch.update(scan_id=scan_id, sealed=True)
                if interrupted:
                    for alert in batch["alerts"]:
                        alert["body"] = "本轮检查中断，以下为已取得的形态观察。\n" + alert["body"]
                remaining.append(batch)
            self._persist(self.engine, self.latest, self.events, remaining, self.updated_at)
            self.pending = remaining

    def pending_batches(self):
        with self.lock:
            return json.loads(json.dumps(self.pending))

    def drain_notifications(self, append):
        with self.lock:
            count = 0
            while self.pending:
                if self.pending[0].get("sealed") is False:
                    break
                if not append(self.pending[0]):
                    self.delivery_error = "PatternJournalWriteFailed"
                    break
                remaining = self.pending[1:]
                self._persist(self.engine, self.latest, self.events, remaining, self.updated_at)
                self.pending = remaining
                self.delivery_error = None
                count += 1
            return count

    def record_scan(self, symbols, errors, *, now_ms):
        with self.lock:
            self.symbols = list(symbols)
            if not self.blocked:
                self.errors = list(errors)
            self.last_scan_at = now_ms / 1000

    def snapshot(self, *, now_ms=None):
        with self.lock:
            now_ms = time.time() * 1000 if now_ms is None else now_ms
            errors = [*self.errors, *([{"error_class": self.delivery_error}] if self.delivery_error else [])]
            status = "unavailable" if self.blocked else "partial" if errors else "starting"
            if not self.blocked and self.last_scan_at is not None:
                status = "stale" if now_ms - self.last_scan_at * 1000 > 120_000 else "partial" if errors else "healthy"
            patterns = list(self.latest.values())
            counts = {stage: sum(row.get("stage") == stage for row in patterns) for stage in STAGE_NAMES}
            result = {"mode": "observation_only", "rule_version": RULE_VERSION, "source": "binance_usdm",
                      "status": status, "started_at": self.started_at, "updated_at": self.updated_at,
                      "last_scan_at": self.last_scan_at, "symbols": self.symbols, "intervals": list(INTERVALS),
                      "errors": errors, "patterns": patterns, "events": self.events, "counts": counts,
                      "pending_notifications": len(self.pending), "recoveries": self.recoveries}
            return json.loads(json.dumps(result, allow_nan=False))
