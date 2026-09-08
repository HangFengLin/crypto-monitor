#!/usr/bin/env python3
"""
OKX Demo order smoke test.

Default mode only prepares an order and prints OKX rule clipping. Add
--place-order to submit a simulated OKX order.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
from typing import Any

from data_client import fetch_okx_historical_klines, fetch_okx_instrument


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OKX Demo 下单参数与规则裁剪烟测")
    parser.add_argument("--symbol", default="SOLUSDT", help="交易对，例如 SOLUSDT 或 SOL-USDT-SWAP")
    parser.add_argument("--side", choices=["buy", "sell"], default="buy", help="下单方向")
    parser.add_argument("--size", default="1", help="下单数量；合约为张数，现货为基础币数量")
    parser.add_argument("--quote-usdt", default="", help="按约多少 USDT 名义价值估算 size，例如 10")
    parser.add_argument("--okx-instrument-type", choices=["SWAP", "SPOT"], default="SWAP")
    parser.add_argument("--trade-mode", choices=["cross", "isolated", "cash"], default="cross")
    parser.add_argument("--order-type", choices=["market", "limit"], default="market")
    parser.add_argument("--price", default=None, help="限价单价格；市价单不要传")
    parser.add_argument("--slippage-ticks", type=int, default=0, help="限价单按方向增加几档滑点容忍")
    parser.add_argument("--place-order", action="store_true", help="确认提交 OKX 模拟盘订单")
    parser.add_argument("--debug-orders", action="store_true", help="打印 OKX 下单请求和原始响应")
    return parser.parse_args()


def decimal_value(value: Any, field_name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"Invalid {field_name}: {value!r}")
    return number


def format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def estimate_size_from_quote(symbol: str, instrument_type: str, quote_usdt: str) -> tuple[str, Decimal, dict[str, Any]]:
    quote_value = decimal_value(quote_usdt, "quote_usdt")
    instrument = fetch_okx_instrument(symbol, instrument_type)
    bars = fetch_okx_historical_klines(symbol, "1m", 1, instrument_type)
    if not bars:
        raise RuntimeError(f"No OKX candles returned for {symbol}")
    price = decimal_value(bars[-1].get("close"), "latest_price")
    contract_value = decimal_value(instrument.get("ctVal", "1") or "1", "ctVal") if instrument_type == "SWAP" else Decimal("1")
    size = quote_value / (price * contract_value)
    return format_decimal(size), price, instrument


def print_order_plan(body: dict[str, Any], instrument: dict[str, Any]) -> None:
    rules = {
        "instId": instrument.get("instId"),
        "minSz": instrument.get("minSz"),
        "lotSz": instrument.get("lotSz"),
        "tickSz": instrument.get("tickSz"),
        "ctVal": instrument.get("ctVal"),
        "ctValCcy": instrument.get("ctValCcy"),
    }
    print("OKX规则:", rules)
    print("裁剪后的请求:", body)


def main() -> None:
    print("This OKX robot entrypoint is retired. Run app.py for research and paper signal tracking.")
    raise SystemExit(2)


if __name__ == "__main__":
    main()
