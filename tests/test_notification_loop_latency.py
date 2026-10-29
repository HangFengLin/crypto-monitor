"""Exercise the real async dispatcher with a virtual clock and private outbox."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
import automatic_notifications
from automatic_notifications import Outbox

REAL_SLEEP = asyncio.sleep


class Clock:
    def __init__(self):
        self.elapsed = 0.0

    def time(self):
        return 100.0 + self.elapsed

    def monotonic(self):
        return 500.0 + self.elapsed

    def time_ns(self):
        return int(self.time() * 1_000_000_000)

    def advance(self, seconds):
        self.elapsed += seconds


class NotificationLoopLatencyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.clock = Clock()
        self.stop = asyncio.Event()
        self.enabled = True
        self.allowed = {key: True for key in app.feishu_notifications.CHANNELS}
        self.calls = []
        self.waits = []
        settings = SimpleNamespace(get=lambda: {"values": {"notifications": {
            "enabled": self.enabled, **self.allowed,
        }}})
        for replacement in (
            patch.object(app, "time", self.clock),
            patch.object(automatic_notifications, "time", self.clock),
            patch.object(app, "RUNTIME_SETTINGS", settings),
            patch.object(app, "automatic_notification_status", {}),
            patch.dict(app.os.environ, {"NOTIFICATIONS_FORCE_OFF": "false"}),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.box = Outbox(Path(temporary.name) / "outbox.json")
        self.box.observe([], [], None, [])

    async def timeout(self, awaitable, timeout):
        if self.stop.is_set():
            return await awaitable
        awaitable.close()
        self.waits.append(timeout)
        self.clock.advance(timeout)
        if self.clock.elapsed > 60 or len(self.waits) > 300:
            self.fail("notification scheduler did not make bounded progress")
        await REAL_SLEEP(0)
        raise TimeoutError

    async def dispatch(self, inputs, sender):
        with patch.object(app, "automatic_notification_inputs", side_effect=inputs), \
             patch.object(app.feishu_notifications, "send", side_effect=sender), \
             patch.object(app.asyncio, "wait_for", side_effect=self.timeout):
            await app.automatic_notification_loop(self.stop, self.box)

    async def test_new_event_is_observed_without_the_old_five_second_idle_wait(self):
        def inputs():
            trades = ([{"id": "new", "status": "open", "opened_at": 100.1}]
                      if self.clock.elapsed >= .1 else [])
            return trades, [], None, []

        def sender(channel, title, body):
            self.clock.advance(.4)
            self.stop.set()
            return {"ok": True}

        await self.dispatch(inputs, sender)
        record = self.box.data["delivery_history"][-1]
        self.assertLess(record["enqueued_at"] - record["created_at"], .6)
        self.assertLess(record["total_age_s"], 1.1)
        self.assertEqual(self.box.data["pending"], [])

    async def test_ready_backlog_continues_across_the_three_attempt_batch_boundary(self):
        # A fixed five-second sleep after attempts 3 and 6 delays an eight-message burst.
        for index in range(8):
            self.box.enqueue(str(index), "signal", str(index), "body", created_at=100)

        def sender(channel, title, body):
            self.calls.append(title)
            self.clock.advance(.4)
            if len(self.calls) == 8:
                self.stop.set()
            return {"ok": True}

        await self.dispatch(lambda: ([], [], None, []), sender)
        self.assertEqual(self.calls, [str(index) for index in range(8)])
        self.assertLess(self.box.data["delivery_history"][-1]["total_age_s"], 4)

    async def test_stop_during_a_send_prevents_another_attempt_in_the_same_batch(self):
        for index in range(3):
            self.box.enqueue(str(index), "signal", str(index), "body", created_at=100)

        def sender(channel, title, body):
            self.calls.append(title)
            self.stop.set()
            return {"ok": True}

        await self.dispatch(lambda: ([], [], None, []), sender)
        self.assertEqual(self.calls, ["0"])
        self.assertEqual([message["key"] for message in self.box.data["pending"]], ["1", "2"])

    async def test_production_pacing_drains_eight_messages_without_batch_gaps(self):
        self.box = Outbox(self.box.path, min_send_interval_s=app.NOTIFICATION_MIN_SEND_INTERVAL_S)
        for index in range(8):
            self.box.enqueue(str(index), "signal", str(index), "body", created_at=100)
        starts = []

        def sender(channel, title, body):
            self.calls.append(title)
            starts.append(self.clock.elapsed)
            self.clock.advance(.4)
            if len(self.calls) == 8:
                self.stop.set()
            return {"ok": True}

        await self.dispatch(lambda: ([], [], None, []), sender)
        self.assertEqual(self.calls, [str(index) for index in range(8)])
        self.assertTrue(all(b - a >= .699 for a, b in zip(starts, starts[1:])), starts)
        self.assertLess(self.box.data["delivery_history"][-1]["total_age_s"], 6)
        self.assertEqual(app.automatic_notification_status["scheduler"]["min_send_interval_s"], .7)

    async def test_switching_notifications_off_is_respected_before_the_next_batch(self):
        for index in range(5):
            self.box.enqueue(str(index), "signal", str(index), "body", created_at=100)

        def inputs():
            if not self.enabled:
                self.stop.set()
            return [], [], None, []

        def sender(channel, title, body):
            self.calls.append(title)
            if len(self.calls) == 3:
                self.enabled = False
            return {"ok": True}

        await self.dispatch(inputs, sender)
        self.assertEqual(self.calls, ["0", "1", "2"])
        self.assertEqual(len(self.box.data["pending"]), 2)


if __name__ == "__main__":
    unittest.main()
