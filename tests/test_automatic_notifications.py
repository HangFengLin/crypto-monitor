import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
        with tempfile.TemporaryDirectory() as folder, patch("automatic_notifications.time.time", return_value=100) as clock:
            box = self.make(Path(folder) / "state.json")
            old = {"path": "old.html", "updated_at": 1}
            box.observe([], [old], None)
            new = {"path": "new.html", "updated_at": 2}
            box.observe([], [old, new], "market")
            box.observe([], [old, new], "market")
            box.observe([], [old, new], None)
            self.assertEqual([m["channel"] for m in box.data["pending"]], ["research", "system"])
            clock.return_value = 161
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

    def test_combination_changes_only_announce_new_fault_modules(self):
        with tempfile.TemporaryDirectory() as folder, patch("automatic_notifications.time.time", return_value=100) as clock:
            box = self.make(Path(folder) / "state.json")
            for timestamp, fault in (
                (100, "信号计算"),
                (105, "信号计算、站点巡检"),
                (110, "信号计算"),
                (115, "站点巡检、信号计算"),
                (120, "信号计算"),
            ):
                clock.return_value = timestamp
                box.observe([], [], fault)
            messages = box.data["pending"]
            self.assertEqual(len(messages), 2)
            self.assertIn("异常模块：站点巡检", messages[1]["body"])
            self.assertNotIn("信号计算", messages[1]["body"])

    def test_each_module_recovers_only_after_its_own_sixty_seconds(self):
        with tempfile.TemporaryDirectory() as folder, patch("automatic_notifications.time.time", return_value=100) as clock:
            box = self.make(Path(folder) / "state.json")
            box.observe([], [], "信号计算、站点巡检")
            for timestamp in (110, 169):
                clock.return_value = timestamp
                box.observe([], [], "信号计算")
            self.assertEqual(len(box.data["pending"]), 1)
            clock.return_value = 170
            box.observe([], [], "信号计算")
            self.assertEqual([m["title"] for m in box.data["pending"]], ["监控异常", "监控恢复"])
            self.assertIn("站点巡检", box.data["pending"][-1]["body"])
            self.assertNotIn("信号计算", box.data["pending"][-1]["body"])
            clock.return_value = 180
            box.observe([], [], "信号计算、站点巡检")
            self.assertEqual(len(box.data["pending"]), 3)
            self.assertIn("异常模块：站点巡检", box.data["pending"][-1]["body"])
            self.assertNotIn("信号计算", box.data["pending"][-1]["body"])

    def test_module_recovery_wait_survives_restart_and_brief_relapse(self):
        with tempfile.TemporaryDirectory() as folder, patch("automatic_notifications.time.time", return_value=100) as clock:
            path = Path(folder) / "state.json"
            box = self.make(path)
            box.observe([], [], "信号计算、站点巡检")
            clock.return_value = 110
            box.observe([], [], "信号计算")
            box = self.make(path)
            clock.return_value = 150
            box.observe([], [], "信号计算、站点巡检")
            self.assertEqual(len(box.data["pending"]), 1)
            clock.return_value = 160
            box.observe([], [], "信号计算")
            box = self.make(path)
            clock.return_value = 219
            box.observe([], [], "信号计算")
            self.assertEqual(len(box.data["pending"]), 1)
            clock.return_value = 220
            box.observe([], [], "信号计算")
            self.assertEqual([m["title"] for m in box.data["pending"]], ["监控异常", "监控恢复"])

    def test_legacy_fault_string_migrates_without_reannouncing_active_modules(self):
        import json

        with tempfile.TemporaryDirectory() as folder, patch("automatic_notifications.time.time", return_value=130) as clock:
            path = Path(folder) / "state.json"
            path.write_text(json.dumps({
                "initialized": True,
                "trades": {},
                "reports": {},
                "pending": [],
                "fault": "信号计算、站点巡检",
                "recovery_since": 100,
                "last_delivery": None,
            }))
            box = self.make(path)
            box.observe([], [], "信号计算")
            self.assertEqual(box.data["pending"], [])
            clock.return_value = 160
            box.observe([], [], "信号计算")
            self.assertEqual([m["title"] for m in box.data["pending"]], ["监控恢复"])
            self.assertIn("站点巡检", box.data["pending"][0]["body"])
            self.assertNotIn("信号计算", box.data["pending"][0]["body"])

    def test_legacy_signal_fault_transfers_to_each_renamed_module_without_resending(self):
        labels = (
            "策略信号", "技术指标", "支撑位监控", "模拟持仓行情",
            "模拟币池扫描", "模拟币池更新", "模拟交易账本", "事件记录",
        )
        for label in labels:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as folder, patch(
                "automatic_notifications.time.time", return_value=100,
            ) as clock:
                box = self.make(Path(folder) / "state.json")
                box.data.update(fault="信号计算", recovery_since=50)
                box.save()
                box = self.make(box.path)
                box.observe([], [], label)
                clock.return_value = 161
                box.observe([], [], label)
                self.assertEqual(box.data["pending"], [])

    def test_legacy_signal_rename_after_startup_preserves_other_fault_modules(self):
        with tempfile.TemporaryDirectory() as folder, patch("automatic_notifications.time.time", return_value=100) as clock:
            box = self.make(Path(folder) / "state.json")
            box.data.update(fault="信号计算、行情、站点巡检", recovery_since=None)
            box.save()
            box = self.make(box.path)
            box.observe([], [], None)
            clock.return_value = 130
            box.observe([], [], "模拟持仓行情、行情")
            self.assertEqual(box.data["pending"], [])
            clock.return_value = 160
            box.observe([], [], "模拟持仓行情、行情")
            self.assertEqual([m["title"] for m in box.data["pending"]], ["监控恢复"])
            self.assertIn("站点巡检", box.data["pending"][0]["body"])
            self.assertNotIn("信号计算", box.data["pending"][0]["body"])
            self.assertNotIn("行情", box.data["pending"][0]["body"])
            clock.return_value = 170
            box.observe([], [], "模拟持仓行情、行情、策略信号")
            self.assertEqual(len(box.data["pending"]), 2)
            self.assertIn("异常模块：策略信号", box.data["pending"][-1]["body"])

    def test_legacy_signal_fault_still_recovers_when_no_renamed_module_is_active(self):
        with tempfile.TemporaryDirectory() as folder, patch("automatic_notifications.time.time", return_value=100) as clock:
            box = self.make(Path(folder) / "state.json")
            box.data.update(fault="信号计算", recovery_since=None)
            box.save()
            box = self.make(box.path)
            box.observe([], [], None)
            clock.return_value = 159
            box.observe([], [], None)
            self.assertEqual(box.data["pending"], [])
            clock.return_value = 160
            box.observe([], [], None)
            self.assertEqual([m["title"] for m in box.data["pending"]], ["监控恢复"])
            self.assertIn("信号计算", box.data["pending"][0]["body"])

    def test_paused_channel_keeps_queue_and_does_not_send(self):
        with tempfile.TemporaryDirectory() as folder:
            box = self.make(Path(folder) / "state.json")
            box.enqueue("one", "signal", "test", "body")
            sent = []
            box.deliver(lambda *args: sent.append(args) or {"ok": True}, allowed_channels=set())
            self.assertEqual(sent, [])
            self.assertEqual(len(box.data["pending"]), 1)
