from __future__ import annotations

import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import data_client
import strategy_universe


class OkxUniverseTest(unittest.TestCase):
    def test_public_json_request_backs_off_on_rate_limit(self) -> None:
        rate_limit = urllib.error.HTTPError("https://example.test", 429, "rate limited", {}, None)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"code":"0"}'

        with patch.object(data_client.urllib.request, "urlopen", side_effect=[rate_limit, response]), patch.object(
            data_client.time, "sleep"
        ) as sleep:
            payload = data_client.read_json_url("https://example.test", attempts=2)

        self.assertEqual(payload, {"code": "0"})
        sleep.assert_called_once_with(0.75)

    def test_fetch_okx_tickers_estimates_derivative_quote_volume(self) -> None:
        payload = {
            "code": "0",
            "data": [{"instId": "BTC-USDT-SWAP", "last": "50000", "volCcy24h": "250"}],
        }

        with patch.object(data_client, "read_json_url", return_value=payload):
            tickers = data_client.fetch_okx_tickers("SWAP")

        self.assertEqual(tickers["BTCUSDT"]["quote_volume"], 12_500_000)
        self.assertEqual(tickers["BTCUSDT"]["last_price"], 50_000)

    def test_okx_universe_expands_only_with_liquid_contracts(self) -> None:
        coins = [
            {"symbol": "btc", "market_cap_rank": 1, "name": "Bitcoin"},
            {"symbol": "thin", "market_cap_rank": 150, "name": "Thin Coin"},
        ]
        symbols = {"BTCUSDT": "BTC-USDT-SWAP", "THINUSDT": "THIN-USDT-SWAP"}
        tickers = {
            "BTCUSDT": {"quote_volume": 20_000_000, "last_price": 50_000},
            "THINUSDT": {"quote_volume": 2_000_000, "last_price": 1},
        }

        with patch.object(strategy_universe, "fetch_coingecko_top_market_symbols", return_value=coins), patch.object(
            strategy_universe, "fetch_okx_trade_symbols", return_value=symbols
        ), patch.object(strategy_universe, "fetch_okx_tickers", return_value=tickers):
            universe = strategy_universe.build_okx_market_cap_universe(200, "USDT", "SWAP", 10_000_000)

        self.assertEqual([item["symbol"] for item in universe], ["BTCUSDT"])
        self.assertEqual(universe[0]["quote_volume"], 20_000_000)


if __name__ == "__main__":
    unittest.main()
