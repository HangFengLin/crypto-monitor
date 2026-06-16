#!/usr/bin/env python3
"""
OKX Demo Trading signal runner.

默认只读取 OKX 模拟账户余额和最新信号，不会下单。
需要真实提交模拟盘订单时，必须显式添加 --place-order 和 --size。
"""

from __future__ import annotations

import argparse
from typing import Any

from data_client import fetch_okx_demo_balance, place_okx_demo_order
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
    args = parse_args()
    balance = fetch_okx_demo_balance(args.balance_ccy)
    signal = latest_project_signal(args.symbol, args.interval, args.limit, args.okx_instrument_type)

    print(f"\nOKX Demo balance ccy: {args.balance_ccy}")
    details = balance.get("details", [])
    if details:
        detail = details[0]
        print(f"可用余额: {detail.get('availBal', detail.get('availEq', 'unknown'))}")
        print(f"权益: {detail.get('eq', 'unknown')}")
    else:
        print("余额详情: 暂无")

    print(f"\n{args.symbol} {args.interval} 最新策略状态")
    print(f"信号: {signal.get('signal')} - {signal.get('signal_name')}")
    print(f"价格: {signal.get('price', 'n/a')}")
    print(f"止损: {signal.get('stop_loss', 'n/a')}")
    print(f"评分: {signal.get('signal_score', 0)}/{signal.get('signal_score_max', 20)} {signal.get('signal_grade', '')}")
    print(f"过滤: {signal.get('filter', '')}")

    if signal.get("signal") not in {"long", "short"}:
        print("\n没有 long/short 实盘信号，不下单。")
        return

    if not args.place_order:
        print("\n检测到可交易信号，但当前是 dry-run。添加 --place-order --size 数量 才会提交模拟盘订单。")
        return

    if not args.size:
        raise SystemExit("--place-order 需要同时提供 --size")

    try:
        order = place_okx_demo_order(
            args.symbol,
            order_side_for_signal(str(signal["signal"])),
            args.size,
            args.okx_instrument_type,
            args.trade_mode,
            debug=args.debug_orders,
        )
        print(f"\n已提交 OKX 模拟盘订单: {order}")
    except Exception as exc:
        print("\nOKX 模拟盘下单失败:", repr(exc))
        raise


if __name__ == "__main__":
    main()
