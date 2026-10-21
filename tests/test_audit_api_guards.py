"""Regression for destructive malformed writes and shared-pool starvation."""
import asyncio
import copy
import gc
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app


class WatchlistRequestTest(unittest.TestCase):
    def test_malformed_payload_preserves_saved_list_and_pending_state(self):
        invalid = ({}, {"watchlist": "BTCUSDT"}, {"watchlist": [{}]},
                   {"watchlist": [None]}, {"watchlist": [{"symbol": 123}]},
                   {"watchlist": [{"symbol": "BTCUSDT"}, {}]})
        for payload in invalid:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                saved = [{"symbol": "BTCUSDT", "interval": "15m"}]
                path = root / "watchlist.json"
                original = json.dumps(saved).encode()
                path.write_bytes(original)
                state = app.MonitorState()
                state.watchlist = copy.deepcopy(saved)
                state.sent_alerts = {"original"}
                state.pending_buy_divergences = {"BTCUSDT:15m": {"strength": 1}}
                with patch.object(app, "STATE_FILE", path), patch.object(app, "state", state), \
                     patch.object(app, "SIGNAL_ENGINES", {("test",): object()}), \
                     patch.object(app, "PUBLIC_DIR", root), patch.object(app, "REPORTS_DIR", root), \
                     patch.object(app, "invalid_watchlist_symbols", return_value=[]):
                    response = TestClient(app.create_app()).post("/api/watchlist", json=payload)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(state.watchlist, saved)
                    self.assertEqual(state.sent_alerts, {"original"})
                    self.assertEqual(state.pending_buy_divergences, {"BTCUSDT:15m": {"strength": 1}})
                    self.assertEqual(len(app.SIGNAL_ENGINES), 1)

    def test_explicit_empty_list_still_clears_monitoring(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(app, "STATE_FILE", root / "watchlist.json"), \
                 patch.object(app, "state", app.MonitorState()), \
                 patch.object(app, "PUBLIC_DIR", root), patch.object(app, "REPORTS_DIR", root):
                response = TestClient(app.create_app()).post("/api/watchlist", json={"watchlist": []})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(app.STATE_FILE.read_text()), [])


class BacktestAdmissionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        for replacement in (patch.object(app, "PUBLIC_DIR", root), patch.object(app, "REPORTS_DIR", root)):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.api = app.create_app()
        self.backtest = next(route.endpoint for route in self.api.routes if route.path == "/api/backtest")
        self.list_runs = next(route.endpoint for route in self.api.routes if route.path == "/api/research-runs")

    async def asyncTearDown(self):
        runner = getattr(self.api.state, "research_jobs", None)
        if runner is not None:
            runner.close()
        self.temp.cleanup()

    async def wait_started(self, started):
        for _ in range(200):
            if started.is_set():
                return
            await asyncio.sleep(.005)
        self.fail("research task never started")

    async def test_busy_research_rejects_new_job_and_preserves_monitor_pool(self):
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        started, release = threading.Event(), threading.Event()

        def work(*args):
            started.set()
            release.wait(3)
            return {"ok": True}

        with patch.object(app, "run_signal_backtest_summary", side_effect=work), \
             patch.object(app.RESEARCH_ARCHIVE, "list", return_value=[]):
            first = asyncio.create_task(self.backtest())
            try:
                await self.wait_started(started)
                try:
                    listing = await asyncio.wait_for(self.list_runs(), timeout=.3)
                except TimeoutError:
                    self.fail("backtest starved the shared monitoring executor")
                self.assertEqual(listing, {"runs": []})
                with self.assertRaises(app.HTTPException) as caught:
                    await self.backtest()
                self.assertEqual(caught.exception.status_code, 429)
            finally:
                release.set()
                await first

    async def test_cancelled_request_keeps_capacity_until_actual_worker_finishes(self):
        started, release = threading.Event(), threading.Event()
        calls = []

        def work(*args):
            calls.append(1)
            started.set()
            if len(calls) == 1:
                release.wait(3)
            return {"ok": True}

        with patch.object(app, "run_signal_backtest_summary", side_effect=work):
            first = asyncio.create_task(self.backtest())
            try:
                await self.wait_started(started)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                with self.assertRaises(app.HTTPException) as caught:
                    await self.backtest()
                self.assertEqual(caught.exception.status_code, 429)
                self.assertEqual(calls, [1])
            finally:
                release.set()

    async def test_cancelled_request_consumes_late_worker_exception_and_recovers_capacity(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        calls, unhandled = [], []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

        def work(*args):
            calls.append(1)
            if len(calls) == 1:
                started.set()
                try:
                    release.wait(3)
                    raise ValueError("late research failure")
                finally:
                    finished.set()
            return {"ok": True}

        try:
            with patch.object(app, "run_signal_backtest_summary", side_effect=work):
                first = asyncio.create_task(self.backtest())
                await self.wait_started(started)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                with self.assertRaises(app.HTTPException) as caught:
                    await self.backtest()
                self.assertEqual(caught.exception.status_code, 429)
                release.set()
                await self.wait_started(finished)
                for _ in range(200):
                    try:
                        result = await self.backtest()
                        break
                    except app.HTTPException as error:
                        self.assertEqual(error.status_code, 429)
                        await asyncio.sleep(.005)
                else:
                    self.fail("failed cancelled worker did not release research capacity")
                self.assertEqual(result, {"ok": True})
                # Force abandoned asyncio wrappers to report any exception
                # after the cancelled HTTP coroutine has lost its awaiter.
                for _ in range(3):
                    await asyncio.sleep(0)
                    gc.collect()
                self.assertEqual(unhandled, [], "cancelled requests must consume late worker exceptions")
        finally:
            release.set()
            loop.set_exception_handler(previous_handler)

    async def test_awaited_worker_failure_returns_502_and_releases_capacity(self):
        with patch.object(app, "run_signal_backtest_summary",
                          side_effect=[ValueError("research failed"), {"ok": True}]):
            with self.assertRaises(app.HTTPException) as caught:
                await self.backtest()
            self.assertEqual(caught.exception.status_code, 502)
            self.assertEqual(await self.backtest(), {"ok": True})

    async def test_large_evidence_scan_also_preserves_monitoring_capacity(self):
        import research_evidence

        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        evidence = next(route.endpoint for route in self.api.routes if route.path == "/api/research-evidence")
        started, release = threading.Event(), threading.Event()

        def scan(*args):
            started.set()
            release.wait(3)
            return {"validations": []}

        with patch.object(research_evidence, "list_validation_evidence", side_effect=scan), \
             patch.object(app.RESEARCH_ARCHIVE, "list", return_value=[]):
            first = asyncio.create_task(evidence())
            try:
                await self.wait_started(started)
                try:
                    listing = await asyncio.wait_for(self.list_runs(), timeout=.3)
                except TimeoutError:
                    self.fail("full replay verification starved the monitoring executor")
                self.assertEqual(listing, {"runs": []})
                with self.assertRaises(app.HTTPException) as caught:
                    await self.backtest()
                self.assertEqual(caught.exception.status_code, 429)
            finally:
                release.set()
                await first


if __name__ == "__main__":
    unittest.main()
