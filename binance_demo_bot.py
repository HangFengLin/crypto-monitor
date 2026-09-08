#!/usr/bin/env python3
"""Run Binance USD-M Demo execution against live USD-M signal data."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import signal
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from automatic_notifications import SenderLease
from binance_demo import DemoBook, DemoClient, DemoEngine, number, order_quantity
from binance_strategy_bot import evaluate_position
from config import load_config, load_env_file
from indicators import calculate_indicators
from okx_market_cap_bot import entry_signal_filter_reason, should_skip_signal
from project_signal_backtest import add_higher_timeframe_context
from strategy import HIGHER_TREND_INTERVAL, ProjectSignalEngine

ROOT = Path(__file__).resolve().parent
VERSION_FILES = (
    "strategy.py",
    "position_manager.py",
    "indicators.py",
    "binance_strategy_bot.py",
    "binance_demo.py",
    "binance_demo_bot.py",
    "okx_market_cap_bot.py",
    "project_signal_backtest.py",
    "config.py",
)


def strategy_version(config):
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode())
    for name in VERSION_FILES:
        digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()[:16]


def latest_signal(client, engine, symbol, interval, limit):
    raw = client.bars(symbol, interval, limit)
    if len(raw) < 60:
        raise ValueError("Insufficient completed history for EMA60")
    interval_ms = raw[-1]["close_time"] - raw[-1]["open_time"] + 1
    if int(time.time() * 1000) - raw[-1]["close_time"] > interval_ms * 2:
        raise ValueError("Stale signal candles")
    for prev, current in zip(raw, raw[1:]):
        if current["open_time"] != prev["close_time"] + 1:
            raise ValueError("Candle history gap")
    bars = calculate_indicators(raw)
    higher = calculate_indicators(client.bars(symbol, HIGHER_TREND_INTERVAL, max(300, limit)))
    bars = add_higher_timeframe_context(bars, higher)
    result = engine.detect([bar for bar in bars if bar.get("macd") is not None])
    result.update(symbol=symbol, interval=interval, price=bars[-1]["close"], kline_close_time=bars[-1]["close_time"])
    return result, bars


def sync_costs(book):
    """Capture actual demo fills/funding; never label a truncated page complete."""
    for p in book.data["positions"]:
        if not p.get("entry_order") or p.get("costs_complete"):
            continue
        start = int(p["opened_at"] * 1000)
        end = int(p.get("closed_at", time.time()) * 1000)
        # Bounded windows avoid silently accepting API retention/window truncation.
        if end - start > 7 * 86400000:
            p["costs_status"] = "requires_historical_reconciliation"
            continue
        fills = book.client.request(
            "GET", "/fapi/v1/userTrades", dict(symbol=p["symbol"], startTime=start, endTime=end, limit=1000)
        )
        income = book.client.request(
            "GET",
            "/fapi/v1/income",
            dict(symbol=p["symbol"], incomeType="FUNDING_FEE", startTime=start, endTime=end, limit=1000),
        )
        p["fills"], p["funding"] = fills, income
        p["costs_status"] = "incomplete_page" if len(fills) >= 1000 or len(income) >= 1000 else "captured"
        # Raw currency-qualified fees retained; no currency conversion invented.
        fees = {}
        for fill in fills:
            asset = fill["commissionAsset"]
            fees[asset] = str(number(fees.get(asset, "0")) + number(fill["commission"]))
        p["commissions"] = fees
        p["realized_pnl"] = str(sum((number(f["realizedPnl"]) for f in fills), number(0)))
        p["costs_complete"] = p["status"] == "closed" and bool(fills) and p["costs_status"] == "captured"
    book.save()


def cycle(book, client, config, engines):
    bot, demo = config.get("bot", {}), config.get("binance_demo", {})
    engine = DemoEngine(book, strategy_version(config), config.get("app", {}).get("strategy_reward_risk", 2))
    engine.reconcile()
    interval, limit = bot.get("interval", "15m"), bot.get("kline_limit", 300)
    for p in book.data["positions"]:
        if p["status"] != "open":
            continue
        detector = engines.setdefault((p["symbol"], p["interval"]), ProjectSignalEngine())
        _, bars = latest_signal(client, detector, p["symbol"], p["interval"], limit)
        trial = copy.deepcopy(p)
        if evaluate_position(trial, bars):
            # Local bar exit price is a reference only; execute and record actual demo fill.
            p["reference_exit_price"] = trial["exit_price"]
            engine.close(p, trial["exit_reason"])
        else:
            p.update(trial)
        book.save()
    engine.reconcile()
    sync_costs(book)
    if book.data.get("fault") or not demo.get("entry_enabled", False):
        return
    if any(p["status"] not in {"open", "closed", "rejected"} for p in book.data["positions"]):
        raise RuntimeError("Unresolved lifecycle; no new entries")
    version = engine.version
    if any(p["status"] == "open" and p["strategy_version"] != version for p in book.data["positions"]):
        raise RuntimeError("Open position belongs to another strategy version")
    live = {
        s["symbol"]: s
        for s in client.public("/fapi/v1/exchangeInfo")["symbols"]
        if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
    }
    demo_symbols = {
        s["symbol"]: s
        for s in client.public("/fapi/v1/exchangeInfo", demo=True)["symbols"]
        if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
    }
    tickers = client.public("/fapi/v1/ticker/24hr")
    universe = sorted(
        (
            t
            for t in tickers
            if t["symbol"] in live
            and t["symbol"] in demo_symbols
            and number(t["quoteVolume"]) >= number(bot.get("min_quote_volume", 5000000))
        ),
        key=lambda t: number(t["quoteVolume"]),
        reverse=True,
    )[: bot.get("universe_top_n", 100)]
    book.data["universe"] = [t["symbol"] for t in universe]
    account = client.request("GET", "/fapi/v3/account")
    equity = min(number(account["availableBalance"]), number(account["totalWalletBalance"]))
    opened = {p["symbol"] for p in book.data["positions"] if p["status"] == "open"}
    max_positions = int(bot.get("max_open_positions", 5))
    for ticker in universe:
        book.data["scan_symbol"] = ticker["symbol"]
        book.save()
        if len(opened) >= max_positions:
            break
        symbol = ticker["symbol"]
        if symbol in opened:
            continue
        detector = engines.setdefault((symbol, interval), ProjectSignalEngine())
        try:
            sig, _ = latest_signal(client, detector, symbol, interval, limit)
            if sig.get("signal") not in {"long", "short"}:
                continue
            if entry_signal_filter_reason(SimpleNamespace(**bot), sig) or should_skip_signal(
                sig, bot.get("min_signal_score", 0), bot.get("min_structure_score", 0)
            ):
                continue
            demo_price = number(client.public("/fapi/v1/premiumIndex", {"symbol": symbol}, demo=True)["markPrice"])
            reference = number(sig["price"])
            if abs(demo_price / reference - 1) * 10000 > number(demo.get("max_price_deviation_bps", 100)):
                raise ValueError("Demo/live price divergence exceeds limit")
            # Size at the less favorable of reference and demo mark prices.
            qty = order_quantity(
                equity,
                reference,
                sig["stop_loss"],
                demo_symbols[symbol],
                bot.get("risk_per_trade_pct", 0.005),
                bot.get("max_position_notional_usdt", 50),
                execution_price=demo_price,
            )
            tick = next(f["tickSize"] for f in demo_symbols[symbol]["filters"] if f["filterType"] == "PRICE_FILTER")
        except (ValueError, RuntimeError, OSError) as exc:
            book.event("signal_skipped", symbol=symbol, reason=str(exc), strategy_version=version)
            continue
        p = engine.enter(sig, qty, tick)
        if p["status"] == "open":
            opened.add(symbol)
        # Reconcile before admitting another entry.
        engine.reconcile()
    book.save()


def main():
    print(
        json.dumps(
            {
                "status": "extension_disabled",
                "mode": "paper",
                "message": "当前使用原网站本地模拟账本；交易所执行适配器暂不启用，无需 API Key",
            },
            ensure_ascii=False,
        )
    )
    return 0


def _archived_execution_main():
    parser = argparse.ArgumentParser(description="Binance USDT 合约模拟盘；没有实盘模式")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="检查配置，不联网、不下单")
    modes.add_argument("--probe", action="store_true", help="仅检查实盘公共行情与 Demo 公共接口")
    modes.add_argument("--run", action="store_true", help="启动官方模拟盘执行")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    load_env_file()
    config = load_config()
    key, secret = os.getenv("BINANCE_DEMO_API_KEY", ""), os.getenv("BINANCE_DEMO_SECRET_KEY", "")
    client = DemoClient(key, secret)
    status = dict(
        execution_mode="binance_demo",
        market_data_source="binance_usdm_live",
        strategy_version=strategy_version(config),
        credentials_configured=bool(key and secret),
        status="ready_for_connection_check" if key and secret else "credentials_missing",
    )
    if args.probe:
        for name, demo in (("live_market", False), ("demo", True)):
            try:
                response = client.public("/fapi/v1/exchangeInfo", demo=demo)
                status[name] = {"ok": True, "symbols": len(response["symbols"])}
            except (RuntimeError, OSError) as exc:
                status[name] = {"ok": False, "error": str(exc)}
        print(json.dumps(status, ensure_ascii=False))
        return 0 if all(status[n]["ok"] for n in ("live_market", "demo")) else 2
    if not args.run or not key or not secret:
        print(json.dumps(status, ensure_ascii=False))
        return 0 if key and secret else 2
    directory = Path(os.getenv("BINANCE_DEMO_DIR", str(ROOT / "runtime" / "binance-demo")))
    lease = SenderLease(directory / "state.json")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        book = DemoBook(directory / "state.json", client)
        if any(
            p["status"] not in {"closed", "rejected"} and p["strategy_version"] != status["strategy_version"]
            for p in book.data["positions"]
        ):
            raise RuntimeError("Restore the strategy version that owns open positions before running")
        versions = directory / "versions" / status["strategy_version"]
        versions.mkdir(parents=True, exist_ok=True)
        (versions / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2))
        for name in VERSION_FILES:
            (versions / name).write_bytes((ROOT / name).read_bytes())
        engines = {}
        while not stop.is_set():
            try:
                book.data["scan_in_progress"] = True
                book.save()
                client.sync_time()
                cycle(book, client, config, engines)
                book.data["last_scan_at"] = time.time()
                book.data["scan_error"] = None
            except (RuntimeError, ValueError, OSError, KeyError) as exc:
                book.data["scan_error"] = f"{type(exc).__name__}: {exc}"
            book.data["scan_in_progress"] = False
            book.save()
            print(
                json.dumps(
                    {
                        "mode": "binance_demo",
                        "scan_error": book.data.get("scan_error"),
                        "fault": book.data.get("fault"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if args.once:
                break
            stop.wait(max(30, config.get("bot", {}).get("poll_seconds", 60)))
        return 2 if book.data.get("scan_error") or book.data.get("fault") else 0
    finally:
        lease.close()


if __name__ == "__main__":
    raise SystemExit(main())
