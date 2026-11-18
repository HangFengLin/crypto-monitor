"""Real price fixtures exercise warmup, persistence and durable alert routing."""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

STEP = 900_000
START = 1_700_000_000_000


def candle(index, close=100, high=101, low=99, opening=100, step=STEP):
    return {"open_time": START + index * step, "close_time": START + (index + 1) * step - 1,
            "open": opening, "high": high, "low": low, "close": close, "volume": 1000}


class PatternRuntimeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "patterns.json"
        self.warmup = [candle(index) for index in range(40)]

    def make(self):
        try:
            from pattern_runtime import PatternRuntime
        except ImportError:
            self.fail("The durable pattern observation runtime is not implemented")
        return PatternRuntime(self.path)

    def test_malformed_nested_checkpoint_is_unavailable_and_preserved(self):
        runtime = self.make()
        runtime.ingest("ETHUSDT", "15m", self.warmup,
                       observed_at_ms=self.warmup[-1]["close_time"] + 1000)
        saved = json.loads(self.path.read_text())
        saved["latest"] = {"broken": {"stage": "forming", "private_token": "should-not-leak"}}
        self.path.write_text(json.dumps(saved))
        before = self.path.read_bytes()
        restarted = self.make()
        payload = restarted.snapshot()
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["patterns"], [])
        self.assertNotIn("should-not-leak", json.dumps(payload))
        self.assertEqual(self.path.read_bytes(), before)

    def test_initial_history_is_labelled_and_never_sent_as_live_alerts(self):
        runtime = self.make()
        bars = [*self.warmup, candle(40, 102, 103, 100)]
        runtime.ingest("ETHUSDT", "15m", bars, observed_at_ms=bars[-1]["close_time"] + 10_000)
        payload = runtime.snapshot(now_ms=bars[-1]["close_time"] + 10_000)
        self.assertTrue(payload["patterns"])
        self.assertTrue(all(row["origin"] == "warmup" for row in payload["patterns"]))
        self.assertEqual(runtime.pending_batches(), [])

    def test_history_retention_uses_candle_time_when_observed_times_are_equal(self):
        runtime = self.make()
        bars = [*self.warmup, candle(40, 102, 103, 100)]
        observed = bars[-1]["close_time"] + 1000
        runtime.ingest("ETHUSDT", "15m", bars, observed_at_ms=observed)
        newest = runtime.snapshot()["patterns"][0]
        runtime.latest = {}
        for index in range(60):
            old = deepcopy(newest)
            old.update(id=f"{index:024x}", stage="expired",
                       bar_close_time=newest["bar_close_time"] - (60 - index) * STEP)
            runtime.latest[old["id"]] = old
        runtime.latest[newest["id"]] = newest
        runtime.ingest("ETHUSDT", "15m", [candle(41, 102, 103, 102, 102)], observed_at_ms=observed + STEP)
        self.assertTrue(newest["id"] in runtime.latest)
        self.assertEqual(list(runtime.latest.values())[0]["id"], newest["id"])

    def test_active_formation_is_not_hidden_by_later_received_old_history(self):
        runtime = self.make()
        bars = [*self.warmup, candle(40, 102, 103, 100)]
        observed = bars[-1]["close_time"] + 1000
        runtime.ingest("ETHUSDT", "15m", bars, observed_at_ms=observed)
        active = runtime.snapshot()["patterns"][0]
        for index in range(60):
            old = deepcopy(active)
            old.update(id=f"{index:024x}", stage="expired", updated_at_ms=observed + 100,
                       available_at_ms=observed + 100,
                       bar_close_time=active["bar_close_time"] - (60 - index) * STEP)
            runtime.latest[old["id"]] = old
        runtime.ingest("ETHUSDT", "15m", [candle(41, 102, 103, 102, 102)], observed_at_ms=observed + STEP)
        self.assertTrue(active["id"] in runtime.latest)
        self.assertEqual(list(runtime.latest.values())[0]["id"], active["id"])

    def test_live_breakout_retest_routes_one_group_and_survives_failed_journal_write(self):
        runtime = self.make()
        runtime.ingest("ETHUSDT", "15m", self.warmup, observed_at_ms=self.warmup[-1]["close_time"] + 1000)
        breakout = candle(40, 102, 103, 100)
        runtime.ingest("ETHUSDT", "15m", [self.warmup[-1], breakout],
                       observed_at_ms=breakout["close_time"] + 1000)
        self.assertEqual(len(runtime.pending_batches()), 1)
        message = runtime.pending_batches()[0]["alerts"][0]
        self.assertIn("突破回踩", message["body"])
        self.assertIn("失效", message["body"])
        self.assertIn("?pattern=", message["body"])
        self.assertIn("#patterns", message["body"])
        self.assertEqual(runtime.drain_notifications(lambda _: False), 0)
        runtime = self.make()
        self.assertEqual(runtime.pending_batches()[0]["alerts"][0]["key"], message["key"])
        accepted = []
        self.assertEqual(runtime.drain_notifications(lambda batch: accepted.append(batch) or True), 1)
        runtime = self.make()
        runtime.ingest("ETHUSDT", "15m", [breakout], observed_at_ms=breakout["close_time"] + 2000)
        self.assertEqual(runtime.pending_batches(), [])
        self.assertEqual(len(accepted), 1)

    def test_late_backfill_is_not_announced_as_a_new_real_time_signal(self):
        runtime = self.make()
        runtime.ingest("ETHUSDT", "15m", self.warmup, observed_at_ms=self.warmup[-1]["close_time"] + 1000)
        breakout = candle(40, 102, 103, 100)
        runtime.ingest("ETHUSDT", "15m", [breakout], observed_at_ms=breakout["close_time"] + 180_000)
        self.assertEqual(runtime.pending_batches(), [])
        self.assertEqual(runtime.snapshot()["patterns"][0]["origin"], "backfill")

    def test_gap_or_revision_cannot_partially_advance_the_engine(self):
        runtime = self.make()
        runtime.ingest("ETHUSDT", "15m", self.warmup, observed_at_ms=self.warmup[-1]["close_time"] + 1000)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            runtime.ingest("ETHUSDT", "15m", [candle(41, 102, 103, 100)],
                           observed_at_ms=candle(41)["close_time"] + 1000)
        self.assertEqual(self.path.read_bytes(), before)
        revised = {**self.warmup[-1], "close": 100.5}
        with self.assertRaises(ValueError):
            runtime.ingest("ETHUSDT", "15m", [revised, candle(40, 102, 103, 100)],
                           observed_at_ms=candle(40)["close_time"] + 1000)
        self.assertEqual(self.path.read_bytes(), before)

    def test_unfinished_bar_is_excluded_and_corrupt_checkpoint_is_not_overwritten(self):
        runtime = self.make()
        unfinished = {**candle(40, 102, 103, 100), "confirmed": False}
        runtime.ingest("ETHUSDT", "15m", [*self.warmup, unfinished],
                       observed_at_ms=unfinished["close_time"] + 1000)
        self.assertEqual(runtime.last_close("ETHUSDT", "15m"), self.warmup[-1]["close_time"])
        self.path.write_text("{broken")
        runtime = self.make()
        self.assertEqual(runtime.snapshot()["status"], "unavailable")
        with self.assertRaises(ValueError):
            runtime.ingest("ETHUSDT", "15m", self.warmup, observed_at_ms=unfinished["close_time"] + 1000)
        self.assertEqual(self.path.read_text(), "{broken")

    def test_pending_group_enters_the_existing_outbox_without_creating_a_trade(self):
        from automatic_notifications import Outbox
        from runtime_utils import append_jsonl

        runtime = self.make()
        log = self.root / "events.jsonl"
        box = Outbox(self.root / "outbox.json")
        box.observe([], [], None, [], log)
        runtime.ingest("ETHUSDT", "15m", self.warmup, observed_at_ms=self.warmup[-1]["close_time"] + 1000)
        bar = candle(40, 102, 103, 100)
        runtime.ingest("ETHUSDT", "15m", [bar], observed_at_ms=bar["close_time"] + 1000)
        runtime.drain_notifications(lambda batch: append_jsonl(log, batch) is None)
        box.observe([], [], None, [], log)
        self.assertEqual(len(box.data["pending"]), 1)
        self.assertEqual(box.data["trades"], {})
        self.assertEqual(box.data["pending"][0]["channel"], "signal")
        saved = json.loads(self.path.read_text())
        self.assertFalse(saved.get("orders_enabled", False))

    def test_one_scan_combines_timeframes_but_does_not_merge_previously_failed_groups(self):
        runtime = self.make()
        for interval, step in [("15m", STEP), ("1h", 3_600_000)]:
            warmup = [candle(index, step=step) for index in range(40)]
            runtime.ingest("ETHUSDT", interval, warmup, observed_at_ms=warmup[-1]["close_time"] + 1000)
            bar = candle(40, 102, 103, 100, step=step)
            try:
                runtime.ingest("ETHUSDT", interval, [bar], observed_at_ms=bar["close_time"] + 1000, scan_id="one")
            except TypeError:
                self.fail("Pattern messages do not have a complete scan boundary")
        self.assertEqual(runtime.drain_notifications(lambda _: self.fail("unfinished scan sent")), 0)
        runtime.finalize_scan("one")
        self.assertEqual(len(runtime.pending_batches()), 1)
        message = runtime.pending_batches()[0]["alerts"][0]
        self.assertIn("15m", message["body"])
        self.assertIn("1h", message["body"])
        runtime.drain_notifications(lambda _: False)
        restored = self.make()
        self.assertEqual(restored.pending_batches()[0]["alerts"][0], message)

    def test_journal_write_failure_is_visible_until_the_group_is_saved(self):
        runtime = self.make()
        runtime.ingest("ETHUSDT", "15m", self.warmup, observed_at_ms=self.warmup[-1]["close_time"] + 1000)
        bar = candle(40, 102, 103, 100)
        runtime.ingest("ETHUSDT", "15m", [bar], observed_at_ms=bar["close_time"] + 1000)
        runtime.record_scan(["ETHUSDT"], [], now_ms=bar["close_time"] + 1000)
        runtime.drain_notifications(lambda _: False)
        payload = runtime.snapshot(now_ms=bar["close_time"] + 1000)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["errors"][-1]["error_class"], "PatternJournalWriteFailed")
        runtime.drain_notifications(lambda _: True)
        self.assertEqual(runtime.snapshot(now_ms=bar["close_time"] + 1000)["status"], "healthy")

    def test_ended_formation_keeps_its_own_chart_and_active_retest_point_is_visible(self):
        runtime = self.make()
        runtime.ingest("ETHUSDT", "15m", self.warmup, observed_at_ms=self.warmup[-1]["close_time"] + 1000)
        breakout = candle(40, 102, 103, 100)
        created = runtime.ingest("ETHUSDT", "15m", [breakout], observed_at_ms=breakout["close_time"] + 1000)[0]
        retest = candle(41, 101.4, 102, 100.9, 101.5)
        runtime.ingest("ETHUSDT", "15m", [retest], observed_at_ms=retest["close_time"] + 1000)
        row = next(row for row in runtime.snapshot()["patterns"] if row["id"] == created["id"])
        self.assertIn("retest", [point["kind"] for point in row["points"]])
        invalid = candle(42, 99, 101.4, 98, 101.4)
        runtime.ingest("ETHUSDT", "15m", [invalid], observed_at_ms=invalid["close_time"] + 1000)
        row = next(row for row in runtime.snapshot()["patterns"] if row["id"] == created["id"])
        self.assertEqual(row["stage"], "invalidated")
        terminal_chart = row["chart"]
        later = [candle(index) for index in range(43, 70)]
        runtime.ingest("ETHUSDT", "15m", later, observed_at_ms=later[-1]["close_time"] + 1000)
        row = next(row for row in runtime.snapshot()["patterns"] if row["id"] == created["id"])
        self.assertEqual(row["chart"], terminal_chart)
