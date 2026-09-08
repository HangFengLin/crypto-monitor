#!/usr/bin/env python3
"""
OKX Demo live strategy bot.

Runs the existing ProjectSignalEngine on OKX candles, submits demo market orders,
tracks one position per process, and sends open/close/error messages to Discord.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from config import load_env_file
from data_client import fetch_okx_demo_positions, normalize_okx_inst_id, parse_float, place_okx_demo_order
from indicators import calculate_indicators
from position_manager import (
    STOP_MODE_CHOICES,
    STRUCTURE_ATR_STOP_MODE,
    calculate_return_pct,
    evaluate_lifecycle_bar,
)
from position_manager import (
    calculate_target_levels as target_levels,
)
from project_signal_backtest import fetch_exchange_klines, fetch_higher_timeframe_context
from runtime_utils import atomic_write_text, post_discord
from strategy import ProjectSignalEngine

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "okx_demo_bot_state.json"
SIGNAL_ENGINE = ProjectSignalEngine()
SHUTDOWN_EVENT = threading.Event()


@dataclass
class OpenPosition:
    symbol: str
    interval: str
    direction: str
    size: str
    entry_price: float
    stop_loss: float
    target_price: float
    protection_price: float
    opened_at: float
    opened_close_time: int
    signal_score: int
    signal_grade: str
    order_id: str
    highest_price: float
    lowest_price: float
    initial_stop_loss: float = 0.0
    active_stop: float = 0.0
    protection_activated: bool = False
    protected_stop_price: float = 0.0
    exit_mode: str = STRUCTURE_ATR_STOP_MODE


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OKX Demo 策略循环，开平仓推送 Discord")
    parser.add_argument("--symbol", default="BTCUSDT", help="交易对，例如 BTCUSDT 或 BTC-USDT-SWAP")
    parser.add_argument("--interval", default="15m", help="策略周期")
    parser.add_argument("--limit", type=int, default=1000, help="每轮拉取 K 线数量")
    parser.add_argument("--size", default="1", help="下单数量。合约为张数，默认 1")
    parser.add_argument("--okx-instrument-type", choices=["SWAP", "SPOT"], default="SWAP")
    parser.add_argument("--trade-mode", choices=["cross", "isolated", "cash"], default="cross")
    parser.add_argument("--reward-risk", type=float, default=2.0, help="止盈目标倍数，默认 2R")
    parser.add_argument("--fee-rate", type=float, default=0.001, help="单边手续费/滑点估计，默认 0.001")
    parser.add_argument(
        "--position-stop-mode",
        choices=STOP_MODE_CHOICES,
        default=STRUCTURE_ATR_STOP_MODE,
        help="持仓退出模式；默认沿用结构 ATR 止盈止损",
    )
    parser.add_argument("--poll-seconds", type=int, default=60, help="轮询间隔，默认 60 秒")
    parser.add_argument("--min-signal-score", type=int, default=0, help="最低信号评分")
    parser.add_argument("--min-structure-score", type=int, default=0, help="最低结构评分")
    parser.add_argument("--place-order", action="store_true", help="显式确认提交 OKX Demo 模拟盘订单；默认只推送信号")
    parser.add_argument("--debug-orders", action="store_true", help="打印 OKX 下单请求和原始响应")
    parser.add_argument("--debug-signals", action="store_true", help="打印策略信号到机器人下单入口的传播节点")
    return parser.parse_args()


def ensure_secret_env(name: str, prompt: str) -> None:
    if not os.getenv(name):
        os.environ[name] = getpass.getpass(prompt)


def send_discord(content: str) -> None:
    load_env_file()
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return
    post_discord(webhook_url, content)


def load_position() -> OpenPosition | None:
    if not STATE_FILE.exists():
        return None
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not payload:
        return None
    return OpenPosition(**payload)


def save_position(position: OpenPosition | None) -> None:
    if position is None:
        atomic_write_text(STATE_FILE, "null\n")
        return
    atomic_write_text(STATE_FILE, json.dumps(asdict(position), ensure_ascii=False, indent=2))


def exchange_position_size(position: dict[str, Any]) -> float | None:
    for key in ("pos", "availPos"):
        value = parse_float(position.get(key))
        if value is not None:
            return value
    return None


def exchange_position_direction(position: dict[str, Any]) -> str | None:
    pos_side = str(position.get("posSide", "")).lower()
    if pos_side in {"long", "short"}:
        return pos_side
    size = exchange_position_size(position)
    if size is None or size == 0:
        return None
    return "long" if size > 0 else "short"


def active_exchange_positions(symbol: str, instrument_type: str) -> list[dict[str, Any]]:
    inst_id = normalize_okx_inst_id(symbol, instrument_type)
    positions = fetch_okx_demo_positions(symbol, instrument_type)
    active = []
    for position in positions:
        if str(position.get("instId", "")).upper() != inst_id:
            continue
        size = exchange_position_size(position)
        if size is not None and abs(size) > 0:
            active.append(position)
    return active


def reconcile_startup_position(args: argparse.Namespace, local_position: OpenPosition | None, no_order: bool) -> tuple[OpenPosition | None, bool]:
    if no_order or args.okx_instrument_type.upper() == "SPOT":
        return local_position, False

    exchange_positions = active_exchange_positions(args.symbol, args.okx_instrument_type)
    if not exchange_positions:
        if local_position is not None:
            save_position(None)
            send_discord(f"OKX Demo 启动校验：交易所无 {args.symbol} 未平仓，本地状态已清空。")
        return None, False

    exchange_position = exchange_positions[0]
    exchange_size = exchange_position_size(exchange_position)
    exchange_direction = exchange_position_direction(exchange_position)
    if local_position is None:
        send_discord(
            "\n".join(
                [
                    "**OKX Demo 启动校验发现交易所已有持仓**",
                    f"{exchange_position.get('instId')} direction={exchange_direction or 'unknown'} size={exchange_size}",
                    "本地 JSON 没有对应风控状态，机器人已暂停开新仓；请手动处理该持仓或补齐本地状态后重启。",
                ]
            )
        )
        return None, True

    paused = False
    if exchange_direction and exchange_direction != local_position.direction:
        paused = True
        send_discord(
            f"OKX Demo 启动校验：本地方向 {local_position.direction} 与交易所方向 {exchange_direction} 不一致，已暂停自动处理。"
        )
    if exchange_size is not None and abs(exchange_size) != parse_float(local_position.size):
        local_position.size = str(abs(exchange_size))
        save_position(local_position)
        send_discord(f"OKX Demo 启动校验：已用交易所真实仓位数量修正本地 size={local_position.size}。")
    return local_position, paused


def latest_signal(symbol: str, interval: str, limit: int, instrument_type: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw_bars = fetch_exchange_klines("okx", symbol, interval, limit, instrument_type)
    bars = fetch_higher_timeframe_context(symbol, calculate_indicators(raw_bars), interval, "okx", instrument_type)
    ready_bars = [bar for bar in bars if bar.get("macd") is not None]
    signal = SIGNAL_ENGINE.detect(ready_bars)
    if bars:
        signal.setdefault("price", bars[-1].get("close"))
        signal.setdefault("kline_close_time", bars[-1].get("close_time") or bars[-1].get("open_time"))
    return signal, bars


def order_side(direction: str, closing: bool = False) -> str:
    if direction == "long":
        return "sell" if closing else "buy"
    return "buy" if closing else "sell"


def should_skip_signal(signal: dict[str, Any], min_signal_score: int, min_structure_score: int) -> bool:
    return (
        int(signal.get("signal_score", 0) or 0) < min_signal_score
        or int(signal.get("structure_score", 0) or 0) < min_structure_score
    )


def signal_debug_enabled(args: argparse.Namespace) -> bool:
    return args.debug_signals or os.getenv("SIGNAL_TRACE", "").strip().lower() in {"1", "true", "yes", "on"}


def debug_signal(args: argparse.Namespace, signal: dict[str, Any], reason: str = "") -> None:
    if not signal_debug_enabled(args):
        return
    parts = [
        time.strftime("%Y-%m-%d %H:%M:%S"),
        "signal_probe",
        args.symbol,
        args.interval,
        f"signal={signal.get('signal')}",
        f"name={signal.get('signal_name')}",
        f"price={signal.get('price', 'n/a')}",
        f"strength={signal.get('strength', 'n/a')}",
        f"score={signal.get('signal_score', 0)}/{signal.get('signal_score_max', 20)}",
        f"structure={signal.get('structure_score', 0)}",
    ]
    if signal.get("confirm_bars") is not None:
        parts.append(f"confirm_bars={signal.get('confirm_bars')}")
    if reason:
        parts.append(f"skip={reason}")
    if signal.get("filter"):
        parts.append(f"filter={signal.get('filter')}")
    print(" | ".join(str(part) for part in parts), flush=True)


def open_position_from_signal(args: argparse.Namespace, signal: dict[str, Any], no_order: bool) -> OpenPosition | None:
    direction = str(signal.get("signal"))
    price = float(signal.get("price"))
    stop_loss = float(signal.get("stop_loss"))
    levels = target_levels(direction, price, stop_loss, args.reward_risk)
    if not levels:
        send_discord(f"OKX Demo 策略信号被跳过：止损无效\n{args.symbol} {args.interval} {direction} price={price} stop={stop_loss}")
        return None

    order = {"ordId": "dry-run"}
    clipped_size = str(args.size)
    if not no_order:
        try:
            order = place_okx_demo_order(
                args.symbol,
                order_side(direction),
                args.size,
                args.okx_instrument_type,
                args.trade_mode,
                debug=args.debug_orders,
            )
            clipped_size = str(order.get("_request", {}).get("sz", args.size))
            print(f"API返回结果: {order}", flush=True)
        except Exception as exc:
            message = f"OKX Demo 开仓下单失败: {repr(exc)}"
            print(message, flush=True)
            send_discord(message)
            raise

    position = OpenPosition(
        symbol=args.symbol,
        interval=args.interval,
        direction=direction,
        size=clipped_size,
        entry_price=price,
        stop_loss=stop_loss,
        target_price=levels["target_price"],
        protection_price=levels["protection_price"],
        opened_at=time.time(),
        opened_close_time=int(signal.get("kline_close_time") or 0),
        signal_score=int(signal.get("signal_score", 0) or 0),
        signal_grade=str(signal.get("signal_grade", "")),
        order_id=str(order.get("ordId", "")),
        highest_price=price,
        lowest_price=price,
        initial_stop_loss=stop_loss,
        active_stop=stop_loss,
        protection_activated=False,
        protected_stop_price=0.0,
        exit_mode=args.position_stop_mode,
    )
    save_position(position)
    mode = "DRY-RUN" if no_order else "已下模拟单"
    send_discord(
        "\n".join(
            [
                f"**OKX Demo 开仓 {mode}**",
                f"{args.symbol} {args.interval} {direction.upper()} size={clipped_size}",
                f"entry={price:.4f} stop={stop_loss:.4f} target={position.target_price:.4f}",
                f"score={position.signal_score} grade={position.signal_grade} order={position.order_id}",
                f"{signal.get('signal_name', '')}",
                f"{signal.get('filter', '')}",
            ]
        )
    )
    return position


def close_position(args: argparse.Namespace, position: OpenPosition, exit_price: float, reason: str, no_order: bool) -> None:
    order = {"ordId": "dry-run"}
    if not no_order:
        try:
            order = place_okx_demo_order(
                position.symbol,
                order_side(position.direction, closing=True),
                position.size,
                args.okx_instrument_type,
                args.trade_mode,
                reduce_only=True,
                debug=args.debug_orders,
            )
            print(f"API返回结果: {order}", flush=True)
        except Exception as exc:
            message = f"OKX Demo 平仓下单失败: {repr(exc)}"
            print(message, flush=True)
            send_discord(message)
            raise
    return_pct = calculate_return_pct(
        position.direction,
        position.entry_price,
        exit_price,
        float(getattr(args, "fee_rate", 0.0)),
    )
    save_position(None)
    mode = "DRY-RUN" if no_order else "已平模拟单"
    send_discord(
        "\n".join(
            [
                f"**OKX Demo 平仓 {mode}**",
                f"{position.symbol} {position.interval} {position.direction.upper()} reason={reason}",
                f"entry={position.entry_price:.4f} exit={exit_price:.4f} return={return_pct:.2%}",
                f"close_order={order.get('ordId', '')}",
            ]
        )
    )


def evaluate_position(args: argparse.Namespace, position: OpenPosition, bars: list[dict[str, Any]], no_order: bool) -> OpenPosition | None:
    latest = bars[-1]
    high = float(latest["high"])
    low = float(latest["low"])
    close = float(latest["close"])
    position.highest_price = max(position.highest_price, high, close)
    position.lowest_price = min(position.lowest_price, low, close)
    save_position(position)

    initial_stop = position.initial_stop_loss or position.stop_loss
    active_stop = position.active_stop or position.stop_loss
    lifecycle = evaluate_lifecycle_bar(
        position.direction,
        low,
        high,
        close,
        position.entry_price,
        initial_stop,
        position.target_price,
        position.protection_price,
        active_stop=active_stop,
        highest_price=position.highest_price,
        lowest_price=position.lowest_price,
        protection_activated=position.protection_activated,
        protected_stop_price=position.protected_stop_price,
        stop_mode=position.exit_mode or getattr(args, "position_stop_mode", STRUCTURE_ATR_STOP_MODE),
        fee_rate=float(getattr(args, "fee_rate", 0.0)),
        atr_value=parse_float(latest.get("atr")),
    )
    position.active_stop = lifecycle.active_stop
    position.stop_loss = lifecycle.active_stop
    position.highest_price = lifecycle.highest_price
    position.lowest_price = lifecycle.lowest_price
    position.protection_activated = lifecycle.protection_activated
    position.protected_stop_price = lifecycle.protected_stop_price
    save_position(position)
    if lifecycle.decision:
        close_position(args, position, lifecycle.decision.exit_price, lifecycle.decision.reason, no_order)
        return None
    return position


def main() -> None:
    print("This OKX robot entrypoint is retired. Run app.py for research and paper signal tracking.")
    raise SystemExit(2)


if __name__ == "__main__":
    main()
