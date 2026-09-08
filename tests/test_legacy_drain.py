import unittest
from types import SimpleNamespace

from scripts.legacy_okx_drain import configure


class LegacyDrainTest(unittest.TestCase):
    def test_only_exits_then_stops_after_exchange_flat(self):
        calls = []
        positions = [{}]
        bot = SimpleNamespace(
            scan_once=lambda args, state: calls.append(args.max_open_positions),
            active_positions=lambda state: positions,
            startup_position_snapshot=lambda args, state: ({}, {}),
            append_event=lambda event: calls.append(event["type"]),
        )
        configure(bot)
        args = SimpleNamespace(max_open_positions=5)
        bot.scan_once(args, {})
        self.assertEqual(calls, [0])
        with self.assertRaises(RuntimeError):
            bot.open_position()
        positions.clear()
        with self.assertRaises(SystemExit) as stopped:
            bot.scan_once(args, {})
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("drain_complete", calls)
