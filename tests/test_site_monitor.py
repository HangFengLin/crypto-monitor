from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class SiteMonitorTest(unittest.TestCase):
    def tearDown(self) -> None:
        app._site_monitor_alert_state.clear()

    def test_parse_targets_supports_named_entries(self) -> None:
        targets = app.parse_site_monitor_targets(
            "health=http://127.0.0.1:8080/api/health, https://example.com/status"
        )

        self.assertEqual(targets[0]["id"], "http:health")
        self.assertEqual(targets[0]["name"], "health")
        self.assertEqual(targets[0]["url"], "http://127.0.0.1:8080/api/health")
        self.assertEqual(targets[1]["name"], "target_2")
        self.assertEqual(targets[1]["url"], "https://example.com/status")

    def test_site_monitor_alerts_only_after_threshold_and_recovers_once(self) -> None:
        env = {**os.environ, "SITE_MONITOR_FAILURE_THRESHOLD": "2"}
        with patch.dict(os.environ, env, clear=True):
            first = [{"id": "http:health", "name": "health", "ok": False}]
            self.assertEqual(app.apply_site_monitor_alert_state(first, now=1000), [])
            self.assertEqual(first[0]["consecutive_failures"], 1)
            self.assertFalse(first[0]["alert_active"])

            second = [{"id": "http:health", "name": "health", "ok": False}]
            notifications = app.apply_site_monitor_alert_state(second, now=1060)
            self.assertEqual([item["status"] for item in notifications], ["firing"])
            self.assertEqual(second[0]["consecutive_failures"], 2)
            self.assertTrue(second[0]["alert_active"])

            third = [{"id": "http:health", "name": "health", "ok": False}]
            self.assertEqual(app.apply_site_monitor_alert_state(third, now=1120), [])
            self.assertEqual(third[0]["consecutive_failures"], 3)
            self.assertTrue(third[0]["alert_active"])

            recovered = [{"id": "http:health", "name": "health", "ok": True}]
            notifications = app.apply_site_monitor_alert_state(recovered, now=1180)
            self.assertEqual([item["status"] for item in notifications], ["recovered"])
            self.assertEqual(recovered[0]["consecutive_failures"], 0)
            self.assertFalse(recovered[0]["alert_active"])

    def test_runtime_report_check_can_require_report_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            reports_dir = Path(tmpdir)
            env = {
                **os.environ,
                "SITE_MONITOR_MIN_REPORT_COUNT": "1",
                "SITE_MONITOR_REPORT_MAX_AGE_SECONDS": "0",
                "SITE_MONITOR_REQUIRE_OKX_BOT_FILES": "false",
                "SITE_MONITOR_REQUIRE_OKX_BOT_OK": "false",
            }
            with patch.dict(os.environ, env, clear=True), patch.object(app, "REPORTS_DIR", reports_dir):
                empty_checks = app.build_site_monitor_runtime_checks()
                self.assertFalse(empty_checks[0]["ok"])
                self.assertEqual(empty_checks[0]["report_count"], 0)

                (reports_dir / "daily.html").write_text("<html>ok</html>", encoding="utf-8")
                report_checks = app.build_site_monitor_runtime_checks()
                self.assertTrue(report_checks[0]["ok"])
                self.assertEqual(report_checks[0]["report_count"], 1)


if __name__ == "__main__":
    unittest.main()
