"""Replay completed observation batches through a private durable outbox."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automatic_notifications import Outbox


class ObservationOutboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.log = self.folder / "events.jsonl"
        self.path = self.folder / "outbox.json"
        self.box = Outbox(self.path)

    def observe(self, trades=None, fault=None):
        try:
            self.box.observe(trades or [], [], fault, [], self.log)
        except TypeError as error:
            if "positional" in str(error):
                self.fail("Outbox cannot consume the durable observation log yet")
            raise

    def append(self, record):
        with self.log.open("ab") as stream:
            stream.write((json.dumps(record, ensure_ascii=False) + "\n").encode())

    def batch(self, suffix="one", body="15m：均线上穿\n1h：接近支撑"):
        return {
            "type": "observation_batch", "event_key": f"observation-round:{suffix}", "created_at": 100,
            "alerts": [{"key": f"observation:{suffix}:ETHUSDT", "title": "ETHUSDT 观察提醒",
                        "body": body, "created_at": 90}],
        }

    def test_first_upgrade_baselines_existing_log_without_replaying_history(self):
        self.box.observe([], [], None)
        self.append(self.batch("old"))
        self.observe()
        self.assertEqual(self.box.data["pending"], [])
        self.append(self.batch("new"))
        self.observe()
        self.assertEqual([message["key"] for message in self.box.data["pending"]],
                         ["alert:observation:new:ETHUSDT"])

    def test_missing_initial_file_can_be_created_later_without_losing_first_batch(self):
        self.observe()
        self.append(self.batch())
        self.observe()
        self.assertEqual([message["title"] for message in self.box.data["pending"]], ["ETHUSDT 观察提醒"])
        self.assertIsNone(self.box.data.get("observation_error"))

    def test_repeated_wait_for_never_created_log_is_healthy_and_preserves_first_batch(self):
        self.observe()
        self.box = Outbox(self.path)
        self.observe()
        self.observe()
        self.assertIsNone(self.box.data.get("observation_error"))
        self.assertEqual(self.box.data["pending"], [])
        self.append(self.batch())
        self.observe()
        self.assertEqual([message["key"] for message in self.box.data["pending"]],
                         ["alert:observation:one:ETHUSDT"])

    def test_all_symbol_groups_enqueue_once_across_repeated_scans_and_restart(self):
        self.observe()
        batch = self.batch()
        batch["alerts"].append({"key": "observation:one:BTCUSDT", "title": "BTCUSDT 观察提醒",
                                "body": "4h：MACD上穿", "created_at": 95})
        self.append(batch)
        self.observe()
        self.box = Outbox(self.path)
        self.observe()
        self.assertEqual([message["key"] for message in self.box.data["pending"]],
                         ["alert:observation:one:ETHUSDT", "alert:observation:one:BTCUSDT"])
        self.assertEqual([message["created_at"] for message in self.box.data["pending"]], [90, 95])
        self.assertIsNone(self.box.data.get("observation_error"))

    def test_failed_group_retry_survives_restart_without_splitting_or_reenqueueing(self):
        self.observe()
        self.append(self.batch())
        self.observe()
        before = dict(self.box.data["pending"][0])
        self.box.deliver(lambda *args: {"ok": False})
        self.box = Outbox(self.path)
        self.observe()
        self.assertEqual(len(self.box.data["pending"]), 1)
        self.assertEqual(self.box.data["pending"][0]["body"], before["body"])
        self.assertEqual(self.box.data["pending"][0]["attempts"], 1)
        self.box.data["pending"][0]["next_attempt"] = 0
        calls = []
        self.box.deliver(lambda channel, title, body: calls.append((title, body)) or {"ok": True})
        self.assertEqual(calls, [(before["title"], before["body"])])
        self.box = Outbox(self.path)
        self.observe()
        self.assertEqual(self.box.data["pending"], [])

    def test_more_than_fifty_observations_remain_in_the_complete_group_body(self):
        self.observe()
        body = "\n".join(f"观察项目{index}：独立原始信号" for index in range(80))
        self.append(self.batch(body=body))
        self.observe()
        self.assertEqual(len(self.box.data["pending"]), 1)
        self.assertTrue(self.box.data["pending"][0]["body"].startswith(body + "\n"))

    def test_partial_final_line_is_not_consumed_until_the_newline_arrives(self):
        self.observe()
        encoded = json.dumps(self.batch(), ensure_ascii=False).encode()
        split = encoded.index("观察".encode()) + 1
        self.log.write_bytes(encoded[:split])
        self.observe()
        self.assertEqual(self.box.data["pending"], [])
        self.assertIsNone(self.box.data.get("observation_error"))
        self.box = Outbox(self.path)
        with self.log.open("ab") as stream:
            stream.write(encoded[split:] + b"\n")
        self.observe()
        self.assertEqual(len(self.box.data["pending"]), 1)

    def test_raw_events_advance_cursor_and_are_not_sent_as_batches(self):
        self.observe()
        self.append({"type": "support", "event_key": "raw", "symbol": "ETHUSDT"})
        self.append(self.batch())
        self.observe()
        self.assertEqual(len(self.box.data["pending"]), 1)
        self.assertEqual(self.box.data["observation_cursor"]["offset"], self.log.stat().st_size)

    def test_polling_reads_only_the_new_tail_and_a_bounded_history_anchor(self):
        self.append({"type": "historical_event", "body": "长历史" * 10000})
        self.observe()
        boundary = self.log.stat().st_size
        self.append(self.batch("new"))
        original_open = Path.open

        class TailReader:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def read(self, size=-1):
                if size < 0 or size > 64:
                    raise AssertionError("polling reread unbounded old history")
                return self.stream.read(size)

            def readline(self, size=-1):
                if self.stream.tell() < boundary:
                    raise AssertionError("polling reparsed the historical prefix")
                return self.stream.readline(size)

        def tail_only_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return TailReader(stream) if path == self.log else stream

        with patch("automatic_notifications.Path.open", new=tail_only_open):
            self.observe()
            self.observe()
        self.assertEqual([message["key"] for message in self.box.data["pending"]],
                         ["alert:observation:new:ETHUSDT"])

    def test_bounded_scan_resumes_remaining_groups_after_restart(self):
        self.observe()
        self.append(self.batch("first"))
        self.append(self.batch("second"))
        with patch("automatic_notifications.OBSERVATION_READ_BUDGET_BYTES", 1):
            self.observe()
        self.assertEqual([message["key"] for message in self.box.data["pending"]],
                         ["alert:observation:first:ETHUSDT"])
        self.box = Outbox(self.path)
        self.observe()
        self.assertEqual([message["key"] for message in self.box.data["pending"]],
                         ["alert:observation:first:ETHUSDT", "alert:observation:second:ETHUSDT"])

    def test_same_inode_truncation_reloads_from_zero_and_deduplicates_old_groups(self):
        self.observe()
        self.append(self.batch("old", body="长历史" * 300))
        self.observe()
        self.log.write_bytes(b"")
        self.append(self.batch("old"))
        self.append(self.batch("new"))
        self.observe()
        self.assertEqual([message["key"] for message in self.box.data["pending"]],
                         ["alert:observation:old:ETHUSDT", "alert:observation:new:ETHUSDT"])

    def test_rotation_reads_new_file_from_zero_and_preserves_pending_groups(self):
        self.observe()
        self.append(self.batch("old"))
        self.observe()
        self.log.rename(self.folder / "events.jsonl.1")
        self.append(self.batch("old"))
        self.append(self.batch("new"))
        self.observe()
        self.box = Outbox(self.path)
        self.observe()
        self.assertEqual([message["key"] for message in self.box.data["pending"]],
                         ["alert:observation:old:ETHUSDT", "alert:observation:new:ETHUSDT"])

    def test_copytruncate_and_regrow_past_saved_offset_does_not_skip_new_groups(self):
        self.observe()
        self.append(self.batch("old"))
        self.observe()
        self.log.write_bytes(b"")
        self.append({"type": "support", "extra": "replacement" * 500})
        self.append(self.batch("new"))
        self.observe()
        self.assertEqual([message["key"] for message in self.box.data["pending"]],
                         ["alert:observation:old:ETHUSDT", "alert:observation:new:ETHUSDT"])

    def test_rotation_drains_unread_old_file_before_the_new_file(self):
        self.observe()
        self.append(self.batch("first"))
        self.observe()
        self.append(self.batch("unread"))
        self.log.rename(self.folder / "events.jsonl.1")
        self.append(self.batch("new"))
        self.observe()
        self.assertEqual([message["key"] for message in self.box.data["pending"]],
                         ["alert:observation:first:ETHUSDT", "alert:observation:unread:ETHUSDT",
                          "alert:observation:new:ETHUSDT"])
        self.assertIsNone(self.box.data.get("observation_error"))

    def test_multiple_retained_rotations_drain_in_chronological_order(self):
        self.observe()
        self.append(self.batch("first"))
        self.observe()
        self.append(self.batch("unread"))
        self.log.rename(self.folder / "events.jsonl.2")
        self.append(self.batch("middle"))
        self.log.rename(self.folder / "events.jsonl.1")
        self.append(self.batch("new"))
        self.box = Outbox(self.path)
        self.observe()
        self.assertEqual([message["key"] for message in self.box.data["pending"]],
                         ["alert:observation:first:ETHUSDT", "alert:observation:unread:ETHUSDT",
                          "alert:observation:middle:ETHUSDT", "alert:observation:new:ETHUSDT"])

    def test_real_event_logger_rotation_preserves_unread_completed_groups(self):
        from runtime_utils import append_jsonl

        self.observe()
        append_jsonl(self.log, self.batch("first"), max_bytes=100000, backups=5)
        self.observe()
        append_jsonl(self.log, self.batch("unread"), max_bytes=100000, backups=5)
        append_jsonl(self.log, self.batch("new"), max_bytes=1, backups=5)
        self.observe()
        self.assertEqual([message["key"] for message in self.box.data["pending"]],
                         ["alert:observation:first:ETHUSDT", "alert:observation:unread:ETHUSDT",
                          "alert:observation:new:ETHUSDT"])

    def test_failed_queue_checkpoint_replays_the_batch_after_restart_without_loss(self):
        self.observe()
        self.append(self.batch())
        original_replace = Path.replace

        def fail_outbox_replace(path, target):
            if target == self.path:
                raise OSError("simulated interrupted outbox persistence")
            return original_replace(path, target)

        with patch("automatic_notifications.Path.replace", new=fail_outbox_replace):
            with self.assertRaises(OSError):
                self.observe()
        self.box = Outbox(self.path)
        self.observe()
        self.assertEqual([message["key"] for message in self.box.data["pending"]],
                         ["alert:observation:one:ETHUSDT"])

    def test_missing_rotated_inode_surfaces_a_gap_instead_of_claiming_complete_recovery(self):
        self.observe()
        self.append(self.batch("old"))
        self.observe()
        self.log.rename(self.folder / "unretained-old-file")
        self.append(self.batch("new"))
        self.observe()
        self.assertEqual(self.box.data.get("observation_error"), "ObservationLogGap")
        self.assertEqual([message["key"] for message in self.box.data["pending"]],
                         ["alert:observation:old:ETHUSDT", "alert:observation:new:ETHUSDT"])
        self.box = Outbox(self.path)
        self.observe()
        self.assertEqual(self.box.data.get("observation_error"), "ObservationLogGap")

    def test_missing_log_after_initialization_preserves_cursor_and_lifecycle_notifications(self):
        self.observe()
        self.append(self.batch())
        self.observe()
        cursor = dict(self.box.data["observation_cursor"])
        self.log.unlink()
        self.observe([{"id": "trade-one", "symbol": "BTCUSDT", "status": "open"}], fault="行情")
        self.assertEqual(self.box.data["observation_cursor"], cursor)
        self.assertIsNotNone(self.box.data.get("observation_error"))
        self.assertEqual([message["channel"] for message in self.box.data["pending"]],
                         ["signal", "signal", "system"])
        self.assertEqual(self.box.data["pending"][1]["key"], "trade-one:open")

    def test_removing_previously_observed_empty_log_still_reports_missing_source(self):
        self.log.write_bytes(b"")
        self.observe()
        cursor = dict(self.box.data["observation_cursor"])
        self.log.unlink()
        self.observe()
        self.assertEqual(self.box.data.get("observation_error"), "ObservationLogMissing")
        self.assertEqual(self.box.data["observation_cursor"], cursor)

    def test_read_error_is_sanitized_and_does_not_prevent_new_open_notification(self):
        self.observe()
        self.append(self.batch())
        original_open = Path.open

        def selective_open(path, *args, **kwargs):
            if path == self.log:
                raise PermissionError("PRIVATE filesystem detail")
            return original_open(path, *args, **kwargs)

        with patch("automatic_notifications.Path.open", new=selective_open):
            self.observe([{"id": "trade-one", "status": "open"}])
        self.assertEqual([message["key"] for message in self.box.data["pending"]], ["trade-one:open"])
        self.assertNotIn("PRIVATE", self.box.data["observation_error"])
        self.observe([{"id": "trade-one", "status": "open"}])
        self.assertEqual(len(self.box.data["pending"]), 2)
        self.assertIsNone(self.box.data.get("observation_error"))

    def test_malformed_json_blocks_that_line_without_losing_later_batch_or_lifecycle(self):
        self.observe()
        self.log.write_bytes(b"{broken}\n")
        self.append(self.batch())
        self.observe([{"id": "trade-one", "status": "open"}])
        self.assertEqual([message["key"] for message in self.box.data["pending"]], ["trade-one:open"])
        self.assertIsNotNone(self.box.data.get("observation_error"))
        self.assertEqual(self.box.data["observation_cursor"]["offset"], 0)
        self.log.write_bytes(b"")
        self.append({"type": "corrected_raw_event"})
        self.append(self.batch())
        self.observe([{"id": "trade-one", "status": "open"}])
        self.assertEqual(len(self.box.data["pending"]), 2)
        self.assertIsNone(self.box.data.get("observation_error"))

    def test_invalid_batch_is_validated_before_any_child_is_enqueued(self):
        self.observe()
        record = self.batch()
        record["alerts"].append({"key": "bad", "title": "missing body"})
        self.append(record)
        self.observe()
        self.assertEqual(self.box.data["pending"], [])
        self.assertIsNotNone(self.box.data.get("observation_error"))
        self.assertEqual(self.box.data["observation_cursor"]["offset"], 0)

    def test_baseline_inside_partial_historical_line_skips_only_its_completion(self):
        old = json.dumps(self.batch("old"), ensure_ascii=False).encode()
        split = len(old) // 2
        self.log.write_bytes(old[:split])
        self.observe()
        with self.log.open("ab") as stream:
            stream.write(old[split:] + b"\n")
        self.append(self.batch("new"))
        self.observe()
        self.assertEqual([message["key"] for message in self.box.data["pending"]],
                         ["alert:observation:new:ETHUSDT"])


if __name__ == "__main__":
    unittest.main()
