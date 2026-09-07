import asyncio
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app
import data_client


class PerformanceFixesTest(unittest.TestCase):
    def test_retry_limit_has_no_final_sleep(self):
        error = urllib.error.HTTPError('https://example.test', 429, 'limited', {}, None)
        with patch.object(data_client.urllib.request, 'urlopen', side_effect=error) as request, patch.object(data_client.time, 'sleep') as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                data_client.read_json_url('https://example.test')
        self.assertEqual(request.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.75, 1.5])

    def test_retry_budget_does_not_wait_past_deadline(self):
        error = urllib.error.HTTPError('https://example.test', 429, 'limited', {'Retry-After': '100'}, None)
        with patch.object(data_client.urllib.request, 'urlopen', side_effect=error) as request, patch.object(data_client.time, 'sleep') as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                data_client.read_json_url('https://example.test', max_elapsed=2)
        self.assertLessEqual(request.call_args.kwargs['timeout'], 2)
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_tail_reader_avoids_full_text_read_and_preserves_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            lines = [json.dumps({'i': i, 'text': '\u4e2d' * 30}, ensure_ascii=False) for i in range(3000)]
            lines.extend(['broken', '[]', '', json.dumps({'i': 3000})])
            path.write_text('\n'.join(lines), encoding='utf-8')
            expected = [json.loads(line) for line in lines[-30:] if line.startswith('{')][-10:]
            with patch.object(Path, 'read_text', side_effect=AssertionError('full read')):
                self.assertEqual(app.read_recent_jsonl(path, 10), expected)
            self.assertEqual(app.read_recent_jsonl(path, 0), [])

    def test_health_reads_event_log_once(self):
        events = [{'type': 'error', 'i': i} for i in range(150)]
        with patch.object(app, 'read_recent_jsonl', return_value=events) as read, patch.object(app, 'load_okx_bot_state_payload', return_value=({}, None)), patch.object(app, 'summarize_okx_bot_status', return_value={}):
            result = app.load_okx_bot_health_detail()
        self.assertEqual(read.call_count, 1)
        self.assertEqual(result['recent_errors'], list(reversed(events[-10:])))

    def test_round_cache_shares_sync_async_and_isolates_mutations(self):
        async def run():
            for _ in range(2):
                with app.monitor_kline_cache():
                    first = await app.monitor_klines_async('BTCUSDT', '15m', 1000)
                    first[0]['close'] = 999
                    second = await asyncio.to_thread(app.monitor_klines, 'BTCUSDT', '15m', 1000)
                    self.assertEqual(second, [{'close': 1}])
        with patch.object(app, 'async_fetch_klines', new_callable=AsyncMock, return_value=[{'close': 1}]) as fetch, patch.object(app, 'fetch_klines') as sync:
            asyncio.run(run())
        self.assertEqual(fetch.call_count, 2)
        sync.assert_not_called()

    def test_concurrent_cache_requests_share_one_fetch(self):
        async def fetch(*args):
            await asyncio.sleep(0)
            return [{'close': 1}]

        async def run():
            with app.monitor_kline_cache():
                results = await asyncio.gather(*[
                    app.monitor_klines_async('BTCUSDT', '15m', 1000) for _ in range(5)
                ])
                self.assertEqual(results, [[{'close': 1}]] * 5)
                await app.monitor_klines_async('BTCUSDT', '15m', 300)
            self.assertIsNone(app._monitor_klines_cache.get())

        with patch.object(app, 'async_fetch_klines', side_effect=fetch) as request:
            asyncio.run(run())
        self.assertEqual(request.call_count, 2)

    def test_cache_context_resets_after_exception(self):
        with self.assertRaises(RuntimeError):
            with app.monitor_kline_cache():
                raise RuntimeError('failed round')
        self.assertIsNone(app._monitor_klines_cache.get())

    def test_default_scan_reuses_klines(self):
        async def run(cached):
            watchlist = [{'symbol': 'BTCUSDT'}, {'symbol': 'ETHUSDT'}]
            async def scan():
                await app.evaluate_support_alerts_async(watchlist, {})
                await app.evaluate_chanlun_signals_async(watchlist)
                await app.evaluate_indicator_signals_async(watchlist)
            if cached:
                with app.monitor_kline_cache():
                    await scan()
            else:
                await scan()

        counts = []
        for cached in (False, True):
            with patch.object(app, 'fetch_klines', return_value=[]) as sync, patch.object(app, 'async_fetch_klines', new_callable=AsyncMock, return_value=[]) as fetch, patch.object(app, 'record_event'), patch.object(app, 'send_alert_notifications_async', new_callable=AsyncMock):
                asyncio.run(run(cached))
                counts.append(sync.call_count + fetch.call_count)
        self.assertGreater(counts[0], counts[1])
        # Empty bars skip the strategy's higher-timeframe fetch.
        self.assertEqual(counts, [18, 8])
