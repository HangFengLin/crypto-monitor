"""Durable change detection and outbox for paper research notifications."""

import fcntl
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

MODULE_FAULT_LABELS = {
    "strategy": "策略信号",
    "indicators": "技术指标",
    "support": "支撑位监控",
    "paper_data": "模拟持仓行情",
    "fault_history": "故障记录",
    "paper_pool": "模拟币池扫描",
    "paper_universe": "模拟币池更新",
    "ledger": "模拟交易账本",
    "events": "事件记录",
    "observations": "观察提醒状态",
}

DELIVERY_HISTORY_LIMIT = 200
OBSERVATION_MAX_LINE_BYTES = 4 * 1024 * 1024
OBSERVATION_READ_BUDGET_BYTES = 8 * 1024 * 1024


def _timestamp(value):
    """Queue timestamps are finite Unix seconds; missing evidence stays unknown."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return value
    return None


def _elapsed(start, end):
    start, end = _timestamp(start), _timestamp(end)
    return end - start if start is not None and end is not None and end >= start else None


def _message_id(key):
    return hashlib.sha256(str(key).encode()).hexdigest()[:24]


def _error_class(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value) else "DeliveryFailed"


def _latency_summary(values):
    values = sorted(value for value in values if _timestamp(value) is not None)
    return {
        "count": len(values),
        "p50": values[math.ceil(len(values) * 0.50) - 1] if values else None,
        "p95": values[math.ceil(len(values) * 0.95) - 1] if values else None,
        "max": values[-1] if values else None,
    }


class SenderLease:
    """OS-held ownership prevents overlapping server processes from sending twice."""

    def __init__(self, path):
        lock_path = Path(str(path) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = lock_path.open("a")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise RuntimeError("Another notification sender owns this queue") from None

    def close(self):
        self.handle.close()


class Outbox:
    def __init__(self, path, *, min_send_interval_s=0.0):
        self.path = Path(path)
        interval = _timestamp(min_send_interval_s)
        if interval is None:
            raise ValueError("Notification pacing interval must be finite and nonnegative")
        self.min_send_interval_s = interval
        persisted = self.path.read_text() if self.path.exists() else None
        self._saved_payload = persisted
        self.data = (
            json.loads(persisted)
            if persisted is not None
            else {
                "initialized": False,
                "trades": {},
                "reports": {},
                "fault": None,
                "pending": [],
                "last_delivery": None,
            }
        )
        self.data.setdefault("delivery_history", [])
        self.data["delivery_history"] = self.data["delivery_history"][-DELIVERY_HISTORY_LIMIT:]
        for message in self.data["pending"]:
            # Upgrade retained work without inventing an event or enqueue time.
            message.setdefault("created_at", None)
            message.setdefault("enqueued_at", None)
            message.setdefault("message_id", _message_id(message["key"]))
        pacing = self.data.get("send_pacing") or {}
        last_attempt = _timestamp(pacing.get("last_attempt_at"))
        not_before = _timestamp(pacing.get("not_before"))
        stored_interval = _timestamp(pacing.get("interval_s")) or 0.0
        if last_attempt is None and interval:
            # A first pacing upgrade can reuse a known send time without replaying work.
            last = self.data.get("last_delivery") or {}
            last_attempt = _timestamp(last.get("attempt_started_at"))
            if last_attempt is None:
                last_attempt = _timestamp(last.get("at"))
        effective_interval = max(interval, stored_interval)
        if last_attempt is not None:
            not_before = max(not_before or 0.0, last_attempt + effective_interval)
        now = time.time()
        latest_attempt = self.data["delivery_history"][-1] if self.data["delivery_history"] else {}
        unknown_send = latest_attempt.get("stage") == "attempt_started"
        # A provisional start can precede slow fsync. Unknown sends need a full cooldown.
        # A restored wall clock may also have moved back: never wait hours for pacing.
        remaining = effective_interval if unknown_send or (last_attempt is not None and now < last_attempt) else max(
            0.0, (not_before or now) - now
        )
        self._send_not_before_monotonic = time.monotonic() + remaining

    def save(self):
        payload = json.dumps(self.data, ensure_ascii=False)
        if payload == self._saved_payload and self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
        # Only immutable bytes known to have survived replace can suppress later writes.
        self._saved_payload = payload

    def enqueue(self, key, channel, title, body, *, created_at=None):
        base = os.getenv("FEISHU_SITE_URL", "http://localhost:3000/").rstrip("/")
        self.data["pending"].append(
            {
                "key": key,
                "channel": channel,
                "title": title,
                "body": f"{body}\n通知编号：{key}\n网站：{base}/",
                "attempts": 0,
                "next_attempt": 0,
                "message_id": _message_id(key),
                "created_at": _timestamp(created_at),
                "enqueued_at": time.time(),
            }
        )

    def observe(self, trades, reports, fault, alerts=None, observation_log=None):
        if alerts is not None:
            # Upgrade existing queues by baselining retained history once.
            seen = self.data.get("alerts")
            if seen is None:
                self.data["alerts"] = {alert["key"]: True for alert in alerts}
            else:
                for alert in alerts:
                    if alert["key"] not in seen:
                        self.enqueue(f"alert:{alert['key']}", "signal", alert["title"], alert["body"],
                                     created_at=alert.get("created_at"))
                        seen[alert["key"]] = True
        current = {str(t["id"]): t.get("status") for t in trades if t.get("id")}
        report_versions = {r["path"]: r.get("updated_at") for r in reports}
        if not self.data["initialized"]:
            self.data.update(initialized=True, trades=current, reports=report_versions)
        else:
            for trade in trades:
                key = str(trade.get("id", ""))
                if not key:
                    continue
                old = self.data["trades"].get(key)
                demo = trade.get("execution_mode") == "binance_demo"
                info = f"{trade.get('symbol')} · {trade.get('interval', '')} · {trade.get('direction', '')}\n入场参考价：{trade.get('entry_price')}\n止损：{trade.get('initial_stop_loss', trade.get('stop_loss'))}\n目标：{trade.get('target_price')}"
                if demo:
                    info = f"Binance 官方合约模拟盘 · 策略版本：{trade.get('strategy_version')}\n{trade.get('symbol')} · {trade.get('direction')}\n信号参考价：{trade.get('reference_price')}\n模拟成交均价：{trade.get('entry_price')}\n成交数量：{trade.get('executed_qty')}\n初始止损：{trade.get('initial_stop_loss')}"
                if old is None:
                    self.enqueue(
                        f"{key}:open", "signal", "💵 Binance 模拟盘开仓成交" if demo else "💵 新开仓信号（模拟）", info,
                        created_at=trade.get("opened_at"),
                    )
                if old != "closed" and trade.get("status") == "closed":
                    result = trade.get("return_pct")
                    gain = f"{result * 100:.2f}%" if isinstance(result, (int, float)) else "未知"
                    reason = trade.get("exit_reason")
                    prefix = ""
                    if reason in ("stop_loss", "protected_stop", "trailing_stop"):
                        prefix = "❌ "
                    elif reason in ("take_profit", "protection_reached"):
                        prefix = "✅ "
                    self.enqueue(
                        f"{key}:closed",
                        "signal",
                        prefix + ("Binance 模拟盘平仓" if demo else "模拟退出"),
                        f"{info}\n退出价：{trade.get('exit_price')}\n退出原因：{trade.get('exit_reason')}\n模拟收益：{gain}",
                        created_at=trade.get("closed_at"),
                    )
            for report in reports:
                path = report["path"]
                if report_versions[path] != self.data["reports"].get(path):
                    base = os.getenv("FEISHU_SITE_URL", "http://localhost:3000/").rstrip("/")
                    self.enqueue(
                        f"report:{path}:{report_versions[path]}",
                        "research",
                        "研究报告已生成或更新",
                        f"报告：{path}\n打开：{base}/reports/{quote(path, safe='/')}",
                        created_at=report.get("updated_at"),
                    )
            # Keep IDs of retained history as well, so reappearing records do not resend.
            self.data["trades"].update(current)
            self.data["reports"].update(report_versions)
        now = time.time()
        active_faults = {name.strip() for name in (fault or "").split("、") if name.strip()}
        faults = self.data.get("faults")
        if faults is None:
            # Retain already-announced modules and recovery waits in older queues.
            faults = {
                name.strip(): self.data.get("recovery_since")
                for name in (self.data.get("fault") or "").split("、")
                if name.strip()
            }
            self.data["faults"] = faults
        renamed_signal_faults = active_faults.intersection(MODULE_FAULT_LABELS.values())
        if "信号计算" in faults and "信号计算" not in active_faults and renamed_signal_faults:
            # The old label covered all these modules; renaming is not a recovery.
            recovery_since = faults.pop("信号计算")
            for name in renamed_signal_faults:
                faults.setdefault(name, recovery_since)
        new_faults = active_faults - faults.keys()
        if new_faults:
            self.enqueue(
                f"system:{time.time_ns()}",
                "system",
                "监控异常",
                f"异常模块：{'、'.join(sorted(new_faults))}。请到网站设置与诊断页查看详情。",
                created_at=now,
            )
        for name in active_faults:
            faults[name] = None
        recovered = []
        for name, recovery_since in list(faults.items()):
            if name in active_faults:
                continue
            if recovery_since is None:
                faults[name] = now
            elif now - recovery_since >= 60:
                recovered.append(name)
                del faults[name]
        if recovered:
            self.enqueue(
                f"system:{time.time_ns()}",
                "system",
                "监控恢复",
                f"恢复模块：{'、'.join(sorted(recovered))}。上述模块已持续恢复至少 60 秒。",
                created_at=now,
            )
        self.data["fault"] = "、".join(sorted(faults)) or None
        self.data["recovery_since"] = min(faults.values()) if faults and not active_faults else None
        if observation_log is not None:
            # A broken observation source must not prevent lifecycle/fault changes above.
            try:
                error = self._observe_observation_log(Path(observation_log))
            except OSError as exc:
                error = "ObservationLog" + type(exc).__name__
            self.data["observation_error"] = error or (
                "ObservationLogGap" if self.data.get("observation_gap") else None
            )
        self.save()

    def _observation_checkpoint(self, stream, path, stat, offset, *, skip_partial=False):
        start = max(0, offset - 64)
        stream.seek(start)
        anchor = hashlib.sha256(stream.read(offset - start)).hexdigest()
        stream.seek(offset)
        self.data["observation_cursor"] = {
            "path": str(path), "device": stat.st_dev, "inode": stat.st_ino,
            "offset": offset, "anchor_sha256": anchor, "skip_partial": skip_partial,
        }

    def _observe_observation_log(self, path):
        """Consume complete durable groups; checkpoint and enqueue in the same save."""
        cursor = self.data.get("observation_cursor")
        try:
            current = path.stat()
        except FileNotFoundError:
            if cursor is None:
                # The new logger may not have created its first file yet.
                self.data["observation_cursor"] = {
                    "path": str(path), "device": None, "inode": None, "offset": 0,
                }
                return None
            if (cursor.get("device"), cursor.get("inode"), cursor.get("offset")) == (None, None, 0):
                # An idle monitor can legitimately leave the source uncreated across ticks/restarts.
                return None
            return "ObservationLogMissing"
        if cursor is None:
            # Upgrade once without replaying observations captured before this consumer.
            with path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                end = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, end - 1))
                partial = bool(end and stream.read(1) != b"\n")
                self._observation_checkpoint(stream, path, stat, end, skip_partial=partial)
            return None

        identity = (cursor.get("device"), cursor.get("inode"))
        sources = [(path, current)]
        if identity != (None, None) and (
            cursor.get("path") != str(path) or identity != (current.st_dev, current.st_ino)
        ):
            # append_jsonl rotates to .1, .2, ...; drain unread retained tails first.
            retained = []
            for backup in path.parent.glob(path.name + ".*"):
                suffix = backup.name[len(path.name) + 1:]
                if suffix.isdigit() and int(suffix) > 0:
                    try:
                        retained.append((int(suffix), backup, backup.stat()))
                    except FileNotFoundError:
                        continue
            retained.sort(key=lambda item: item[0], reverse=True)
            match = next((item for item in retained if (item[2].st_dev, item[2].st_ino) == identity), None)
            if match is None or cursor.get("path") != str(path):
                # New groups can continue, but missing history remains explicitly unknown.
                self.data["observation_gap"] = True
                cursor = {"offset": 0}
            else:
                newer = [item for item in retained if item[0] < match[0]]
                if len(newer) != match[0] - 1:
                    self.data["observation_gap"] = True
                sources = [(match[1], match[2])] + [(item[1], item[2]) for item in newer] + sources

        consumed = 0
        for index, (source, expected_stat) in enumerate(sources):
            offset = cursor.get("offset", 0) if index == 0 else 0
            skip_partial = cursor.get("skip_partial", False) if index == 0 else False
            with source.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                if (stat.st_dev, stat.st_ino) != (expected_stat.st_dev, expected_stat.st_ino):
                    return "ObservationLogChanged"
                if offset > stat.st_size:
                    offset, skip_partial = 0, False
                elif offset and index == 0 and cursor.get("anchor_sha256"):
                    start = max(0, offset - 64)
                    stream.seek(start)
                    anchor = hashlib.sha256(stream.read(offset - start)).hexdigest()
                    if anchor != cursor["anchor_sha256"]:
                        # Detect copytruncate even when replacement grew past the old offset.
                        offset, skip_partial = 0, False
                stream.seek(offset)
                while consumed < OBSERVATION_READ_BUDGET_BYTES:
                    start = stream.tell()
                    line = stream.readline(OBSERVATION_MAX_LINE_BYTES + 1)
                    consumed += len(line)
                    if not line:
                        break
                    if skip_partial:
                        # Baseline landed inside an old line: discard only its remainder.
                        offset = stream.tell()
                        skip_partial = not line.endswith(b"\n")
                        continue
                    if len(line) > OBSERVATION_MAX_LINE_BYTES:
                        self._observation_checkpoint(stream, path, stat, start)
                        return "ObservationLogLineTooLarge"
                    if not line.endswith(b"\n"):
                        self._observation_checkpoint(stream, path, stat, start)
                        return "ObservationLogIncomplete" if index < len(sources) - 1 else None
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        self._observation_checkpoint(stream, path, stat, start)
                        return "InvalidObservationJSON"
                    if not isinstance(record, dict):
                        self._observation_checkpoint(stream, path, stat, start)
                        return "InvalidObservationRecord"
                    if record.get("type") == "observation_batch":
                        alerts = record.get("alerts")
                        valid = isinstance(record.get("event_key"), str) and bool(record["event_key"])
                        valid = valid and isinstance(alerts, list)
                        keys = set()
                        for alert in alerts if isinstance(alerts, list) else []:
                            if (not isinstance(alert, dict)
                                    or not all(isinstance(alert.get(field), str) and alert[field]
                                               for field in ("key", "title", "body"))
                                    or alert["key"] in keys):
                                valid = False
                                break
                            keys.add(alert["key"])
                        if not valid:
                            self._observation_checkpoint(stream, path, stat, start)
                            return "InvalidObservationBatch"
                        seen = self.data.setdefault("alerts", {})
                        for alert in alerts:
                            if alert["key"] not in seen:
                                self.enqueue(f"alert:{alert['key']}", "signal", alert["title"], alert["body"],
                                             created_at=alert.get("created_at", record.get("created_at")))
                                seen[alert["key"]] = True
                    offset = stream.tell()
                self._observation_checkpoint(stream, path, stat, offset, skip_partial=skip_partial)
                if consumed >= OBSERVATION_READ_BUDGET_BYTES:
                    return None
        return None

    def _pacing_delay(self):
        remaining = self._send_not_before_monotonic - time.monotonic()
        return remaining if remaining > 1e-9 else 0.0

    def next_due_delay(self, allowed_channels=None):
        """Earliest eligible FIFO head, including retry and shared-robot pacing."""
        heads = set()
        delays = []
        now = time.time()
        pacing_delay = self._pacing_delay()
        for message in self.data["pending"]:
            channel = message["channel"]
            if channel in heads or (allowed_channels is not None and channel not in allowed_channels):
                continue
            heads.add(channel)
            retry_at = _timestamp(message.get("next_attempt")) or 0.0
            delays.append(max(pacing_delay, retry_at - now, 0.0))
        return min(delays) if delays else None

    def delivery_timing(self, allowed_channels=None):
        """Safe bounded diagnostic window, never a complete delivery/read receipt log."""
        history = self.data["delivery_history"]
        completed = [record for record in history if record["stage"] in {"accepted", "failed"}]
        now = time.time()
        ages = [_elapsed(message.get("enqueued_at"), now) for message in self.data["pending"]]
        unknown = sum(age is None for age in ages)
        return {
            "history_limit": DELIVERY_HISTORY_LIMIT,
            "retained_count": len(history),
            "accepted_count": sum(record["stage"] == "accepted" for record in history),
            "failed_count": sum(record["stage"] == "failed" for record in history),
            "inflight_count": len(history) - len(completed),
            "pending_oldest_age_s": max(ages) if ages and not unknown else None,
            "pending_unknown_age_count": unknown,
            "min_send_interval_s": self.min_send_interval_s,
            "pacing_delay_s": self._pacing_delay(),
            "next_due_delay_s": self.next_due_delay(allowed_channels),
            "recent_attempts": [dict(record) for record in history[-20:]],
            **{field: _latency_summary(record.get(field) for record in completed)
               for field in ("queue_wait_s", "send_duration_s", "total_age_s")},
        }

    def deliver(self, sender, allowed_channels=None, *, should_stop=None):
        # Bound work per scan and preserve unsuccessful messages for later attempts.
        blocked_channels = set()
        attempts = 0
        accepted = 0
        failed = 0
        stopped = False
        for message in list(self.data["pending"]):
            channel = message["channel"]
            if allowed_channels is not None and channel not in allowed_channels:
                continue
            if channel in blocked_channels:
                continue
            if message["next_attempt"] > time.time():
                blocked_channels.add(channel)
                continue
            if attempts >= 3:
                break
            if should_stop is not None and should_stop():
                stopped = True
                break
            if self._pacing_delay() > 0:
                break
            attempts += 1
            started_at = time.time()
            started_monotonic = time.monotonic()
            self.data["send_pacing"] = {
                "last_attempt_at": started_at,
                "not_before": started_at + self.min_send_interval_s,
                "interval_s": self.min_send_interval_s,
            }
            self._send_not_before_monotonic = started_monotonic + self.min_send_interval_s
            attempt = message.get("delivery_attempts", message["attempts"]) + 1
            message["delivery_attempts"] = attempt
            # Explicit whitelist: message contents and sender/raw exception text stay out.
            record = {
                "message_id": message["message_id"], "channel": channel, "attempt": attempt,
                "stage": "attempt_started", "created_at": _timestamp(message.get("created_at")),
                "enqueued_at": _timestamp(message.get("enqueued_at")), "attempt_started_at": started_at,
                "accepted_at": None, "failed_at": None,
                "queue_wait_s": _elapsed(message.get("enqueued_at"), started_at),
                "send_duration_s": None, "total_age_s": None, "retry_delay_s": None,
                "error_class": None, "clock_skew": False,
            }
            record["clock_skew"] = any(
                a is not None and b is not None and b < a
                for a, b in ((record["created_at"], record["enqueued_at"]), (record["enqueued_at"], started_at))
            )
            self.data["delivery_history"].append(record)
            del self.data["delivery_history"][:-DELIVERY_HISTORY_LIMIT]
            # A process interruption after this point leaves an unknown, started attempt.
            self.save()
            # Disk persistence must not consume the interval between real sender calls.
            # Only a completed record can claim this actual start; the durable provisional
            # start remains unknown if the process is interrupted during the request.
            started_at = time.time()
            started_monotonic = time.monotonic()
            self.data["send_pacing"].update(
                last_attempt_at=started_at, not_before=started_at + self.min_send_interval_s
            )
            self._send_not_before_monotonic = started_monotonic + self.min_send_interval_s
            record.update(
                attempt_started_at=started_at,
                queue_wait_s=_elapsed(record["enqueued_at"], started_at),
            )
            try:
                result = sender(message["channel"], message["title"], message["body"])
                if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                    result = {"ok": False, "error_class": "InvalidSenderResult"}
            except Exception as exc:
                result = {"ok": False, "error_class": type(exc).__name__}
            duration = time.monotonic() - started_monotonic
            finished_at = time.time()
            wall_pairs = ((record["created_at"], record["enqueued_at"]),
                          (record["enqueued_at"], started_at), (started_at, finished_at))
            clock_skew = any(a is not None and b is not None and b < a for a, b in wall_pairs)
            record.update(
                stage="accepted" if result["ok"] else "failed",
                accepted_at=finished_at if result["ok"] else None,
                failed_at=finished_at if not result["ok"] else None,
                send_duration_s=duration if duration >= 0 and math.isfinite(duration) else None,
                total_age_s=None if clock_skew else _elapsed(record["created_at"], finished_at),
                queue_wait_s=None if clock_skew else record["queue_wait_s"],
                error_class=None if result["ok"] else _error_class(result.get("error_class")),
                clock_skew=clock_skew,
            )
            if result["ok"]:
                accepted += 1
                record["retry_delay_s"] = 0
                self.data["pending"].remove(message)
            else:
                failed += 1
                blocked_channels.add(channel)
                message["attempts"] += 1
                record["retry_delay_s"] = min(300, 15 * 2 ** min(message["attempts"], 5))
                message["next_attempt"] = finished_at + record["retry_delay_s"]
            self.data["last_delivery"] = {
                **record, "ok": result["ok"], "at": finished_at,
                "error": None if result["ok"] else f"飞书发送失败（{record['error_class']}）",
            }
            self.save()
        return {
            "attempted": attempts, "accepted": accepted, "failed": failed,
            "stopped": stopped, "next_due_delay_s": self.next_due_delay(allowed_channels),
        }
