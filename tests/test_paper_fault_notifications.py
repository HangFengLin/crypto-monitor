"""Paper fault thresholds gate real outbox messages, without changing fills."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from automatic_notifications import Outbox
from monitor_faults import FaultTracker


class PaperFaultNotificationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tracker = FaultTracker(self.root / "faults.json")
        self.clock = 1000.
        for replacement in (
            patch.object(app, "state", app.MonitorState()),
            patch.object(app, "FAULT_TRACKER", self.tracker),
            patch.object(app.time, "time", side_effect=lambda: self.clock),
            patch.object(app, "site_monitor_snapshot", return_value={"enabled": False}),
            patch.object(app, "list_report_files", return_value=[]),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.box = Outbox(self.root / "outbox.json")

    def observe(self, now, failed=True):
        self.clock = now
        app.state.module_errors["paper_data"] = "TESTUSDT:15m 缺少有效新鲜报价" if failed else None
        app.state.paper_data_details = ([{
            "symbol": "TESTUSDT", "interval": "15m", "kind": "stale_trade",
            "source_age_ms": 40000, "refresh_error": "invalid",
        }] if failed else [])
        app.publish_monitor_faults()
        self.box.observe(*app.automatic_notification_inputs())

    def test_short_trade_gap_is_visible_and_recorded_without_feishu_alert(self):
        for now in (1000, 1012, 1024):
            self.observe(now)
        self.assertTrue(app.snapshot()["module_errors"]["paper_data"])
        self.assertFalse(app.state.paper_data_fault["alert_active"])
        self.observe(1031, failed=False)
        self.assertEqual(self.box.data["pending"], [])
        diagnostics = app.fault_diagnostics()
        self.assertTrue(diagnostics["available"])
        recovered = [x for x in diagnostics["recent_events"] if x["event"] == "fault_recovered"]
        self.assertEqual(recovered[-1]["details"][0]["symbol"], "TESTUSDT")
        self.assertEqual(diagnostics["quote_max_age_ms"], 30000)

    def test_sustained_failure_alerts_once_and_brief_recovery_does_not_realert(self):
        for now in (1000, 1010, 1060, 1090):
            self.observe(now)
        self.assertEqual([m["title"] for m in self.box.data["pending"]], ["监控异常"])
        self.observe(1100, failed=False)
        self.observe(1130)
        self.assertTrue(app.state.paper_data_fault["alert_active"])
        self.assertEqual(len(self.box.data["pending"]), 1)
        self.observe(1140, failed=False)
        for now in (1150, 1170, 1190):
            self.observe(now, failed=False)
        self.observe(1200, failed=False)
        self.assertEqual([m["title"] for m in self.box.data["pending"]], ["监控异常", "监控恢复"])
        self.observe(1201)
        self.assertFalse(app.state.paper_data_fault["alert_active"])
        self.assertEqual(len(self.box.data["pending"]), 2)

    def test_stalled_monitor_does_not_recover_from_one_old_success(self):
        for now in (1000, 1010, 1060):
            self.observe(now)
        self.observe(1100, failed=False)
        self.clock = 1160
        self.box.observe(*app.automatic_notification_inputs())
        self.assertEqual([m["title"] for m in self.box.data["pending"]], ["监控异常"])

    def test_restart_preserves_announced_fault_until_a_fresh_monitor_round(self):
        for now in (1000, 1010, 1060):
            self.observe(now)
        app.state = app.MonitorState()
        self.clock = 1100
        with patch.dict(app.os.environ, {"MONITOR_FAULT_FILE": str(self.root / "faults.json")}):
            app.initialize_fault_history()
        self.box.observe(*app.automatic_notification_inputs())
        self.clock = 1170
        self.box.observe(*app.automatic_notification_inputs())
        self.assertEqual([m["title"] for m in self.box.data["pending"]], ["监控异常"])

    def test_journal_write_failure_is_visible_and_cannot_suppress_outage(self):
        with patch.object(self.tracker, "observe", side_effect=OSError("private path")):
            self.observe(1000)
        self.assertIn("OSError", app.state.module_errors["fault_history"])
        self.assertIn("模拟持仓行情", self.box.data["pending"][0]["body"])
        self.assertNotIn("private path", json.dumps(app.fault_diagnostics()))

    def test_corrupt_state_is_not_replaced_and_fault_history_is_reported(self):
        path = self.root / "corrupt.json"
        path.write_text("broken original bytes")
        with patch.dict(app.os.environ, {"MONITOR_FAULT_FILE": str(path)}):
            app.initialize_fault_history()
        app.publish_monitor_faults()
        self.assertEqual(path.read_text(), "broken original bytes")
        self.assertIn("故障记录", app.state.module_errors["fault_history"])
        self.assertFalse(app.fault_diagnostics()["available"])
