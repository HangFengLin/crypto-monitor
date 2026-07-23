from __future__ import annotations

import math
from typing import Any

from data_client import (
    fetch_all_tickers,
    fetch_binance_spot_symbols,
    fetch_coingecko_top_market_symbols,
    fetch_okx_tickers,
    fetch_okx_trade_symbols,
    parse_float,
)

STABLE_BASE_ASSETS = {
    "USDT",
    "USDC",
    "FDUSD",
    "TUSD",
    "DAI",
    "USDE",
    "USDD",
    "BUSD",
    "PYUSD",
    "USDP",
    "USDS",
    "USD1",
    "U",
    "STABLE",
    "GUSD",
    "LUSD",
    "FRAX",
    "SUSD",
}


def build_market_cap_universe(
    top_n: int = 100,
    quote_asset: str = "USDT",
    min_quote_volume: float = 10_000_000,
) -> list[dict[str, Any]]:
    """Build a Binance spot universe from market-cap leaders, filtered by liquidity."""
    quote = quote_asset.upper()
    available_symbols = fetch_binance_spot_symbols(quote)
    tickers = fetch_all_tickers()
    market_cap_coins = fetch_coingecko_top_market_symbols(top_n)

    universe = []
    seen: set[str] = set()
    for coin in market_cap_coins:
        base = str(coin.get("symbol", "")).upper()
        if not base or base in STABLE_BASE_ASSETS:
            continue

        symbol = f"{base}{quote}"
        if symbol in seen or symbol not in available_symbols:
            continue

        ticker = tickers.get(symbol, {})
        quote_volume = parse_float(ticker.get("quoteVolume")) or 0.0
        if quote_volume < min_quote_volume:
            continue

        seen.add(symbol)
        universe.append(
            {
                "symbol": symbol,
                "base_asset": base,
                "quote_asset": quote,
                "market_cap_rank": coin.get("market_cap_rank"),
                "name": coin.get("name"),
                "quote_volume": quote_volume,
                "last_price": ticker.get("lastPrice"),
            }
        )
    return universe


def build_okx_market_cap_universe(
    top_n: int = 100,
    quote_asset: str = "USDT",
    instrument_type: str = "SWAP",
    min_quote_volume: float = 10_000_000,
) -> list[dict[str, Any]]:
    """Build a liquid OKX universe from market-cap leaders."""
    quote = quote_asset.upper()
    trade_symbols = fetch_okx_trade_symbols(quote, instrument_type)
    tickers = fetch_okx_tickers(instrument_type)
    market_cap_coins = fetch_coingecko_top_market_symbols(top_n)

    universe = []
    seen: set[str] = set()
    for coin in market_cap_coins:
        base = str(coin.get("symbol", "")).upper()
        if not base or base in STABLE_BASE_ASSETS:
            continue

        symbol = f"{base}{quote}"
        inst_id = trade_symbols.get(symbol)
        if not inst_id or symbol in seen:
            continue

        ticker = tickers.get(symbol, {})
        quote_volume = parse_float(ticker.get("quote_volume"))
        if quote_volume is None or not math.isfinite(quote_volume) or quote_volume < min_quote_volume:
            continue

        seen.add(symbol)
        universe.append(
            {
                "symbol": symbol,
                "inst_id": inst_id,
                "base_asset": base,
                "quote_asset": quote,
                "instrument_type": instrument_type.upper(),
                "market_cap_rank": coin.get("market_cap_rank"),
                "name": coin.get("name"),
                "quote_volume": quote_volume,
                "last_price": ticker.get("last_price"),
            }
        )
    return universe
