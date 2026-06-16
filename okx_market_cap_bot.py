#!/usr/bin/env python3
"""
OKX Demo multi-symbol strategy bot.

This bot is the automated trading side of the project. It scans a market-cap
top-N universe, runs the existing ProjectSignalEngine, and can place OKX Demo
orders while tracking multiple positions locally.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from config import config_value, load_config, load_env_file
from data_client import parse_float, place_okx_demo_order
from indicators import calculate_indicators
from project_signal_backtest import fetch_exchange_klines, fetch_higher_timeframe_context
from strategy import ProjectSignalEngine
from strategy_universe import build_okx_market_cap_universe


ROOT = Path(__file__).resolve().parent
CONFIG = load_config()
STATE_FILE = ROOT / "okx_market_cap_bot_state.json"
EVENT_LOG_FILE = ROOT / "okx_market_cap_bot_events.jsonl"
SIGNAL_ENGINES: dict[tuple[str, str], ProjectSignalEngine] = {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OKX Demo 市值前百多币种策略机器人")
    parser.add_argument("--top-n", type=int, default=int(config_value(CONFIG, "bot", "universe_top_n", 100)))
    parser.add_argument("--quote-asset", default=str(config_value(CONFIG, "bot", "universe_quote_asset", "USDT")))
    parser.add_argument("--interval", default=str(config_value(CONFIG, "bot", "interval", "15m")))
    parser.add_argument("--limit", type=int, default=int(config_value(CONFIG, "bot", "kline_limit", 1000)))
    parser.add_argument("--poll-seconds", type=int, default=int(config_value(CONFIG, "bot", "poll_seconds", 60)))
    parser.add_argument("--max-open-positions", type=int, default=int(config_value(CONFIG, "bot", "max_open_positions", 5)))
    parser.add_argument("--size", default=str(config_value(CONFIG, "bot", "position_size", "1")), help="每个信号下单数量；合约为张数")
    parser.add_argument("--okx-instrument-type", choices=["SWAP", "SPOT"], default="SWAP")
    parser.add_argument("--trade-mode", choices=["cross", "isolated", "cash"], default="cross")
    parser.add_argument("--reward-risk", type=float, default=float(config_value(CONFIG, "app", "strategy_reward_risk", 2.0)))
    parser.add_argument("--min-signal-score", type=int, default=int(config_value(CONFIG, "bot", "min_signal_score", 0)))
    parser.add_argument("--min-structure-score", type=int, default=int(config_value(CONFIG, "bot", "min_structure_score", 0)))
    parser.add_argument("--place-order", action="store_true", help="提交 OKX Demo 模拟盘订单；不加则只记录和推送信号")
    parser.add_argument("--prompt-secrets", action="store_true", help="本地交互式输入 OKX API；云服务器请使用 .env")
    parser.add_argument("--debug-signals", action="store_true", help="打印每个标的的策略状态、过滤和跳过原因")
    parser.add_argument("--once", action="store_true", help="只扫描一轮后退出")
    return parser.parse_args()


def ensure_secret_env(name: str, prompt: str, allow_prompt: bool = False) -> None:
    if not os.getenv(name):
        if allow_prompt and sys.stdin.isatty():
            os.environ[name] = getpass.getpass(prompt)
            return
        raise RuntimeError(f"Missing {name}; set it in .env or export it before starting with --place-order")


def ensure_order_environment(args: argparse.Namespace) -> None:
    missing = []
    for name, prompt in (
        ("OKX_API_KEY", "OKX_API_KEY: "),
        ("OKX_SECRET_KEY", "OKX_SECRET_KEY: "),
        ("OKX_PASSPHRASE", "OKX_PASSPHRASE: "),
    ):
        try:
            ensure_secret_env(name, prompt, args.prompt_secrets)
        except RuntimeError:
            missing.append(name)
    if missing:
        message = "OKX place-order mode cannot start; missing env: " + ", ".join(missing)
        append_event({"type": "startup_error", "error": message, "place_order": True})
        send_discord(message)
        raise RuntimeError(message)


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
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            response.read()
    except Exception as exc:
        print(f"discord_notify_failed: {type(exc).__name__}: {exc}", flush=True)


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
        file.write(json.dumps({"created_at": time.time(), **event}, ensure_ascii=False, separators=(",", ":")) + "\n")


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


def order_side(direction: str, closing: bool = False) -> str:
    if direction == "long":
        return "sell" if closing else "buy"
    return "buy" if closing else "sell"


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


def latest_signal(symbol: str, interval: str, limit: int, instrument_type: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw_bars = fetch_exchange_klines("okx", symbol, interval, limit, instrument_type)
    bars = fetch_higher_timeframe_context(symbol, calculate_indicators(raw_bars), interval, "okx", instrument_type)
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


def build_position(args: argparse.Namespace, symbol: str, signal: dict[str, Any], order_id: str) -> Optional[dict[str, Any]]:
    direction = str(signal.get("signal", ""))
    entry_price = parse_float(signal.get("price"))
    stop_loss = parse_float(signal.get("stop_loss"))
    if direction not in {"long", "short"} or entry_price is None or stop_loss is None:
        return None

    levels = target_levels(direction, entry_price, stop_loss, args.reward_risk)
    if not levels:
        return None

    return {
        "id": f"{symbol}:{args.interval}:{direction}:{signal.get('divergence_time') or signal.get('kline_close_time')}",
        "symbol": symbol,
        "interval": args.interval,
        "direction": direction,
        "size": str(args.size),
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
        "order_id": order_id,
        "highest_price": entry_price,
        "lowest_price": entry_price,
    }


def open_position(args: argparse.Namespace, symbol: str, signal: dict[str, Any]) -> Optional[dict[str, Any]]:
    direction = str(signal.get("signal", ""))
    order_id = "signal-only"
    if args.place_order:
        order = place_okx_demo_order(
            symbol,
            order_side(direction),
            args.size,
            args.okx_instrument_type,
            args.trade_mode,
        )
        order_id = str(order.get("ordId", ""))

    position = build_position(args, symbol, signal, order_id)
    if not position:
        return None

    mode = "OKX Demo 已下单" if args.place_order else "SIGNAL-ONLY"
    send_discord(
        "\n".join(
            [
                f"**市值前百策略开仓 {mode}**",
                f"{symbol} {args.interval} {position['direction'].upper()} size={args.size}",
                f"entry={position['entry_price']:.6g} stop={position['stop_loss']:.6g} target={position['target_price']:.6g}",
                f"score={position.get('signal_score')} structure={position.get('structure_score')} grade={position.get('signal_grade')}",
                f"order={order_id}",
                str(signal.get("filter", "")),
            ]
        )
    )
    append_event({"type": "open", **position, "signal_name": signal.get("signal_name"), "filter": signal.get("filter")})
    return position


def close_position(args: argparse.Namespace, position: dict[str, Any], exit_price: float, reason: str) -> None:
    close_order_id = "signal-only"
    if args.place_order:
        order = place_okx_demo_order(
            str(position["symbol"]),
            order_side(str(position["direction"]), closing=True),
            str(position["size"]),
            args.okx_instrument_type,
            args.trade_mode,
            reduce_only=True,
        )
        close_order_id = str(order.get("ordId", ""))

    entry_price = float(position["entry_price"])
    direction = str(position["direction"])
    return_pct = exit_price / entry_price - 1 if direction == "long" else entry_price / exit_price - 1
    position.update(
        {
            "status": "closed",
            "exit_price": exit_price,
            "exit_reason": reason,
            "closed_at": time.time(),
            "return_pct": return_pct,
            "close_order_id": close_order_id,
        }
    )
    mode = "OKX Demo 已平仓" if args.place_order else "SIGNAL-ONLY"
    send_discord(
        "\n".join(
            [
                f"**市值前百策略平仓 {mode}**",
                f"{position['symbol']} {position['interval']} {direction.upper()} reason={reason}",
                f"entry={entry_price:.6g} exit={exit_price:.6g} return={return_pct:.2%}",
                f"close_order={close_order_id}",
            ]
        )
    )
    append_event({"type": "close", **position})


def evaluate_position(args: argparse.Namespace, position: dict[str, Any], bars: list[dict[str, Any]]) -> bool:
    if not bars or position.get("status") != "open":
        return False

    latest = bars[-1]
    high = float(latest["high"])
    low = float(latest["low"])
    close = float(latest["close"])
    position["highest_price"] = max(float(position.get("highest_price") or position["entry_price"]), high, close)
    position["lowest_price"] = min(float(position.get("lowest_price") or position["entry_price"]), low, close)

    direction = str(position["direction"])
    stop_loss = float(position["stop_loss"])
    target_price = float(position["target_price"])
    protection_price = float(position["protection_price"])

    if direction == "long":
        if low <= stop_loss:
            close_position(args, position, stop_loss, "stop_loss")
            return True
        if high >= target_price:
            close_position(args, position, target_price, "take_profit")
            return True
        if high >= protection_price:
            close_position(args, position, protection_price, "protection_reached")
            return True
    else:
        if high >= stop_loss:
            close_position(args, position, stop_loss, "stop_loss")
            return True
        if low <= target_price:
            close_position(args, position, target_price, "take_profit")
            return True
        if low <= protection_price:
            close_position(args, position, protection_price, "protection_reached")
            return True
    return False


def active_positions(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [position for position in state.get("positions", []) if position.get("status") == "open"]


def scan_once(args: argparse.Namespace, state: dict[str, Any]) -> None:
    universe = build_okx_market_cap_universe(args.top_n, args.quote_asset, args.okx_instrument_type)
    skipped: Counter[str] = Counter()
    opened_count = 0
    error_count = 0
    open_symbols = {str(position.get("symbol")) for position in active_positions(state)}

    for position in active_positions(state):
        try:
            _signal, bars = latest_signal(str(position["symbol"]), str(position["interval"]), args.limit, args.okx_instrument_type)
            evaluate_position(args, position, bars)
        except Exception as exc:
            error_count += 1
            message = f"{position.get('symbol')} 持仓检查异常：{type(exc).__name__}: {exc}"
            append_event({"type": "error", "symbol": position.get("symbol"), "error": message})
            send_discord(message)

    capacity = max(0, args.max_open_positions - len(active_positions(state)))
    if capacity <= 0:
        save_state(state)
        append_event(
            {
                "type": "scan",
                "universe_size": len(universe),
                "open_positions": len(active_positions(state)),
                "opened": opened_count,
                "errors": error_count,
                "skipped": dict(skipped),
                "capacity": 0,
                "place_order": args.place_order,
            }
        )
        return

    existing_ids = {str(position.get("id")) for position in state.get("positions", [])}
    for item in universe:
        symbol = str(item["symbol"])
        if symbol in open_symbols:
            skipped["already_open"] += 1
            continue

        try:
            signal, _bars = latest_signal(symbol, args.interval, args.limit, args.okx_instrument_type)
        except Exception as exc:
            error_count += 1
            append_event({"type": "error", "symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
            debug_signal(args, symbol, {"signal": "error", "signal_name": f"{type(exc).__name__}: {exc}"}, "exception")
            continue

        if signal.get("signal") not in {"long", "short"}:
            skipped["not_trade_signal"] += 1
            debug_signal(args, symbol, signal, "not_trade_signal")
            continue
        if should_skip_signal(signal, args.min_signal_score, args.min_structure_score):
            skipped["score_filter"] += 1
            debug_signal(args, symbol, signal, "score_filter")
            continue

        probe = build_position(args, symbol, signal, "probe")
        if not probe or probe["id"] in existing_ids:
            skipped["duplicate_or_invalid_position"] += 1
            debug_signal(args, symbol, signal, "duplicate_or_invalid_position")
            continue

        try:
            position = open_position(args, symbol, signal)
        except Exception as exc:
            error_count += 1
            message = f"{symbol} 开仓失败：{type(exc).__name__}: {exc}"
            append_event({"type": "error", "symbol": symbol, "error": message, "signal": signal})
            send_discord(message)
            debug_signal(args, symbol, signal, "open_position_failed")
            continue
        if not position:
            skipped["duplicate_or_invalid_position"] += 1
            continue

        state.setdefault("positions", []).append(position)
        existing_ids.add(position["id"])
        open_symbols.add(symbol)
        opened_count += 1
        capacity -= 1
        if capacity <= 0:
            break

    save_state(state)
    append_event(
        {
            "type": "scan",
            "universe_size": len(universe),
            "open_positions": len(active_positions(state)),
            "opened": opened_count,
            "errors": error_count,
            "skipped": dict(skipped),
            "capacity": max(0, args.max_open_positions - len(active_positions(state))),
            "place_order": args.place_order,
        }
    )


def main() -> None:
    args = parse_args()
    if args.place_order:
        ensure_order_environment(args)
    state = load_state()

    mode = "OKX Demo 下单" if args.place_order else "只记录信号"
    append_event(
        {
            "type": "startup",
            "top_n": args.top_n,
            "interval": args.interval,
            "max_open_positions": args.max_open_positions,
            "okx_instrument_type": args.okx_instrument_type,
            "trade_mode": args.trade_mode,
            "place_order": args.place_order,
            "mode": mode,
        }
    )
    send_discord(
        f"市值前百 OKX 策略机器人启动：top={args.top_n} interval={args.interval} "
        f"max_positions={args.max_open_positions} mode={mode}"
    )

    while True:
        started_at = time.time()
        scan_once(args, state)
        print(
            time.strftime("%Y-%m-%d %H:%M:%S"),
            "scan_done",
            "open_positions",
            len(active_positions(state)),
            "mode",
            mode,
            flush=True,
        )
        if args.once:
            break
        elapsed = time.time() - started_at
        time.sleep(max(10, args.poll_seconds - int(elapsed)))


if __name__ == "__main__":
    main()
