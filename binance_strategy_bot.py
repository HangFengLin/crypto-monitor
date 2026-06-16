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
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

from config import config_value, load_config, load_env_file
from data_client import fetch_klines, parse_float
from indicators import calculate_indicators
from project_signal_backtest import fetch_higher_timeframe_context
from strategy import ProjectSignalEngine
from strategy_universe import build_market_cap_universe


ROOT = Path(__file__).resolve().parent
CONFIG = load_config()
STATE_FILE = ROOT / "binance_strategy_bot_state.json"
EVENT_LOG_FILE = ROOT / "binance_strategy_bot_events.jsonl"
SIGNAL_ENGINES: dict[tuple[str, str], ProjectSignalEngine] = {}


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
    payload = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        response.read()


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"positions": []}
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"positions": []}
    return payload if isinstance(payload, dict) else {"positions": []}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def append_event(event: dict[str, Any]) -> None:
    with EVENT_LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


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


def target_levels(direction: str, entry_price: float, stop_loss: float, reward_risk: float) -> Optional[dict[str, float]]:
    if direction == "long":
        risk = entry_price - stop_loss
        if risk <= 0:
            return None
        return {"risk": risk, "target_price": entry_price + risk * reward_risk, "protection_price": entry_price + risk}

    if direction == "short":
        risk = stop_loss - entry_price
        if risk <= 0:
            return None
        return {"risk": risk, "target_price": entry_price - risk * reward_risk, "protection_price": entry_price - risk}
    return None


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


def open_position(symbol: str, interval: str, signal: dict[str, Any], reward_risk: float) -> Optional[dict[str, Any]]:
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
        "target_price": levels["target_price"],
        "protection_price": levels["protection_price"],
        "risk": levels["risk"],
        "opened_at": time.time(),
        "opened_kline_close_time": signal.get("kline_close_time"),
        "signal_score": signal.get("signal_score"),
        "structure_score": signal.get("structure_score"),
        "signal_grade": signal.get("signal_grade"),
    }


def evaluate_position(position: dict[str, Any], bars: list[dict[str, Any]]) -> bool:
    direction = str(position.get("direction"))
    stop_loss = parse_float(position.get("stop_loss"))
    target_price = parse_float(position.get("target_price"))
    protection_price = parse_float(position.get("protection_price"))
    opened_close_time = int(position.get("opened_kline_close_time") or 0)
    if stop_loss is None or target_price is None or protection_price is None:
        return False

    future_bars = [bar for bar in bars if int(bar.get("close_time") or 0) > opened_close_time]
    for bar in future_bars[-3:]:
        high = parse_float(bar.get("high"))
        low = parse_float(bar.get("low"))
        if high is None or low is None:
            continue

        reason = None
        exit_price = None
        if direction == "long":
            if low <= stop_loss:
                reason, exit_price = "stop_loss", stop_loss
            elif high >= target_price:
                reason, exit_price = "take_profit", target_price
            elif high >= protection_price:
                reason, exit_price = "protection_reached", protection_price
        elif direction == "short":
            if high >= stop_loss:
                reason, exit_price = "stop_loss", stop_loss
            elif low <= target_price:
                reason, exit_price = "take_profit", target_price
            elif low <= protection_price:
                reason, exit_price = "protection_reached", protection_price

        if reason and exit_price is not None:
            position["status"] = "closed"
            position["exit_reason"] = reason
            position["exit_price"] = exit_price
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
    state = load_state()
    send_discord(f"Binance 策略机器人启动：top={args.top_n} interval={args.interval} max_positions={args.max_open_positions}")

    while True:
        started_at = time.time()
        scan_once(args, state)
        print(
            time.strftime("%Y-%m-%d %H:%M:%S"),
            "scan_done",
            "positions",
            len([position for position in state.get("positions", []) if position.get("status") == "open"]),
            flush=True,
        )
        if args.once:
            break
        elapsed = time.time() - started_at
        time.sleep(max(10, args.poll_seconds - int(elapsed)))


if __name__ == "__main__":
    main()
