from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class AppOkxStatusTest(unittest.TestCase):
    def test_successful_scan_clears_old_current_error_but_keeps_history(self) -> None:
        events = [
            {"created_at": 1000, "type": "error", "symbol": "BTCUSDT", "error": "rate limited"},
            {"created_at": 1010, "type": "scan", "errors": 0, "opened": 0},
        ]

        with patch.object(app.time, "time", return_value=1020), patch.object(Path, "exists", return_value=True):
            status = app.summarize_okx_bot_status(events, {"positions": []})

        self.assertIsNone(status["last_error"])
        self.assertEqual(status["last_error_event"]["error"], "rate limited")

    def test_scan_with_errors_keeps_current_error(self) -> None:
        events = [
            {"created_at": 1000, "type": "error", "symbol": "BTCUSDT", "error": "rate limited"},
            {"created_at": 1010, "type": "scan", "errors": 1, "opened": 0},
        ]

        with patch.object(app.time, "time", return_value=1020), patch.object(Path, "exists", return_value=True):
            status = app.summarize_okx_bot_status(events, {"positions": []})

        self.assertEqual(status["last_error"], "rate limited")

    def test_okx_status_falls_back_to_legacy_bot_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            runtime_state = base / "runtime" / "okx_market_cap_bot_state.json"
            runtime_log = base / "runtime" / "okx_market_cap_bot_events.jsonl"
            legacy_state = base / "okx_market_cap_bot_state.json"
            legacy_log = base / "okx_market_cap_bot_events.jsonl"
            legacy_state.write_text(json.dumps({"positions": [{"symbol": "HUSDT", "status": "open"}]}), encoding="utf-8")
            legacy_log.write_text(json.dumps({"created_at": 1000, "type": "scan", "open_positions": 1}) + "\n", encoding="utf-8")

            with patch.object(app, "OKX_BOT_STATE_FILE", runtime_state), patch.object(
                app, "OKX_BOT_EVENT_LOG_FILE", runtime_log
            ), patch.object(app, "OKX_BOT_LEGACY_STATE_FILE", legacy_state), patch.object(
                app, "OKX_BOT_LEGACY_EVENT_LOG_FILE", legacy_log
            ), patch.object(
                app.time, "time", return_value=1100
            ):
                status = app.load_okx_bot_status()

            self.assertTrue(status["state_exists"])
            self.assertTrue(status["event_log_exists"])
            self.assertEqual(status["open_positions"], 1)
            self.assertEqual(status["state_path"], str(legacy_state))
            self.assertEqual(status["event_log_path"], str(legacy_log))


if __name__ == "__main__":
    unittest.main()
