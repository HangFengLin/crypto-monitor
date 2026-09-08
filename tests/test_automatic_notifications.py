import tempfile
import unittest
from pathlib import Path


class AutomaticNotificationTests(unittest.TestCase):
    def test_only_one_sender_can_own_a_queue_until_released(self):
        from automatic_notifications import SenderLease

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            first = SenderLease(path)
            try:
                with self.assertRaises(RuntimeError):
                    SenderLease(path)
            finally:
                first.close()
            successor = SenderLease(path)
            successor.close()

    def test_failed_open_does_not_allow_exit_to_overtake_it(self):
        with tempfile.TemporaryDirectory() as folder:
            box = self.make(Path(folder) / "state.json")
            box.enqueue("trade:open", "signal", "open", "test")
            box.enqueue("trade:closed", "signal", "closed", "test")
            box.enqueue("report", "research", "report", "test")
            calls = []

            def sender(channel, title, body):
                calls.append(title)
                return {"ok": title != "open"}

            box.deliver(sender)
            self.assertEqual(calls, ["open", "report"])
            calls.clear()
            box.deliver(sender)
            self.assertEqual(calls, [])
            box.data["pending"][0]["next_attempt"] = 0
            box.deliver(lambda channel, title, body: calls.append(title) or {"ok": True})
            self.assertEqual(calls, ["open", "closed"])

    def test_retrying_messages_do_not_block_other_channels(self):
        with tempfile.TemporaryDirectory() as folder:
            box = self.make(Path(folder) / "state.json")
            for i in range(3):
                box.enqueue(str(i), "signal", "test", "test")
            box.deliver(lambda *args: {"ok": False})
            box.enqueue("report", "research", "test", "test")
            sent = []
            box.deliver(lambda *args: sent.append(args[0]) or {"ok": True})
            self.assertEqual(sent, ["research"])

    def make(self, path):
        from automatic_notifications import Outbox

        return Outbox(path)

    def test_baseline_then_open_close_and_restart_do_not_repeat(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "outbox.json"
            box = self.make(path)
            box.observe([], [], None)
            trade = {"id": "t1", "symbol": "BTCUSDT", "status": "open", "entry_price": 100}
            box.observe([trade], [], None)
            box.observe([trade], [], None)
            self.assertEqual(len(box.data["pending"]), 1)
            trade = {**trade, "status": "closed", "exit_price": 110, "return_pct": 0.1}
            box.observe([trade], [], None)
            sent = []
            box.deliver(lambda *args: sent.append(args) or {"ok": True})
            self.assertEqual([s[0] for s in sent], ["signal", "signal"])
            restored = self.make(path)
            restored.observe([trade], [], None)
            self.assertEqual(restored.data["pending"], [])

    def test_old_reports_not_sent_new_reports_and_fault_recovery_routed(self):
        with tempfile.TemporaryDirectory() as folder:
            box = self.make(Path(folder) / "state.json")
            old = {"path": "old.html", "updated_at": 1}
            box.observe([], [old], None)
            new = {"path": "new.html", "updated_at": 2}
            box.observe([], [old, new], "market")
            box.observe([], [old, new], "market")
            box.observe([], [old, new], None)
            self.assertEqual([m["channel"] for m in box.data["pending"]], ["research", "system"])
            box.data["recovery_since"] -= 61
            box.observe([], [old, new], None)
            self.assertEqual([m["channel"] for m in box.data["pending"]], ["research", "system", "system"])

    def test_failed_delivery_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            box = self.make(path)
            box.observe([], [], None)
            box.observe([], [], "market")
            box.deliver(lambda *args: {"ok": False, "error": "timeout"})
            restored = self.make(path)
            self.assertEqual(len(restored.data["pending"]), 1)
            self.assertEqual(restored.data["pending"][0]["attempts"], 1)

    def test_brief_recovery_does_not_reannounce_same_fault(self):
        from unittest.mock import patch

        with (
            tempfile.TemporaryDirectory() as folder,
            patch("automatic_notifications.time.time", return_value=100) as clock,
        ):
            box = self.make(Path(folder) / "state.json")
            box.observe([], [], "signal")
            clock.return_value = 110
            box.observe([], [], None)
            clock.return_value = 120
            box.observe([], [], "signal")
            self.assertEqual(len(box.data["pending"]), 1)
            clock.return_value = 130
            box.observe([], [], None)
            clock.return_value = 191
            box.observe([], [], None)
            self.assertEqual([m["title"] for m in box.data["pending"]], ["监控异常", "监控恢复"])

    def test_paused_channel_keeps_queue_and_does_not_send(self):
        with tempfile.TemporaryDirectory() as folder:
            box = self.make(Path(folder) / "state.json")
            box.enqueue("one", "signal", "test", "body")
            sent = []
            box.deliver(lambda *args: sent.append(args) or {"ok": True}, allowed_channels=set())
            self.assertEqual(sent, [])
            self.assertEqual(len(box.data["pending"]), 1)
