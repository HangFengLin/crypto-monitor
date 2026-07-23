from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backtest_notifier import DISCORD_CONTENT_LIMIT, BacktestNotifier


class BacktestNotifierTest(unittest.TestCase):
    def test_dedicated_webhook_has_priority_and_shared_webhook_is_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "BACKTEST_DISCORD_ENABLED": "true",
                "BACKTEST_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/dedicated/value",
                "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/shared/value",
            }
            with patch.dict(os.environ, env, clear=True):
                notifier = BacktestNotifier.from_env("run", Path(tmpdir))
            self.assertTrue(notifier.enabled)
            self.assertEqual(notifier.webhook_url, env["BACKTEST_DISCORD_WEBHOOK_URL"])

            env["BACKTEST_DISCORD_WEBHOOK_URL"] = ""
            with patch.dict(os.environ, env, clear=True):
                fallback = BacktestNotifier.from_env("run", Path(tmpdir))
            self.assertEqual(fallback.webhook_url, env["DISCORD_WEBHOOK_URL"])

    def test_disabled_by_default_even_when_shared_webhook_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/shared/value"},
                clear=True,
            ):
                notifier = BacktestNotifier.from_env("run", Path(tmpdir))
            with patch("backtest_notifier.post_discord") as post:
                self.assertFalse(notifier.send("STARTED", "hello", force=True))
            post.assert_not_called()

    def test_progress_uses_ten_percent_buckets_and_rate_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            notifier = BacktestNotifier(
                "run", Path(tmpdir), enabled=True, webhook_url="https://example.test", min_interval=900
            )
            with patch("backtest_notifier.post_discord") as post, patch(
                "backtest_notifier.time.time", side_effect=[1_000.0, 1_100.0, 2_000.0]
            ):
                self.assertTrue(notifier.progress(10, 100, "10%"))
                self.assertFalse(notifier.progress(20, 100, "20% throttled"))
                self.assertTrue(notifier.progress(30, 100, "30%"))
            self.assertEqual(post.call_count, 2)
            self.assertEqual(notifier.last_progress_bucket, 3)
            deliveries = [
                json.loads(line)["delivery"]
                for line in notifier.log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(deliveries, ["sent", "throttled", "sent"])

            with patch("backtest_notifier.post_discord") as post:
                self.assertFalse(notifier.progress(100, 100, "100%"))
            post.assert_not_called()

    def test_warning_and_one_off_events_are_deduplicated_after_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            notifier = BacktestNotifier(
                "run", Path(tmpdir), enabled=True, webhook_url="https://example.test", min_interval=900
            )
            with patch("backtest_notifier.post_discord") as post:
                self.assertTrue(notifier.warning("rate-limit", "limited"))
                self.assertFalse(notifier.warning("rate-limit", "again"))
                self.assertTrue(notifier.send("COMPLETED", "done", force=True))
            self.assertEqual(post.call_count, 2)

            state = notifier.snapshot()
            restored = BacktestNotifier(
                "run",
                Path(tmpdir),
                enabled=True,
                webhook_url="https://example.test",
                restored_state=state,
            )
            with patch("backtest_notifier.post_discord") as post:
                self.assertFalse(restored.warning("rate-limit", "again"))
                self.assertFalse(restored.send("COMPLETED", "again", force=True))
            post.assert_not_called()
            self.assertEqual(restored.snapshot(), state)

    def test_from_env_counts_resume_without_losing_notification_state(self) -> None:
        state = {
            "last_sent_at": 100.0,
            "last_progress_bucket": 4,
            "sent_events": ["event:STARTED"],
            "warning_keys": ["memory"],
            "resume_count": 2,
        }
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {}, clear=True):
            notifier = BacktestNotifier.from_env("run", Path(tmpdir), state)
        self.assertEqual(notifier.resume_count, 3)
        self.assertEqual(notifier.last_progress_bucket, 4)

    def test_report_url_encodes_relative_path_and_rejects_outside_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            reports = Path(tmpdir) / "reports"
            report = reports / "run with space" / "报告.html"
            notifier = BacktestNotifier(
                "run", reports, report_public_base_url="http://119.28.142.113/"
            )
            self.assertEqual(
                notifier.build_report_url(report, reports),
                "http://119.28.142.113/reports/run%20with%20space/%E6%8A%A5%E5%91%8A.html",
            )
            self.assertEqual(notifier.build_report_url(Path(tmpdir) / "other.html", reports), "")

    def test_content_is_truncated_and_webhooks_are_redacted_from_delivery_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            webhook = "https://discord.com/api/webhooks/123/very-secret"
            other_webhook = "https://discord.com/api/webhooks/456/also-secret"
            notifier = BacktestNotifier(
                "run", Path(tmpdir), enabled=True, webhook_url=webhook
            )
            with patch("backtest_notifier.post_discord") as post:
                notifier.send(
                    "STARTED",
                    f"{webhook} {other_webhook} " + "x" * 3_000,
                    force=True,
                    metadata={"api_token": "do-not-log", "nested": {"password": "no"}},
                )
            delivered = post.call_args.args[1]
            self.assertLessEqual(len(delivered), DISCORD_CONTENT_LIMIT)
            self.assertNotIn("very-secret", delivered)
            self.assertNotIn("also-secret", delivered)
            log_text = notifier.log_path.read_text(encoding="utf-8")
            self.assertNotIn("very-secret", log_text)
            self.assertNotIn("also-secret", log_text)
            self.assertNotIn("do-not-log", log_text)
            self.assertNotIn('"password":"no"', log_text)

    def test_network_and_event_log_failures_never_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            notifier = BacktestNotifier(
                "run", Path(tmpdir), enabled=True, webhook_url="https://example.test"
            )
            with patch("backtest_notifier.append_jsonl", side_effect=OSError("disk")), patch(
                "backtest_notifier.post_discord", side_effect=OSError("network")
            ):
                self.assertFalse(notifier.send("STARTED", "hello", force=True))

    def test_network_failure_is_logged_as_failed_without_pending_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            notifier = BacktestNotifier(
                "run", Path(tmpdir), enabled=True, webhook_url="https://example.test"
            )
            with patch("backtest_notifier.post_discord", side_effect=OSError("network")):
                self.assertFalse(notifier.send("STARTED", "hello", force=True))
            rows = [
                json.loads(line)
                for line in notifier.log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["delivery"] for row in rows], ["failed"])
            self.assertNotIn("pending", notifier.log_path.read_text(encoding="utf-8"))

    def test_events_log_is_valid_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            notifier = BacktestNotifier("run", Path(tmpdir))
            notifier.send("INTERRUPTED", "checkpoint saved", force=True)
            rows = [
                json.loads(line)
                for line in notifier.log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(rows[0]["event"], "INTERRUPTED")
            self.assertEqual(rows[0]["delivery"], "disabled")


if __name__ == "__main__":
    unittest.main()
