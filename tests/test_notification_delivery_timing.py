"""Offline delivery timing: real outbox persistence, no external messages."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
import feishu_notifications
from automatic_notifications import Outbox


class NotificationDeliveryTimingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "outbox.json"
        self.wall = 100.0
        self.monotonic = 10.0
        for replacement in (
            patch("automatic_notifications.time.time", side_effect=lambda: self.wall),
            patch("automatic_notifications.time.monotonic", side_effect=lambda: self.monotonic),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.box = Outbox(self.path)
        self.box.observe([], [], None, [])

    def test_success_links_source_enqueue_and_attempt_without_recording_message_content(self):
        # Missing pre-send persistence, wall/monotonic confusion or raw IDs break this.
        self.box.enqueue("private-event-key", "signal", "private-title", "private-body", created_at=90)
        self.wall = 105.0

        def accepted(channel, title, body):
            persisted = json.loads(self.path.read_text())["delivery_history"][-1]
            self.assertEqual(persisted["stage"], "attempt_started")
            self.assertEqual(persisted["attempt_started_at"], 105.0)
            self.wall = 107.5
            self.monotonic = 10.75
            return {"ok": True}

        self.box.deliver(accepted)
        restored = Outbox(self.path)
        record = restored.data["delivery_history"][-1]
        self.assertEqual(record["stage"], "accepted")
        self.assertEqual(record["attempt"], 1)
        self.assertEqual(record["created_at"], 90)
        self.assertEqual(record["enqueued_at"], 100)
        self.assertEqual(record["accepted_at"], 107.5)
        self.assertIsNone(record["failed_at"])
        self.assertEqual(record["queue_wait_s"], 5)
        self.assertEqual(record["send_duration_s"], 0.75)
        self.assertEqual(record["total_age_s"], 17.5)
        self.assertEqual(record["retry_delay_s"], 0)
        self.assertEqual(restored.data["last_delivery"]["message_id"], record["message_id"])
        self.assertEqual(restored.data["pending"], [])
        history = json.dumps(restored.data["delivery_history"])
        for private in ("private-event-key", "private-title", "private-body", "http"):
            self.assertNotIn(private, history)

    def test_sender_exception_records_safe_failure_and_preserves_fifo_across_restart(self):
        # Escaping exceptions, losing retries, or logging raw exception text break this.
        self.box.enqueue("opening", "signal", "open", "body", created_at=95)
        self.box.enqueue("closing", "signal", "closed", "body", created_at=96)

        def failed(*args):
            self.wall = 102.0
            self.monotonic = 10.5
            raise TimeoutError("https://private-webhook/secret-token")

        self.box.deliver(failed)
        restored = Outbox(self.path)
        failure = restored.data["delivery_history"][-1]
        self.assertEqual(failure["stage"], "failed")
        self.assertEqual(failure["error_class"], "TimeoutError")
        self.assertEqual(failure["retry_delay_s"], 30)
        self.assertEqual(failure["failed_at"], 102)
        self.assertEqual(failure["send_duration_s"], 0.5)
        self.assertEqual([m["title"] for m in restored.data["pending"]], ["open", "closed"])
        self.assertNotIn("secret-token", json.dumps(restored.data))
        self.wall = 131
        restored.deliver(lambda *args: self.fail("retry ran before its deadline"))
        self.wall = 132
        restored.deliver(lambda *args: {"ok": True})
        records = restored.data["delivery_history"]
        self.assertEqual([r["stage"] for r in records], ["failed", "accepted", "accepted"])
        self.assertEqual([r["attempt"] for r in records], [1, 2, 1])
        self.assertEqual(records[0]["message_id"], records[1]["message_id"])
        self.assertNotEqual(records[1]["message_id"], records[2]["message_id"])
        self.assertEqual(restored.data["pending"], [])

    def test_legacy_pending_preserves_unknown_times_without_replaying_seen_history(self):
        # Using upgrade time as enqueue/source time fabricates latency for old queues.
        self.path.write_text(json.dumps({
            "initialized": True, "trades": {"old-trade": "open"}, "reports": {},
            "alerts": {"old-alert": True}, "fault": None, "last_delivery": None,
            "pending": [{"key": "old-trade:open", "channel": "signal", "title": "old",
                         "body": "old", "attempts": 2, "next_attempt": 0}],
        }))
        restored = Outbox(self.path)
        restored.observe([{"id": "old-trade", "status": "open"}], [], None,
                         [{"key": "old-alert", "title": "old", "body": "old"}])
        summary = restored.delivery_timing()
        self.assertIsNone(summary["pending_oldest_age_s"])
        self.assertEqual(summary["pending_unknown_age_count"], 1)
        restored.deliver(lambda *args: {"ok": True})
        record = restored.data["delivery_history"][-1]
        self.assertEqual(record["attempt"], 3)
        for field in ("created_at", "enqueued_at", "queue_wait_s", "total_age_s"):
            self.assertIsNone(record[field], field)
        self.assertEqual(restored.data["pending"], [])

    def test_interrupted_attempt_stays_unknown_after_restart_and_retry(self):
        # A process interruption must never be reported as a successful acceptance.
        self.box.enqueue("interrupted", "signal", "title", "body", created_at=90)
        with self.assertRaises(SystemExit):
            self.box.deliver(lambda *args: (_ for _ in ()).throw(SystemExit()))
        restored = Outbox(self.path)
        restored.deliver(lambda *args: {"ok": True})
        summary = restored.delivery_timing()
        self.assertEqual([r["stage"] for r in summary["recent_attempts"]], ["attempt_started", "accepted"])
        self.assertEqual(summary["recent_attempts"][1]["attempt"], 2)
        self.assertEqual(summary["inflight_count"], 1)
        self.assertEqual(summary["accepted_count"], 1)
        self.assertEqual(summary["send_duration_s"]["count"], 1)
        self.assertIsNone(summary["recent_attempts"][0]["accepted_at"])

    def test_backward_wall_clock_marks_unknown_age_but_keeps_monotonic_send_duration(self):
        # Clamping negative wall-clock ages to zero conceals a clock adjustment.
        self.box.enqueue("clock-change", "system", "title", "body", created_at=100)
        self.wall = 90

        def accepted(*args):
            self.wall = 92
            self.monotonic = 10.25
            return {"ok": True}

        self.box.deliver(accepted)
        record = self.box.data["delivery_history"][-1]
        self.assertTrue(record["clock_skew"])
        self.assertIsNone(record["queue_wait_s"])
        self.assertIsNone(record["total_age_s"])
        self.assertEqual(record["send_duration_s"], 0.25)

    def test_clock_skew_is_persisted_even_when_the_attempt_never_finishes(self):
        # A crash must not erase clock skew already known before the request began.
        self.box.enqueue("clock-crash", "signal", "title", "body", created_at=95)
        self.wall = 90
        with self.assertRaises(SystemExit):
            self.box.deliver(lambda *args: (_ for _ in ()).throw(SystemExit()))
        record = Outbox(self.path).delivery_timing()["recent_attempts"][-1]
        self.assertTrue(record["clock_skew"])
        self.assertIsNone(record["queue_wait_s"])

    def test_percentiles_describe_only_measured_completed_attempts(self):
        # Wrong rank/order or averaging percentiles misreports the retained window.
        for index, duration in enumerate((2.0, 1.0, 3.0, 4.0)):
            self.box.enqueue(str(index), "signal", "title", "body", created_at=90)

            def accepted(*args, duration=duration):
                self.monotonic += duration
                return {"ok": True}

            self.box.deliver(accepted)
        summary = Outbox(self.path).delivery_timing()
        self.assertEqual(summary["send_duration_s"], {"count": 4, "p50": 2.0, "p95": 4.0, "max": 4.0})

    def test_source_times_survive_observe_for_alert_trade_report_and_system_messages(self):
        # Dropping original event times in any observer silently measures only queue time.
        self.box.observe([{"id": "trade", "status": "open", "opened_at": 80}],
                         [{"path": "report.html", "updated_at": 85}], "行情",
                         [{"key": "alert", "title": "alert", "body": "body", "created_at": 75}])
        messages = self.box.data["pending"]
        self.assertEqual([m["created_at"] for m in messages], [75, 80, 85, 100])
        self.box.observe([{"id": "trade", "status": "closed", "opened_at": 80, "closed_at": 99}],
                         [{"path": "report.html", "updated_at": 85}], "行情", [])
        self.assertEqual(self.box.data["pending"][-1]["created_at"], 99)

    def test_retained_attempt_window_and_status_output_are_bounded_and_redacted(self):
        # Unbounded history or exposing sender fields makes diagnosis itself expensive/leaky.
        for index in range(205):
            self.box.enqueue(f"private-key-{index}", "signal", "private-title", "private-body", created_at=90)
            self.box.deliver(lambda *args: {"ok": False, "error": "https://private-webhook/token",
                                          "error_class": "TimeoutError", "body": "private-sender-body"})
            # Clear only this isolated fixture's blocked item to exercise the retained window.
            self.box.data["pending"].clear()
            self.wall += 1
        restored = Outbox(self.path)
        summary = restored.delivery_timing()
        self.assertEqual(summary["retained_count"], 200)
        self.assertEqual(len(summary["recent_attempts"]), 20)
        self.assertEqual(summary["failed_count"], 200)
        self.assertEqual(summary["queue_wait_s"]["p95"], 0)
        self.assertEqual(summary["total_age_s"]["max"], 214)
        safe = json.dumps({"history": restored.data["delivery_history"], "last": restored.data["last_delivery"],
                           "summary": summary})
        for private in ("private-key", "private-title", "private-body", "private-webhook", "token", "private-sender-body"):
            self.assertNotIn(private, safe)

    def test_pending_age_and_latency_summary_do_not_treat_missing_or_inflight_as_zero(self):
        # The oldest pending item and percentile sample counts must reflect actual evidence.
        self.box.enqueue("first", "signal", "one", "body", created_at=90)
        self.wall = 110
        self.box.enqueue("second", "research", "two", "body", created_at=105)
        self.wall = 125
        summary = self.box.delivery_timing()
        self.assertEqual(summary["pending_oldest_age_s"], 25)
        self.assertEqual(summary["pending_unknown_age_count"], 0)
        self.assertEqual(summary["send_duration_s"]["count"], 0)
        self.assertIsNone(summary["send_duration_s"]["p95"])

    def test_app_preserves_alert_source_time_and_publishes_safe_timing_summary(self):
        # Forgetting either app integration produces apparently timed but untraceable status.
        with patch.object(app, "state", app.MonitorState()), \
             patch.object(app, "list_report_files", return_value=[]), \
             patch.object(app, "site_monitor_snapshot", return_value={"enabled": False}):
            app.state.events = [{"type": "indicator", "event_key": "new", "created_at": 78,
                                 "symbol": "BTCUSDT", "interval": "15m", "price": 100}]
            alerts = app.automatic_notification_inputs()[3]
            self.assertEqual(alerts[0]["created_at"], 78)

        async def run():
            stop = asyncio.Event()

            def accepted(*args):
                stop.set()
                return {"ok": True}

            self.box.enqueue("one", "signal", "private-title", "private-body", created_at=90)
            settings = SimpleNamespace(get=lambda: {"values": {"notifications": {
                "enabled": True, "signal": True, "system": True, "research": True,
            }}})
            with patch.object(app, "automatic_notification_inputs", return_value=([], [], None, [])), \
                 patch.object(app, "RUNTIME_SETTINGS", settings), \
                 patch.object(app, "automatic_notification_status", {}), \
                 patch.object(feishu_notifications, "send", side_effect=accepted):
                await app.automatic_notification_loop(stop, self.box)
                route = next(r for r in app.create_app().routes if r.path == "/api/notifications/status")
                payload = await route.endpoint()
                self.assertEqual(payload["automatic"]["delivery_timing"]["accepted_count"], 1)
                self.assertNotIn("private-body", json.dumps(payload))

        asyncio.run(run())


class FeishuTransportTimingTest(unittest.TestCase):
    def test_transport_reports_acceptance_duration_and_sanitized_failure_class(self):
        # Reporting call-start as acceptance, or persisting raw transport errors, breaks this.
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"code":0}'

        wall = [100.0]
        monotonic = [5.0]

        def request(*args, **kwargs):
            wall[0] = 102.0
            monotonic[0] = 5.4
            return Response()

        with patch.object(feishu_notifications, "webhook", return_value="https://open.feishu.cn/open-apis/bot/v2/hook/00000000-0000-0000-0000-000000000000"), \
             patch.object(feishu_notifications.time, "time", side_effect=lambda: wall[0]), \
             patch.object(feishu_notifications.time, "monotonic", side_effect=lambda: monotonic[0]), \
             patch.object(feishu_notifications.urllib.request, "urlopen", side_effect=request):
            result = feishu_notifications.send("signal", "title", "body")
            self.assertTrue(result["ok"])
            self.assertEqual(result["attempt_started_at"], 100)
            self.assertEqual(result["accepted_at"], 102)
            self.assertAlmostEqual(result["send_duration_s"], 0.4)
            with patch.object(feishu_notifications.urllib.request, "urlopen", side_effect=TimeoutError("private-url-token")):
                failure = feishu_notifications.send("signal", "title", "body")
            self.assertEqual(failure["error_class"], "TimeoutError")
            self.assertIsNone(failure["accepted_at"])
            self.assertEqual(failure["failed_at"], 102)
            self.assertNotIn("private-url-token", json.dumps(failure))
