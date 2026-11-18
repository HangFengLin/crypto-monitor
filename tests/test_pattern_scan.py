"""Actual scan/runtime integration with isolated public-market fixtures and storage."""

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import app
from pattern_runtime import INTERVALS, PatternRuntime

TARGET = 1_800_000_000_000  # Aligned to both 15-minute and one-hour boundaries.


def candle(index, interval, *, close=100, high=101, low=99):
    span = INTERVALS[interval]
    end = TARGET + (index - 40) * span
    return {"open_time": end - span, "close_time": end - 1, "open": 100,
            "high": high, "low": low, "close": close, "volume": 10}


class PatternScanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = PatternRuntime(self.root / "patterns.json")
        self.trades = [{"id": "existing-paper", "symbol": "ETHUSDT", "status": "open", "entry_price": 100}]
        for replacement in (
            patch.object(app, "PATTERN_RUNTIME", self.runtime),
            patch.object(app, "state", app.MonitorState(
                watchlist=[{"symbol": "ETHUSDT"}], strategy_trades=copy.deepcopy(self.trades))),
            patch.object(app, "EVENT_LOG_FILE", self.root / "events.jsonl"),
            patch.object(app, "STRATEGY_TRADES_FILE", self.root / "paper.json"),
            patch.object(app.time, "time", return_value=(TARGET + 10_000) / 1000),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def warm(self, interval):
        rows = [candle(index, interval) for index in range(40)]
        self.runtime.ingest("ETHUSDT", interval, rows, observed_at_ms=rows[-1]["close_time"] + 1000)

    def records(self):
        path = self.root / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    async def test_closed_timeframes_share_one_symbol_message_and_future_bar_is_not_ingested(self):
        for interval in INTERVALS:
            self.warm(interval)

        async def market(symbol, interval, limit):
            self.assertEqual(symbol, "ETHUSDT")
            self.assertEqual(limit, 4)
            return [candle(39, interval), candle(40, interval, close=102, high=103, low=100),
                    candle(41, interval, close=104, high=105, low=100)]

        with patch.object(app, "fetch_pattern_bars", new=market):
            await app.scan_patterns_once()
        records = self.records()
        raw = [row for row in records if row["type"] == "chart_pattern"]
        groups = [row for row in records if row["type"] == "observation_batch"]
        self.assertEqual(len(raw), 2)
        self.assertEqual(len(groups), 1)
        self.assertEqual([row["bar_close_time"] for row in raw], [TARGET - 1, TARGET - 1])
        self.assertTrue(all(row["origin"] == "live" for row in raw))
        self.assertIn("15m", groups[0]["alerts"][0]["body"])
        self.assertIn("1h", groups[0]["alerts"][0]["body"])
        for interval in INTERVALS:
            self.assertEqual(self.runtime.last_close("ETHUSDT", interval), TARGET - 1)
        self.assertEqual(app.state.strategy_trades, self.trades)
        self.assertFalse((self.root / "paper.json").exists())
        self.assertIsNone(app.state.module_errors["patterns"])

    async def test_one_public_market_failure_is_visible_and_does_not_touch_paper_positions(self):
        for interval in INTERVALS:
            self.warm(interval)

        async def market(symbol, interval, limit):
            if interval == "1h":
                raise TimeoutError("private diagnostic text must not enter public status")
            return [candle(40, interval, close=102, high=103, low=100)]

        with patch.object(app, "fetch_pattern_bars", new=market):
            await app.scan_patterns_once()
        snapshot = self.runtime.snapshot(now_ms=TARGET + 10_000)
        self.assertEqual(snapshot["status"], "partial")
        self.assertEqual(snapshot["errors"], [{"symbol": "ETHUSDT", "interval": "1h", "error_class": "TimeoutError"}])
        self.assertNotIn("private diagnostic", json.dumps(snapshot))
        self.assertTrue(app.state.module_errors["patterns"])
        self.assertEqual(app.state.strategy_trades, self.trades)

    async def test_already_processed_closed_bars_are_not_requested_again(self):
        for interval in INTERVALS:
            self.warm(interval)
            row = candle(40, interval)
            self.runtime.ingest("ETHUSDT", interval, [row], observed_at_ms=TARGET + 1000)
        before = (self.root / "patterns.json").read_bytes()

        async def forbidden_market(*args):
            self.fail("a completed pattern bar was fetched again")

        with patch.object(app, "fetch_pattern_bars", new=forbidden_market):
            await app.scan_patterns_once()
        self.assertEqual((self.root / "patterns.json").read_bytes(), before)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.runtime.snapshot(now_ms=TARGET + 10_000)["status"], "healthy")

    async def test_an_unfinished_only_response_does_not_advance_or_hide_failure(self):
        for interval in INTERVALS:
            self.warm(interval)
        before = (self.root / "patterns.json").read_bytes()

        async def unfinished(symbol, interval, limit):
            return [candle(41, interval, close=102, high=103, low=100)]

        with patch.object(app, "fetch_pattern_bars", new=unfinished):
            await app.scan_patterns_once()
        self.assertEqual((self.root / "patterns.json").read_bytes(), before)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.runtime.snapshot(now_ms=TARGET + 10_000)["status"], "partial")
        self.assertEqual(app.state.strategy_trades, self.trades)

    async def test_public_reader_uses_futures_endpoint_and_preserves_exchange_times(self):
        row = candle(40, "15m", close=102, high=103, low=100)
        encoded = [[row["open_time"], "100", "103", "100", "102", "10", row["close_time"]]]
        requests = []

        async def public_reader(url, **kwargs):
            requests.append(url)
            return encoded

        with patch.object(app, "async_read_json_url", new=public_reader):
            result = await app.fetch_pattern_bars("ETHUSDT", "15m", 240)
        self.assertEqual(result, [row])
        parsed = urlparse(requests[0])
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                         ("https", "fapi.binance.com", "/fapi/v1/klines"))
        self.assertEqual(parse_qs(parsed.query), {"symbol": ["ETHUSDT"], "interval": ["15m"], "limit": ["240"]})

    async def test_background_loop_reports_storage_failure_without_raising_into_other_loops(self):
        stop = asyncio.Event()

        async def failed_scan():
            stop.set()
            raise OSError("isolated fixture failure")

        with patch.object(app, "scan_patterns_once", new=failed_scan):
            await app.pattern_monitor_loop(stop)
        self.assertIn("OSError", app.state.module_errors["patterns"])
        self.assertEqual(app.state.strategy_trades, self.trades)
