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
    read_json_url,
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


def select_binance_paper_universe(coins, instruments, top_n=100, selection="exchange_top_n"):
    """Rank eligible USD-M perpetuals by CoinGecko market cap, never volume."""
    by_base = {}
    for coin in coins:
        rank = coin.get("market_cap_rank")
        base = str(coin.get("symbol", "")).upper()
        if not base or base in STABLE_BASE_ASSETS or not isinstance(rank, int) or isinstance(rank, bool) or rank <= 0:
            continue
        by_base.setdefault(base, {})[coin.get("id")] = coin
    candidates = {}
    for instrument in instruments:
        if (instrument.get("status") != "TRADING" or instrument.get("contractType") != "PERPETUAL"
                or instrument.get("quoteAsset") != "USDT"):
            continue
        base = instrument.get("baseAsset", "")
        # Multiplier contracts retain their Binance symbol and price units.
        underlying = base
        if base not in by_base:
            for prefix in ("1000000", "1000", "1M"):
                if base.startswith(prefix) and base[len(prefix):] in by_base:
                    underlying = base[len(prefix):]
                    break
        matches = by_base.get(underlying, {})
        if len(matches) != 1:
            continue
        coin = next(iter(matches.values()))
        if not coin.get("id") or (selection == "global_top_n" and coin["market_cap_rank"] > top_n):
            continue
        item = {
            "symbol": instrument["symbol"], "base_asset": base, "quote_asset": "USDT",
            "coin_id": coin["id"], "name": coin.get("name", underlying),
            "market_cap_rank": coin["market_cap_rank"],
        }
        # One contract per underlying asset; prefer the exact, unscaled contract.
        old = candidates.get(coin["id"])
        if old is None or base == underlying:
            candidates[coin["id"]] = item
    return sorted(candidates.values(), key=lambda row: (row["market_cap_rank"], row["symbol"]))[:top_n]


def fetch_binance_paper_universe(top_n=100, selection="exchange_top_n"):
    from urllib.parse import urlencode

    exchange = read_json_url("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=12, attempts=2)
    instruments = exchange["symbols"]
    coins = []
    # Bounded pagination also allows excluding ambiguous tickers across pages.
    for page in range(1, 5):
        params = urlencode(dict(vs_currency="usd", order="market_cap_desc", per_page=250, page=page, sparkline="false"))
        rows = read_json_url("https://api.coingecko.com/api/v3/coins/markets?" + params, timeout=20, attempts=2)
        if not isinstance(rows, list) or not rows:
            raise ValueError("市值数据为空或无效")
        coins.extend(rows)
        selected = select_binance_paper_universe(coins, instruments, top_n, selection)
        if selection == "global_top_n" or len(selected) >= top_n:
            return selected
        if len(rows) < 250:
            break
    raise ValueError(f"有效币安合约不足 {top_n} 个，暂停更新币池")


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
