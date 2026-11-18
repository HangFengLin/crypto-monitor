"""Reject corrupt public observation payloads before API use or notification replay."""

import copy
import unittest

try:
    from pattern_validation import validate_runtime_payload
except ModuleNotFoundError:
    validate_runtime_payload = None


def event():
    return {
        "id": "a" * 24, "pattern": "breakout_retest", "direction": "long", "stage": "forming",
        "symbol": "ETHUSDT", "interval": "15m", "rule_version": "pattern_observer_v1",
        "mode": "observation_only", "level": 101, "invalid_level": 100, "atr": 2,
        "current_price": 102, "detected_at_ms": 100019, "updated_at_ms": 100019,
        "bar_close_time": 99999, "available_at_ms": 100019, "origin": "live", "source": "binance_usdm",
        "reason": "BREAKOUT_DETECTED", "points": [
            {"kind": "range_boundary", "time_ms": 98999, "price": 101},
            {"kind": "breakout", "time_ms": 99999, "price": 102},
        ],
        "chart": [{"open_time": 99000, "close_time": 99999, "open": 100, "high": 103,
                   "low": 100, "close": 102, "volume": 10}],
    }


def batch():
    return {"type": "observation_batch", "event_key": "pattern-round:123", "created_at": 100.019,
            "raw_events": [event()], "alerts": [
                {"key": "pattern:123:ETHUSDT", "title": "ETHUSDT 形态观察",
                 "body": "15m：突破回踩；关键位101，失效位100", "created_at": 100.019},
            ]}


def payload():
    row = event()
    return {"mode": "observation_only", "rule_version": "pattern_observer_v1", "engine": {},
            "latest": {row["id"]: row}, "events": [copy.deepcopy(row)], "pending": [batch()],
            "updated_at": 100.019, "symbols": ["ETHUSDT"]}


class PatternValidationTest(unittest.TestCase):
    def validate(self, value):
        self.assertIsNotNone(validate_runtime_payload, "runtime checkpoint projection validation is missing")
        return validate_runtime_payload(value)

    def rejects(self, value):
        self.assertIsNotNone(validate_runtime_payload, "runtime checkpoint projection validation is missing")
        with self.assertRaisesRegex(ValueError, "^InvalidPatternCheckpoint$"):
            validate_runtime_payload(value)

    def test_accepts_complete_legacy_unsealed_batch_and_does_not_mutate_input(self):
        saved = payload()
        original = copy.deepcopy(saved)
        self.assertIsNone(self.validate(saved))
        self.assertEqual(saved, original)

    def test_accepts_explicit_scan_group_metadata_and_safe_chart_scope_metadata(self):
        saved = payload()
        saved["pending"][0].update(scan_id="scan-0123", sealed=False)
        saved["latest"]["a" * 24].update(chart_observed_at_ms=110019, in_scope=True)
        self.assertIsNone(self.validate(saved))

    def test_rejects_string_rows_and_mismatched_latest_identity(self):
        for value in ("raw private content", None, 42, []):
            saved = payload()
            saved["latest"]["a" * 24] = value
            self.rejects(saved)
        saved = payload()
        saved["latest"] = {"b" * 24: event()}
        self.rejects(saved)

    def test_rejects_extra_private_fields_in_public_event_or_point(self):
        saved = payload()
        saved["latest"]["a" * 24]["authorization"] = "private-value"
        self.rejects(saved)
        saved = payload()
        saved["events"][0]["points"][0]["secret_url"] = "private-value"
        self.rejects(saved)

    def test_rejects_nonfinite_prices_boolean_times_and_invalid_time_order(self):
        for field, value in (("level", float("nan")), ("invalid_level", float("inf")),
                             ("level", True), ("available_at_ms", True), ("updated_at_ms", 99900)):
            saved = payload()
            saved["events"][0][field] = value
            self.rejects(saved)
        saved = payload()
        saved["events"][0]["points"][0]["time_ms"] = 100000
        self.rejects(saved)

    def test_rejects_unknown_origin_source_pattern_and_direction(self):
        for field, value in (("origin", "unverified"), ("source", "private_exchange"),
                             ("pattern", "arbitrary"), ("direction", "automatic_order")):
            saved = payload()
            saved["events"][0][field] = value
            self.rejects(saved)
        saved = payload()
        saved["events"][0].update(pattern="double_bottom", direction="short")
        self.rejects(saved)

    def test_rejects_invalid_or_oversized_chart(self):
        saved = payload()
        saved["events"][0]["chart"][0]["low"] = 104
        self.rejects(saved)
        saved = payload()
        saved["events"][0]["chart"][0]["volume"] = -1
        self.rejects(saved)
        saved = payload()
        saved["events"][0]["chart"] *= 81
        self.rejects(saved)

    def test_rejects_malformed_pending_batch_and_alert(self):
        saved = payload()
        saved["pending"] = ["not a batch"]
        self.rejects(saved)
        saved = payload()
        saved["pending"][0]["alerts"][0]["body"] = {"private": "value"}
        self.rejects(saved)
        saved = payload()
        saved["pending"][0]["sealed"] = 1
        self.rejects(saved)

    def test_warmup_or_backfill_event_cannot_enter_live_notification_batch(self):
        for origin in ("warmup", "backfill"):
            saved = payload()
            saved["pending"][0]["raw_events"][0]["origin"] = origin
            self.rejects(saved)

    def test_rejects_oversized_event_and_pending_histories(self):
        saved = payload()
        saved["events"] *= 101
        self.rejects(saved)
        saved = payload()
        saved["pending"] *= 257
        self.rejects(saved)

    def test_rejects_wrapper_version_mode_and_private_fields(self):
        for key, value in (("rule_version", "v999"), ("mode", "live_trading"),
                           ("symbols", "ETHUSDT"), ("updated_at", True), ("engine", [])):
            saved = payload()
            saved[key] = value
            self.rejects(saved)
        saved = payload()
        saved["private_token"] = "private-value"
        self.rejects(saved)
