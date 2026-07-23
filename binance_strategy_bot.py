#!/usr/bin/env python3
"""
Binance market-cap universe strategy bot.

This process is separate from the monitoring website. It scans Binance-listed
USDT pairs from the market-cap top N universe and runs the existing
ProjectSignalEngine without changing strategy logic.
"""

from __future__ import annotations

import argparse
import json
import os
import signal as os_signal
import threading
import time
from pathlib import Path
from typing import Any

from config import config_value, load_config, load_env_file
from data_client import fetch_klines, parse_float
from indicators import calculate_indicators
from position_manager import (
    STRUCTURE_ATR_STOP_MODE,
    evaluate_bar_exit,
    evaluate_lifecycle_bar,
)
from position_manager import (
    calculate_target_levels as target_levels,
)
from project_signal_backtest import fetch_higher_timeframe_context
from runtime_utils import append_jsonl, atomic_write_text, post_discord
from strategy import ProjectSignalEngine
from strategy_universe import build_market_cap_universe

ROOT = Path(__file__).resolve().parent
CONFIG = load_config()
STATE_FILE = ROOT / "binance_strategy_bot_state.json"
EVENT_LOG_FILE = ROOT / "binance_strategy_bot_events.jsonl"
SIGNAL_ENGINES: dict[tuple[str, str], ProjectSignalEngine] = {}
SHUTDOWN_EVENT = threading.Event()
POSITION_STOP_MODE = str(config_value(CONFIG, "bot", "position_stop_mode", STRUCTURE_ATR_STOP_MODE))
POSITION_FEE_RATE = float(config_value(CONFIG, "bot", "fee_rate", config_value(CONFIG, "app", "strategy_fee_rate", 0.001)))


def shutdown_signal_handler(signum: int, frame: Any) -> None:
    SHUTDOWN_EVENT.set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Binance 前百市值币种策略机器人")
    parser.add_argument("--top-n", type=int, default=int(config_value(CONFIG, "bot", "universe_top_n", 100)))
    parser.add_argument("--quote-asset", default=str(config_value(CONFIG, "bot", "universe_quote_asset", "USDT")))
    parser.add_argument("--interval", default=str(config_value(CONFIG, "bot", "interval", "15m")))
    parser.add_argument("--limit", type=int, default=int(config_value(CONFIG, "bot", "kline_limit", 1000)))
    parser.add_argument("--poll-seconds", type=int, default=int(config_value(CONFIG, "bot", "poll_seconds", 60)))
    parser.add_argument("--min-quote-volume", type=float, default=float(config_value(CONFIG, "bot", "min_quote_volume", 10_000_000)))
    parser.add_argument("--max-open-positions", type=int, default=int(config_value(CONFIG, "bot", "max_open_positions", 5)))
    parser.add_argument("--min-signal-score", type=int, default=int(config_value(CONFIG, "bot", "min_signal_score", 0)))
    parser.add_argument("--min-structure-score", type=int, default=int(config_value(CONFIG, "bot", "min_structure_score", 0)))
    parser.add_argument("--reward-risk", type=float, default=float(config_value(CONFIG, "app", "strategy_reward_risk", 2.0)))
    parser.add_argument("--debug-signals", action="store_true", help="打印每个标的的策略状态、过滤和跳过原因")
    parser.add_argument("--once", action="store_true", help="只扫描一轮后退出")
    return parser.parse_args()


def send_discord(content: str) -> None:
    load_env_file()
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return
    post_discord(webhook_url, content)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"positions": []}
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"positions": []}
    return payload if isinstance(payload, dict) else {"positions": []}


def save_state(state: dict[str, Any]) -> None:
    atomic_write_text(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))


def append_event(event: dict[str, Any]) -> None:
    append_jsonl(EVENT_LOG_FILE, event)


def signal_debug_enabled(args: argparse.Namespace) -> bool:
    return args.debug_signals or os.getenv("SIGNAL_TRACE", "").strip().lower() in {"1", "true", "yes", "on"}


def debug_signal(args: argparse.Namespace, symbol: str, signal: dict[str, Any], reason: str = "") -> None:
    if not signal_debug_enabled(args):
        return
    parts = [
        time.strftime("%Y-%m-%d %H:%M:%S"),
        "signal_probe",
        symbol,
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


def signal_engine(symbol: str, interval: str) -> ProjectSignalEngine:
    key = (symbol.upper(), interval)
    engine = SIGNAL_ENGINES.get(key)
    if engine is None:
        engine = ProjectSignalEngine()
        SIGNAL_ENGINES[key] = engine
    return engine


def latest_signal(symbol: str, interval: str, limit: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw_bars = fetch_klines(symbol, interval, limit)
    bars = fetch_higher_timeframe_context(symbol, calculate_indicators(raw_bars), interval, "binance")
    ready_bars = [bar for bar in bars if bar.get("macd") is not None]
    signal = signal_engine(symbol, interval).detect(ready_bars)
    if bars:
        signal.setdefault("symbol", symbol)
        signal.setdefault("interval", interval)
        signal.setdefault("price", bars[-1].get("close"))
        signal.setdefault("kline_close_time", bars[-1].get("close_time") or bars[-1].get("open_time"))
    return signal, bars


def should_skip_signal(signal: dict[str, Any], min_signal_score: int, min_structure_score: int) -> bool:
    return (
        int(signal.get("signal_score", 0) or 0) < min_signal_score
        or int(signal.get("structure_score", 0) or 0) < min_structure_score
    )


def open_position(symbol: str, interval: str, signal: dict[str, Any], reward_risk: float) -> dict[str, Any] | None:
    direction = str(signal.get("signal", ""))
    entry_price = parse_float(signal.get("price"))
    stop_loss = parse_float(signal.get("stop_loss"))
    if direction not in {"long", "short"} or entry_price is None or stop_loss is None:
        return None

    levels = target_levels(direction, entry_price, stop_loss, reward_risk)
    if not levels:
        return None

    return {
        "id": f"{symbol}:{interval}:{direction}:{signal.get('divergence_time') or signal.get('kline_close_time')}",
        "symbol": symbol,
        "interval": interval,
        "direction": direction,
        "status": "open",
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "initial_stop_loss": stop_loss,
        "active_stop": stop_loss,
        "target_price": levels["target_price"],
        "protection_price": levels["protection_price"],
        "risk": levels["risk"],
        "protection_activated": False,
        "protected_stop_price": 0.0,
        "exit_mode": POSITION_STOP_MODE,
        "opened_at": time.time(),
        "opened_kline_close_time": signal.get("kline_close_time"),
        "last_evaluated_close_time": signal.get("kline_close_time"),
        "signal_score": signal.get("signal_score"),
        "structure_score": signal.get("structure_score"),
        "signal_grade": signal.get("signal_grade"),
    }


def evaluate_position(position: dict[str, Any], bars: list[dict[str, Any]]) -> bool:
    direction = str(position.get("direction"))
    entry_price = parse_float(position.get("entry_price"))
    initial_stop = parse_float(position.get("initial_stop_loss")) or parse_float(position.get("stop_loss"))
    active_stop = parse_float(position.get("active_stop")) or initial_stop
    target_price = parse_float(position.get("target_price"))
    protection_price = parse_float(position.get("protection_price"))
    opened_close_time = int(position.get("opened_kline_close_time") or 0)
    last_evaluated_close_time = int(position.get("last_evaluated_close_time") or opened_close_time)
    if initial_stop is None or target_price is None or protection_price is None:
        return False
    now_ms = int(time.time() * 1000)
    completed_bars = [
        bar
        for bar in bars
        if int(bar.get("close_time") or 0) <= now_ms and bar.get("confirmed") is not False
    ]
    future_bars = (
        [bar for bar in completed_bars if int(bar.get("close_time") or 0) > last_evaluated_close_time]
        if last_evaluated_close_time
        else completed_bars[-1:]
    )
    if entry_price is None:
        for bar in future_bars:
            close_time = int(bar.get("close_time") or 0)
            high = parse_float(bar.get("high"))
            low = parse_float(bar.get("low"))
            if high is None or low is None:
                continue
            decision = evaluate_bar_exit(direction, low, high, initial_stop, target_price, protection_price)
            if close_time:
                position["last_evaluated_close_time"] = close_time
            if decision:
                position["status"] = "closed"
                position["exit_reason"] = decision.reason
                position["exit_price"] = decision.exit_price
                position["closed_at"] = time.time()
                return True
        return False

    for bar in future_bars:
        close_time = int(bar.get("close_time") or 0)
        high = parse_float(bar.get("high"))
        low = parse_float(bar.get("low"))
        close = parse_float(bar.get("close"))
        if high is None or low is None:
            continue

        lifecycle = evaluate_lifecycle_bar(
            direction,
            low,
            high,
            close if close is not None else high,
            entry_price,
            initial_stop,
            target_price,
            protection_price,
            active_stop=active_stop,
            highest_price=parse_float(position.get("highest_price")),
            lowest_price=parse_float(position.get("lowest_price")),
            protection_activated=bool(position.get("protection_activated")),
            protected_stop_price=parse_float(position.get("protected_stop_price")) or 0.0,
            stop_mode=str(position.get("exit_mode") or POSITION_STOP_MODE),
            fee_rate=POSITION_FEE_RATE,
            atr_value=parse_float(bar.get("atr")),
        )
        active_stop = lifecycle.active_stop
        position["active_stop"] = lifecycle.active_stop
        position["stop_loss"] = lifecycle.active_stop
        position["highest_price"] = lifecycle.highest_price
        position["lowest_price"] = lifecycle.lowest_price
        position["protection_activated"] = lifecycle.protection_activated
        position["protected_stop_price"] = lifecycle.protected_stop_price
        if close_time:
            position["last_evaluated_close_time"] = close_time
        if lifecycle.decision:
            position["status"] = "closed"
            position["exit_reason"] = lifecycle.decision.reason
            position["exit_price"] = lifecycle.decision.exit_price
            position["closed_at"] = time.time()
            return True
    return False


def scan_once(args: argparse.Namespace, state: dict[str, Any]) -> None:
    universe = build_market_cap_universe(args.top_n, args.quote_asset, args.min_quote_volume)
    open_positions = [position for position in state.get("positions", []) if position.get("status") == "open"]
    open_symbols = {str(position.get("symbol")) for position in open_positions}

    for position in open_positions:
        try:
            _signal, bars = latest_signal(str(position["symbol"]), str(position["interval"]), args.limit)
            if evaluate_position(position, bars):
                event = {"type": "close", **position}
                append_event(event)
                send_discord(f"Binance 策略平仓：{position['symbol']} {position['direction']} {position['exit_reason']} exit={position['exit_price']}")
        except Exception as exc:
            append_event({"type": "error", "symbol": position.get("symbol"), "error": f"{type(exc).__name__}: {exc}", "created_at": time.time()})

    open_positions = [position for position in state.get("positions", []) if position.get("status") == "open"]
    capacity = max(0, args.max_open_positions - len(open_positions))
    if capacity <= 0:
        save_state(state)
        return

    for item in universe:
        symbol = str(item["symbol"])
        if symbol in open_symbols:
            continue
        try:
            signal, _bars = latest_signal(symbol, args.interval, args.limit)
        except Exception as exc:
            append_event({"type": "error", "symbol": symbol, "error": f"{type(exc).__name__}: {exc}", "created_at": time.time()})
            debug_signal(args, symbol, {"signal": "error", "signal_name": f"{type(exc).__name__}: {exc}"}, "exception")
            continue

        if signal.get("signal") not in {"long", "short"}:
            debug_signal(args, symbol, signal, "not_trade_signal")
            continue
        if should_skip_signal(signal, args.min_signal_score, args.min_structure_score):
            debug_signal(args, symbol, signal, "score_filter")
            continue

        position = open_position(symbol, args.interval, signal, args.reward_risk)
        if not position:
            debug_signal(args, symbol, signal, "invalid_position")
            continue

        state.setdefault("positions", []).append(position)
        open_symbols.add(symbol)
        capacity -= 1
        event = {"type": "open", **position, "signal_name": signal.get("signal_name"), "filter": signal.get("filter")}
        append_event(event)
        send_discord(
            "\n".join(
                [
                    "Binance 策略开仓信号",
                    f"{symbol} {args.interval} {position['direction'].upper()}",
                    f"entry={position['entry_price']} stop={position['stop_loss']} target={position['target_price']}",
                    f"score={position.get('signal_score')} structure={position.get('structure_score')} grade={position.get('signal_grade')}",
                    str(signal.get("filter", "")),
                ]
            )
        )
        if capacity <= 0:
            break

    save_state(state)


def main() -> None:
    args = parse_args()
    SHUTDOWN_EVENT.clear()
    os_signal.signal(os_signal.SIGTERM, shutdown_signal_handler)
    os_signal.signal(os_signal.SIGINT, shutdown_signal_handler)
    state = load_state()
    send_discord(f"Binance 策略机器人启动：top={args.top_n} interval={args.interval} max_positions={args.max_open_positions}")

    while not SHUTDOWN_EVENT.is_set():
        started_at = time.time()
        scan_once(args, state)
        print(
            time.strftime("%Y-%m-%d %H:%M:%S"),
            "scan_done",
            "positions",
            len([position for position in state.get("positions", []) if position.get("status") == "open"]),
            flush=True,
        )
        if args.once or SHUTDOWN_EVENT.is_set():
            break
        elapsed = time.time() - started_at
        SHUTDOWN_EVENT.wait(max(10, args.poll_seconds - int(elapsed)))

    save_state(state)
    append_event({"type": "shutdown", "created_at": time.time()})


if __name__ == "__main__":
    main()
