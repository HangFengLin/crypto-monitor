"""Read-only public snapshots: missing evidence and private keys stay explicit."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class JindanWebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, **changes):
        value = {"schema_version": 1, "display_name": "金丹v1.0", "mode": "observation_only",
                 "updated_at": 990, "phase": "FEATURE_INITIALIZATION", "counts": {"price": 0, "flow": None},
                 "last_receive_ms": None, "events": [], "boundaries": {"orders_enabled": False}}
        value.update(changes)
        (self.root / "status.json").write_text(json.dumps(value))

    def read(self, now=1000):
        import jindan_web
        return jindan_web.read_status(self.root, now=now)

    def test_missing_snapshot_is_unknown_and_does_not_create_runtime_files(self):
        data = self.read()
        self.assertEqual(data["availability"], "missing")
        self.assertIsNone(data["updated_at"])
        self.assertIsNone(data["counts"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_zero_is_preserved_while_unavailable_counts_remain_null(self):
        self.write()
        data = self.read()
        self.assertEqual(data["availability"], "available")
        self.assertEqual(data["age_seconds"], 10)
        self.assertEqual(data["counts"]["price"], 0)
        self.assertIsNone(data["counts"]["flow"])
        self.assertIsNone(data["last_receive_ms"])

    def test_stale_snapshot_does_not_claim_current_collection(self):
        self.write()
        data = self.read(now=1200)
        self.assertEqual(data["availability"], "stale")
        self.assertEqual(data["age_seconds"], 210)

    def test_invalid_or_future_clock_is_unavailable_evidence(self):
        for changes in [{"schema_version": 2}, {"updated_at": None}, {"updated_at": 2000}, {"updated_at": True}]:
            with self.subTest(changes=changes):
                self.write(**changes)
                self.assertEqual(self.read()["availability"], "invalid")

    def test_oversized_json_and_symlink_are_rejected_without_reading_arbitrary_targets(self):
        (self.root / "status.json").write_bytes(b" " * (512 * 1024 + 1))
        self.assertEqual(self.read()["availability"], "invalid")
        (self.root / "status.json").unlink()
        private = self.root / "private.json"
        private.write_text('{"token":"NEVER_PUBLIC"}')
        (self.root / "status.json").symlink_to(private)
        self.assertEqual(self.read()["availability"], "unavailable")
        self.assertNotIn("NEVER_PUBLIC", json.dumps(self.read()))

    def test_private_keys_error_text_and_unregistered_symbols_are_not_exposed(self):
        self.write(token="NEVER_PUBLIC", env={"key": "NEVER_PUBLIC"},
                   errors=[{"code": "CONNECTION_FAILED", "message": "https://secret:NEVER_PUBLIC@host", "at_ms": 990000}],
                   per_symbol={"BTCUSDT": {"closed_bars": 0, "token": "NEVER_PUBLIC"}, "PRIVATE": {"token": "NEVER_PUBLIC"}},
                   events=[{"event_id": "a" * 64, "symbol": "BTCUSDT", "flow_status": "UNKNOWN", "secret": "NEVER_PUBLIC", "pnl": 99}],
                   boundaries={"orders_enabled": True, "token": "NEVER_PUBLIC"})
        data = self.read()
        self.assertNotIn("NEVER_PUBLIC", json.dumps(data))
        self.assertNotIn("PRIVATE", data["per_symbol"])
        self.assertEqual(data["boundaries"]["orders_enabled"], True)
        self.assertEqual(data["events"][0]["flow_status"], "UNKNOWN")
        self.assertNotIn("pnl", data["events"][0])

    def test_unimplemented_scoring_capabilities_and_public_gaps_are_preserved(self):
        self.write(capabilities={"raw_closed_1m": True, "e2_labels": False, "formal_scoring": False,
                                 "private_token": "NEVER_PUBLIC"},
                   readiness_gaps=["E2_LABELS_NOT_IMPLEMENTED", "SCORE_PREFLIGHT_REQUIRED"])
        data = self.read()
        self.assertIs(data["capabilities"]["e2_labels"], False)
        self.assertIs(data["capabilities"]["formal_scoring"], False)
        self.assertIn("E2_LABELS_NOT_IMPLEMENTED", data["readiness_gaps"])
        self.assertNotIn("NEVER_PUBLIC", json.dumps(data))

    def test_oversized_numeric_fields_cannot_crash_the_public_projection(self):
        self.write(last_receive_ms=10 ** 400, counts={"price_events": 10 ** 400})
        data = self.read()
        self.assertEqual(data["availability"], "available")
        self.assertIsNone(data["last_receive_ms"])
        self.assertIsNone(data["counts"]["price_events"])

    def test_recent_event_payload_is_bounded(self):
        self.write(events=[{"event_id": str(i), "symbol": "BTCUSDT"} for i in range(80)])
        self.assertEqual(len(self.read()["events"]), 50)

    def test_malformed_json_is_invalid_not_a_server_error(self):
        (self.root / "status.json").write_text('{"schema_version":1,')
        self.assertEqual(self.read()["availability"], "invalid")

    def test_research_projection_drops_paths_and_does_not_create_missing_results(self):
        import jindan_web
        self.assertEqual(jindan_web.read_research(self.root)["availability"], "missing")
        data = {"status": "EXPLORATORY_COMPLETE", "source_path": "/private/NEVER_PUBLIC",
                "main_metrics": [{"policy_id": "FLOW_BASE", "BASE": {"total_net_return": -0.02, "token": "NEVER_PUBLIC"}}],
                "final_2026_read": False, "orders_enabled": False}
        (self.root / "research_public.json").write_text(json.dumps(data))
        projected = jindan_web.read_research(self.root)
        self.assertEqual(projected["availability"], "available")
        self.assertEqual(projected["main_metrics"][0]["BASE"]["total_net_return"], -0.02)
        self.assertNotIn("NEVER_PUBLIC", json.dumps(projected))

    def test_only_public_research_file_is_read_and_reproduction_calendar_is_preserved(self):
        import jindan_web
        (self.root / "research.json").write_text('{"status":"RAW_PRIVATE_SUMMARY"}')
        self.assertEqual(jindan_web.read_research(self.root)["availability"], "missing")
        (self.root / "research_public.json").write_text(json.dumps({"status": "COMPLETE", "calendar_days": 700,
            "reproduction": {"status": "PASS_EXACT", "calendar_days": 731, "cost_replays": 52}, "main_metrics": []}))
        data = jindan_web.read_research(self.root)
        self.assertEqual(data["reproduction"]["calendar_days"], 731)
        self.assertEqual(data["reproduction"]["cost_replays"], 52)

    def test_collector_health_projection_has_no_storage_path_or_raw_clock_extras(self):
        self.write(unresolved_gap_count=2, counts={"reconnect_count": 3},
            per_symbol={"BTCUSDT": {"processed_bar_ms": 990000, "closed_bars": 4}},
            storage={"used_bytes": 20, "limit_bytes": 100, "free_bytes": 200, "min_free_bytes": 50, "ok": True,
                     "path": "/private/NEVER_PUBLIC"},
            clock={"utc_offset_estimate_ms": -2, "offset_uncertainty_ms": None, "estimated_utc_ms": 990000,
                   "private": "NEVER_PUBLIC"})
        data = self.read()
        self.assertEqual(data["unresolved_gap_count"], 2)
        self.assertEqual(data["reconnect_count"], 3)
        self.assertEqual(data["per_symbol"]["BTCUSDT"]["processed_bar_ms"], 990000)
        self.assertIsNone(data["clock"]["offset_uncertainty_ms"])
        self.assertIsNone(data["reference_clock_usable"])
        self.assertIs(data["storage"]["ok"], True)
        self.assertNotIn("NEVER_PUBLIC", json.dumps(data))

    def test_app_registers_read_only_routes_and_events_share_status_freshness(self):
        import app
        self.write()
        with patch.dict(os.environ, {"JINDAN_STATE_DIR": str(self.root)}):
            client = TestClient(app.create_app())
            for endpoint in ["status", "events", "research"]:
                response = client.get("/api/jindan/" + endpoint)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get("cache-control"), "no-store")
                self.assertEqual(client.post("/api/jindan/" + endpoint, json={}).status_code, 405)
            self.assertEqual(client.get("/api/jindan/events").json()["availability"], "stale")


if __name__ == "__main__":
    unittest.main()
