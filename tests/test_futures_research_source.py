import unittest
from unittest.mock import patch

import data_client


class FuturesResearchSourceTest(unittest.TestCase):
    def test_historical_futures_uses_fapi_and_excludes_unclosed_bar(self):
        rows = [[1000, "1", "2", "1", "2", "5", 1999], [2000, "2", "3", "2", "3", "5", 9999999999999]]
        with (
            patch.object(data_client, "read_json_url", return_value=rows) as request,
            patch.object(data_client.time, "sleep"),
        ):
            bars = data_client.fetch_historical_klines("BTCUSDT", "15m", 2, market="binance_usdm")
        self.assertTrue(request.call_args.args[0].startswith("https://fapi.binance.com/fapi/v1/klines?"))
        self.assertEqual(len(bars), 1)
