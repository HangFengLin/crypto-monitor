import asyncio
import json
import unittest
from unittest.mock import patch

import app

REAL_SLEEP = asyncio.sleep


class Clock:
    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.on_sleep = on_sleep

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self.now)
        await REAL_SLEEP(0)


class StateEventStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_connection_gets_snapshot_immediately_then_idle_comment_at_fifteen_seconds(self):
        # A duplicate data frame would consume bandwidth every poll instead of
        # keeping an idle connection alive with a small comment.
        clock = Clock()
        value = {"updated_at": 100, "prices": {"BTCUSDT": {"lastPrice": 0}}, "last_error": None}
        with patch.object(app, "snapshot", return_value=value), \
             patch.object(app, "time", clock), \
             patch.object(app.asyncio, "sleep", clock.sleep):
            stream = app.state_event_stream()
            first = await anext(stream)
            self.assertEqual(json.loads(first.removeprefix("data: ")), value)
            self.assertEqual(clock.now, 0)
            idle = await anext(stream)
            self.assertEqual(idle, ": heartbeat\n\n")
            self.assertEqual(clock.now, 15)
            await stream.aclose()

    async def test_price_timestamp_error_and_watchlist_changes_are_published_without_waiting_for_heartbeat(self):
        # Comparing only a subset (for example prices or updated_at) loses
        # valid non-price changes at an equal market scan timestamp.
        for change in (
            {"updated_at": 101},
            {"prices": {"BTCUSDT": {"lastPrice": 1}}},
            {"last_error": "行情暂时不可用"},
            {"watchlist": []},
        ):
            with self.subTest(change=change):
                value = {"updated_at": 100, "prices": {"BTCUSDT": {"lastPrice": 0}},
                         "last_error": None, "watchlist": [{"symbol": "BTCUSDT"}]}
                clock = Clock(lambda _now, current=value, update=change: current.update(update))
                with patch.object(app, "snapshot", side_effect=lambda current=value: current), \
                     patch.object(app, "time", clock), \
                     patch.object(app.asyncio, "sleep", clock.sleep):
                    stream = app.state_event_stream()
                    await anext(stream)
                    updated = await anext(stream)
                    self.assertTrue(updated.startswith("data: "))
                    self.assertEqual(json.loads(updated.removeprefix("data: ")), value)
                    self.assertEqual(clock.now, 3)
                    await stream.aclose()

    async def test_dictionary_key_reordering_is_not_a_business_update(self):
        value = {"updated_at": 100, "prices": {"BTCUSDT": {"lastPrice": 0, "priceChange": None}}}

        def reorder(_now):
            value["prices"]["BTCUSDT"] = {"priceChange": None, "lastPrice": 0}

        clock = Clock(reorder)
        with patch.object(app, "snapshot", side_effect=lambda: value), \
             patch.object(app, "time", clock), \
             patch.object(app.asyncio, "sleep", clock.sleep):
            stream = app.state_event_stream()
            await anext(stream)
            self.assertEqual(await anext(stream), ": heartbeat\n\n")
            await stream.aclose()

    async def test_reconnection_receives_current_snapshot_even_when_previous_connection_sent_it(self):
        value = {"updated_at": 100, "prices": {"BTCUSDT": {"lastPrice": 0}}}
        with patch.object(app, "snapshot", return_value=value):
            first_connection = app.state_event_stream()
            first = await anext(first_connection)
            await first_connection.aclose()
            second_connection = app.state_event_stream()
            self.assertEqual(await anext(second_connection), first)
            await second_connection.aclose()

    async def test_waiting_stream_is_promptly_cancellable(self):
        sleeping = asyncio.Event()

        async def wait_forever(_seconds):
            sleeping.set()
            await asyncio.Event().wait()

        with patch.object(app, "snapshot", return_value={"updated_at": 100}), \
             patch.object(app.asyncio, "sleep", wait_forever):
            stream = app.state_event_stream()
            await anext(stream)
            pending = asyncio.create_task(anext(stream))
            await sleeping.wait()
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            await stream.aclose()


if __name__ == "__main__":
    unittest.main()
