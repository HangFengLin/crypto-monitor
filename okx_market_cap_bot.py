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
import signal as os_signal
import sys
import time
import urllib.request
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any, Optional

from config import config_value, load_config, load_env_file
from data_client import fetch_okx_demo_balance, fetch_okx_instrument, parse_float, place_okx_demo_order
from indicators import calculate_indicators
from project_signal_backtest import fetch_exchange_klines, fetch_higher_timeframe_context
from strategy import ProjectSignalEngine
from strategy_universe import build_okx_market_cap_universe


ROOT = Path(__file__).resolve().parent
CONFIG = load_config()


def config_int(section: str, key: str, default: int, env_name: Optional[str] = None) -> int:
    fallback: Any = os.getenv(env_name, default) if env_name else default
    value = config_value(CONFIG, section, key, fallback)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def config_float(section: str, key: str, default: float, env_name: Optional[str] = None) -> float:
    fallback: Any = os.getenv(env_name, default) if env_name else default
    value = config_value(CONFIG, section, key, fallback)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def config_symbol_set(section: str, key: str, default: tuple[str, ...] = (), env_name: Optional[str] = None) -> set[str]:
    env_value = os.getenv(env_name, "").strip() if env_name else ""
    value: Any = env_value if env_value else config_value(CONFIG, section, key, list(default))
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = default
    return {str(item).strip().upper() for item in values if str(item).strip()}


DEFAULT_STATE_FILE = ROOT / "okx_market_cap_bot_state.json"
DEFAULT_EVENT_LOG_FILE = ROOT / "okx_market_cap_bot_events.jsonl"
LEGACY_STATE_FILE = DEFAULT_STATE_FILE
STATE_FILE = Path(os.getenv("OKX_BOT_STATE_FILE", str(DEFAULT_STATE_FILE))).expanduser()
EVENT_LOG_FILE = Path(os.getenv("OKX_BOT_EVENT_LOG_FILE", str(DEFAULT_EVENT_LOG_FILE))).expanduser()
SIGNAL_ENGINES: dict[tuple[str, str], ProjectSignalEngine] = {}
NON_RETRYABLE_ORDER_ERROR_CODES = {"51001", "51087"}
ORDER_SYMBOL_DISABLE_SECONDS = 24 * 60 * 60


class ScanTimeout(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OKX Demo 市值前百多币种策略机器人")
    parser.add_argument("--top-n", type=int, default=int(config_value(CONFIG, "bot", "universe_top_n", 100)))
    parser.add_argument("--quote-asset", default=str(config_value(CONFIG, "bot", "universe_quote_asset", "USDT")))
    parser.add_argument("--interval", default=str(config_value(CONFIG, "bot", "interval", "15m")))
    parser.add_argument("--limit", type=int, default=int(config_value(CONFIG, "bot", "kline_limit", 1000)))
    parser.add_argument("--poll-seconds", type=int, default=int(config_value(CONFIG, "bot", "poll_seconds", 60)))
    parser.add_argument(
        "--scan-timeout-seconds",
        type=int,
        default=config_int("bot", "scan_timeout_seconds", 300, "BOT_SCAN_TIMEOUT_SECONDS"),
        help="单轮扫描最大允许秒数；超时后退出进程交给 Docker 重启",
    )
    parser.add_argument("--max-open-positions", type=int, default=int(config_value(CONFIG, "bot", "max_open_positions", 5)))
    parser.add_argument("--size", default=str(config_value(CONFIG, "bot", "position_size", "1")), help="每个信号下单数量；合约为张数")
    parser.add_argument(
        "--position-sizing",
        choices=["fixed", "risk"],
        default=str(config_value(CONFIG, "bot", "position_sizing", "fixed")),
        help="fixed 使用 --size 固定张数；risk 按账户权益和止损距离动态计算张数",
    )
    parser.add_argument(
        "--risk-per-trade-pct",
        type=float,
        default=config_float("bot", "risk_per_trade_pct", 0.005, "BOT_RISK_PER_TRADE_PCT"),
        help="risk 模式下单笔最大风险占账户权益比例，默认 0.005=0.5%%",
    )
    parser.add_argument(
        "--max-position-notional-usdt",
        type=float,
        default=config_float("bot", "max_position_notional_usdt", 50.0, "BOT_MAX_POSITION_NOTIONAL_USDT"),
        help="risk 模式下单笔最大名义价值，0 表示不限制",
    )
    parser.add_argument(
        "--sizing-equity-usdt",
        type=float,
        default=config_float("bot", "sizing_equity_usdt", 0.0, "BOT_SIZING_EQUITY_USDT"),
        help="risk 模式下用于 dry-run/无余额接口时的权益估算；0 表示读取 OKX Demo 余额",
    )
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
        message = f"{type(exc).__name__}: {exc}"
        print(f"discord_notify_failed: {message}", flush=True)
        try:
            append_event({"type": "discord_error", "error": message, "content": content[:500]})
        except Exception as log_exc:
            print(f"discord_error_log_failed: {type(log_exc).__name__}: {log_exc}", flush=True)


def load_state() -> dict[str, Any]:
    source_file = STATE_FILE
    if not source_file.exists() and STATE_FILE != LEGACY_STATE_FILE and LEGACY_STATE_FILE.exists():
        source_file = LEGACY_STATE_FILE
    if not source_file.exists():
        return {"positions": []}
    try:
        payload = json.loads(source_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"positions": []}
    state = payload if isinstance(payload, dict) else {"positions": []}
    if source_file != STATE_FILE:
        try:
            save_state(state)
            append_event({"type": "state_migrated", "from": str(source_file), "to": str(STATE_FILE)})
        except OSError as exc:
            print(f"state_migration_failed: {type(exc).__name__}: {exc}", flush=True)
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def append_event(event: dict[str, Any]) -> None:
    EVENT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with EVENT_LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps({"created_at": time.time(), **event}, ensure_ascii=False, separators=(",", ":")) + "\n")


def scan_timeout_handler(signum: int, frame: Any) -> None:
    raise ScanTimeout("scan timed out")


def install_scan_timeout(seconds: int) -> None:
    if seconds <= 0:
        return
    os_signal.signal(os_signal.SIGALRM, scan_timeout_handler)
    os_signal.alarm(seconds)


def clear_scan_timeout() -> None:
    try:
        os_signal.alarm(0)
    except (AttributeError, ValueError):
        return


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


def okx_position_side(args: argparse.Namespace, direction: str) -> Optional[str]:
    if args.okx_instrument_type.upper() != "SWAP":
        return None
    mode = str(config_value(CONFIG, "bot", "okx_position_mode", os.getenv("OKX_POSITION_MODE", ""))).strip().lower()
    if mode in {"long_short", "long-short", "hedge", "dual", "dual_side", "dual-side"} and direction in {"long", "short"}:
        return direction
    return None


def okx_order_context(args: argparse.Namespace, symbol: str, direction: str, closing: bool = False, order_symbol: Optional[str] = None) -> dict[str, Any]:
    side = order_side(direction, closing=closing)
    position_side = okx_position_side(args, direction)
    context: dict[str, Any] = {
        "symbol": symbol,
        "order_symbol": order_symbol or symbol,
        "side": side,
        "size": args.size,
        "instrument_type": args.okx_instrument_type,
        "trade_mode": args.trade_mode,
        "closing": closing,
    }
    if position_side:
        context["position_side"] = position_side
    return context


def decimal_value(value: Any, field_name: str, *, positive: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError(f"Invalid {field_name}: {value!r}")
    return number


def format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def extract_okx_equity_usdt(balance: dict[str, Any]) -> Decimal:
    for field in ("totalEq", "adjEq", "isoEq"):
        raw_value = balance.get(field)
        if raw_value not in {None, ""}:
            value = decimal_value(raw_value, field)
            if value > 0:
                return value

    details = balance.get("details")
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            if str(detail.get("ccy", "")).upper() != "USDT":
                continue
            for field in ("availEq", "eqUsd", "eq", "cashBal"):
                raw_value = detail.get(field)
                if raw_value not in {None, ""}:
                    value = decimal_value(raw_value, f"details.{field}")
                    if value > 0:
                        return value

    raise ValueError("OKX balance response does not include positive USDT equity")


def sizing_equity(args: argparse.Namespace) -> Decimal:
    configured = decimal_value(args.sizing_equity_usdt, "sizing_equity_usdt")
    if configured > 0:
        return configured
    if not getattr(args, "place_order", False):
        return Decimal("1000")
    return extract_okx_equity_usdt(fetch_okx_demo_balance("USDT"))


def contract_value(instrument: dict[str, Any], instrument_type: str) -> Decimal:
    if instrument_type.upper() == "SWAP":
        return decimal_value(instrument.get("ctVal", "1") or "1", "ctVal", positive=True)
    return Decimal("1")


def estimate_notional_usdt(size: Decimal, price: Decimal, instrument: dict[str, Any], instrument_type: str) -> Decimal:
    return size * price * contract_value(instrument, instrument_type)


def calculate_risk_position_size(
    *,
    equity: Decimal,
    risk_per_trade_pct: Decimal,
    max_position_notional_usdt: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal,
    instrument: dict[str, Any],
    instrument_type: str,
) -> tuple[Decimal, dict[str, Any]]:
    if equity <= 0:
        raise ValueError(f"Equity must be positive: {equity}")
    if risk_per_trade_pct <= 0:
        raise ValueError(f"risk_per_trade_pct must be positive: {risk_per_trade_pct}")
    price_risk = abs(entry_price - stop_loss)
    if price_risk <= 0:
        raise ValueError("Entry and stop-loss must differ for risk sizing")

    ct_val = contract_value(instrument, instrument_type)
    risk_budget = equity * risk_per_trade_pct
    risk_per_contract = price_risk * ct_val
    raw_size = risk_budget / risk_per_contract

    notional_cap = Decimal("0")
    if max_position_notional_usdt > 0:
        notional_cap = max_position_notional_usdt / (entry_price * ct_val)
        raw_size = min(raw_size, notional_cap)

    lot_size = decimal_value(instrument.get("lotSz", "1") or "1", "lotSz", positive=True)
    min_size = decimal_value(instrument.get("minSz", lot_size) or lot_size, "minSz", positive=True)
    clipped_size = floor_to_step(raw_size, lot_size)
    if clipped_size < min_size:
        raise ValueError(
            "Risk-sized order is below OKX minimum after lot-size clipping: "
            f"raw={format_decimal(raw_size)}, clipped={format_decimal(clipped_size)}, "
            f"minSz={format_decimal(min_size)}, lotSz={format_decimal(lot_size)}"
        )

    return clipped_size, {
        "mode": "risk",
        "equity_usdt": format_decimal(equity),
        "risk_per_trade_pct": format_decimal(risk_per_trade_pct),
        "risk_budget_usdt": format_decimal(risk_budget),
        "price_risk": format_decimal(price_risk),
        "risk_per_contract_usdt": format_decimal(risk_per_contract),
        "raw_size": format_decimal(raw_size),
        "max_position_notional_usdt": format_decimal(max_position_notional_usdt),
        "notional_cap_size": format_decimal(notional_cap) if notional_cap > 0 else "",
        "notional_usdt": format_decimal(estimate_notional_usdt(clipped_size, entry_price, instrument, instrument_type)),
        "ctVal": instrument.get("ctVal"),
        "ctValCcy": instrument.get("ctValCcy"),
        "lotSz": instrument.get("lotSz"),
        "minSz": instrument.get("minSz"),
    }


def position_size_for_signal(args: argparse.Namespace, order_symbol: str, signal: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if args.position_sizing == "fixed":
        return str(args.size), {"mode": "fixed"}

    entry_price = decimal_value(signal.get("price"), "entry_price", positive=True)
    stop_loss = decimal_value(signal.get("stop_loss"), "stop_loss", positive=True)
    instrument = fetch_okx_instrument(order_symbol, args.okx_instrument_type)
    size, sizing = calculate_risk_position_size(
        equity=sizing_equity(args),
        risk_per_trade_pct=decimal_value(args.risk_per_trade_pct, "risk_per_trade_pct", positive=True),
        max_position_notional_usdt=decimal_value(args.max_position_notional_usdt, "max_position_notional_usdt"),
        entry_price=entry_price,
        stop_loss=stop_loss,
        instrument=instrument,
        instrument_type=args.okx_instrument_type,
    )
    return format_decimal(size), sizing


def format_order_failure(title: str, context: dict[str, Any], exc: Exception) -> str:
    fields = [
        f"symbol={context.get('symbol')}",
        f"order_symbol={context.get('order_symbol')}",
        f"side={context.get('side')}",
        f"posSide={context.get('position_side', '-')}",
        f"size={context.get('size')}",
        f"tdMode={context.get('trade_mode')}",
        f"closing={context.get('closing')}",
        f"error={type(exc).__name__}: {exc}",
    ]
    return title + "\n" + "\n".join(fields)


def is_non_retryable_order_error(exc: Exception) -> bool:
    text = str(exc)
    return any(code in text for code in NON_RETRYABLE_ORDER_ERROR_CODES)


def disabled_order_symbols(state: dict[str, Any]) -> dict[str, Any]:
    disabled = state.setdefault("disabled_order_symbols", {})
    if not isinstance(disabled, dict):
        disabled = {}
        state["disabled_order_symbols"] = disabled

    now = time.time()
    for symbol, info in list(disabled.items()):
        if isinstance(info, dict) and parse_float(info.get("until")) is not None and float(info["until"]) <= now:
            disabled.pop(symbol, None)
    return disabled


def configured_order_symbol_blocklist() -> set[str]:
    return config_symbol_set("bot", "okx_order_symbol_blocklist", env_name="OKX_ORDER_SYMBOL_BLOCKLIST")


def disable_order_symbol(state: dict[str, Any], order_symbol: str, exc: Exception) -> None:
    disabled_order_symbols(state)[order_symbol] = {
        "until": time.time() + ORDER_SYMBOL_DISABLE_SECONDS,
        "reason": f"{type(exc).__name__}: {exc}",
    }


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


def build_position(
    args: argparse.Namespace,
    symbol: str,
    signal: dict[str, Any],
    order_id: str,
    size: Optional[str] = None,
    sizing: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
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
        "inst_id": signal.get("inst_id"),
        "interval": args.interval,
        "direction": direction,
        "size": str(size or args.size),
        "position_sizing": sizing or {"mode": "fixed"},
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


def open_position(args: argparse.Namespace, symbol: str, signal: dict[str, Any], order_symbol: Optional[str] = None) -> Optional[dict[str, Any]]:
    direction = str(signal.get("signal", ""))
    order_id = "signal-only"
    order_symbol = order_symbol or symbol
    signal.setdefault("inst_id", order_symbol)
    order_context = okx_order_context(args, symbol, direction, order_symbol=order_symbol)
    order_size, sizing = position_size_for_signal(args, order_symbol, signal)
    order_context["size"] = order_size
    order_context["position_sizing"] = sizing
    if args.place_order:
        try:
            order = place_okx_demo_order(
                order_symbol,
                str(order_context["side"]),
                order_size,
                args.okx_instrument_type,
                args.trade_mode,
                position_side=order_context.get("position_side"),
                debug=args.debug_signals,
            )
        except Exception as exc:
            message = format_order_failure("OKX 开仓下单失败", order_context, exc)
            append_event({"type": "order_error", "phase": "open", "order_context": order_context, "error": f"{type(exc).__name__}: {exc}", "signal": signal})
            send_discord(message)
            raise
        order_id = str(order.get("ordId", ""))

    position = build_position(args, symbol, signal, order_id, order_size, sizing)
    if not position:
        return None

    mode = "OKX Demo 已下单" if args.place_order else "SIGNAL-ONLY"
    send_discord(
        "\n".join(
            [
                f"**市值前百策略开仓 {mode}**",
                f"{symbol} {args.interval} {position['direction'].upper()} size={order_size} sizing={sizing.get('mode')}",
                f"notional={sizing.get('notional_usdt', '-')} USDT risk_budget={sizing.get('risk_budget_usdt', '-')}",
                f"entry={position['entry_price']:.6g} stop={position['stop_loss']:.6g} target={position['target_price']:.6g}",
                f"score={position.get('signal_score')} structure={position.get('structure_score')} grade={position.get('signal_grade')}",
                f"order={order_id}",
                str(signal.get("filter", "")),
            ]
        )
    )
    append_event({"type": "open", **position, "order_context": order_context, "signal_name": signal.get("signal_name"), "filter": signal.get("filter")})
    return position


def close_position(args: argparse.Namespace, position: dict[str, Any], exit_price: float, reason: str) -> None:
    close_order_id = "signal-only"
    direction = str(position["direction"])
    order_symbol = str(position.get("inst_id") or position["symbol"])
    order_context = okx_order_context(args, str(position["symbol"]), direction, closing=True, order_symbol=order_symbol)
    order_context["size"] = str(position["size"])
    order_context["position_sizing"] = position.get("position_sizing")
    if args.place_order:
        try:
            order = place_okx_demo_order(
                order_symbol,
                str(order_context["side"]),
                str(position["size"]),
                args.okx_instrument_type,
                args.trade_mode,
                position_side=order_context.get("position_side"),
                reduce_only=True,
                debug=args.debug_signals,
            )
        except Exception as exc:
            message = format_order_failure("OKX 平仓下单失败", order_context, exc)
            append_event({"type": "order_error", "phase": "close", "order_context": order_context, "error": f"{type(exc).__name__}: {exc}", "position": position})
            send_discord(message)
            raise
        close_order_id = str(order.get("ordId", ""))

    entry_price = float(position["entry_price"])
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
    append_event({"type": "close", **position, "order_context": order_context})


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
    order_symbol_blocklist = configured_order_symbol_blocklist() if args.place_order else set()

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
        order_symbol = str(item.get("inst_id") or symbol)
        if symbol in open_symbols:
            skipped["already_open"] += 1
            continue
        if order_symbol.upper() in order_symbol_blocklist:
            skipped["order_symbol_blocklisted"] += 1
            continue
        if args.place_order and order_symbol in disabled_order_symbols(state):
            skipped["order_symbol_disabled"] += 1
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
            position = open_position(args, symbol, signal, order_symbol)
        except Exception as exc:
            error_count += 1
            message = f"{symbol} 开仓失败：{type(exc).__name__}: {exc}"
            append_event({"type": "error", "symbol": symbol, "error": message, "signal": signal})
            if is_non_retryable_order_error(exc):
                disable_order_symbol(state, order_symbol, exc)
                append_event({"type": "order_symbol_disabled", "symbol": symbol, "order_symbol": order_symbol, "error": f"{type(exc).__name__}: {exc}"})
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
            "scan_timeout_seconds": args.scan_timeout_seconds,
            "okx_instrument_type": args.okx_instrument_type,
            "trade_mode": args.trade_mode,
            "position_sizing": args.position_sizing,
            "risk_per_trade_pct": args.risk_per_trade_pct,
            "max_position_notional_usdt": args.max_position_notional_usdt,
            "place_order": args.place_order,
            "mode": mode,
            "order_symbol_blocklist": sorted(configured_order_symbol_blocklist()) if args.place_order else [],
        }
    )
    send_discord(
        f"市值前百 OKX 策略机器人启动：top={args.top_n} interval={args.interval} "
        f"max_positions={args.max_open_positions} sizing={args.position_sizing} mode={mode}"
    )

    while True:
        started_at = time.time()
        append_event({"type": "scan_started", "timeout_seconds": args.scan_timeout_seconds, "place_order": args.place_order})
        try:
            install_scan_timeout(args.scan_timeout_seconds)
            scan_once(args, state)
        except ScanTimeout as exc:
            elapsed = time.time() - started_at
            message = f"市值前百 OKX 策略机器人扫描超时：elapsed={elapsed:.1f}s timeout={args.scan_timeout_seconds}s，进程将退出并由 Docker 重启。"
            append_event({"type": "scan_timeout", "elapsed_seconds": round(elapsed, 3), "timeout_seconds": args.scan_timeout_seconds, "error": f"{type(exc).__name__}: {exc}"})
            send_discord(message)
            raise SystemExit(124) from exc
        except Exception as exc:
            elapsed = time.time() - started_at
            message = f"市值前百 OKX 策略机器人扫描异常：elapsed={elapsed:.1f}s error={type(exc).__name__}: {exc}，进程将退出并由 Docker 重启。"
            append_event(
                {
                    "type": "scan_error",
                    "elapsed_seconds": round(elapsed, 3),
                    "timeout_seconds": args.scan_timeout_seconds,
                    "error": f"{type(exc).__name__}: {exc}",
                    "place_order": args.place_order,
                }
            )
            try:
                save_state(state)
            except OSError as save_exc:
                append_event({"type": "error", "error": f"save_state_failed after scan_error: {type(save_exc).__name__}: {save_exc}"})
            send_discord(message)
            raise SystemExit(1)
        finally:
            clear_scan_timeout()
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
