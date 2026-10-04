import asyncio
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import data_client


class FuturesResearchSourceTest(unittest.TestCase):
    def test_usdm_open_interest_uses_symbol_in_sync_and_async_transports(self):
        def response(url, **kwargs):
            params = parse_qs(urlparse(url).query)
            if params == {"symbol": ["BTCUSDT"], "period": ["15m"], "limit": ["20"]}:
                return [{"sumOpenInterest": "10"}, {"sumOpenInterest": "15"}]
            return []

        async def async_response(url, **kwargs):
            return response(url, **kwargs)

        with patch.object(data_client, "read_json_url", side_effect=response), patch.object(
            data_client, "async_read_json_url", side_effect=async_response
        ):
            self.assertEqual(data_client.fetch_open_interest_ratio("BTCUSDT")["openInterestRatio"], 1.5)
            self.assertEqual(asyncio.run(data_client.async_fetch_open_interest_ratio("BTCUSDT"))["openInterestRatio"], 1.5)

    def test_historical_futures_uses_fapi_and_excludes_unclosed_bar(self):
        rows = [[1000, "1", "2", "1", "2", "5", 1999], [2000, "2", "3", "2", "3", "5", 9999999999999]]
        with (
            patch.object(data_client, "read_json_url", return_value=rows) as request,
            patch.object(data_client.time, "sleep"),
        ):
            bars = data_client.fetch_historical_klines("BTCUSDT", "15m", 2, market="binance_usdm")
        self.assertTrue(request.call_args.args[0].startswith("https://fapi.binance.com/fapi/v1/klines?"))
        self.assertEqual(len(bars), 1)
