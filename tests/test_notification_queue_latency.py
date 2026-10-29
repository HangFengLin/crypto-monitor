"""Bounded FIFO draining and restart-safe pacing without external messages."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automatic_notifications import Outbox


class NotificationQueueLatencyTest(unittest.TestCase):
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

    def advance(self, seconds):
        self.wall += seconds
        self.monotonic += seconds

    def accepted(self, *args):
        self.advance(0.4)
        return {"ok": True}

    def paced(self, interval=0.7):
        return Outbox(self.path, min_send_interval_s=interval)

    def test_five_due_messages_continue_after_the_three_attempt_batch_without_fixed_sleep(self):
        # Returning no scheduling hint or inserting a batch pause adds five seconds.
        for index in range(5):
            self.box.enqueue(str(index), "signal", str(index), "body", created_at=90)
        first = self.box.deliver(self.accepted)
        self.assertIsInstance(first, dict)
        self.assertEqual(first["attempted"], 3)
        self.assertEqual(first["next_due_delay_s"], 0)
        self.assertEqual(self.box.next_due_delay({"signal"}), 0)
        second = self.box.deliver(self.accepted)
        self.assertEqual(second["accepted"], 2)
        self.assertAlmostEqual(self.wall, 102)
        self.assertIsNone(second["next_due_delay_s"])
        self.assertEqual(self.box.data["pending"], [])

    def test_only_enabled_channel_heads_control_retry_wakeup(self):
        # A later signal cannot overtake a failed opening, or wake the worker early.
        self.box.enqueue("opening", "signal", "open", "body")
        self.box.enqueue("closing", "signal", "closed", "body")
        self.box.enqueue("report", "research", "report", "body")
        self.box.data["pending"][0].update(attempts=1, next_attempt=130)
        self.box.data["pending"][2]["next_attempt"] = 110
        self.assertEqual(self.box.next_due_delay({"signal"}), 30)
        self.assertEqual(self.box.next_due_delay({"signal", "research"}), 10)
        self.wall = 110
        result = self.box.deliver(self.accepted)
        self.assertEqual(result["attempted"], 1)
        self.assertEqual([x["title"] for x in self.box.data["pending"]], ["open", "closed"])
        self.assertAlmostEqual(result["next_due_delay_s"], 19.6)

    def test_disabled_channels_do_not_create_due_work_or_a_busy_loop(self):
        # A disabled channel with due messages must yield an idle scheduling hint.
        self.box.enqueue("one", "signal", "one", "body")
        self.assertIsNone(self.box.next_due_delay(set()))
        self.assertIsNone(self.box.next_due_delay({"research"}))
        result = self.box.deliver(lambda *args: self.fail("disabled channel sent"), set())
        self.assertEqual(result["attempted"], 0)
        self.assertIsNone(result["next_due_delay_s"])
        self.assertEqual(len(self.box.data["pending"]), 1)

    def test_global_pacing_limits_distinct_channels_and_resumes_at_exact_due_time(self):
        # Per-channel-only pacing would burst when channels share one robot.
        box = self.paced()
        box.enqueue("one", "signal", "one", "body")
        box.enqueue("two", "research", "two", "body")
        first = box.deliver(self.accepted)
        self.assertEqual(first["attempted"], 1)
        self.assertAlmostEqual(first["next_due_delay_s"], 0.3)
        self.assertEqual(box.data["pending"][0].get("delivery_attempts", 0), 0)
        self.advance(0.29)
        self.assertEqual(box.deliver(self.accepted)["attempted"], 0)
        self.advance(0.01)
        self.assertEqual(box.deliver(self.accepted)["attempted"], 1)
        self.assertEqual(box.data["pending"], [])

    def test_five_paced_messages_drain_at_the_rate_interval_without_batch_gap(self):
        # Sending the fourth item after a fixed five-second pause misses its due time.
        box = self.paced()
        for index in range(5):
            box.enqueue(str(index), "signal", str(index), "body")
        starts = []

        def sender(*args):
            starts.append(round(self.wall, 3))
            return self.accepted()

        while box.data["pending"]:
            result = box.deliver(sender)
            if result["next_due_delay_s"] is not None:
                self.advance(result["next_due_delay_s"])
        self.assertEqual(starts, [100.0, 100.7, 101.4, 102.1, 102.8])
        self.assertAlmostEqual(self.wall, 103.2)

    def test_restart_preserves_remaining_pacing_without_fabricating_source_times(self):
        # Restarting the process must not reset an active pacing reservation.
        box = self.paced()
        box.enqueue("one", "signal", "one", "body")
        box.enqueue("two", "signal", "two", "body")
        box.deliver(self.accepted)
        restored = self.paced()
        self.assertAlmostEqual(restored.next_due_delay(), 0.3)
        self.assertEqual(restored.deliver(self.accepted)["attempted"], 0)
        self.advance(0.3)
        restored.deliver(self.accepted)
        self.assertEqual([r["attempt"] for r in restored.data["delivery_history"]], [1, 1])
        self.assertTrue(all(r["created_at"] is None for r in restored.data["delivery_history"]))

    def test_monotonic_pacing_survives_wall_clock_rollback_in_the_same_process(self):
        # A wall clock adjustment must neither bypass pacing nor stall it for hours.
        box = self.paced()
        box.enqueue("one", "signal", "one", "body")
        box.enqueue("two", "signal", "two", "body")
        box.deliver(self.accepted)
        self.wall = 10
        self.assertAlmostEqual(box.next_due_delay(), 0.3)
        self.advance(0.3)
        self.assertEqual(box.deliver(self.accepted)["attempted"], 1)

    def test_restart_after_wall_clock_rollback_waits_at_most_one_full_interval(self):
        # Restoring a future absolute deadline directly could block the queue for hours.
        box = self.paced()
        box.enqueue("one", "signal", "one", "body")
        box.enqueue("two", "signal", "two", "body")
        box.deliver(self.accepted)
        self.wall = 10
        restored = self.paced()
        self.assertAlmostEqual(restored.next_due_delay(), 0.7)
        self.advance(0.7)
        self.assertEqual(restored.deliver(self.accepted)["attempted"], 1)

    def test_failure_keeps_fifo_and_retry_backoff_while_other_channels_can_be_due(self):
        # A failed opening cannot be overtaken, but must not delay a report by 30 seconds.
        box = self.paced()
        for key, channel in (("opening", "signal"), ("closing", "signal"), ("report", "research")):
            box.enqueue(key, channel, key, "body")
        sent = []

        def sender(channel, title, body):
            sent.append(title)
            self.advance(0.4)
            return {"ok": title != "opening", "error_class": "TimeoutError"}

        box.deliver(sender)
        self.assertAlmostEqual(box.next_due_delay(), 0.3)
        self.advance(0.3)
        box.deliver(sender)
        self.assertEqual(sent, ["opening", "report"])
        self.assertEqual([x["title"] for x in box.data["pending"]], ["opening", "closing"])
        self.assertAlmostEqual(box.next_due_delay(), 29.3)

    def test_crash_reserves_global_pacing_before_sender_and_keeps_unknown_attempt(self):
        # Unknown network outcomes still consume pacing and preserve retry ordering.
        box = self.paced()
        box.enqueue("one", "signal", "one", "body")

        def interrupted(*args):
            persisted = json.loads(self.path.read_text())
            self.assertEqual(persisted["send_pacing"]["last_attempt_at"], 100)
            self.assertAlmostEqual(persisted["send_pacing"]["not_before"], 100.7)
            raise SystemExit()

        with self.assertRaises(SystemExit):
            box.deliver(interrupted)
        restored = self.paced()
        self.assertEqual(restored.deliver(self.accepted)["attempted"], 0)
        self.advance(0.7)
        restored.deliver(self.accepted)
        self.assertEqual([r["stage"] for r in restored.data["delivery_history"]], ["attempt_started", "accepted"])
        self.assertEqual([r["attempt"] for r in restored.data["delivery_history"]], [1, 2])

    def test_slow_presend_fsync_does_not_spend_the_actual_sender_pacing_interval(self):
        # Reserving only before disk I/O permits two real requests almost back-to-back.
        box = self.paced()
        box.enqueue("one", "signal", "one", "body", created_at=90)
        box.enqueue("two", "research", "two", "body", created_at=90)
        starts = []
        sync_calls = [0]

        def fsync(*args):
            sync_calls[0] += 1
            if sync_calls[0] == 1:
                self.advance(0.8)

        def sender(*args):
            starts.append(self.monotonic)
            self.advance(0.05)
            return {"ok": True}

        with patch("automatic_notifications.os.fsync", side_effect=fsync):
            first = box.deliver(sender)
        self.assertEqual(first["attempted"], 1)
        self.assertAlmostEqual(first["next_due_delay_s"], 0.65)
        record = box.data["delivery_history"][0]
        self.assertAlmostEqual(record["attempt_started_at"], 100.8)
        self.assertAlmostEqual(record["queue_wait_s"], 0.8)
        self.assertAlmostEqual(record["send_duration_s"], 0.05)
        persisted = json.loads(self.path.read_text())
        self.assertAlmostEqual(persisted["send_pacing"]["last_attempt_at"], 100.8)
        self.advance(0.65)
        box.deliver(sender)
        self.assertAlmostEqual(starts[1] - starts[0], 0.7)

    def test_restart_of_unknown_send_waits_full_interval_after_slow_presend_fsync(self):
        # A provisional deadline may already have expired while the unknown request runs.
        box = self.paced()
        box.enqueue("one", "signal", "one", "body")

        def interrupted(*args):
            raise SystemExit()

        with patch("automatic_notifications.os.fsync", side_effect=lambda *args: self.advance(0.8)):
            with self.assertRaises(SystemExit):
                box.deliver(interrupted)
        restored = self.paced()
        self.assertAlmostEqual(restored.next_due_delay(), 0.7)
        self.assertEqual(restored.deliver(self.accepted)["attempted"], 0)
        self.advance(0.7)
        restored.deliver(self.accepted)
        self.assertEqual([r["attempt"] for r in restored.data["delivery_history"]], [1, 2])

    def test_stop_callback_prevents_next_attempt_without_consuming_its_counter(self):
        # Shutdown after one send must not start the remaining bounded batch.
        for index in range(3):
            self.box.enqueue(str(index), "signal", str(index), "body")
        stopped = [False]

        def sender(*args):
            stopped[0] = True
            return {"ok": True}

        result = self.box.deliver(sender, should_stop=lambda: stopped[0])
        self.assertEqual(result["attempted"], 1)
        self.assertTrue(result["stopped"])
        self.assertEqual(len(self.box.data["pending"]), 2)
        self.assertTrue(all(m.get("delivery_attempts", 0) == 0 for m in self.box.data["pending"]))

    def test_repeated_observe_skips_identical_disk_replacement_but_nested_changes_persist(self):
        # Caching a mutable dict misses nested changes; unconditional writes wear disk.
        original_inode = self.path.stat().st_ino
        self.box.observe([], [], None, [])
        self.assertEqual(self.path.stat().st_ino, original_inode)
        self.box.enqueue("one", "signal", "one", "body")
        self.box.save()
        self.box.data["pending"][0]["body"] = "changed nested value"
        self.box.save()
        self.assertEqual(json.loads(self.path.read_text())["pending"][0]["body"], "changed nested value")

    def test_failed_fsync_or_replace_does_not_mark_unsaved_changes_as_persisted(self):
        # An optimistic cache update would silently drop the next retry after I/O failure.
        for boundary in ("automatic_notifications.os.fsync", "automatic_notifications.Path.replace"):
            with self.subTest(boundary=boundary):
                self.box.data["test_marker"] = boundary
                before = self.path.read_text()
                with patch(boundary, side_effect=OSError("isolated write failure")):
                    with self.assertRaises(OSError):
                        self.box.save()
                self.assertEqual(self.path.read_text(), before)
                self.box.save()
                self.assertEqual(json.loads(self.path.read_text())["test_marker"], boundary)

    def test_first_save_upgrade_and_recreation_of_deleted_path_are_not_skipped(self):
        # Equal in-memory bytes do not prove a deleted or newly upgraded file exists.
        self.path.unlink()
        self.box.save()
        self.assertTrue(self.path.exists())
        legacy = Path(self.path.parent) / "legacy.json"
        legacy.write_text(json.dumps({"initialized": True, "trades": {}, "reports": {}, "pending": [],
                                      "fault": None, "last_delivery": None}))
        restored = Outbox(legacy)
        restored.save()
        self.assertIn("delivery_history", json.loads(legacy.read_text()))
        fresh = Path(self.path.parent) / "fresh.json"
        Outbox(fresh).save()
        self.assertTrue(fresh.exists())

    def test_scheduler_restart_does_not_reenqueue_already_seen_source_events(self):
        # Persisting scheduler metadata must not replace or reset existing dedupe state.
        alert = {"key": "new", "title": "title", "body": "body", "created_at": 90}
        self.box.observe([], [], None, [alert])
        self.box.deliver(self.accepted)
        restored = self.paced()
        restored.observe([], [], None, [alert])
        self.assertEqual(restored.data["pending"], [])
        self.assertEqual(len(restored.data["delivery_history"]), 1)
