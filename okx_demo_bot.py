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
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from config import load_env_file
from data_client import fetch_okx_demo_positions, normalize_okx_inst_id, parse_float, place_okx_demo_order
from indicators import calculate_indicators
from project_signal_backtest import fetch_exchange_klines, fetch_higher_timeframe_context
from strategy import ProjectSignalEngine


ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "okx_demo_bot_state.json"
SIGNAL_ENGINE = ProjectSignalEngine()


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OKX Demo 策略循环，开平仓推送 Discord")
    parser.add_argument("--symbol", default="BTCUSDT", help="交易对，例如 BTCUSDT 或 BTC-USDT-SWAP")
    parser.add_argument("--interval", default="15m", help="策略周期")
    parser.add_argument("--limit", type=int, default=1000, help="每轮拉取 K 线数量")
    parser.add_argument("--size", default="1", help="下单数量。合约为张数，默认 1")
    parser.add_argument("--okx-instrument-type", choices=["SWAP", "SPOT"], default="SWAP")
    parser.add_argument("--trade-mode", choices=["cross", "isolated", "cash"], default="cross")
    parser.add_argument("--reward-risk", type=float, default=2.0, help="止盈目标倍数，默认 2R")
    parser.add_argument("--poll-seconds", type=int, default=60, help="轮询间隔，默认 60 秒")
    parser.add_argument("--min-signal-score", type=int, default=0, help="最低信号评分")
    parser.add_argument("--min-structure-score", type=int, default=0, help="最低结构评分")
    parser.add_argument("--no-order", action="store_true", help="只推送信号，不提交 OKX 模拟盘订单")
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
    payload = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        response.read()


def load_position() -> Optional[OpenPosition]:
    if not STATE_FILE.exists():
        return None
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not payload:
        return None
    return OpenPosition(**payload)


def save_position(position: Optional[OpenPosition]) -> None:
    tmp_file = STATE_FILE.with_suffix(".tmp")
    if position is None:
        tmp_file.write_text("null\n", encoding="utf-8")
        tmp_file.replace(STATE_FILE)
        return
    tmp_file.write_text(json.dumps(asdict(position), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_file.replace(STATE_FILE)


def exchange_position_size(position: dict[str, Any]) -> Optional[float]:
    for key in ("pos", "availPos"):
        value = parse_float(position.get(key))
        if value is not None:
            return value
    return None


def exchange_position_direction(position: dict[str, Any]) -> Optional[str]:
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


def reconcile_startup_position(args: argparse.Namespace, local_position: Optional[OpenPosition], no_order: bool) -> tuple[Optional[OpenPosition], bool]:
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


def target_levels(direction: str, entry_price: float, stop_loss: float, reward_risk: float) -> Optional[dict[str, float]]:
    if direction == "long":
        risk = entry_price - stop_loss
        if risk <= 0:
            return None
        return {
            "target_price": entry_price + risk * reward_risk,
            "protection_price": entry_price + risk,
        }
    risk = stop_loss - entry_price
    if risk <= 0:
        return None
    return {
        "target_price": entry_price - risk * reward_risk,
        "protection_price": entry_price - risk,
    }


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


def open_position_from_signal(args: argparse.Namespace, signal: dict[str, Any], no_order: bool) -> Optional[OpenPosition]:
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
    if position.direction == "long":
        return_pct = exit_price / position.entry_price - 1
    else:
        return_pct = position.entry_price / exit_price - 1
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


def evaluate_position(args: argparse.Namespace, position: OpenPosition, bars: list[dict[str, Any]], no_order: bool) -> Optional[OpenPosition]:
    latest = bars[-1]
    high = float(latest["high"])
    low = float(latest["low"])
    close = float(latest["close"])
    position.highest_price = max(position.highest_price, high, close)
    position.lowest_price = min(position.lowest_price, low, close)
    save_position(position)

    if position.direction == "long":
        if low <= position.stop_loss:
            close_position(args, position, position.stop_loss, "stop_loss", no_order)
            return None
        if high >= position.target_price:
            close_position(args, position, position.target_price, "take_profit", no_order)
            return None
        if high >= position.protection_price:
            close_position(args, position, position.protection_price, "protection_reached", no_order)
            return None
    else:
        if high >= position.stop_loss:
            close_position(args, position, position.stop_loss, "stop_loss", no_order)
            return None
        if low <= position.target_price:
            close_position(args, position, position.target_price, "take_profit", no_order)
            return None
        if low <= position.protection_price:
            close_position(args, position, position.protection_price, "protection_reached", no_order)
            return None
    return position


def main() -> None:
    args = parse_args()
    ensure_secret_env("OKX_API_KEY", "OKX_API_KEY: ")
    ensure_secret_env("OKX_SECRET_KEY", "OKX_SECRET_KEY: ")
    ensure_secret_env("OKX_PASSPHRASE", "OKX_PASSPHRASE: ")
    ensure_secret_env("DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_URL: ")

    no_order = bool(args.no_order)
    position = load_position()
    position, trading_paused = reconcile_startup_position(args, position, no_order)
    send_discord(
        f"OKX Demo 策略已启动：{args.symbol} {args.interval} size={args.size} poll={args.poll_seconds}s "
        f"{'(dry-run)' if no_order else '(模拟盘下单)'}"
    )

    while True:
        try:
            signal, bars = latest_signal(args.symbol, args.interval, args.limit, args.okx_instrument_type)
            if not bars:
                raise RuntimeError("no OKX candles returned")
            debug_signal(args, signal)
            if signal_debug_enabled(args) and signal.get("signal") in {"long", "short"}:
                print(f"3. 机器人已收到指令准备发车: {signal}", flush=True)

            if trading_paused:
                debug_signal(args, signal, "trading_paused")
                pass
            elif position:
                position = evaluate_position(args, position, bars, no_order)
            elif signal.get("signal") in {"long", "short"}:
                if should_skip_signal(signal, args.min_signal_score, args.min_structure_score):
                    debug_signal(args, signal, "score_filter")
                    send_discord(
                        f"OKX Demo 信号已过滤：{args.symbol} {args.interval} {signal.get('signal')} "
                        f"score={signal.get('signal_score')} structure={signal.get('structure_score')}"
                    )
                else:
                    position = open_position_from_signal(args, signal, no_order)
            else:
                debug_signal(args, signal, "not_trade_signal")

            latest = bars[-1]
            print(
                time.strftime("%Y-%m-%d %H:%M:%S"),
                args.symbol,
                args.interval,
                "close",
                latest.get("close"),
                "signal",
                signal.get("signal"),
                "position",
                position.direction if position else "none",
                flush=True,
            )
        except Exception as exc:
            message = f"OKX Demo 策略异常：{type(exc).__name__}: {repr(exc)}"
            print(message, flush=True)
            try:
                send_discord(message)
            except Exception as discord_exc:
                print(f"Discord 推送失败：{discord_exc}", flush=True)
        time.sleep(max(10, args.poll_seconds))


if __name__ == "__main__":
    main()
