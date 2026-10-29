"""The VPS compresses bulk responses without buffering its live event stream."""

import asyncio
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class WebsiteCompressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(app, "PUBLIC_DIR", self.root),
                        patch.object(app, "REPORTS_DIR", self.root / "reports")]
        for replacement in self.patches:
            replacement.start()
        self.api = app.create_app()

    async def asyncTearDown(self):
        self.api.state.research_jobs.close()
        for replacement in reversed(self.patches):
            replacement.stop()
        self.temp.cleanup()

    async def request(self, path, accept_encoding):
        messages = []
        received = False
        never = asyncio.Event()

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await never.wait()

        async def send(message):
            messages.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
                 "query_string": b"", "root_path": "", "server": ("localhost", 80),
                 "client": ("127.0.0.1", 1234),
                 "headers": [(b"accept-encoding", accept_encoding.encode())]}
        await asyncio.wait_for(self.api(scope, receive, send), 2)
        start = next(message for message in messages if message["type"] == "http.response.start")
        headers = {key.decode().lower(): value.decode() for key, value in start["headers"]}
        body = b"".join(message.get("body", b"") for message in messages
                        if message["type"] == "http.response.body")
        return start["status"], headers, body

    async def test_large_state_is_compressed_on_the_wire_and_remains_identical_json(self):
        payload = {"events": [{"title": "测试消息", "body": "price" * 100}] * 40}
        with patch.object(app, "snapshot", return_value=payload):
            status, headers, body = await self.request("/api/state", "gzip, deflate")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-encoding"), "gzip")
        self.assertIn("Accept-Encoding", headers.get("vary", ""))
        self.assertEqual(json.loads(gzip.decompress(body)), payload)
        self.assertLess(len(body), len(json.dumps(payload).encode()) // 4)

    async def test_vps_javascript_is_compressed_too(self):
        asset = self.root / "site" / "assets" / "app.js"
        asset.parent.mkdir(parents=True)
        content = b"const price = 123;\n" * 500
        asset.write_bytes(content)
        status, headers, body = await self.request("/site/assets/app.js", "gzip")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-encoding"), "gzip")
        self.assertEqual(gzip.decompress(body), content)

    async def test_explicitly_unacceptable_gzip_and_identity_clients_get_plain_json(self):
        payload = {"prices": "123" * 1000}
        for encoding in ("identity", "gzip;q=0, identity;q=1"):
            with self.subTest(encoding=encoding), patch.object(app, "snapshot", return_value=payload):
                status, headers, body = await self.request("/api/state", encoding)
                self.assertEqual(status, 200)
                self.assertNotIn("content-encoding", headers)
                self.assertEqual(json.loads(body), payload)

    async def test_event_frames_remain_uncompressed_and_flush_before_stream_finishes(self):
        first_frame = asyncio.Event()
        allow_finish = asyncio.Event()
        frames = []

        async def stream(*args, **kwargs):
            yield "data: {\"price\":1}\n\n"
            await allow_finish.wait()
            yield ": heartbeat\n\n"

        original_call = self.api

        async def intercept(scope, receive, send):
            async def forward(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    frames.append(message["body"])
                    first_frame.set()
                await send(message)
            await original_call(scope, receive, forward)

        with patch.object(app, "state_event_stream", stream):
            self.api = intercept
            task = asyncio.create_task(self.request("/api/events", "gzip"))
            try:
                await asyncio.wait_for(first_frame.wait(), .5)
                self.assertEqual(frames[0], b'data: {"price":1}\n\n')
            finally:
                allow_finish.set()
                status, headers, body = await task
                self.api = original_call
        self.assertEqual(status, 200)
        self.assertNotIn("content-encoding", headers)
        self.assertEqual(body, b'data: {"price":1}\n\n: heartbeat\n\n')


if __name__ == "__main__":
    unittest.main()
