"""Best-effort Discord notifications for long-running universe backtests.

Notification delivery is deliberately isolated from the backtest.  Neither a
local event-log failure nor a Discord failure is allowed to escape this module.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from runtime_utils import append_jsonl, post_discord

DISCORD_CONTENT_LIMIT = 2_000
ONE_OFF_EVENTS = frozenset({"STARTED", "VALIDATION", "COMPLETED", "FAILED"})
SENSITIVE_KEY_PARTS = (
    "webhook",
    "token",
    "secret",
    "password",
    "api_key",
    "authorization",
)
DISCORD_WEBHOOK_PATTERN = re.compile(
    r"https://(?:canary\.|ptb\.)?(?:discord(?:app)?\.com)/api/webhooks/[^\s\"'<>]+",
    re.IGNORECASE,
)


def _env_enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_int(value: str | None, default: int) -> int:
    try:
        return max(0, int(value or default))
    except (TypeError, ValueError):
        return default


class BacktestNotifier:
    """Persist and optionally deliver low-frequency backtest notifications."""

    def __init__(
        self,
        run_id: str,
        output_dir: Path,
        *,
        enabled: bool = False,
        webhook_url: str = "",
        min_interval: int = 900,
        report_public_base_url: str = "",
        restored_state: Mapping[str, Any] | None = None,
    ) -> None:
        state = dict(restored_state or {})
        self.run_id = str(run_id)
        self.output_dir = Path(output_dir)
        self.enabled = bool(enabled and webhook_url.strip())
        self.webhook_url = webhook_url.strip()
        self.min_interval = max(0, int(min_interval))
        self.report_public_base_url = report_public_base_url.strip().rstrip("/")
        self.log_path = self.output_dir / "events.jsonl"

        self.last_sent_at = _coerce_float(state.get("last_sent_at"), 0.0)
        self.last_progress_bucket = _coerce_int(
            state.get("last_progress_bucket"), 0, minimum=0, maximum=10
        )
        self.sent_events = {
            str(value) for value in state.get("sent_events", []) if str(value)
        }
        self.warning_keys = {
            str(value) for value in state.get("warning_keys", []) if str(value)
        }
        self.resume_count = _coerce_int(state.get("resume_count"), 0, minimum=0)

    @classmethod
    def from_env(
        cls,
        run_id: str,
        output_dir: Path,
        restored_state: Mapping[str, Any] | None = None,
    ) -> BacktestNotifier:
        """Create a notifier using the dedicated webhook before the shared one."""
        dedicated = os.getenv("BACKTEST_DISCORD_WEBHOOK_URL", "").strip()
        shared = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        notifier = cls(
            run_id,
            output_dir,
            enabled=_env_enabled(os.getenv("BACKTEST_DISCORD_ENABLED", "false")),
            webhook_url=dedicated or shared,
            min_interval=_safe_int(
                os.getenv("BACKTEST_DISCORD_MIN_INTERVAL"), 900
            ),
            report_public_base_url=os.getenv(
                "REPORT_PUBLIC_BASE_URL", ""
            ).strip(),
            restored_state=restored_state,
        )
        if restored_state is not None:
            notifier.resume_count += 1
        return notifier

    def send(
        self,
        event: str,
        message: str,
        *,
        force: bool = False,
        dedupe_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        """Log an event and try to deliver it, returning whether Discord accepted it."""
        event_name = str(event).strip().upper() or "UNKNOWN"
        key = str(dedupe_key) if dedupe_key is not None else None
        if key is None and event_name in ONE_OFF_EVENTS:
            key = f"event:{event_name}"

        now = time.time()
        if key and key in self.sent_events:
            self._log(
                now,
                event_name,
                message,
                metadata,
                delivery="deduplicated",
                dedupe_key=key,
            )
            return False

        self._log(
            now,
            event_name,
            message,
            metadata,
            delivery="disabled" if not self.enabled else "pending",
            dedupe_key=key,
        )
        if not self.enabled:
            return False
        if not force and now - self.last_sent_at < self.min_interval:
            return False

        content = self._discord_content(event_name, message)
        try:
            post_discord(self.webhook_url, content)
        except Exception as exc:  # notifications must never stop a backtest
            self._log(
                time.time(),
                "DISCORD_ERROR",
                f"{type(exc).__name__}: {exc}",
                {"source_event": event_name},
                delivery="failed",
                dedupe_key=key,
            )
            return False

        self.last_sent_at = now
        if key:
            self.sent_events.add(key)
        return True

    def progress(
        self,
        completed: int,
        total: int,
        message: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        """Deliver at most one message for each newly reached 10 percent bucket."""
        if total <= 0:
            return False
        bounded_completed = min(max(0, int(completed)), int(total))
        bucket = min(10, (bounded_completed * 10) // int(total))
        # Completion has its own force-delivered event with the final report.
        if bucket <= 0 or bucket >= 10 or bucket <= self.last_progress_bucket:
            return False

        delivered = self.send(
            "PROGRESS",
            message,
            metadata={
                **dict(metadata or {}),
                "completed": bounded_completed,
                "total": int(total),
                "progress_bucket": bucket,
            },
        )
        if delivered:
            self.last_progress_bucket = bucket
        return delivered

    def warning(
        self,
        key: str,
        message: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        """Deliver a warning once per stable key, bypassing normal throttling."""
        warning_key = str(key).strip()
        if not warning_key or warning_key in self.warning_keys:
            return False
        delivered = self.send(
            "WARNING",
            message,
            force=True,
            dedupe_key=f"warning:{warning_key}",
            metadata=metadata,
        )
        if delivered:
            self.warning_keys.add(warning_key)
        return delivered

    def snapshot(self) -> dict[str, Any]:
        """Return JSON-serializable notification state for the checkpoint."""
        return {
            "last_sent_at": self.last_sent_at,
            "last_progress_bucket": self.last_progress_bucket,
            "sent_events": sorted(self.sent_events),
            "warning_keys": sorted(self.warning_keys),
            "resume_count": self.resume_count,
        }

    def build_report_url(self, report_path: Path, reports_root: Path) -> str:
        """Build the public /reports URL, encoding every relative path segment."""
        if not self.report_public_base_url:
            return ""
        try:
            relative = Path(report_path).resolve().relative_to(
                Path(reports_root).resolve()
            )
        except (OSError, ValueError):
            return ""
        encoded_path = "/".join(quote(part, safe="") for part in relative.parts)
        return f"{self.report_public_base_url}/reports/{encoded_path}"

    def _discord_content(self, event: str, message: str) -> str:
        safe_message = str(self._redact(message))
        content = (
            f"**Universe Backtest: {event}**\n"
            f"Run: `{self.run_id}`\n{safe_message}"
        )
        if len(content) <= DISCORD_CONTENT_LIMIT:
            return content
        suffix = "\n…(truncated)"
        return content[: DISCORD_CONTENT_LIMIT - len(suffix)] + suffix

    def _log(
        self,
        timestamp: float,
        event: str,
        message: str,
        metadata: Mapping[str, Any] | None,
        *,
        delivery: str,
        dedupe_key: str | None,
    ) -> None:
        payload = {
            "time": timestamp,
            "source": "backtest_notifier",
            "event": event,
            "run_id": self.run_id,
            "message": self._redact(str(message)),
            "metadata": self._redact(dict(metadata or {})),
            "delivery": delivery,
        }
        if dedupe_key:
            payload["dedupe_key"] = self._redact(dedupe_key)
        try:
            append_jsonl(self.log_path, payload)
        except Exception:
            pass

    def _redact(self, value: Any, key: str = "") -> Any:
        if any(part in key.lower() for part in SENSITIVE_KEY_PARTS):
            return "[REDACTED]"
        if isinstance(value, Mapping):
            return {
                str(item_key): self._redact(item_value, str(item_key))
                for item_key, item_value in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            redacted = value
            if self.webhook_url:
                redacted = redacted.replace(self.webhook_url, "[REDACTED]")
            return DISCORD_WEBHOOK_PATTERN.sub("[REDACTED]", redacted)
        return value


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(
    value: Any,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result
