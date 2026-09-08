#!/usr/bin/env python3
"""
OKX Demo Trading signal runner.

默认只读取 OKX 模拟账户余额和最新信号，不会下单。
需要真实提交模拟盘订单时，必须显式添加 --place-order 和 --size。
"""

from __future__ import annotations

import argparse
from typing import Any

from indicators import calculate_indicators
from project_signal_backtest import fetch_exchange_klines, fetch_higher_timeframe_context
from strategy import ProjectSignalEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OKX 模拟盘项目信号执行器")
    parser.add_argument("--symbol", default="BTCUSDT", help="交易对，例如 BTCUSDT 或 BTC-USDT-SWAP")
    parser.add_argument("--interval", default="15m", help="K线周期，例如 15m、1h、4h")
    parser.add_argument("--limit", type=int, default=1000, help="信号计算使用的 K 线数量")
    parser.add_argument("--okx-instrument-type", choices=["SWAP", "SPOT"], default="SWAP", help="OKX 品种类型")
    parser.add_argument("--balance-ccy", default="USDT", help="读取模拟账户余额币种")
    parser.add_argument("--trade-mode", choices=["cross", "isolated", "cash"], default="cross", help="OKX tdMode")
    parser.add_argument("--size", default="", help="下单数量。合约为张数，现货为基础币数量")
    parser.add_argument("--place-order", action="store_true", help="确认提交 OKX 模拟盘市价单")
    parser.add_argument("--debug-orders", action="store_true", help="打印 OKX 下单请求和原始响应")
    return parser.parse_args()


def latest_project_signal(symbol: str, interval: str, limit: int, okx_instrument_type: str) -> dict[str, Any]:
    raw_bars = fetch_exchange_klines("okx", symbol, interval, limit, okx_instrument_type)
    bars = fetch_higher_timeframe_context(symbol, calculate_indicators(raw_bars), interval, "okx", okx_instrument_type)
    ready_bars = [bar for bar in bars if bar.get("macd") is not None]
    return ProjectSignalEngine().detect(ready_bars)


def order_side_for_signal(signal: str) -> str:
    if signal == "long":
        return "buy"
    if signal == "short":
        return "sell"
    raise ValueError(f"unsupported signal: {signal}")


def main() -> None:
    print("This OKX robot entrypoint is retired. Run app.py for research and paper signal tracking.")
    raise SystemExit(2)


if __name__ == "__main__":
    main()
