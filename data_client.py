from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from config import load_env_file

try:
    import aiohttp
except ModuleNotFoundError:
    aiohttp = None

load_env_file()


BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_OPEN_INTEREST_URL = "https://fapi.binance.com/fapi/v1/openInterest"
BINANCE_OPEN_INTEREST_HIST_URL = "https://fapi.binance.com/futures/data/openInterestHist"
COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
GATE_SPOT_TICKERS_URL = "https://api.gateio.ws/api/v4/spot/tickers"
GATE_SPOT_CANDLESTICKS_URL = "https://api.gateio.ws/api/v4/spot/candlesticks"
OKX_BASE_URL = "https://www.okx.com"
OKX_CANDLES_PATH = "/api/v5/market/candles"
OKX_HISTORY_CANDLES_PATH = "/api/v5/market/history-candles"
OKX_TICKERS_PATH = "/api/v5/market/tickers"
OKX_PUBLIC_INSTRUMENTS_PATH = "/api/v5/public/instruments"
OKX_ACCOUNT_POSITIONS_PATH = "/api/v5/account/positions"

OKX_BAR_MAP = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "2h": "2H",
    "4h": "4H",
    "6h": "6H",
    "12h": "12H",
    "1d": "1D",
    "1w": "1W",
}

INTERVAL_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def parse_float(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_json_url(url: str, timeout: int = 20, attempts: int = 3, max_elapsed: float = 60.0) -> Any:
    if timeout <= 0 or attempts < 1 or max_elapsed <= 0:
        raise ValueError("timeout, attempts and max_elapsed must be positive")
    deadline = time.monotonic() + max_elapsed
    last_error: Exception | None = None
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            with urllib.request.urlopen(request, timeout=min(timeout, remaining)) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            retry_after = parse_float(exc.headers.get("Retry-After")) if exc.headers else None
            delay = max(0.0, retry_after) if retry_after is not None else min(30.0, 0.75 * (2**attempt))
            if exc.code != 429:
                delay = 0.3
        except (http.client.IncompleteRead, OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            delay = 0.3
        if attempt + 1 >= attempts or delay >= deadline - time.monotonic():
            break
        time.sleep(delay)
    if last_error:
        raise last_error
    raise TimeoutError("request failed")


def normalize_ticker(ticker: dict[str, Any]) -> dict[str, Any]:
    last_price = parse_float(ticker.get("lastPrice"))
    return {
        "symbol": ticker.get("symbol"),
        "price": last_price,
        "lastPrice": last_price,
        "priceChange": parse_float(ticker.get("priceChange")),
        "priceChangePercent": parse_float(ticker.get("priceChangePercent")),
        "highPrice": parse_float(ticker.get("highPrice")),
        "lowPrice": parse_float(ticker.get("lowPrice")),
        "volume": parse_float(ticker.get("volume")),
        "quoteVolume": parse_float(ticker.get("quoteVolume")),
        "openPrice": parse_float(ticker.get("openPrice")),
    }


def market_data_source() -> str:
    source = os.getenv("MARKET_DATA_SOURCE", "binance").strip().lower()
    return source if source in {"binance", "gate"} else "binance"


def secondary_market_data_source() -> str:
    return "binance" if market_data_source() == "gate" else "gate"


def market_source_order() -> list[str]:
    primary = market_data_source()
    secondary = secondary_market_data_source()
    return [primary, secondary] if primary != secondary else [primary]


def recoverable_http_errors() -> tuple[type[BaseException], ...]:
    errors: tuple[type[BaseException], ...] = (
        http.client.IncompleteRead,
        OSError,
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        ValueError,
    )
    if aiohttp is not None:
        errors = (aiohttp.ClientError, *errors)
    return errors


def gate_currency_pair(symbol: str) -> str:
    normalized = symbol.strip().upper().replace("-", "_")
    if "_" in normalized:
        return normalized
    if normalized.endswith("USDT"):
        return f"{normalized[:-4]}_USDT"
    if normalized.endswith("USD"):
        return f"{normalized[:-3]}_USD"
    return normalized


def gate_interval(interval: str) -> str:
    return {
        "1m": "1m",
        "3m": "1m",
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "1h": "1h",
        "4h": "4h",
        "1d": "1d",
    }.get(interval, interval)


def interval_ms(interval: str) -> int:
    return INTERVAL_SECONDS.get(interval, 60) * 1000


def normalize_gate_ticker(ticker: dict[str, Any], symbol: str) -> dict[str, Any]:
    last_price = parse_float(ticker.get("last"))
    change_percent = parse_float(ticker.get("change_percentage"))
    open_price = None
    price_change = None
    if last_price is not None and change_percent is not None:
        denominator = 1 + (change_percent / 100)
        if denominator:
            open_price = last_price / denominator
            price_change = last_price - open_price
    return {
        "symbol": symbol,
        "price": last_price,
        "lastPrice": last_price,
        "priceChange": price_change,
        "priceChangePercent": change_percent,
        "highPrice": parse_float(ticker.get("high_24h")),
        "lowPrice": parse_float(ticker.get("low_24h")),
        "volume": parse_float(ticker.get("base_volume")),
        "quoteVolume": parse_float(ticker.get("quote_volume")),
        "openPrice": open_price,
    }


def fetch_gate_tickers(symbols: list[str]) -> dict[str, dict[str, Any]]:
    tickers: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        pair = gate_currency_pair(symbol)
        params = urllib.parse.urlencode({"currency_pair": pair})
        payload = read_json_url(f"{GATE_SPOT_TICKERS_URL}?{params}", timeout=8, attempts=3)
        rows = payload if isinstance(payload, list) else [payload]
        if rows:
            tickers[symbol] = normalize_gate_ticker(rows[0], symbol)
    return tickers


_shared_sessions: dict[int, aiohttp.ClientSession] = {}


async def get_http_session() -> aiohttp.ClientSession:
    if aiohttp is None:
        raise RuntimeError("aiohttp is not installed; run python3 -m pip install -r requirements.txt")

    loop_key = id(asyncio.get_running_loop())
    session = _shared_sessions.get(loop_key)
    if session is None or session.closed:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Encoding": "identity",
                "Connection": "keep-alive",
            },
            trust_env=True,
        )
        _shared_sessions[loop_key] = session
    return session


async def close_http_session() -> None:
    loop_key = id(asyncio.get_running_loop())
    session = _shared_sessions.pop(loop_key, None)
    if session is not None and not session.closed:
        await session.close()


async def async_fetch_gate_tickers(symbols: list[str], concurrency: int = 8) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}

    sem = asyncio.Semaphore(max(1, concurrency))
    results: dict[str, dict[str, Any]] = {}

    async def load_one(symbol: str) -> None:
        pair = gate_currency_pair(symbol)
        params = urllib.parse.urlencode({"currency_pair": pair})
        async with sem:
            payload = await async_read_json_url(f"{GATE_SPOT_TICKERS_URL}?{params}", timeout=8, attempts=3)
        rows = payload if isinstance(payload, list) else [payload]
        if rows:
            results[symbol] = normalize_gate_ticker(rows[0], symbol)

    await asyncio.gather(*(load_one(symbol) for symbol in symbols))
    return results


def normalize_gate_candlestick(row: list[Any], interval: str) -> dict[str, Any]:
    open_time = int(float(row[0])) * 1000
    return {
        "open_time": open_time,
        "open": parse_float(row[5]),
        "high": parse_float(row[3]),
        "low": parse_float(row[4]),
        "close": parse_float(row[2]),
        "volume": parse_float(row[6]),
        "close_time": open_time + interval_ms(interval) - 1,
    }


def fetch_gate_klines(symbol: str, interval: str, limit: int = 300) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "currency_pair": gate_currency_pair(symbol),
            "interval": gate_interval(interval),
            "limit": min(max(1, limit), 1000),
        }
    )
    payload = read_json_url(f"{GATE_SPOT_CANDLESTICKS_URL}?{params}", timeout=8, attempts=3)
    rows = payload if isinstance(payload, list) else []
    return [normalize_gate_candlestick(row, interval) for row in rows if isinstance(row, list) and len(row) >= 6]


def fetch_binance_tickers(symbols: list[str]) -> dict[str, dict[str, Any]]:
    params = urllib.parse.urlencode({"symbols": json.dumps(symbols, separators=(",", ":"))})
    payload = read_json_url(f"{BINANCE_TICKER_URL}?{params}", timeout=12)
    tickers = payload if isinstance(payload, list) else [payload]
    return {ticker["symbol"]: normalize_ticker(ticker) for ticker in tickers}


def fetch_tickers(symbols: list[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    errors: list[str] = []
    for source in market_source_order():
        try:
            if source == "gate":
                return fetch_gate_tickers(symbols)
            return fetch_binance_tickers(symbols)
        except recoverable_http_errors() as exc:
            errors.append(f"{source}: {exc}")
    raise RuntimeError("; ".join(errors) or "market ticker request failed")


def fetch_all_tickers() -> dict[str, dict[str, Any]]:
    payload = read_json_url(BINANCE_TICKER_URL, timeout=20)
    tickers = payload if isinstance(payload, list) else [payload]
    return {ticker["symbol"]: normalize_ticker(ticker) for ticker in tickers if ticker.get("symbol")}


def fetch_binance_spot_symbols(quote_asset: str = "USDT") -> set[str]:
    payload = read_json_url(BINANCE_EXCHANGE_INFO_URL, timeout=20)
    symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
    quote = quote_asset.upper()
    return {
        item["symbol"]
        for item in symbols
        if item.get("status") == "TRADING"
        and item.get("quoteAsset") == quote
        and item.get("isSpotTradingAllowed") is not False
    }


def fetch_coingecko_top_market_symbols(limit: int = 100) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": max(1, min(250, limit)),
            "page": 1,
            "sparkline": "false",
        }
    )
    payload = read_json_url(f"{COINGECKO_MARKETS_URL}?{params}", timeout=20)
    if not isinstance(payload, list):
        return []
    return [
        {
            "id": item.get("id"),
            "symbol": str(item.get("symbol", "")).upper(),
            "name": item.get("name"),
            "market_cap_rank": item.get("market_cap_rank"),
        }
        for item in payload
        if item.get("symbol")
    ]


def fetch_klines(symbol: str, interval: str, limit: int = 300) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    errors: list[str] = []
    for source in market_source_order():
        try:
            if source == "gate":
                return fetch_gate_klines(symbol, interval, limit)
            payload = read_json_url(f"{BINANCE_KLINES_URL}?{params}")
            break
        except recoverable_http_errors() as exc:
            errors.append(f"{source}: {exc}")
    else:
        raise RuntimeError("; ".join(errors) or "market kline request failed")
    klines = []
    for row in payload:
        klines.append(
            {
                "open_time": row[0],
                "open": parse_float(row[1]),
                "high": parse_float(row[2]),
                "low": parse_float(row[3]),
                "close": parse_float(row[4]),
                "volume": parse_float(row[5]),
                "close_time": row[6],
            }
        )
    return klines


def fetch_historical_klines(symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
    rows: list[list[Any]] = []
    end_time: int | None = None
    while len(rows) < limit:
        batch_limit = min(1000, limit - len(rows))
        query = {"symbol": symbol.upper(), "interval": interval, "limit": batch_limit}
        if end_time is not None:
            query["endTime"] = end_time
        params = urllib.parse.urlencode(query)
        batch = read_json_url(f"{BINANCE_KLINES_URL}?{params}")
        if not batch:
            break
        rows = batch + rows
        end_time = int(batch[0][0]) - 1
        time.sleep(0.08)

    bars = []
    for row in rows[-limit:]:
        bars.append(
            {
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "close_time": int(row[6]),
            }
        )
    return bars


def normalize_okx_inst_id(symbol: str, instrument_type: str = "SWAP") -> str:
    normalized = symbol.strip().upper().replace("_", "-")
    if "-" in normalized:
        return normalized
    if normalized.endswith("USDT"):
        base = normalized[:-4]
        suffix = "SWAP" if instrument_type.upper() == "SWAP" else "SPOT"
        return f"{base}-USDT-{suffix}" if suffix == "SWAP" else f"{base}-USDT"
    return normalized


def normalize_okx_symbol(inst_id: str) -> str:
    parts = inst_id.strip().upper().split("-")
    if len(parts) >= 2:
        return f"{parts[0]}{parts[1]}"
    return inst_id.strip().upper().replace("-", "")


def okx_bar(interval: str) -> str:
    return OKX_BAR_MAP.get(interval, interval)


def _parse_okx_candle(row: list[Any]) -> dict[str, Any]:
    # OKX candle rows: ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm.
    open_time = int(row[0])
    return {
        "open_time": open_time,
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5]),
        "close_time": open_time,
        "confirmed": str(row[8]) == "1" if len(row) > 8 else True,
    }


def fetch_okx_historical_klines(symbol: str, interval: str, limit: int, instrument_type: str = "SWAP") -> list[dict[str, Any]]:
    inst_id = normalize_okx_inst_id(symbol, instrument_type)
    rows: list[list[Any]] = []
    after: int | None = None
    while len(rows) < limit:
        batch_limit = min(100, limit - len(rows))
        query: dict[str, Any] = {"instId": inst_id, "bar": okx_bar(interval), "limit": batch_limit}
        if after is not None:
            query["after"] = after
        params = urllib.parse.urlencode(query)
        path = OKX_HISTORY_CANDLES_PATH if after is not None else OKX_CANDLES_PATH
        payload = read_json_url(f"{OKX_BASE_URL}{path}?{params}")
        if str(payload.get("code")) != "0":
            raise RuntimeError(f"OKX candles failed: {payload.get('msg') or payload}")
        batch = payload.get("data", [])
        if not batch:
            break
        rows.extend(batch)
        after = int(batch[-1][0])
        time.sleep(0.08)

    bars = [_parse_okx_candle(row) for row in rows]
    bars.sort(key=lambda bar: int(bar["open_time"]))
    return bars[-limit:]


def fetch_okx_trade_symbols(quote_asset: str = "USDT", instrument_type: str = "SWAP") -> dict[str, str]:
    inst_type = instrument_type.upper()
    quote = quote_asset.upper()
    params = urllib.parse.urlencode({"instType": inst_type})
    payload = read_json_url(f"{OKX_BASE_URL}{OKX_PUBLIC_INSTRUMENTS_PATH}?{params}", timeout=20)
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"OKX instruments failed: {payload.get('msg') or payload}")

    symbols: dict[str, str] = {}
    for item in payload.get("data", []):
        if item.get("state") != "live":
            continue
        inst_id = str(item.get("instId", "")).upper()
        if not inst_id:
            continue
        if inst_type == "SWAP" and item.get("settleCcy") != quote:
            continue
        if inst_type == "SPOT" and item.get("quoteCcy") != quote:
            continue
        symbols[normalize_okx_symbol(inst_id)] = inst_id
    return symbols


def fetch_okx_tickers(instrument_type: str = "SWAP") -> dict[str, dict[str, Any]]:
    """Return OKX tickers keyed by compact symbol, including estimated quote volume."""
    inst_type = instrument_type.upper()
    params = urllib.parse.urlencode({"instType": inst_type})
    payload = read_json_url(f"{OKX_BASE_URL}{OKX_TICKERS_PATH}?{params}", timeout=20)
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"OKX tickers failed: {payload.get('msg') or payload}")

    tickers: dict[str, dict[str, Any]] = {}
    for item in payload.get("data", []):
        if not isinstance(item, dict):
            continue
        inst_id = str(item.get("instId", "")).upper()
        last_price = parse_float(item.get("last"))
        currency_volume = parse_float(item.get("volCcy24h"))
        if not inst_id:
            continue
        # For derivatives OKX reports volCcy24h in base currency; convert it
        # to an estimated quote value so the liquidity threshold stays in USDT.
        quote_volume = currency_volume
        if inst_type in {"SWAP", "FUTURES", "OPTION"}:
            quote_volume = currency_volume * last_price if currency_volume is not None and last_price is not None else None
        tickers[normalize_okx_symbol(inst_id)] = {
            "inst_id": inst_id,
            "last_price": last_price,
            "quote_volume": quote_volume,
        }
    return tickers


def fetch_okx_instrument(symbol: str, instrument_type: str = "SWAP") -> dict[str, Any]:
    inst_type = instrument_type.upper()
    inst_id = normalize_okx_inst_id(symbol, instrument_type)
    params = urllib.parse.urlencode({"instType": inst_type, "instId": inst_id})
    payload = read_json_url(f"{OKX_BASE_URL}{OKX_PUBLIC_INSTRUMENTS_PATH}?{params}", timeout=20)
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"OKX instruments failed: {payload.get('msg') or payload}")
    instruments = [item for item in payload.get("data", []) if isinstance(item, dict)]
    if not instruments:
        raise RuntimeError(f"OKX instrument not found: {inst_id}")
    return instruments[0]


def okx_timestamp() -> str:
    from datetime import datetime

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def okx_sign(timestamp: str, method: str, request_path: str, body: str, secret_key: str) -> str:
    message = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def okx_authenticated_request(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    simulated: bool = True,
    timeout: int = 20,
) -> Any:
    api_key = os.getenv("OKX_API_KEY", "")
    secret_key = os.getenv("OKX_SECRET_KEY", "")
    passphrase = os.getenv("OKX_PASSPHRASE", "")
    if not api_key or not secret_key or not passphrase:
        raise RuntimeError("Missing OKX_API_KEY, OKX_SECRET_KEY, or OKX_PASSPHRASE environment variables")

    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    request_path = f"{path}{query}"
    payload = json.dumps(body or {}, separators=(",", ":")) if body is not None else ""
    timestamp = okx_timestamp()
    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": okx_sign(timestamp, method, request_path, payload, secret_key),
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    if simulated:
        headers["x-simulated-trading"] = "1"
    request = urllib.request.Request(
        f"{OKX_BASE_URL}{request_path}",
        data=payload.encode("utf-8") if payload else None,
        headers=headers,
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        try:
            error_payload = json.loads(error_body)
        except json.JSONDecodeError:
            error_payload = error_body
        raise RuntimeError(f"OKX HTTP {exc.code}: {error_payload}") from exc
    if str(result.get("code")) != "0":
        raise RuntimeError(f"OKX request failed: {result}")
    return result


def fetch_okx_demo_balance(ccy: str = "USDT") -> dict[str, Any]:
    result = okx_authenticated_request("GET", "/api/v5/account/balance", params={"ccy": ccy}, simulated=True)
    return result.get("data", [{}])[0]


def fetch_okx_demo_positions(symbol: str | None = None, instrument_type: str = "SWAP") -> list[dict[str, Any]]:
    params: dict[str, Any] = {"instType": instrument_type.upper()}
    if symbol:
        params["instId"] = normalize_okx_inst_id(symbol, instrument_type)
    result = okx_authenticated_request("GET", OKX_ACCOUNT_POSITIONS_PATH, params=params, simulated=True)
    return [item for item in result.get("data", []) if isinstance(item, dict)]


def _decimal_value(value: Any, field_name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"Invalid {field_name}: {value!r}")
    return number


def _format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _price_to_tick(value: Decimal, tick: Decimal, side: str) -> Decimal:
    if tick <= 0:
        return value
    rounding = ROUND_CEILING if side.lower() == "buy" else ROUND_FLOOR
    return (value / tick).to_integral_value(rounding=rounding) * tick


def _okx_debug_orders_enabled() -> bool:
    return os.getenv("OKX_DEBUG_ORDERS", "").strip().lower() in {"1", "true", "yes", "on"}


def prepare_okx_demo_order(
    symbol: str,
    side: str,
    size: str,
    instrument_type: str = "SWAP",
    trade_mode: str = "cross",
    order_type: str = "market",
    position_side: str | None = None,
    reduce_only: bool = False,
    price: Any | None = None,
    slippage_ticks: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    instrument = fetch_okx_instrument(symbol, instrument_type)
    inst_id = str(instrument.get("instId") or normalize_okx_inst_id(symbol, instrument_type))
    lot_size = _decimal_value(instrument.get("lotSz", "1"), "lotSz")
    min_size = _decimal_value(instrument.get("minSz", lot_size), "minSz")
    requested_size = _decimal_value(size, "size")
    if requested_size <= 0:
        raise ValueError(f"Order size must be positive: {size!r}")

    clipped_size = _floor_to_step(requested_size, lot_size)
    if clipped_size < min_size:
        raise ValueError(
            "Order size below OKX minimum after lot-size clipping: "
            f"requested={_format_decimal(requested_size)}, clipped={_format_decimal(clipped_size)}, "
            f"minSz={_format_decimal(min_size)}, lotSz={_format_decimal(lot_size)}, instId={inst_id}"
        )

    normalized_order_type = order_type.lower()
    body: dict[str, Any] = {
        "instId": inst_id,
        "tdMode": trade_mode,
        "side": side.lower(),
        "ordType": normalized_order_type,
        "sz": _format_decimal(clipped_size),
    }
    if position_side:
        body["posSide"] = position_side
    if reduce_only:
        body["reduceOnly"] = "true"
    if normalized_order_type == "limit":
        if price is None:
            raise ValueError("Limit orders require price")
        tick_size = _decimal_value(instrument.get("tickSz", "0"), "tickSz")
        requested_price = _decimal_value(price, "price")
        adjusted_price = requested_price
        if slippage_ticks:
            slippage = tick_size * Decimal(abs(slippage_ticks))
            adjusted_price = requested_price + slippage if side.lower() == "buy" else requested_price - slippage
        clipped_price = _price_to_tick(adjusted_price, tick_size, side)
        if clipped_price <= 0:
            raise ValueError(f"Limit price must be positive after tick clipping: {price!r}")
        body["px"] = _format_decimal(clipped_price)
    elif price is not None:
        raise ValueError("Market orders must not include price")

    return body, instrument


def place_okx_demo_order(
    symbol: str,
    side: str,
    size: str,
    instrument_type: str = "SWAP",
    trade_mode: str = "cross",
    order_type: str = "market",
    position_side: str | None = None,
    reduce_only: bool = False,
    price: Any | None = None,
    slippage_ticks: int = 0,
    debug: bool = False,
) -> dict[str, Any]:
    body, instrument = prepare_okx_demo_order(
        symbol,
        side,
        size,
        instrument_type,
        trade_mode,
        order_type,
        position_side,
        reduce_only,
        price,
        slippage_ticks,
    )
    debug_enabled = debug or _okx_debug_orders_enabled()
    if debug_enabled:
        print(f"OKX order request: {body}", flush=True)
    result = okx_authenticated_request("POST", "/api/v5/trade/order", body=body, simulated=True)
    data = result.get("data", [{}])[0]
    if debug_enabled:
        print(f"OKX order response: {data}", flush=True)
    if str(data.get("sCode", "0")) != "0":
        raise RuntimeError(f"OKX order rejected: response={data}, request={body}")
    data["_request"] = body
    data["_instrument_rules"] = {
        "instId": instrument.get("instId"),
        "minSz": instrument.get("minSz"),
        "lotSz": instrument.get("lotSz"),
        "tickSz": instrument.get("tickSz"),
        "ctVal": instrument.get("ctVal"),
        "ctValCcy": instrument.get("ctValCcy"),
    }
    return data


def fetch_funding_rate(symbol: str) -> dict[str, Any]:
    if market_data_source() == "gate":
        return {"symbol": symbol, "lastFundingRate": None, "nextFundingTime": None}
    params = urllib.parse.urlencode({"symbol": symbol})
    payload = read_json_url(f"{BINANCE_FUNDING_URL}?{params}", timeout=12)
    return {
        "symbol": payload.get("symbol", symbol),
        "markPrice": parse_float(payload.get("markPrice")),
        "indexPrice": parse_float(payload.get("indexPrice")),
        "lastFundingRate": parse_float(payload.get("lastFundingRate")),
        "nextFundingTime": payload.get("nextFundingTime"),
    }


def fetch_open_interest(symbol: str) -> dict[str, Any]:
    if market_data_source() == "gate":
        return {"symbol": symbol, "openInterest": None}
    params = urllib.parse.urlencode({"symbol": symbol})
    payload = read_json_url(f"{BINANCE_OPEN_INTEREST_URL}?{params}", timeout=12)
    return {
        "symbol": payload.get("symbol", symbol),
        "openInterest": parse_float(payload.get("openInterest")),
        "time": payload.get("time"),
    }


def fetch_open_interest_ratio(symbol: str, period: str = "15m", limit: int = 20) -> dict[str, Any]:
    if market_data_source() == "gate":
        return {"symbol": symbol, "openInterest": None, "openInterestRatio": None}
    params = urllib.parse.urlencode({"pair": symbol, "contractType": "PERPETUAL", "period": period, "limit": limit})
    payload = read_json_url(f"{BINANCE_OPEN_INTEREST_HIST_URL}?{params}", timeout=12)
    rows = payload if isinstance(payload, list) else []
    values = [parse_float(row.get("sumOpenInterest")) for row in rows if isinstance(row, dict)]
    valid_values = [value for value in values if value is not None]
    if not valid_values:
        return {"symbol": symbol, "openInterest": None, "openInterestRatio": None}
    latest = valid_values[-1]
    baseline_values = valid_values[:-1] or valid_values
    baseline = sum(baseline_values) / len(baseline_values)
    return {
        "symbol": symbol,
        "openInterest": latest,
        "openInterestRatio": latest / baseline if baseline else None,
    }


def test_market_api(symbol: str) -> dict[str, Any]:
    started_at = time.time()
    try:
        ticker = fetch_tickers([symbol]).get(symbol, {})
        return {
            "ok": True,
            "source": "server",
            "symbol": ticker.get("symbol", symbol),
            "lastPrice": ticker.get("price"),
            "duration_ms": round((time.time() - started_at) * 1000),
        }
    except (http.client.IncompleteRead, OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
        return {
            "ok": False,
            "source": "server",
            "symbol": symbol,
            "error": str(exc),
            "duration_ms": round((time.time() - started_at) * 1000),
        }


def test_json_dependency(name: str, url: str, timeout: int = 12, attempts: int = 1) -> dict[str, Any]:
    started_at = time.time()
    try:
        payload = read_json_url(url, timeout=timeout, attempts=attempts)
        return {
            "ok": True,
            "name": name,
            "duration_ms": round((time.time() - started_at) * 1000),
            "sample": summarize_dependency_payload(payload),
        }
    except (http.client.IncompleteRead, OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
        return {
            "ok": False,
            "name": name,
            "error": str(exc),
            "duration_ms": round((time.time() - started_at) * 1000),
        }


def summarize_dependency_payload(payload: Any) -> str:
    if isinstance(payload, list):
        return f"{len(payload)} rows"
    if isinstance(payload, dict):
        keys = ", ".join(list(payload.keys())[:5])
        return keys or "object"
    return type(payload).__name__


def test_external_dependencies(symbol: str, discord_url: str = "") -> dict[str, Any]:
    normalized_symbol = symbol.upper().strip() or "BTCUSDT"
    gate_pair = gate_currency_pair(normalized_symbol)
    binance_params = urllib.parse.urlencode({"symbols": json.dumps([normalized_symbol], separators=(",", ":"))})
    gate_params = urllib.parse.urlencode({"currency_pair": gate_pair})
    okx_params = urllib.parse.urlencode({"instType": "SWAP"})
    coingecko_params = urllib.parse.urlencode(
        {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 1,
            "page": 1,
            "sparkline": "false",
        }
    )

    checks = [
        {
            **test_json_dependency("Binance Spot Ticker", f"{BINANCE_TICKER_URL}?{binance_params}", timeout=12, attempts=3),
            "source": "binance",
        },
        {
            **test_json_dependency("Gate.io Spot Ticker", f"{GATE_SPOT_TICKERS_URL}?{gate_params}", timeout=8, attempts=3),
            "source": "gate",
        },
        {
            **test_json_dependency("OKX Public Instruments", f"{OKX_BASE_URL}{OKX_PUBLIC_INSTRUMENTS_PATH}?{okx_params}", timeout=12, attempts=2),
            "source": "okx",
        },
        {
            **test_json_dependency("CoinGecko Markets", f"{COINGECKO_MARKETS_URL}?{coingecko_params}", timeout=12, attempts=2),
            "source": "coingecko",
        },
    ]

    if discord_url:
        checks.append({**test_json_dependency("Discord Webhook", discord_url, timeout=10, attempts=1), "source": "discord"})
    else:
        checks.append(
            {
                "ok": False,
                "skipped": True,
                "name": "Discord Webhook",
                "source": "discord",
                "error": "DISCORD_WEBHOOK_URL is not configured",
                "duration_ms": 0,
            }
        )

    return {
        "ok": all(check.get("ok") or check.get("skipped") for check in checks),
        "symbol": normalized_symbol,
        "market_data_source": market_data_source(),
        "checks": checks,
    }


async def async_read_json_url(url: str, timeout: int = 20, attempts: int = 3) -> Any:
    if aiohttp is None:
        raise RuntimeError("aiohttp is not installed; run python3 -m pip install -r requirements.txt")

    last_error: Exception | None = None
    timeout_config = aiohttp.ClientTimeout(total=timeout)
    for _ in range(attempts):
        try:
            session = await get_http_session()
            async with session.get(url, timeout=timeout_config) as response:
                response.raise_for_status()
                return await response.json()
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            await _async_sleep(0.3)
    if last_error:
        raise last_error
    raise TimeoutError("request failed")


async def _async_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def async_fetch_tickers(symbols: list[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    errors: list[str] = []
    for source in market_source_order():
        try:
            if source == "gate":
                return await async_fetch_gate_tickers(symbols)
            params = urllib.parse.urlencode({"symbols": json.dumps(symbols, separators=(",", ":"))})
            payload = await async_read_json_url(f"{BINANCE_TICKER_URL}?{params}", timeout=12)
            tickers = payload if isinstance(payload, list) else [payload]
            return {ticker["symbol"]: normalize_ticker(ticker) for ticker in tickers}
        except recoverable_http_errors() as exc:
            errors.append(f"{source}: {exc}")
    raise RuntimeError("; ".join(errors) or "market ticker request failed")


async def asyncio_to_thread(func, *args):
    return await asyncio.to_thread(func, *args)


async def async_fetch_klines(symbol: str, interval: str, limit: int = 300) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    errors: list[str] = []
    for source in market_source_order():
        try:
            if source == "gate":
                return await asyncio_to_thread(fetch_gate_klines, symbol, interval, limit)
            payload = await async_read_json_url(f"{BINANCE_KLINES_URL}?{params}")
            break
        except recoverable_http_errors() as exc:
            errors.append(f"{source}: {exc}")
    else:
        raise RuntimeError("; ".join(errors) or "market kline request failed")
    return [
        {
            "open_time": row[0],
            "open": parse_float(row[1]),
            "high": parse_float(row[2]),
            "low": parse_float(row[3]),
            "close": parse_float(row[4]),
            "volume": parse_float(row[5]),
            "close_time": row[6],
        }
        for row in payload
    ]


async def async_fetch_funding_rate(symbol: str) -> dict[str, Any]:
    if market_data_source() == "gate":
        return {"symbol": symbol, "lastFundingRate": None, "nextFundingTime": None}
    params = urllib.parse.urlencode({"symbol": symbol})
    payload = await async_read_json_url(f"{BINANCE_FUNDING_URL}?{params}", timeout=12)
    return {
        "symbol": payload.get("symbol", symbol),
        "markPrice": parse_float(payload.get("markPrice")),
        "indexPrice": parse_float(payload.get("indexPrice")),
        "lastFundingRate": parse_float(payload.get("lastFundingRate")),
        "nextFundingTime": payload.get("nextFundingTime"),
    }


async def async_fetch_open_interest_ratio(symbol: str, period: str = "15m", limit: int = 20) -> dict[str, Any]:
    if market_data_source() == "gate":
        return {"symbol": symbol, "openInterest": None, "openInterestRatio": None}
    params = urllib.parse.urlencode({"pair": symbol, "contractType": "PERPETUAL", "period": period, "limit": limit})
    payload = await async_read_json_url(f"{BINANCE_OPEN_INTEREST_HIST_URL}?{params}", timeout=12)
    rows = payload if isinstance(payload, list) else []
    values = [parse_float(row.get("sumOpenInterest")) for row in rows if isinstance(row, dict)]
    valid_values = [value for value in values if value is not None]
    if not valid_values:
        return {"symbol": symbol, "openInterest": None, "openInterestRatio": None}
    latest = valid_values[-1]
    baseline_values = valid_values[:-1] or valid_values
    baseline = sum(baseline_values) / len(baseline_values)
    return {
        "symbol": symbol,
        "openInterest": latest,
        "openInterestRatio": latest / baseline if baseline else None,
    }
