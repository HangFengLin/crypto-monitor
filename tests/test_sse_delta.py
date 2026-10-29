import asyncio
import json
import unittest
from unittest.mock import patch

import app


class StreamClock:
    def __init__(self, update=None):
        self.now = 0
        self.update = update

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        if self.update:
            self.update()


def unpack(frame):
    return json.loads(frame.removeprefix("data: "))


class DeltaStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_initial_snapshot_then_patch_preserves_null_zero_deletion_and_reconnect(self):
        state = {"prices": {"BTC": 1}, "zero": 5, "empty": "x", "delete": 7, "large": "x" * 10000}
        def change():
            state.update(prices={"BTC": 0}, zero=0, empty=None)
            state.pop("delete", None)
        clock = StreamClock(change)
        with patch.object(app, "snapshot", side_effect=lambda: state), \
             patch.object(app, "time", clock), patch.object(app.asyncio, "sleep", clock.sleep):
            stream = app.state_event_stream("full", delta=True)
            first = unpack(await stream.__anext__())
            self.assertEqual(first["type"], "snapshot")
            self.assertEqual(first["seq"], 0)
            self.assertEqual(first["state"]["delete"], 7)
            update = unpack(await stream.__anext__())
            self.assertEqual(update["type"], "patch")
            self.assertEqual(update["seq"], 1)
            self.assertEqual(update["set"], {"prices": {"BTC": 0}, "zero": 0, "empty": None})
            self.assertEqual(update["remove"], ["delete"])
            self.assertLess(len(json.dumps(update)), len(json.dumps(first)) / 10)
            await stream.aclose()
            reconnect = app.state_event_stream("full", delta=True)
            new_first = unpack(await reconnect.__anext__())
            self.assertEqual(new_first["type"], "snapshot")
            self.assertEqual(new_first["seq"], 0)
            self.assertEqual(new_first["state"], state)
            await reconnect.aclose()

    async def test_delta_idle_heartbeat_and_existing_stream_format(self):
        clock = StreamClock()
        with patch.object(app, "snapshot", return_value={"a": None}), \
             patch.object(app, "time", clock), patch.object(app.asyncio, "sleep", clock.sleep):
            stream = app.state_event_stream("compact", delta=True)
            self.assertEqual(unpack(await stream.__anext__())["state"], {"a": None})
            self.assertEqual(await stream.__anext__(), ": heartbeat\n\n")
            self.assertEqual(clock.now, 15)
            await stream.aclose()
            existing = app.state_event_stream("compact")
            self.assertEqual(unpack(await existing.__anext__()), {"a": None})
            await existing.aclose()

    async def test_route_exposes_delta_without_changing_default_contract(self):
        application = app.create_app()
        endpoint = next(route.endpoint for route in application.routes if getattr(route, "path", None) == "/api/events")
        try:
            with patch.object(app, "snapshot", return_value={"watchlist": []}):
                response = await endpoint(detail="compact", delta=True)
                self.assertEqual(unpack(await response.body_iterator.__anext__())["state"], {"watchlist": []})
                await response.body_iterator.aclose()
                old_response = await endpoint(detail="compact")
                self.assertEqual(unpack(await old_response.body_iterator.__anext__()), {"watchlist": []})
                await old_response.body_iterator.aclose()
        finally:
            application.state.research_jobs.close()

    async def test_http_query_enables_delta_and_flushes_initial_snapshot(self):
        application = app.create_app()
        done = asyncio.Event()
        never = asyncio.Event()
        messages = []
        first = True
        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await never.wait()
        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                done.set()
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "GET", "scheme": "http", "path": "/api/events", "raw_path": b"/api/events",
                 "query_string": b"detail=compact&delta=1", "root_path": "", "server": ("localhost", 80),
                 "client": ("127.0.0.1", 1234), "headers": [(b"accept-encoding", b"gzip")]}
        try:
            with patch.object(app, "snapshot", return_value={"prices": {"BTC": 0}}):
                task = asyncio.create_task(application(scope, receive, send))
                await asyncio.wait_for(done.wait(), 2)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            response = next(item for item in messages if item["type"] == "http.response.start")
            self.assertEqual(response["status"], 200)
            self.assertNotIn(b"content-encoding", dict(response["headers"]))
            body = next(item["body"] for item in messages if item["type"] == "http.response.body" and item.get("body"))
            self.assertEqual(unpack(body.decode())["state"], {"prices": {"BTC": 0}})
        finally:
            application.state.research_jobs.close()
