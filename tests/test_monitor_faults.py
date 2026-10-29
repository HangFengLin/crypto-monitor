"""Fault gates use independent observations and retain safe incident evidence."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

DETAIL = {"symbol": "TESTUSDT", "interval": "15m", "kind": "stale_trade",
          "source_age_ms": 31_000, "refresh_error": "invalid", "error_type": None}


class MonitorFaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "faults.json"

    def tracker(self):
        self.assertIsNotNone(importlib.util.find_spec("monitor_faults"),
                             "Persistent monitor fault tracking is missing")
        from monitor_faults import FaultTracker
        return FaultTracker(self.path)

    def activate(self, tracker):
        tracker.observe("paper_data", [DETAIL], now=100, observation_id="a")
        tracker.observe("paper_data", [DETAIL], now=130, observation_id="b")
        return tracker.observe("paper_data", [DETAIL], now=160, observation_id="c")

    def test_thirty_one_to_fifty_nine_second_failures_remain_diagnostic_only(self):
        tracker = self.tracker()
        for timestamp, observation in ((100, "a"), (131, "b"), (159, "c")):
            state = tracker.observe("paper_data", [DETAIL], now=timestamp, observation_id=observation)
            self.assertTrue(state["active"])
            self.assertFalse(state["alert_active"])
        self.assertEqual(state["consecutive_failures"], 3)
        self.assertEqual(state["first_failure_at"], 100)
        self.assertEqual(state["threshold_rounds"], 3)
        self.assertEqual(state["threshold_duration_seconds"], 60)
        self.assertEqual([event["event"] for event in tracker.events()], ["fault_started"])

    def test_three_failure_rounds_over_sixty_seconds_activate_once(self):
        tracker = self.tracker()
        state = self.activate(tracker)
        self.assertTrue(state["alert_active"])
        tracker.observe("paper_data", [DETAIL], now=170, observation_id="d")
        self.assertEqual([event["event"] for event in tracker.events()],
                         ["fault_started", "alert_activated"])

    def test_sixty_seconds_with_only_two_rounds_does_not_activate(self):
        tracker = self.tracker()
        tracker.observe("paper_data", [DETAIL], now=100, observation_id="a")
        state = tracker.observe("paper_data", [DETAIL], now=160, observation_id="b")
        self.assertEqual(state["consecutive_failures"], 2)
        self.assertFalse(state["alert_active"])

    def test_duplicate_observation_ids_cannot_manufacture_rounds_even_after_restart(self):
        tracker = self.tracker()
        tracker.observe("paper_data", [DETAIL], now=100, observation_id="a")
        tracker.observe("paper_data", [DETAIL], now=130, observation_id="b")
        tracker = self.tracker()
        state = tracker.observe("paper_data", [DETAIL], now=160, observation_id="a")
        self.assertEqual(state["consecutive_failures"], 2)
        self.assertFalse(state["alert_active"])
        state = tracker.observe("paper_data", [DETAIL], now=161, observation_id="c")
        self.assertTrue(state["alert_active"])
        self.assertEqual(state["first_failure_at"], 100)

    def test_success_resets_an_unconfirmed_failure_streak(self):
        tracker = self.tracker()
        tracker.observe("paper_data", [DETAIL], now=100, observation_id="a")
        tracker.observe("paper_data", [DETAIL], now=130, observation_id="b")
        healthy = tracker.observe("paper_data", [], now=140, observation_id="success")
        self.assertFalse(healthy["active"])
        self.assertEqual(healthy["details"], [])
        self.assertEqual(healthy["last_details"][0]["symbol"], "TESTUSDT")
        state = tracker.observe("paper_data", [DETAIL], now=160, observation_id="c")
        self.assertEqual(state["consecutive_failures"], 1)
        self.assertEqual(state["first_failure_at"], 160)
        self.assertFalse(state["alert_active"])

    def test_other_modules_keep_immediate_alert_behavior(self):
        tracker = self.tracker()
        state = tracker.observe("strategy", [{"kind": "invalid_data", "error_type": "TypeError"}],
                                now=100, observation_id="a")
        self.assertTrue(state["alert_active"])

    def test_confirmed_fault_reactivates_during_brief_recovery_without_new_gate(self):
        tracker = self.tracker()
        self.activate(tracker)
        recovered = tracker.observe("paper_data", [], now=170, observation_id="success")
        self.assertFalse(recovered["active"])
        self.assertFalse(recovered["alert_active"])
        self.assertTrue(recovered["armed"])
        tracker = self.tracker()
        relapsed = tracker.observe("paper_data", [DETAIL], now=220, observation_id="relapse")
        self.assertTrue(relapsed["alert_active"])
        self.assertEqual(relapsed["consecutive_failures"], 1)
        self.assertEqual(sum(event["event"] == "alert_activated" for event in tracker.events()), 1)

    def test_sixty_seconds_of_recovery_disarms_the_next_failure(self):
        tracker = self.tracker()
        self.activate(tracker)
        tracker.observe("paper_data", [], now=170, observation_id="success")
        tracker.observe("paper_data", [], now=190, observation_id="healthy190")
        tracker.observe("paper_data", [], now=210, observation_id="healthy210")
        state = tracker.observe("paper_data", [], now=229, observation_id="success2")
        self.assertTrue(state["armed"])
        state = tracker.observe("paper_data", [], now=230, observation_id="success3")
        self.assertFalse(state["armed"])
        state = tracker.observe("paper_data", [DETAIL], now=231, observation_id="relapse")
        self.assertFalse(state["alert_active"])
        self.assertEqual(state["consecutive_failures"], 1)

    def test_long_unobserved_gap_does_not_make_a_relapse_look_like_confirmed_recovery(self):
        tracker = self.tracker()
        self.activate(tracker)
        tracker.observe("paper_data", [], now=170, observation_id="success")
        state = tracker.observe("paper_data", [DETAIL], now=240, observation_id="relapse")
        self.assertTrue(state["armed"])
        self.assertTrue(state["alert_active"])

    def test_restart_with_a_long_sampling_gap_restarts_the_healthy_confirmation(self):
        tracker = self.tracker()
        self.activate(tracker)
        tracker.observe("paper_data", [], now=170, observation_id="success")
        tracker = self.tracker()
        state = tracker.observe("paper_data", [], now=240, observation_id="resumed")
        self.assertTrue(state["armed"])
        self.assertEqual(state["recovery_since"], 240)
        tracker.observe("paper_data", [], now=270, observation_id="healthy270")
        state = tracker.observe("paper_data", [], now=300, observation_id="healthy300")
        self.assertFalse(state["armed"])

    def test_rollback_does_not_create_elapsed_failure_or_recovery_time(self):
        tracker = self.tracker()
        for timestamp, observation in ((100, "a"), (20, "b"), (80, "c")):
            state = tracker.observe("paper_data", [DETAIL], now=timestamp, observation_id=observation)
            self.assertFalse(state["alert_active"])
        state = tracker.observe("paper_data", [DETAIL], now=160, observation_id="d")
        self.assertTrue(state["alert_active"])
        tracker.observe("paper_data", [], now=170, observation_id="success")
        tracker.observe("paper_data", [], now=100, observation_id="rollback")
        self.assertTrue(tracker.summary()["modules"]["paper_data"]["armed"])

    def test_recovery_journal_retains_changed_details_and_duration(self):
        tracker = self.tracker()
        tracker.observe("paper_data", [DETAIL], now=100, observation_id="a")
        changed = dict(DETAIL, kind="quote_request_failed", refresh_error="TimeoutError")
        tracker.observe("paper_data", [changed], now=125, observation_id="b")
        tracker.observe("paper_data", [changed], now=130, observation_id="c")
        tracker.observe("paper_data", [], now=140, observation_id="success")
        tracker.observe("paper_data", [], now=145, observation_id="success2")
        events = tracker.events()
        self.assertEqual([event["event"] for event in events],
                         ["fault_started", "fault_updated", "fault_recovered"])
        self.assertEqual(events[-1]["duration_seconds"], 40)
        self.assertEqual(events[-1]["details"][0]["refresh_error"], "TimeoutError")
        restored = self.tracker().summary()["modules"]["paper_data"]
        self.assertFalse(restored["active"])
        self.assertEqual(restored["last_details"][0]["kind"], "quote_request_failed")

    def test_corrupt_existing_state_is_rejected_without_overwriting_it(self):
        for document in ("{bad json", "[]", '{"version":1,"modules":{"paper_data":{}}}'):
            with self.subTest(document=document):
                self.path.write_text(document)
                with self.assertRaises(ValueError):
                    self.tracker()
                self.assertEqual(self.path.read_text(), document)

    def test_journal_failure_is_visible_without_suppressing_a_real_fault(self):
        tracker = self.tracker()
        tracker.journal_path.mkdir()
        state = self.activate(tracker)
        self.assertTrue(state["alert_active"])
        self.assertIn("IsADirectoryError", state["journal_error"])
        self.assertIn("IsADirectoryError", self.tracker().summary()["journal_error"])
        tracker.journal_path.rmdir()
        state = tracker.observe("paper_data", [DETAIL], now=170, observation_id="d")
        self.assertIsNone(state["journal_error"])
        self.assertEqual([event["event"] for event in tracker.events()],
                         ["fault_started", "alert_activated"])

    def test_state_write_failure_is_explicit_and_keeps_the_previous_file(self):
        tracker = self.tracker()
        tracker.observe("paper_data", [DETAIL], now=100, observation_id="a")
        before = self.path.read_bytes()
        with patch("monitor_faults.os.replace", side_effect=PermissionError("SECRET_URL_TOKEN")):
            state = tracker.observe("paper_data", [DETAIL], now=130, observation_id="b")
        self.assertTrue(state["active"])
        self.assertIn("PermissionError", state["journal_error"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("SECRET", json.dumps(tracker.summary()))

    def test_only_safe_structured_fields_reach_state_and_journal(self):
        tracker = self.tracker()
        unsafe = dict(DETAIL, symbol="https://secret.example/TOKEN", interval="15m?token=SECRET",
                      kind="TOKEN_123", refresh_error="https://secret.example/TimeoutError",
                      error_type="SECRETApiKeyError", source_age_ms=float("nan"),
                      url="https://secret.example/TOKEN", token="SECRET")
        state = tracker.observe("paper_data", [unsafe], now=100,
                                observation_id="https://secret.example/OBSERVATION_TOKEN")
        self.assertTrue(state["active"])
        self.assertEqual(state["details"][0]["kind"], "invalid_data")
        combined = json.dumps(tracker.summary()) + self.path.read_text() + tracker.journal_path.read_text()
        for private in ("secret.example", "TOKEN", "SECRET", "OBSERVATION"):
            self.assertNotIn(private, combined)
        state["details"].append({"token": "escaped mutation"})
        self.assertEqual(len(tracker.summary()["modules"]["paper_data"]["details"]), 1)

    def test_recent_events_are_bounded_and_malformed_lines_are_not_exposed(self):
        tracker = self.tracker()
        tracker.observe("paper_data", [DETAIL], now=100, observation_id="a")
        for count in range(250):
            tracker.observe("paper_data", [dict(DETAIL, source_age_ms=31_001 + count)],
                            now=101 + count, observation_id=str(count))
        self.assertEqual(len(tracker.events(limit=10)), 10)
        self.assertLessEqual(len(tracker.events(limit=9999)), 200)
        with tracker.journal_path.open("a") as stream:
            stream.write('{"token":"SECRET"}\nnot-json-SECRET\n')
        recent = tracker.events(limit=3)
        self.assertEqual(len(recent), 3)
        self.assertNotIn("SECRET", json.dumps(recent))
        self.assertIsNotNone(tracker.summary()["journal_error"])

    def test_healthy_observations_do_not_erase_the_last_fault_details(self):
        tracker = self.tracker()
        tracker.observe("paper_data", [DETAIL], now=100, observation_id="a")
        tracker.observe("paper_data", [], now=120, observation_id="success")
        tracker.observe("paper_data", [], now=200, observation_id="success2")
        self.assertEqual(self.tracker().summary()["modules"]["paper_data"]["last_details"][0]["kind"],
                         "stale_trade")

    def test_long_incidents_keep_only_a_bounded_recent_observation_id_window(self):
        tracker = self.tracker()
        for count in range(1100):
            tracker.observe("paper_data", [DETAIL], now=100 + count, observation_id=str(count))
        saved = json.loads(self.path.read_text())
        self.assertLessEqual(len(saved["modules"]["paper_data"]["seen_failure_ids"]), 1024)
        state = tracker.observe("paper_data", [DETAIL], now=1300, observation_id="1099")
        self.assertEqual(state["consecutive_failures"], 1100)

    def test_jsonl_state_filename_cannot_collide_with_the_append_journal(self):
        self.path = self.path.with_suffix(".jsonl")
        tracker = self.tracker()
        self.assertNotEqual(tracker.path, tracker.journal_path)
        tracker.observe("paper_data", [DETAIL], now=100, observation_id="a")
        self.assertEqual(tracker.events()[0]["event"], "fault_started")
        self.assertTrue(self.tracker().summary()["modules"]["paper_data"]["active"])

    def test_unbounded_numeric_detail_is_sanitized_without_crashing_monitoring(self):
        tracker = self.tracker()
        try:
            state = tracker.observe("paper_data", [dict(DETAIL, source_age_ms=10 ** 1000)],
                                    now=100, observation_id="a")
        except OverflowError:
            self.fail("Untrusted source age overflow must remain an active safe diagnostic")
        self.assertTrue(state["active"])
        self.assertIsNone(state["details"][0]["source_age_ms"])

    def test_an_immediate_module_stays_immediate_when_the_wall_clock_moves_backwards(self):
        tracker = self.tracker()
        tracker.observe("strategy", [], now=200, observation_id="success")
        state = tracker.observe("strategy", [dict(DETAIL, kind="invalid_data")], now=100, observation_id="a")
        self.assertTrue(state["alert_active"])

    def test_diagnostic_duration_reports_observed_elapsed_time_not_alert_threshold(self):
        tracker = self.tracker()
        state = tracker.observe("paper_data", [DETAIL], now=100, observation_id="a")
        self.assertEqual(state["duration_seconds"], 0)
        state = tracker.observe("paper_data", [DETAIL], now=130, observation_id="b")
        self.assertEqual(state["duration_seconds"], 30)
        self.assertEqual(state["threshold_duration_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
