from __future__ import annotations

import asyncio
import http.client
import json
import math
import os
import smtplib
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import feishu_notifications
from api_payloads import PayloadDetail, project_paper_trades, project_state
from automatic_notifications import MODULE_FAULT_LABELS, Outbox, SenderLease
from config import ENV_FILE, config_value, load_config, load_env_file
from data_client import (
    async_fetch_binance_usdm_point_quote,
    async_fetch_funding_rate,
    async_fetch_klines,
    async_fetch_tickers,
    close_http_session,
    fetch_binance_spot_symbols,
    fetch_funding_rate,
    fetch_klines,
    fetch_open_interest_ratio,
    market_data_source,
    recoverable_http_errors,
    test_external_dependencies,
)
from indicators import atr, calculate_indicators, macd, rolling_average
from jindan_web import register_jindan_routes
from monitor_faults import FaultTracker
from observation_support import advance_support
from paper_universe import PaperUniverse
from position_manager import (
    LEGACY_RATIO_V1,
    LINEAR_USDM_V1,
    STRUCTURE_ATR_STOP_MODE,
    calculate_return_pct,
    calculate_target_levels,
    classify_return_outcome,
    evaluate_lifecycle_bar,
)
from product_settings import ENGINE_VERSION, SettingsConflict, SettingsStore, execution_settings
from research_archive import ResearchArchive
from research_jobs import BoundedResearchJobs, ResearchBusy
from runtime_utils import append_jsonl, post_discord
from runtime_utils import code_fingerprint as build_code_fingerprint
from signal_engine_state import PersistentSignalEngine
from strategy import (
    DEFAULT_CONFIG,
    HIGHER_TREND_INTERVAL,
    ProjectSignalEngine,
    StrategyConfig,
    build_chan_structure_context,
    check_buy_filter,
    check_sell_filter,
    confirmation_passed,
    confirmation_wait_text,
    cross_direction,
    detect_bearish_divergence,
    detect_bullish_divergence,
    find_local_extremes,
    grade_signal,
    latest_ma_direction,
    latest_macd_direction,
    score_signal,
)
from strategy_universe import fetch_binance_paper_universe

try:
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles

    from http_transport import WebsiteGZipMiddleware
except ModuleNotFoundError:
    uvicorn = None
    FastAPI = None
    HTTPException = None
    Request = None
    CORSMiddleware = None
    StreamingResponse = None
    StaticFiles = None


automatic_notification_status = {"enabled": False, "running": False, "pending": 0, "error": None}
NOTIFICATION_OBSERVATION_INTERVAL_S = 0.5
NOTIFICATION_MIN_SEND_INTERVAL_S = 0.7


def automatic_notification_inputs():
    with state_lock:
        trades = [dict(trade) for trade in state.strategy_trades]
        events = [dict(event) for event in reversed(state.events)
                  if event.get("type") in {"indicator", "support"} and event.get("event_key")
                  and not event.get("observation_round_id")]
        faults = [state.published_fault] if state.published_fault else []
        paper_fault = dict(state.paper_data_fault)
    last_observed = paper_fault.get("last_observed_at")
    if (paper_fault.get("armed") and isinstance(last_observed, (int, float))
            and time.time() - last_observed > 30):
        # Notification ticks cannot prove recovery while monitor rounds stall.
        faults.append(MODULE_FAULT_LABELS["paper_data"])
    monitor = site_monitor_snapshot()
    if monitor.get("enabled") and (
        monitor.get("alert_active") or any(result.get("alert_active") for result in monitor.get("results", []))
    ):
        faults.append("站点巡检")
    alerts = []
    for event in events:
        title, body = format_alert_text(event)
        alerts.append({"key": event["event_key"], "title": title, "body": body,
                       "created_at": event.get("created_at")})
    return trades, list_report_files(), "、".join(faults) or None, alerts, EVENT_LOG_FILE


async def automatic_notification_loop(stop_event, box):
    while not stop_event.is_set():
        delay = NOTIFICATION_OBSERVATION_INTERVAL_S
        try:

            def scan():
                box.observe(*automatic_notification_inputs())
                settings = RUNTIME_SETTINGS.get()["values"]["notifications"]
                enabled = settings["enabled"] and not env_bool("NOTIFICATIONS_FORCE_OFF", False)
                automatic_notification_status["enabled"] = enabled
                allowed = {key for key in feishu_notifications.CHANNELS if settings[key]} if enabled else set()
                batch = {"attempted": 0, "accepted": 0, "failed": 0, "stopped": False}
                if enabled:
                    batch = box.deliver(
                        feishu_notifications.send, allowed, should_stop=stop_event.is_set,
                    )
                return batch, box.next_due_delay(allowed), allowed

            batch, next_due, allowed = await asyncio.to_thread(scan)
            if next_due is not None:
                # Give the loop a cancellation point between bounded batches;
                # a ready backlog must not incur the former five-second pause.
                delay = min(delay, max(0.01, next_due))
            automatic_notification_status.update(
                running=True,
                pending=len(box.data["pending"]),
                error=box.data.get("observation_error"),
                last_checked_at=time.time(),
                last_delivery=box.data.get("last_delivery"),
                delivery_timing=box.delivery_timing(allowed),
                scheduler={
                    "observation_interval_s": NOTIFICATION_OBSERVATION_INTERVAL_S,
                    "min_send_interval_s": box.min_send_interval_s,
                    "next_scan_delay_s": delay,
                    "last_batch_attempted": batch["attempted"],
                },
            )
        except Exception as exc:
            automatic_notification_status.update(running=False, error=f"通知任务异常（{type(exc).__name__}）")
            delay = 5
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            pass


ROOT = Path(__file__).resolve().parent
CONFIG = load_config()


def config_int(section: str, key: str, default: int, env_name: str | None = None) -> int:
    fallback: Any = os.getenv(env_name, default) if env_name else default
    value = config_value(CONFIG, section, key, fallback)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def config_string_tuple(section: str, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = config_value(CONFIG, section, key, list(default))
    if not isinstance(value, (list, tuple)):
        return default
    normalized = tuple(str(item).strip() for item in value if str(item).strip())
    return normalized or default


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


PUBLIC_DIR = ROOT / "public"
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", str(ROOT / "reports"))).expanduser()
STATE_FILE = Path(os.getenv("WATCHLIST_FILE", str(ROOT / "watchlist.json"))).expanduser()
EVENT_LOG_FILE = Path(os.getenv("EVENT_LOG_FILE", str(ROOT / "signal_events.jsonl"))).expanduser()
STRATEGY_TRADES_FILE = Path(os.getenv("STRATEGY_TRADES_FILE", str(ROOT / "strategy_trades.json"))).expanduser()
OKX_DEMO_LEDGER_FILE = Path(
    os.getenv("OKX_DEMO_LEDGER_FILE", str(ROOT / "runtime" / "okx_market_cap_bot_state.json"))
).expanduser()
STARTED_AT = time.time()
EVENT_HISTORY_LIMIT = 500
DEFAULT_SIGNAL_INTERVAL = str(config_value(CONFIG, "app", "default_signal_interval", "15m"))
HOURLY_SUMMARY_SYMBOLS = config_string_tuple("app", "hourly_summary_symbols", ("BTCUSDT", "ETHUSDT"))
HOURLY_SUMMARY_INTERVAL_SECONDS = int(config_value(CONFIG, "app", "hourly_summary_interval_seconds", 3600))
INDICATOR_INTERVALS = config_string_tuple("app", "indicator_intervals", ("15m", "1h", "4h", "1d"))
SUPPORT_INTERVALS = config_string_tuple("app", "support_intervals", ("15m", "1h", "4h", "1d"))
SUPPORT_TOUCH_TOLERANCE = {
    str(interval): float(tolerance)
    for interval, tolerance in dict(
        config_value(CONFIG, "app", "support_touch_tolerance", {"15m": 0.003, "1h": 0.004, "4h": 0.006, "1d": 0.008})
    ).items()
}
MA_FAST_PERIOD = int(config_value(CONFIG, "strategy", "ma_fast_period", 5))
MA_SLOW_PERIOD = int(config_value(CONFIG, "strategy", "ma_slow_period", 10))
KLINE_LIMIT = int(config_value(CONFIG, "app", "kline_limit", 1000))
TD_KLINE_LIMIT = int(config_value(CONFIG, "app", "td_kline_limit", 80))
MIN_DIVERGENCE_STRENGTH = float(config_value(CONFIG, "strategy", "min_divergence_strength", 0.25))
BUY_RSI_THRESHOLD = float(config_value(CONFIG, "strategy", "buy_rsi_threshold", 40))
BUY_VOLUME_RATIO = float(config_value(CONFIG, "strategy", "buy_volume_ratio", 0.8))
SELL_RSI_THRESHOLD = float(config_value(CONFIG, "strategy", "sell_rsi_threshold", 60))
SELL_VOLUME_RATIO = float(config_value(CONFIG, "strategy", "sell_volume_ratio", 0.8))
CONFIRM_MAX_BARS = int(config_value(CONFIG, "strategy", "confirm_max_bars", 12))
ATR_STOP_MULTIPLIER = float(config_value(CONFIG, "strategy", "atr_stop_multiplier", 1.5))
SELL_TREND_BREAKER_INTERVAL = str(config_value(CONFIG, "app", "sell_trend_breaker_interval", "4h"))
SELL_TREND_BREAKER_LIMIT = int(config_value(CONFIG, "app", "sell_trend_breaker_limit", 60))
MAX_WORKERS = int(config_value(CONFIG, "app", "max_workers", 12))
STRATEGY_REWARD_RISK = float(config_value(CONFIG, "app", "strategy_reward_risk", 2.0))
STRATEGY_FEE_RATE = float(config_value(CONFIG, "app", "strategy_fee_rate", 0.001))
STRATEGY_STOP_MODE = str(config_value(CONFIG, "app", "strategy_stop_mode", STRUCTURE_ATR_STOP_MODE))
STRATEGY_HISTORY_LIMIT = int(config_value(CONFIG, "app", "strategy_history_limit", 200))
RECORD_STRATEGY_TRADES = bool(config_value(CONFIG, "app", "record_strategy_trades", True))
PAPER_UNIVERSE_ENABLED = env_bool("PAPER_UNIVERSE_ENABLED", bool(config_value(CONFIG, "paper_universe", "enabled", True)))
PAPER_UNIVERSE_INTERVAL = str(config_value(CONFIG, "paper_universe", "interval", "15m"))
PAPER_UNIVERSE_WORKERS = max(1, min(4, int(config_value(CONFIG, "paper_universe", "workers", 2))))
PAPER_UNIVERSE = PaperUniverse(
    os.getenv("PAPER_UNIVERSE_FILE", str(STRATEGY_TRADES_FILE.parent / "paper-universe.json")),
    top_n=int(config_value(CONFIG, "paper_universe", "top_n", 100)),
    selection=str(config_value(CONFIG, "paper_universe", "selection", "exchange_top_n")),
    refresh_seconds=int(config_value(CONFIG, "paper_universe", "refresh_seconds", 21600)),
)
REPORT_FILE_SUFFIXES = {".html", ".htm"}
SITE_MONITOR_DEFAULT_PORT = os.getenv("PORT", "8080").strip() or "8080"
SITE_MONITOR_DEFAULT_TARGETS = (
    f"health=http://127.0.0.1:{SITE_MONITOR_DEFAULT_PORT}/api/health,"
    f"reports_api=http://127.0.0.1:{SITE_MONITOR_DEFAULT_PORT}/api/reports,"
    f"reports_page=http://127.0.0.1:{SITE_MONITOR_DEFAULT_PORT}/reports.html"
)
SITE_MONITOR_LAST_RESULTS_LIMIT = 20


DEFAULT_WATCHLIST = [
    {
        "symbol": "BTCUSDT",
        "email": "",
        "signal": True,
        "indicator_alert": True,
        "support_alert": True,
        "interval": "15m",
    },
    {
        "symbol": "ETHUSDT",
        "email": "",
        "signal": True,
        "indicator_alert": True,
        "support_alert": True,
        "interval": "15m",
    },
]


@dataclass
class MonitorState:
    watchlist: list[dict[str, Any]] = field(default_factory=list)
    prices: dict[str, dict[str, Any]] = field(default_factory=dict)
    signals: dict[str, dict[str, Any]] = field(default_factory=dict)
    paper_pool_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    paper_pool_scan: dict[str, Any] = field(default_factory=dict)
    indicator_signals: dict[str, dict[str, Any]] = field(default_factory=dict)
    support_levels: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    strategy_trades: list[dict[str, Any]] = field(default_factory=list)
    strategy_stats: dict[str, Any] = field(default_factory=dict)
    pending_buy_divergences: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_sell_divergences: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_error: str | None = None
    signal_error: str | None = None
    module_errors: dict[str, str | None] = field(default_factory=dict)
    paper_data_details: list[dict[str, Any]] = field(default_factory=list)
    paper_data_fault: dict[str, Any] = field(default_factory=dict)
    published_fault: str | None = None
    summary_error: str | None = None
    discord_last_ok_at: float | None = None
    discord_last_error: str | None = None
    updated_at: float | None = None
    last_summary_at: float | None = None
    sent_alerts: set[str] = field(default_factory=set)
    support_alert_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    indicator_alert_keys: dict[str, str] = field(default_factory=dict)
    observation_round: dict[str, Any] | None = None
    observation_pending_rounds: list[dict[str, Any]] = field(default_factory=list)
    observation_saved_payload: str | None = None
    observation_checkpoint_ok: bool = True
    site_monitor: dict[str, Any] = field(default_factory=dict)


state = MonitorState()
state_lock = threading.Lock()
SIGNAL_ENGINES: dict[tuple[str, ...], ProjectSignalEngine] = {}
_signal_engines_lock = threading.Lock()
PAPER_QUOTE_MAX_AGE_MS = 30_000
FAULT_TRACKER: FaultTracker | None = None
RESEARCH_ARCHIVE = ResearchArchive(
    Path(os.getenv("RESEARCH_RUNS_DIR", str(STRATEGY_TRADES_FILE.parent / "research-runs")))
)
RUNTIME_SETTINGS = SettingsStore(
    Path(os.getenv("RUNTIME_SETTINGS_FILE", str(STRATEGY_TRADES_FILE.parent / "runtime-settings.json")))
)
_site_monitor_last_run_at: float | None = None
_site_monitor_alert_state: dict[str, dict[str, Any]] = {}


def discord_webhook_url() -> str:
    if not env_bool("DISCORD_ENABLED", True):
        return ""
    load_env_file()
    return os.getenv("DISCORD_WEBHOOK_URL", "").strip()


def env_file_has_key(key: str) -> bool:
    if not ENV_FILE.exists():
        return False
    try:
        for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            env_key = line.split("=", 1)[0].strip()
            if env_key.startswith("export "):
                env_key = env_key.removeprefix("export ").strip()
            if env_key == key:
                return True
    except OSError:
        return False
    return False


def code_fingerprint() -> str:
    return build_code_fingerprint(
        ROOT,
        (
            "app.py",
            "config.py",
            "docker-compose.yml",
            "binance_strategy_bot.py",
            "position_manager.py",
            "runtime_utils.py",
            "monitor_faults.py",
            "paper_universe.py",
            "strategy_universe.py",
        ),
    )


def list_report_files() -> list[dict[str, Any]]:
    """Return report files served from REPORTS_DIR, newest first."""
    if not REPORTS_DIR.exists():
        return []

    reports = []
    for path in REPORTS_DIR.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in REPORT_FILE_SUFFIXES:
            continue
        try:
            stat = path.stat()
            relative_path = path.relative_to(REPORTS_DIR).as_posix()
        except (OSError, ValueError):
            continue
        reports.append(
            {
                "name": path.name,
                "path": relative_path,
                "url": f"/reports/{relative_path}",
                "size_bytes": stat.st_size,
                "updated_at": stat.st_mtime,
            }
        )
    return sorted(reports, key=lambda item: item["updated_at"], reverse=True)


def site_monitor_enabled() -> bool:
    return env_bool("SITE_MONITOR_ENABLED", True)


def site_monitor_interval_seconds() -> int:
    return max(10, parse_int(os.getenv("SITE_MONITOR_INTERVAL_SECONDS", "60"), 60))


def site_monitor_failure_threshold() -> int:
    return max(1, parse_int(os.getenv("SITE_MONITOR_FAILURE_THRESHOLD", "3"), 3))


def site_monitor_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("SITE_MONITOR_TIMEOUT_SECONDS", "8")))
    except (TypeError, ValueError):
        return 8.0


def site_monitor_min_report_count() -> int:
    return max(0, parse_int(os.getenv("SITE_MONITOR_MIN_REPORT_COUNT", "0"), 0))


def site_monitor_report_max_age_seconds() -> int:
    return max(0, parse_int(os.getenv("SITE_MONITOR_REPORT_MAX_AGE_SECONDS", "0"), 0))


def parse_site_monitor_targets(raw_targets: str | None = None) -> list[dict[str, str]]:
    raw_targets = SITE_MONITOR_DEFAULT_TARGETS if raw_targets is None else raw_targets
    targets = []
    for index, raw_item in enumerate(raw_targets.replace("\n", ",").split(","), start=1):
        item = raw_item.strip()
        if not item:
            continue
        name = f"target_{index}"
        url = item
        if "=" in item and "://" not in item.split("=", 1)[0]:
            raw_name, raw_url = item.split("=", 1)
            name = raw_name.strip() or name
            url = raw_url.strip()
        if not urlparse(url).scheme:
            continue
        targets.append({"id": f"http:{name}", "name": name, "url": url})
    return targets


def configured_site_monitor_targets() -> list[dict[str, str]]:
    return parse_site_monitor_targets(os.getenv("SITE_MONITOR_TARGETS", SITE_MONITOR_DEFAULT_TARGETS))


def tls_days_remaining(url: str, timeout: float) -> float | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    port = parsed.port or 443
    context = ssl.create_default_context()
    with socket.create_connection((parsed.hostname, port), timeout=timeout) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=parsed.hostname) as tls_socket:
            cert = tls_socket.getpeercert()
    not_after = cert.get("notAfter")
    if not isinstance(not_after, str):
        return None
    return round((ssl.cert_time_to_seconds(not_after) - time.time()) / 86400, 2)


def probe_site_monitor_target(target: dict[str, str], timeout: float | None = None) -> dict[str, Any]:
    started_at = time.time()
    timeout = site_monitor_timeout_seconds() if timeout is None else timeout
    request = urllib.request.Request(
        target["url"],
        headers={"User-Agent": "crypto-monitor-site-probe/1.0"},
        method="GET",
    )
    result = {
        "id": target["id"],
        "name": target["name"],
        "url": target["url"],
        "type": "http",
        "ok": False,
        "checked_at": started_at,
        "duration_ms": None,
        "status_code": None,
        "error": None,
        "tls_days_remaining": None,
    }
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(65536)
            status_code = int(getattr(response, "status", response.getcode()))
            result["status_code"] = status_code
            result["duration_ms"] = round((time.time() - started_at) * 1000, 2)
            try:
                result["tls_days_remaining"] = tls_days_remaining(target["url"], timeout)
            except (OSError, ssl.SSLError, ValueError):
                result["tls_days_remaining"] = None
            result["ok"] = 200 <= status_code < 400
            content_type = response.headers.get("Content-Type", "")
            if result["ok"] and ("application/json" in content_type or body.lstrip().startswith(b"{")):
                try:
                    payload = json.loads(body.decode("utf-8"))
                    if isinstance(payload, dict):
                        result["payload_ok"] = payload.get("ok")
                        if payload.get("ok") is False:
                            result["ok"] = False
                            result["error"] = "json payload ok=false"
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
    except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        result["duration_ms"] = round((time.time() - started_at) * 1000, 2)
        result["error"] = str(exc)
    return result


def build_site_monitor_runtime_checks() -> list[dict[str, Any]]:
    now = time.time()
    checks: list[dict[str, Any]] = []
    min_reports = site_monitor_min_report_count()
    max_report_age = site_monitor_report_max_age_seconds()

    if min_reports > 0 or max_report_age > 0:
        reports = list_report_files()
        latest_report = reports[0] if reports else None
        latest_age = round(now - latest_report["updated_at"], 3) if latest_report else None
        ok = len(reports) >= min_reports
        error = None
        if not ok:
            error = f"report_count {len(reports)} < {min_reports}"
        if ok and max_report_age > 0 and latest_age is not None and latest_age > max_report_age:
            ok = False
            error = f"latest report age {int(latest_age)}s > {max_report_age}s"
        checks.append(
            {
                "id": "runtime:reports",
                "name": "reports_runtime",
                "type": "runtime",
                "ok": ok,
                "checked_at": now,
                "report_count": len(reports),
                "latest_report": latest_report,
                "latest_report_age_seconds": latest_age,
                "error": error,
            }
        )

    return checks


def apply_site_monitor_alert_state(results: list[dict[str, Any]], now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    threshold = site_monitor_failure_threshold()
    notifications: list[dict[str, Any]] = []
    current_ids = {str(result["id"]) for result in results}

    for result in results:
        check_id = str(result["id"])
        check_state = _site_monitor_alert_state.setdefault(
            check_id,
            {"consecutive_failures": 0, "alert_active": False, "last_status_change_at": None},
        )
        if result.get("ok"):
            was_active = bool(check_state.get("alert_active"))
            previous_failures = int(check_state.get("consecutive_failures") or 0)
            check_state["consecutive_failures"] = 0
            if was_active:
                check_state["alert_active"] = False
                check_state["last_status_change_at"] = now
                notifications.append({"status": "recovered", "result": result, "previous_failures": previous_failures})
        else:
            failures = int(check_state.get("consecutive_failures") or 0) + 1
            check_state["consecutive_failures"] = failures
            if failures >= threshold and not check_state.get("alert_active"):
                check_state["alert_active"] = True
                check_state["last_status_change_at"] = now
                notifications.append({"status": "firing", "result": result, "previous_failures": failures})

        result["consecutive_failures"] = int(check_state.get("consecutive_failures") or 0)
        result["alert_active"] = bool(check_state.get("alert_active"))

    for check_id in list(_site_monitor_alert_state):
        if check_id not in current_ids:
            _site_monitor_alert_state.pop(check_id, None)

    return notifications


def site_monitor_snapshot() -> dict[str, Any]:
    with state_lock:
        monitor = dict(state.site_monitor)
    monitor.setdefault("enabled", site_monitor_enabled())
    monitor.setdefault("ok", True)
    monitor.setdefault("results", [])
    monitor.setdefault("last_run_at", _site_monitor_last_run_at)
    return monitor


async def maybe_run_site_monitor_async() -> None:
    global _site_monitor_last_run_at
    now = time.time()
    if not site_monitor_enabled():
        with state_lock:
            state.site_monitor = {
                "enabled": False,
                "ok": True,
                "last_run_at": now,
                "results": [],
                "targets": [],
                "failure_threshold": site_monitor_failure_threshold(),
            }
        return
    if _site_monitor_last_run_at and now - _site_monitor_last_run_at < site_monitor_interval_seconds():
        return

    targets = configured_site_monitor_targets()
    http_results = await asyncio.gather(*(asyncio.to_thread(probe_site_monitor_target, target) for target in targets))
    results = [*http_results, *build_site_monitor_runtime_checks()]
    notifications = apply_site_monitor_alert_state(results, now)
    ok = all(result.get("ok") for result in results)
    _site_monitor_last_run_at = now
    with state_lock:
        state.site_monitor = {
            "enabled": True,
            "ok": ok,
            "last_run_at": now,
            "results": results[-SITE_MONITOR_LAST_RESULTS_LIMIT:],
            "targets": targets,
            "failure_threshold": site_monitor_failure_threshold(),
            "interval_seconds": site_monitor_interval_seconds(),
            "task_failures": 0,
            "alert_active": any(result.get("alert_active") for result in results),
        }

    for notification in notifications:
        if notification["status"] == "recovered" and not env_bool("SITE_MONITOR_RECOVERY_NOTIFY", True):
            continue
        event = {
            "type": "site_monitor",
            "status": notification["status"],
            "check": notification["result"].get("name"),
            "url": notification["result"].get("url"),
            "error": notification["result"].get("error"),
            "status_code": notification["result"].get("status_code"),
            "consecutive_failures": notification["result"].get("consecutive_failures"),
            "created_at": now,
        }
        record_event(event, f"site_monitor:{notification['status']}:{notification['result'].get('id')}:{int(now)}")
        await send_alert_notifications_async(event)


def pending_signal_key(symbol: str, interval: str) -> str:
    return f"{symbol}:{interval}"


def set_pending_buy_divergence(symbol: str, interval: str, candidate: dict[str, Any]) -> None:
    with state_lock:
        state.pending_buy_divergences[pending_signal_key(symbol, interval)] = candidate


def get_pending_buy_divergence(symbol: str, interval: str) -> dict[str, Any] | None:
    with state_lock:
        candidate = state.pending_buy_divergences.get(pending_signal_key(symbol, interval))
        return dict(candidate) if candidate else None


def clear_pending_buy_divergence(symbol: str, interval: str) -> None:
    with state_lock:
        state.pending_buy_divergences.pop(pending_signal_key(symbol, interval), None)


def set_pending_sell_divergence(symbol: str, interval: str, candidate: dict[str, Any]) -> None:
    with state_lock:
        state.pending_sell_divergences[pending_signal_key(symbol, interval)] = candidate


def get_pending_sell_divergence(symbol: str, interval: str) -> dict[str, Any] | None:
    with state_lock:
        candidate = state.pending_sell_divergences.get(pending_signal_key(symbol, interval))
        return dict(candidate) if candidate else None


def clear_pending_sell_divergence(symbol: str, interval: str) -> None:
    with state_lock:
        state.pending_sell_divergences.pop(pending_signal_key(symbol, interval), None)


def load_watchlist() -> list[dict[str, Any]]:
    if not STATE_FILE.exists():
        save_watchlist(DEFAULT_WATCHLIST)
        return DEFAULT_WATCHLIST

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return normalize_watchlist(data)
    except (OSError, json.JSONDecodeError):
        return DEFAULT_WATCHLIST


def save_watchlist(watchlist: list[dict[str, Any]]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(STATE_FILE, json.dumps(normalize_watchlist(watchlist), ensure_ascii=False, indent=2))


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tmp_path.open("w", encoding=encoding) as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def load_event_history(limit: int = 50) -> list[dict[str, Any]]:
    if not EVENT_LOG_FILE.exists():
        return []

    events: list[dict[str, Any]] = []
    try:
        for line in EVENT_LOG_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
    except (OSError, json.JSONDecodeError):
        return []
    return list(reversed(events[-limit:]))


def load_all_event_history() -> list[dict[str, Any]]:
    if not EVENT_LOG_FILE.exists():
        return []

    events: list[dict[str, Any]] = []
    try:
        for line in EVENT_LOG_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
    except (OSError, json.JSONDecodeError):
        return []
    return events


def load_strategy_trades() -> list[dict[str, Any]]:
    if not STRATEGY_TRADES_FILE.exists():
        return []

    try:
        data = json.loads(STRATEGY_TRADES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("策略交易账本不可读取或已损坏，停止采用空账本") from error
    if isinstance(data, dict):
        trades = data.get("trades")
    else:
        trades = data
    if not isinstance(trades, list) or any(not isinstance(trade, dict) for trade in trades):
        raise ValueError("策略交易账本结构无效，停止采用空账本")
    return trades


def okx_demo_ledger_candidates() -> list[Path]:
    candidates = [OKX_DEMO_LEDGER_FILE]
    legacy_file = ROOT / "okx_market_cap_bot_state.json"
    if legacy_file not in candidates:
        candidates.append(legacy_file)
    return candidates


def okx_demo_outcome(position: dict[str, Any]) -> str:
    if position.get("status") == "open":
        return "open"
    return_pct = parse_float(position.get("return_pct"))
    if return_pct is None:
        return "unknown"
    if return_pct > 0:
        return "win"
    if return_pct < 0:
        return "loss"
    return "breakeven"


def okx_demo_notional_usdt(position: dict[str, Any]) -> float | None:
    sizing = position.get("position_sizing")
    if isinstance(sizing, dict):
        notional = parse_float(sizing.get("notional_usdt"))
        if notional is not None:
            return notional
    entry_price = parse_float(position.get("entry_price"))
    size = parse_float(position.get("size"))
    if entry_price is None or size is None:
        return None
    ct_val = parse_float(sizing.get("ctVal")) if isinstance(sizing, dict) else None
    multiplier = ct_val if ct_val is not None else 1.0
    return abs(entry_price * size * multiplier)


def normalize_okx_demo_position(position: dict[str, Any]) -> dict[str, Any]:
    trade = dict(position)
    trade["mode"] = "okx_demo"
    trade["outcome"] = okx_demo_outcome(position)
    notional_usdt = okx_demo_notional_usdt(position)
    return_pct = parse_float(position.get("return_pct"))
    trade["notional_usdt"] = notional_usdt
    trade["pnl_usdt"] = notional_usdt * return_pct if notional_usdt is not None and return_pct is not None else None
    trade["pnl_usdt_estimated"] = trade["pnl_usdt"] is not None
    return trade


def calculate_okx_demo_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [trade for trade in trades if trade.get("status") == "closed"]
    open_trades = [trade for trade in trades if trade.get("status") == "open"]
    returns = [parse_float(trade.get("return_pct")) or 0.0 for trade in closed]
    pnls = [parse_float(trade.get("pnl_usdt")) or 0.0 for trade in closed]
    wins = [trade for trade in closed if trade.get("outcome") == "win"]
    losses = [trade for trade in closed if trade.get("outcome") == "loss"]
    breakevens = [trade for trade in closed if trade.get("outcome") == "breakeven"]
    return {
        "total_trades": len(trades),
        "open_trades": len(open_trades),
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "breakevens": len(breakevens),
        "win_rate": len(wins) / len(closed) if closed else 0.0,
        "expectancy": sum(returns) / len(returns) if returns else 0.0,
        "total_return": sum(returns),
        "realized_pnl_usdt": sum(pnls),
    }


def load_okx_demo_ledger() -> dict[str, Any]:
    source = next((path for path in okx_demo_ledger_candidates() if path.exists()), OKX_DEMO_LEDGER_FILE)
    if not source.exists():
        return {
            "mode": "okx_demo",
            "source_exists": False,
            "source_updated_at": None,
            "trades": [],
            "stats": calculate_okx_demo_stats([]),
            "retention_note": "未找到 OKX Demo 历史账本；不会创建或清理数据",
        }
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "mode": "okx_demo",
            "source_exists": True,
            "source_updated_at": source.stat().st_mtime if source.exists() else None,
            "trades": [],
            "stats": calculate_okx_demo_stats([]),
            "error": "OKX Demo 历史账本读取失败",
            "retention_note": "保留原始账本；请先修复 JSON 文件后再展示",
        }
    positions = data.get("positions", []) if isinstance(data, dict) else []
    trades = [normalize_okx_demo_position(position) for position in positions if isinstance(position, dict)]
    trades.sort(key=lambda trade: parse_float(trade.get("closed_at")) or parse_float(trade.get("opened_at")) or 0.0, reverse=True)
    return {
        "mode": "okx_demo",
        "source_exists": True,
        "source_updated_at": source.stat().st_mtime,
        "trades": trades,
        "stats": calculate_okx_demo_stats(trades),
        "retention_note": "展示保留的 OKX Demo 历史开平仓记录；USDT 盈亏按记录名义本金估算，不代表账户净值",
    }


def retain_strategy_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Limit completed history without losing an active observation."""
    closed = [trade for trade in trades if trade.get("status") != "open"]
    retained = {id(trade) for trade in closed[-STRATEGY_HISTORY_LIMIT:]}
    return [trade for trade in trades if trade.get("status") == "open" or id(trade) in retained]


def save_strategy_trades(trades: list[dict[str, Any]]) -> None:
    payload = {
        "updated_at": time.time(),
        "trades": retain_strategy_trades(trades),
    }
    try:
        atomic_write_text(STRATEGY_TRADES_FILE, json.dumps(payload, ensure_ascii=False, indent=2))
        with state_lock:
            state.module_errors["ledger"] = None
    except OSError:
        with state_lock:
            state.module_errors["ledger"] = "策略交易账本写入失败"


def append_event_to_disk(event: dict[str, Any]) -> bool:
    try:
        append_jsonl(EVENT_LOG_FILE, event)
        with state_lock:
            state.module_errors["events"] = None
        return True
    except OSError:
        with state_lock:
            state.module_errors["events"] = "事件日志写入失败"
        return False


def record_event(event: dict[str, Any], event_key: str | None = None) -> None:
    stored_event = dict(event)
    stored_event.setdefault("created_at", time.time())
    if event_key:
        stored_event["event_key"] = event_key

    with state_lock:
        if stored_event.get("type") in {"indicator", "support"} and state.observation_round is not None:
            stored_event["observation_round_id"] = state.observation_round["id"]
            state.observation_round["events"].append(dict(stored_event))
        if event_key:
            state.sent_alerts.add(event_key)
        state.events.insert(0, stored_event)
        state.events = state.events[:50]

    append_event_to_disk(stored_event)


def observation_checkpoint_path() -> Path:
    return EVENT_LOG_FILE.with_name(EVENT_LOG_FILE.name + ".observations.json")


def save_observation_checkpoint() -> None:
    with state_lock:
        if not state.observation_checkpoint_ok:
            return
        if (not state.support_alert_states and not state.indicator_alert_keys
                and not state.observation_pending_rounds and not (state.observation_round or {}).get("events")
                and state.observation_saved_payload is None):
            return
        payload = json.dumps({
            "support_states": state.support_alert_states,
            "indicator_keys": state.indicator_alert_keys,
            "active_round": state.observation_round if (state.observation_round or {}).get("events") else None,
            "pending_rounds": state.observation_pending_rounds,
        }, ensure_ascii=False)
        previous = state.observation_saved_payload
    try:
        if payload != previous or not observation_checkpoint_path().exists():
            atomic_write_text(observation_checkpoint_path(), payload)
        with state_lock:
            state.observation_saved_payload = payload
            state.module_errors["observations"] = None
    except OSError:
        with state_lock:
            state.module_errors["observations"] = "观察提醒状态写入失败"


def restore_observation_checkpoint(events: list[dict[str, Any]]) -> None:
    path = observation_checkpoint_path()
    try:
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            support_states = saved.get("support_states")
            indicator_keys = saved.get("indicator_keys")
            pending = saved.get("pending_rounds")
            if not isinstance(support_states, dict) or not isinstance(indicator_keys, dict) or not isinstance(pending, list):
                raise ValueError("invalid observation checkpoint")
            for key, structure in support_states.items():
                if (not isinstance(key, str) or not isinstance(structure, dict)
                        or structure.get("status") not in {"near", "far", "broken"}
                        or any(isinstance(structure.get(name), bool)
                               or not isinstance(structure.get(name), (int, float))
                               or not math.isfinite(structure[name])
                               for name in ("support", "tolerance_pct"))
                        or structure["support"] <= 0 or structure["tolerance_pct"] < 0):
                    raise ValueError("invalid support state")
            if any(not isinstance(key, str) or not isinstance(value, str) for key, value in indicator_keys.items()):
                raise ValueError("invalid indicator state")
            active = saved.get("active_round")
            if active is not None:
                pending = [*pending, active]
            for item in pending:
                if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                        or not isinstance(item.get("created_at"), (int, float))
                        or not isinstance(item.get("events"), list)):
                    raise ValueError("invalid pending observation round")
                for event in item["events"]:
                    if (not isinstance(event, dict) or event.get("type") not in {"support", "indicator"}
                            or not isinstance(event.get("symbol"), str) or not isinstance(event.get("event_key"), str)):
                        raise ValueError("invalid pending observation event")
        else:
            support_states, indicator_keys, pending = {}, {}, []
            # Seed existing structures without presenting old touches as new alerts.
            for event in events:
                if event.get("type") == "support" and event.get("symbol") and event.get("interval"):
                    key = f"{event['symbol']}:{event['interval']}"
                    structure, _ = advance_support(None, {**event, "tolerance_pct": event.get("tolerance_pct", .4)})
                    if structure is not None:
                        support_states[key] = structure
                elif event.get("type") == "indicator" and event.get("event_key"):
                    key = f"{event.get('symbol')}:{event.get('interval')}:{event.get('indicator', 'ma')}"
                    indicator_keys[key] = event["event_key"]
        committed = {event.get("event_key", "").removeprefix("observation-round:")
                     for event in events if event.get("type") == "observation_batch"}
        rounds = {item["id"]: item for item in pending}
        for event in events:
            round_id = event.get("observation_round_id")
            if (not isinstance(round_id, str) or event.get("type") not in {"support", "indicator"}
                    or not event.get("symbol") or not event.get("event_key")
                    or (round_id in committed and round_id not in rounds)):
                continue
            # The raw append may have survived an interruption before the next
            # checkpoint. Recover that journal tail before suppressing raw sends.
            recovered = rounds.setdefault(round_id, {
                "id": round_id, "created_at": event.get("created_at", time.time()),
                "events": [], "complete": False,
            })
            if event["event_key"] not in {item["event_key"] for item in recovered["events"]}:
                recovered["events"].append(event)
                if event["type"] == "support":
                    structure, _ = advance_support(None, event)
                    if structure is not None:
                        support_states[f"{event['symbol']}:{event.get('interval')}"] = structure
                else:
                    key = f"{event['symbol']}:{event.get('interval')}:{event.get('indicator', 'ma')}"
                    indicator_keys[key] = event["event_key"]
        pending = list(rounds.values())
        with state_lock:
            state.support_alert_states = support_states
            state.indicator_alert_keys = indicator_keys
            state.sent_alerts.update(indicator_keys.values())
            state.observation_round = None
            state.observation_pending_rounds = pending
            state.observation_checkpoint_ok = True
        # Establish the outbox baseline before committing recovered groups.
        save_observation_checkpoint()
    except (OSError, ValueError, TypeError, AttributeError):
        with state_lock:
            state.observation_checkpoint_ok = False
            state.module_errors["observations"] = "观察提醒状态损坏或不可读，已暂停观察提醒"


def observation_batch(round_data: dict[str, Any]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for event in round_data["events"]:
        groups.setdefault(event["symbol"], []).append(event)
    alerts = []
    for symbol, events in groups.items():
        lines = []
        for event in events:
            interval = event.get("interval", "")
            if event.get("type") == "indicator":
                detail = event.get("indicator_label") or event.get("indicator_name", "指标交叉")
                lines.append(f"{interval}：{detail}，参考价 {format_decimal(event.get('price'))}")
            else:
                label = {"broken": "报价跌破原支撑区", "retest": "再次接近支撑", "level_changed": "接近新支撑"}.get(
                    event.get("support_transition"), "首次接近支撑"
                )
                lines.append(f"{interval}：{label} {format_decimal(event.get('support'))}，"
                             f"当前价 {format_decimal(event.get('price'))}，"
                             f"距离 {format_signed_percent(event.get('distance_pct'))}")
        if not round_data.get("complete"):
            lines.insert(0, "本轮检查未完成，以下为已取得的观察信息。")
        lines.append("观察信息，不代表开仓信号。")
        timestamps = [event["created_at"] for event in events if isinstance(event.get("created_at"), (int, float))]
        alerts.append({
            "key": f"observation:{round_data['id']}:{symbol}", "title": f"{symbol} 观察提醒",
            "body": "\n".join(lines), "created_at": min(timestamps) if timestamps else None,
            "source_event_keys": [event["event_key"] for event in events],
        })
    return {"type": "observation_batch", "event_key": f"observation-round:{round_data['id']}",
            "created_at": round_data["created_at"], "complete": bool(round_data.get("complete")), "alerts": alerts}


def commit_observation_rounds() -> list[dict[str, Any]]:
    """Persist complete groups before exposing them to the separate Feishu sender."""
    delivered = []
    while True:
        with state_lock:
            pending = state.observation_pending_rounds[0] if state.observation_pending_rounds else None
        if pending is None:
            break
        batch = observation_batch(pending)
        if not append_event_to_disk(batch):
            break
        with state_lock:
            state.observation_pending_rounds.pop(0)
        delivered.extend(batch["alerts"])
    save_observation_checkpoint()
    return delivered


async def finish_observation_round_async(complete: bool) -> None:
    with state_lock:
        active = state.observation_round
        state.observation_round = None
        if active and active["events"]:
            active["complete"] = complete
            state.observation_pending_rounds.append(active)
    save_observation_checkpoint()
    alerts = commit_observation_rounds()
    for alert in alerts:
        await send_alert_notifications_async({"type": "observation_summary", **alert})


def normalize_watchlist(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []

    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).upper().strip()
        if not symbol:
            continue
        normalized.append(
            {
                "symbol": symbol,
                "email": str(item.get("email", "")).strip(),
                "signal": item.get("signal", True) is not False,
                "indicator_alert": item.get("indicator_alert", True) is not False,
                "support_alert": item.get("support_alert", True) is not False,
                "interval": normalize_interval(item.get("interval")),
            }
        )
    return normalized


def invalid_watchlist_symbols(watchlist: list[dict[str, Any]]) -> list[str]:
    if not env_bool("WATCHLIST_VALIDATE_SYMBOLS", True):
        return []
    symbols = sorted({item["symbol"] for item in watchlist if str(item.get("symbol", "")).endswith("USDT")})
    if not symbols:
        return []
    try:
        if market_data_source() == "binance_usdm":
            from binance_demo import DemoClient

            available = {
                s["symbol"]
                for s in DemoClient().public("/fapi/v1/exchangeInfo")["symbols"]
                if s.get("status") == "TRADING"
                and s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
            }
        else:
            available = fetch_binance_spot_symbols("USDT", timeout=8, attempts=1)
    except (
        http.client.IncompleteRead,
        OSError,
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        KeyError,
        RuntimeError,
    ):
        return []
    return [symbol for symbol in symbols if symbol not in available]


def normalize_interval(value: Any) -> str:
    interval = str(value or DEFAULT_SIGNAL_INTERVAL).strip()
    return interval if interval in {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"} else DEFAULT_SIGNAL_INTERVAL


def parse_float(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def strategy_trade_id(signal: dict[str, Any]) -> str:
    divergence_time = signal.get("divergence_time") or signal.get("kline_close_time") or signal.get("created_at")
    return f"{signal.get('symbol')}:{signal.get('interval', DEFAULT_SIGNAL_INTERVAL)}:{signal.get('signal')}:{divergence_time}"


def calculate_strategy_levels(direction: str, entry_price: float, stop_loss: float) -> dict[str, float] | None:
    return calculate_target_levels(direction, entry_price, stop_loss, STRATEGY_REWARD_RISK)


def build_strategy_trade_from_signal(signal: dict[str, Any]) -> dict[str, Any] | None:
    runtime = execution_settings(signal.get("runtime_config") or RUNTIME_SETTINGS.get())
    paper = runtime["values"]["paper"]
    direction = str(signal.get("signal", ""))
    if not paper["enabled"] or paper["signal_direction"] not in {"all", direction}:
        return None
    if (parse_float(signal.get("signal_score")) or 0) < paper["min_signal_score"]:
        return None
    if direction not in {"long", "short"}:
        return None

    entry_price = parse_float(signal.get("price"))
    stop_loss = parse_float(signal.get("stop_loss"))
    if entry_price is None or stop_loss is None:
        return None

    levels = calculate_target_levels(direction, entry_price, stop_loss, paper["reward_risk"])
    if not levels:
        return None

    opened_at = parse_float(signal.get("created_at")) or time.time()
    return {
        "id": strategy_trade_id(signal),
        "mode": "paper",
        "status": "open",
        "outcome": "open",
        "exit_reason": "open",
        "symbol": str(signal.get("symbol", "")).upper(),
        "interval": normalize_interval(signal.get("interval", DEFAULT_SIGNAL_INTERVAL)),
        "direction": direction,
        "signal_name": signal.get("signal_name", ""),
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "initial_stop_loss": stop_loss,
        "active_stop": stop_loss,
        "risk": levels["risk"],
        "target_price": levels["target_price"],
        "protection_price": levels["protection_price"],
        "protection_activated": False,
        "protected_stop_price": 0.0,
        "exit_mode": paper["stop_mode"],
        "fee_rate": paper["fee_rate"],
        "return_model": LINEAR_USDM_V1,
        "config_revision": runtime["config_revision"],
        "config_engine_version": runtime["config_engine_version"],
        "execution_revision": runtime["execution_revision"],
        "engine_version": runtime["engine_version"],
        "runtime_config": runtime,
        "market_data_source": runtime["market_data_source"],
        "opened_at": opened_at,
        "opened_kline_close_time": signal.get("kline_close_time"),
        "last_evaluated_close_time": signal.get("kline_close_time"),
        "signal_ready_time": signal.get("signal_ready_time"),
        "decision_close_time": signal.get("decision_close_time", signal.get("kline_close_time")),
        "observed_at": signal.get("observed_at"),
        "observed_at_unit": signal.get("observed_at_unit"),
        "input_sha256": signal.get("input_sha256"),
        "input_snapshot_ref": signal.get("input_snapshot_ref"),
        "entry_boundary": "signal_reference_price",
        "fill_boundary": "open_before_range_v1_adverse_stop_at_observed_open",
        "divergence_time": signal.get("divergence_time"),
        "event_key": signal.get("event_key"),
        "strength": signal.get("strength"),
        "signal_grade": signal.get("signal_grade"),
        "signal_score": signal.get("signal_score"),
        "divergence_type": signal.get("divergence_type"),
        "highest_price": entry_price,
        "lowest_price": entry_price,
        "current_price": entry_price,
        "unrealized_pct": 0.0,
        "return_pct": None,
        "exit_price": None,
        "closed_at": None,
    }


def strategy_return_pct(
    direction: str, entry_price: float, exit_price: float, fee_rate: float = STRATEGY_FEE_RATE,
    *, return_model: str = LEGACY_RATIO_V1,
) -> float:
    return calculate_return_pct(direction, entry_price, exit_price, fee_rate, return_model=return_model)


def apply_strategy_lifecycle(
    trade: dict[str, Any], low: float, high: float, close: float, atr_value: float | None = None,
    *, open_price: float | None = None,
) -> bool:
    direction = str(trade.get("direction"))
    entry_price = parse_float(trade.get("entry_price"))
    initial_stop = parse_float(trade.get("initial_stop_loss")) or parse_float(trade.get("stop_loss"))
    active_stop = parse_float(trade.get("active_stop")) or initial_stop
    target_price = parse_float(trade.get("target_price"))
    protection_price = parse_float(trade.get("protection_price"))
    if entry_price is None or initial_stop is None or target_price is None or protection_price is None:
        return False

    lifecycle = evaluate_lifecycle_bar(
        direction,
        low,
        high,
        close,
        entry_price,
        initial_stop,
        target_price,
        protection_price,
        active_stop=active_stop,
        highest_price=parse_float(trade.get("highest_price")),
        lowest_price=parse_float(trade.get("lowest_price")),
        protection_activated=bool(trade.get("protection_activated")),
        protected_stop_price=parse_float(trade.get("protected_stop_price")) or 0.0,
        stop_mode=str(trade.get("exit_mode") or STRATEGY_STOP_MODE),
        fee_rate=trade.get("fee_rate", STRATEGY_FEE_RATE),
        return_model=trade.get("return_model", LEGACY_RATIO_V1),
        atr_value=atr_value,
        open_price=open_price,
    )
    trade["active_stop"] = lifecycle.active_stop
    trade["stop_loss"] = lifecycle.active_stop
    trade["highest_price"] = lifecycle.highest_price
    trade["lowest_price"] = lifecycle.lowest_price
    trade["protection_activated"] = lifecycle.protection_activated
    trade["protected_stop_price"] = lifecycle.protected_stop_price
    if not lifecycle.decision:
        return False
    close_strategy_trade(trade, lifecycle.decision.exit_price, lifecycle.decision.reason, lifecycle.decision.outcome)
    trade["exit_phase"] = lifecycle.exit_phase
    return True


def refresh_strategy_trade_mark(trade: dict[str, Any], current_price: float) -> None:
    entry_price = parse_float(trade.get("entry_price"))
    if entry_price is None or entry_price <= 0:
        return
    direction = str(trade.get("direction"))
    trade["current_price"] = current_price
    trade["highest_price"] = max(parse_float(trade.get("highest_price")) or entry_price, current_price)
    trade["lowest_price"] = min(parse_float(trade.get("lowest_price")) or entry_price, current_price)
    trade["unrealized_pct"] = strategy_return_pct(
        direction, entry_price, current_price, trade.get("fee_rate", STRATEGY_FEE_RATE),
        return_model=trade.get("return_model", LEGACY_RATIO_V1),
    )


def close_strategy_trade(trade: dict[str, Any], exit_price: float, reason: str, outcome: str) -> None:
    entry_price = parse_float(trade.get("entry_price"))
    if entry_price is None:
        return
    trade["status"] = "closed"
    trade["exit_reason"] = reason
    trade["outcome"] = outcome
    trade["exit_price"] = exit_price
    trade["closed_at"] = time.time()
    trade["exit_engine_version"] = ENGINE_VERSION
    trade["return_pct"] = strategy_return_pct(
        str(trade.get("direction")), entry_price, exit_price, trade.get("fee_rate", STRATEGY_FEE_RATE),
        return_model=trade.get("return_model", LEGACY_RATIO_V1),
    )
    if trade.get("return_model") == LINEAR_USDM_V1:
        trade["outcome"] = classify_return_outcome(
            str(trade.get("direction")), entry_price, exit_price,
            trade.get("fee_rate", STRATEGY_FEE_RATE), return_model=LINEAR_USDM_V1,
        )


def evaluate_open_strategy_trade(trade: dict[str, Any], current_price: float) -> bool:
    if trade.get("status") != "open":
        return False

    refresh_strategy_trade_mark(trade, current_price)
    return apply_strategy_lifecycle(trade, current_price, current_price, current_price, open_price=current_price)


def evaluate_open_strategy_trade_with_bars(
    trade: dict[str, Any], bars: list[dict[str, Any]], current_price: float | None
) -> bool:
    if trade.get("status") != "open":
        return False

    opened_close_time = parse_int(trade.get("opened_kline_close_time"), 0)
    last_evaluated = parse_int(trade.get("last_evaluated_close_time"), opened_close_time)

    # Validate the entire unseen batch before changing marks or progress. A
    # same-time conflicting revision must not partially consume this batch.
    unique_bars: dict[int, dict[str, Any]] = {}
    now_ms = int(time.time() * 1000)
    for bar in bars:
        close_time = parse_int(bar.get("close_time"), 0)
        if (opened_close_time <= 0 or close_time <= max(opened_close_time, last_evaluated)
                or close_time > now_ms or bar.get("confirmed") is False or bar.get("is_closed") is False):
            continue
        for price_field in ("low", "high", "close"):
            number = parse_float(bar.get(price_field))
            if number is None or not math.isfinite(number) or number <= 0:
                raise ValueError("策略持仓K线价格无效")
        if float(bar["low"]) > float(bar["high"]) or not float(bar["low"]) <= float(bar["close"]) <= float(bar["high"]):
            raise ValueError("策略持仓K线价格范围无效")
        open_price = parse_float(bar.get("open"))
        if "open" in bar and (open_price is None or not math.isfinite(open_price)
                               or not float(bar["low"]) <= open_price <= float(bar["high"])):
            raise ValueError("策略持仓K线开盘价无效")
        previous = unique_bars.get(close_time)
        if previous is not None and previous != bar:
            raise ValueError("策略持仓K线同一时刻出现冲突数据")
        unique_bars[close_time] = bar
    future_bars = list(unique_bars.values())
    for bar in sorted(future_bars, key=lambda item: parse_int(item.get("close_time"), 0)):
        high = parse_float(bar.get("high"))
        low = parse_float(bar.get("low"))
        close = parse_float(bar.get("close"))
        if high is None or low is None:
            continue
        trade["last_evaluated_close_time"] = bar["close_time"]
        if apply_strategy_lifecycle(
            trade, low, high, close if close is not None else high, parse_float(bar.get("atr")),
            open_price=parse_float(bar.get("open")),
        ):
            # An opening gap exits before the later range/close is observable.
            refresh_strategy_trade_mark(trade, trade["exit_price"])
            trade["exit_kline_close_time"] = bar["close_time"]
            return True
        if close is not None:
            refresh_strategy_trade_mark(trade, close)

    if current_price is None:
        return False
    return evaluate_open_strategy_trade(trade, current_price)


def calculate_strategy_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Report retained trades separately by their recorded return model.

    Historical outcomes remain untouched; net wins use finite recorded returns.
    Missing results and unrecognized accounting must never become zero returns.
    """
    performance_fields = (
        "wins", "losses", "breakevens", "win_rate", "expectancy", "total_return",
        "avg_win", "avg_loss", "payoff_ratio", "profit_factor",
        "stop_loss_rate", "protection_rate",
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    return_models: dict[str, int] = {}
    for trade in trades:
        model = str(trade["return_model"]) if "return_model" in trade else LEGACY_RATIO_V1
        grouped.setdefault(model, []).append(trade)
        if trade.get("status") == "closed":
            return_models[model] = return_models.get(model, 0) + 1

    model_stats: dict[str, dict[str, Any]] = {}
    for model, records in grouped.items():
        closed = [trade for trade in records if trade.get("status") == "closed"]
        returns = []
        for trade in closed:
            value = trade.get("return_pct")
            if isinstance(value, bool):
                continue
            try:
                parsed = parse_float(value)
            except OverflowError:
                parsed = None
            if parsed is not None and math.isfinite(parsed):
                returns.append(parsed)
        recognized = model in {LEGACY_RATIO_V1, LINEAR_USDM_V1}
        group = {
            "recognized": recognized,
            "total_trades": len(records),
            "open_trades": sum(trade.get("status") == "open" for trade in records),
            "closed_trades": len(closed),
            "valid_return_count": len(returns),
            "missing_return_count": len(closed) - len(returns),
            **dict.fromkeys(performance_fields),
        }
        if recognized:
            wins = [value for value in returns if value > 1e-12]
            losses = [-value for value in returns if value < -1e-12]
            avg_win = sum(wins) / len(wins) if wins else None
            avg_loss = sum(losses) / len(losses) if losses else None
            group.update(
                wins=len(wins), losses=len(losses), breakevens=len(returns) - len(wins) - len(losses),
                win_rate=len(wins) / len(returns) if returns else None,
                expectancy=sum(returns) / len(returns) if returns else None,
                total_return=sum(returns) if returns else None,
                avg_win=avg_win, avg_loss=avg_loss,
                payoff_ratio=avg_win / avg_loss if avg_win is not None and avg_loss is not None else None,
                profit_factor=sum(wins) / sum(losses) if losses else None,
                stop_loss_rate=(sum(trade.get("exit_reason") == "stop_loss" for trade in closed) / len(closed)
                                if closed else None),
                protection_rate=(sum(trade.get("exit_reason") in {"protection_reached", "protected_stop", "trailing_stop"}
                                     for trade in closed) / len(closed) if closed else None),
            )
        model_stats[model] = group

    closed_count = sum(group["closed_trades"] for group in model_stats.values())
    single_known_model = len(model_stats) == 1 and next(iter(model_stats.values()))["recognized"]
    summary_scope = ("NO_CLOSED_RECORDS" if not closed_count else
                     "SINGLE_RETURN_MODEL" if single_known_model else "SPLIT_BY_RETURN_MODEL")
    stats = {
        "total_trades": len(trades),
        "open_trades": sum(group["open_trades"] for group in model_stats.values()),
        "closed_trades": closed_count,
        "valid_return_count": sum(group["valid_return_count"] for group in model_stats.values()),
        "missing_return_count": sum(group["missing_return_count"] for group in model_stats.values()),
        **dict.fromkeys(performance_fields),
        "return_models": return_models,
        "mixed_return_models": len(return_models) > 1,
        "return_model_stats": model_stats,
        "summary_scope": summary_scope,
        "comparability_note": ("收益和净胜率仅按收益版本分别统计；不同口径及未知口径不合并，未知口径不计算绩效。"
                               if not single_known_model and model_stats else
                               "净胜率按有效收益的正负计算；缺失收益不作零，逐笔比例不代表账户净值。"),
    }
    if summary_scope == "SINGLE_RETURN_MODEL":
        group = next(iter(model_stats.values()))
        stats.update({field: group[field] for field in performance_fields})
    return stats


def register_strategy_signal(signal: dict[str, Any]) -> bool:
    with state_lock:
        if state.module_errors.get("ledger"):
            return False
    membership = None
    if PAPER_UNIVERSE_ENABLED:
        pool = PAPER_UNIVERSE.status()
        if (not pool["ready"] or signal.get("paper_universe_snapshot_id") != pool.get("snapshot_id")
                or signal.get("interval", DEFAULT_SIGNAL_INTERVAL) != PAPER_UNIVERSE_INTERVAL):
            return False
        membership = next((member for member in pool["members"] if member["symbol"] == signal.get("symbol")), None)
        if membership is None:
            return False
    trade = build_strategy_trade_from_signal(signal)
    if not trade:
        return False
    if membership:
        trade["paper_universe"] = {**membership, "snapshot_id": pool["snapshot_id"], "updated_at": pool["updated_at"]}

    changed = False
    with state_lock:
        existing_ids = {str(item.get("id")) for item in state.strategy_trades}
        if trade["id"] not in existing_ids:
            state.strategy_trades.append(trade)
            state.strategy_trades = retain_strategy_trades(state.strategy_trades)
            state.strategy_stats = calculate_strategy_stats(state.strategy_trades)
            changed = True
        trades_snapshot = [dict(item) for item in state.strategy_trades]

    if changed:
        save_strategy_trades(trades_snapshot)
    return changed


def qualified_paper_quote(ticker: dict[str, Any] | None) -> float | None:
    """A point-price fill requires a finite price and a fresh source clock.

    Binance's source closeTime is retained by the adapter. The poll's local
    arrival time cannot substitute for a missing or stale source timestamp.
    """
    if not ticker:
        return None
    price = parse_float(ticker.get("lastPrice"))
    timestamp = parse_float(ticker.get("closeTime"))
    if (price is None or not math.isfinite(price) or price <= 0
            or timestamp is None or not math.isfinite(timestamp) or timestamp <= 0):
        return None
    age = time.time() * 1000 - timestamp
    return price if 0 <= age <= PAPER_QUOTE_MAX_AGE_MS else None


async def update_strategy_trades_with_prices_async(prices: dict[str, dict[str, Any]]) -> None:
    changed = False
    with state_lock:
        if state.module_errors.get("ledger"):
            return
        open_keys = sorted(
            {
                (
                    str(trade.get("symbol", "")).upper(),
                    normalize_interval(trade.get("interval", DEFAULT_SIGNAL_INTERVAL)),
                )
                for trade in state.strategy_trades
                if trade.get("status") == "open"
            }
        )

    data_errors: dict[tuple[str, str], str] = {}
    data_details: list[dict[str, Any]] = []
    async def load_bars(symbol: str, interval: str) -> tuple[tuple[str, str], list[dict[str, Any]]]:
        try:
            bars = await async_fetch_klines(symbol, interval, KLINE_LIMIT)
            unique = {}
            for bar in bars:
                close_time = parse_int(bar.get("close_time"), 0)
                if close_time in unique and unique[close_time] != bar:
                    raise ValueError("策略持仓K线同一时刻出现冲突数据")
                unique[close_time] = bar
            bars = [unique[key] for key in sorted(unique)]
            # Match the research lifecycle's causal ATR calculation, including
            # its history warm-up; raw exchange candles contain only OHLCV.
            return (symbol, interval), [{**bar, "atr": value} for bar, value in zip(bars, atr(bars))] if bars else []
        except (
            http.client.IncompleteRead,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            OSError,
            RuntimeError,
            ValueError,
        ) as error:
            data_errors[(symbol, interval)] = f"{symbol}:{interval} K线不可用 ({type(error).__name__})"
            data_details.append({
                "symbol": symbol, "interval": interval, "kind": "kline_unavailable",
                "error_type": type(error).__name__,
            })
            return (symbol, interval), []

    bars_by_key = (
        dict(await asyncio.gather(*(load_bars(symbol, interval) for symbol, interval in open_keys)))
        if open_keys
        else {}
    )

    # History loading can outlive a quote's source-clock budget. Refresh only
    # rejected quotes after that wait; never re-date a stale exchange response.
    paper_quotes = dict(prices)
    quote_refresh_errors: dict[str, str] = {}
    if market_data_source() == "binance_usdm":
        refresh_symbols = sorted({symbol for symbol, _ in open_keys
                                  if qualified_paper_quote(paper_quotes.get(symbol)) is None})

        async def refresh_quote(symbol):
            try:
                return symbol, await async_fetch_binance_usdm_point_quote(symbol), None
            except (*recoverable_http_errors(), RuntimeError, TypeError) as error:
                return symbol, None, type(error).__name__

        for symbol, quote, error in await asyncio.gather(*(refresh_quote(symbol) for symbol in refresh_symbols)):
            if quote is not None:
                paper_quotes[symbol] = quote
            if error:
                quote_refresh_errors[symbol] = error

    with state_lock:
        for trade in state.strategy_trades:
            if trade.get("status") != "open":
                continue
            before = dict(trade)
            symbol = str(trade.get("symbol", "")).upper()
            interval = normalize_interval(trade.get("interval", DEFAULT_SIGNAL_INTERVAL))
            ticker = paper_quotes.get(symbol)
            current_price = qualified_paper_quote(ticker)
            bars = bars_by_key.get((symbol, interval), [])
            if current_price is None and (prices.get(symbol) or not bars):
                source_time = parse_float((ticker or {}).get("closeTime"))
                source_age = (time.time() * 1000 - source_time
                              if source_time is not None and math.isfinite(source_time) and source_time > 0 else None)
                age_label = "unknown" if source_age is None else "future" if source_age < 0 else f"{int(source_age)}ms"
                refresh_error = quote_refresh_errors.get(symbol, "invalid")
                data_errors[(symbol, interval)] = (
                    f"{symbol}:{interval} 缺少有效新鲜报价 (source_age={age_label}, refresh={refresh_error})"
                )
                data_details.append({
                    "symbol": symbol, "interval": interval,
                    "kind": ("quote_request_failed" if symbol in quote_refresh_errors else
                             "stale_trade" if source_age is not None and source_age > PAPER_QUOTE_MAX_AGE_MS
                             else "invalid_quote"),
                    "source_age_ms": int(source_age) if source_age is not None else None,
                    "refresh_error": refresh_error,
                })
            try:
                if bars and evaluate_open_strategy_trade_with_bars(trade, bars, current_price):
                    changed = True
                elif not bars and current_price is not None and evaluate_open_strategy_trade(trade, current_price):
                    changed = True
            except ValueError as error:
                data_errors[(symbol, interval)] = f"{symbol}:{interval} {error}"
                data_details.append({
                    "symbol": symbol, "interval": interval, "kind": "invalid_data",
                    "error_type": type(error).__name__,
                })
            changed = changed or before != trade
        state.module_errors["paper_data"] = "; ".join(data_errors.values()) or None
        state.paper_data_details = data_details
        state.strategy_stats = calculate_strategy_stats(state.strategy_trades)
        trades_snapshot = [dict(item) for item in state.strategy_trades]

    if changed:
        save_strategy_trades(trades_snapshot)


def seed_strategy_trades_from_events(
    events: list[dict[str, Any]], existing_trades: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    existing_ids = {str(trade.get("id")) for trade in existing_trades}
    trades = list(existing_trades)
    for event in events:
        if event.get("type") != "chanlun" or event.get("signal") not in {"long", "short"}:
            continue
        trade = build_strategy_trade_from_signal(event)
        if not trade or trade["id"] in existing_ids:
            continue
        trades.append(trade)
        existing_ids.add(trade["id"])
    return retain_strategy_trades(trades)


def fetch_higher_trend_state(symbol: str) -> dict[str, Any]:
    bars = calculate_indicators(fetch_klines(symbol, SELL_TREND_BREAKER_INTERVAL, SELL_TREND_BREAKER_LIMIT))
    ready_bars = [
        bar
        for bar in bars
        if bar.get("close") is not None and bar.get("ema60") is not None and bar.get("macd") is not None
    ]
    if not ready_bars:
        return {"trend": "unknown"}
    latest = ready_bars[-1]
    return {
        "trend": latest.get("trend", "unknown"),
        "close": latest.get("close"),
        "ema20": latest.get("ema20"),
        "ema60": latest.get("ema60"),
        "ema200": latest.get("ema200"),
        "macd": latest.get("macd"),
    }


def fetch_microstructure_state(symbol: str, interval: str) -> dict[str, Any]:
    if interval not in {"15m", "30m", "1h", "4h"}:
        return {}
    state_data: dict[str, Any] = {}
    try:
        funding = fetch_funding_rate(symbol)
        state_data["funding_rate"] = funding.get("lastFundingRate")
    except (http.client.IncompleteRead, OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
        pass
    try:
        oi = fetch_open_interest_ratio(symbol, interval)
        state_data["open_interest"] = oi.get("openInterest")
        state_data["open_interest_ratio"] = oi.get("openInterestRatio")
    except (http.client.IncompleteRead, OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
        pass
    return state_data


def check_buy_trend_breaker(symbol: str, interval: str) -> tuple[bool, str, dict[str, Any]]:
    if interval != DEFAULT_SIGNAL_INTERVAL:
        return False, "", {"trend": "unknown"}

    higher = fetch_higher_trend_state(symbol)
    close = higher.get("close")
    ema60 = higher.get("ema60")
    macd_value = higher.get("macd")
    if close is not None and ema60 is not None and macd_value is not None and close < ema60 and macd_value < 0:
        return (
            True,
            f"✗ {SELL_TREND_BREAKER_INTERVAL}空头趋势熔断：价格={close:.2f}<EMA60={ema60:.2f}, MACD={macd_value:.4f}<0",
            higher,
        )
    return False, "", higher


def check_sell_trend_breaker(symbol: str, interval: str) -> tuple[bool, str]:
    if interval != DEFAULT_SIGNAL_INTERVAL:
        return False, ""

    higher = fetch_higher_trend_state(symbol)
    close = higher.get("close")
    ema60 = higher.get("ema60")
    macd_value = higher.get("macd")
    if close is None or ema60 is None or macd_value is None:
        return False, ""

    if close > ema60 and macd_value > 0:
        return (
            True,
            f"✗ {SELL_TREND_BREAKER_INTERVAL}多头趋势熔断：价格={close:.2f}>EMA60={ema60:.2f}, MACD={macd_value:.4f}>0",
        )
    return False, ""


def td_setup(bars: list[dict[str, Any]], period: int = 9) -> int:
    closes = [bar.get("close") for bar in bars]
    if len(closes) < period + 4:
        return 0
    if any(closes[-index] is None or closes[-index - 4] is None for index in range(1, period + 1)):
        return 0

    buy = all(closes[-index] <= closes[-index - 4] for index in range(1, period + 1))
    sell = all(closes[-index] >= closes[-index - 4] for index in range(1, period + 1))
    if buy:
        return 1
    if sell:
        return -1
    return 0


def direction_label(direction: str) -> str:
    if direction == "up":
        return "上穿"
    if direction == "down":
        return "下穿"
    return "未触发"


async def detect_indicator_signal_async(symbol: str, interval: str) -> dict[str, Any]:
    bars = await async_fetch_klines(symbol, interval, KLINE_LIMIT)
    return build_indicator_signal_from_bars(symbol, interval, bars)


def build_indicator_signal_from_bars(symbol: str, interval: str, bars: list[dict[str, Any]]) -> dict[str, Any]:
    if len(bars) < 60:
        return build_indicator_wait_signal(symbol, interval, "K线不足")

    closes = [bar.get("close") for bar in bars]
    ma_fast = rolling_average(closes, MA_FAST_PERIOD)
    ma_slow = rolling_average(closes, MA_SLOW_PERIOD)
    macd_line, macd_signal, _hist = macd(closes, 12, 26, 9)
    latest = bars[-1]
    previous_index = len(bars) - 2
    current_index = len(bars) - 1

    ma_direction = cross_direction(
        ma_fast[previous_index],
        ma_fast[current_index],
        ma_slow[previous_index],
        ma_slow[current_index],
    )
    macd_direction = cross_direction(
        macd_line[previous_index],
        macd_line[current_index],
        macd_signal[previous_index],
        macd_signal[current_index],
    )

    return {
        "type": "indicator",
        "symbol": symbol,
        "interval": interval,
        "price": latest.get("close"),
        "kline_close_time": latest.get("close_time"),
        "created_at": time.time(),
        "ma": {
            "fast_period": MA_FAST_PERIOD,
            "slow_period": MA_SLOW_PERIOD,
            "fast": ma_fast[current_index],
            "slow": ma_slow[current_index],
            "direction": ma_direction,
            "label": f"MA{MA_FAST_PERIOD}/{MA_SLOW_PERIOD}{direction_label(ma_direction)}",
        },
        "macd": {
            "dif": macd_line[current_index],
            "dea": macd_signal[current_index],
            "direction": macd_direction,
            "label": f"MACD DIF/DEA{direction_label(macd_direction)}",
        },
    }


def build_indicator_wait_signal(symbol: str, interval: str, reason: str) -> dict[str, Any]:
    return {
        "type": "indicator",
        "symbol": symbol,
        "interval": interval,
        "price": None,
        "kline_close_time": None,
        "created_at": time.time(),
        "ma": {"direction": "none", "label": reason},
        "macd": {"direction": "none", "label": reason},
    }


def detect_support_level(symbol: str, interval: str, current_price: float | None) -> dict[str, Any]:
    bars = fetch_klines(symbol, interval, KLINE_LIMIT)
    return build_support_level_from_bars(symbol, interval, current_price, bars)


async def detect_support_level_async(symbol: str, interval: str, current_price: float | None) -> dict[str, Any]:
    bars = await async_fetch_klines(symbol, interval, KLINE_LIMIT)
    return build_support_level_from_bars(symbol, interval, current_price, bars)


def build_support_level_from_bars(
    symbol: str, interval: str, current_price: float | None, bars: list[dict[str, Any]]
) -> dict[str, Any]:
    if len(bars) < 30 or current_price is None:
        return build_support_wait_level(symbol, interval, "K线不足")

    lows = [bar.get("low") for bar in bars]
    valid_lows = [value for value in lows if value is not None]
    if len(valid_lows) < 30:
        return build_support_wait_level(symbol, interval, "低点不足")

    _peaks, troughs = find_local_extremes(valid_lows, 3)
    recent_troughs = troughs[-12:]
    candidate_lows = [low for _index, low in recent_troughs if low <= current_price]
    if not candidate_lows:
        candidate_lows = [low for low in valid_lows[-40:] if low <= current_price]
    support = max(candidate_lows) if candidate_lows else min(valid_lows[-40:])
    tolerance = SUPPORT_TOUCH_TOLERANCE.get(interval, 0.004)
    distance_pct = ((current_price - support) / support * 100) if support else None
    touched = support is not None and current_price <= support * (1 + tolerance)
    touch_price = support * (1 + tolerance) if support is not None else None
    near_touches = count_support_touches(valid_lows[-120:], support, max(support * tolerance, support * 0.001))

    return {
        "type": "support",
        "symbol": symbol,
        "interval": interval,
        "price": current_price,
        "support": support,
        "touch_price": touch_price,
        "distance_pct": distance_pct,
        "tolerance_pct": tolerance * 100,
        "touches": near_touches,
        "touched": touched,
        "kline_close_time": bars[-1].get("close_time"),
        "created_at": time.time(),
    }


def build_support_wait_level(symbol: str, interval: str, reason: str) -> dict[str, Any]:
    return {
        "type": "support",
        "symbol": symbol,
        "interval": interval,
        "price": None,
        "support": None,
        "touch_price": None,
        "distance_pct": None,
        "tolerance_pct": SUPPORT_TOUCH_TOLERANCE.get(interval, 0.004) * 100,
        "touches": 0,
        "touched": False,
        "reason": reason,
        "kline_close_time": None,
        "created_at": time.time(),
    }


def count_support_touches(lows: list[float], support: float, band: float) -> int:
    return sum(1 for low in lows if abs(low - support) <= band)


def nearest_index_by_close_time(bars: list[dict[str, Any]], close_time: int) -> int:
    if not bars:
        raise ValueError("bars must not be empty")
    return min(range(len(bars)), key=lambda index: abs(int(bars[index]["close_time"]) - close_time))


def td_signal_label(value: int) -> str:
    if value == 1:
        return "买"
    if value == -1:
        return "卖"
    return "-"


def get_td_signals(symbol: str, timestamp_ms: int) -> dict[str, int]:
    signals = {}
    for interval in ("1m", "3m", "5m", "15m", "30m"):
        try:
            bars = fetch_klines(symbol, interval, TD_KLINE_LIMIT)
        except (
            http.client.IncompleteRead,
            OSError,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            ValueError,
        ):
            signals[interval] = 0
            continue
        if not bars:
            signals[interval] = 0
            continue
        index = nearest_index_by_close_time(bars, timestamp_ms)
        window = bars[max(0, index - 50) : index + 1]
        signals[interval] = td_setup(window, 9) if len(window) >= 50 else 0
    return signals


def signal_engine(symbol: str, interval: str, runtime=None, namespace="watchlist") -> ProjectSignalEngine:
    runtime = execution_settings(runtime or RUNTIME_SETTINGS.get())
    key = (namespace, symbol.upper(), normalize_interval(interval), runtime["execution_revision"], runtime["engine_version"])
    with _signal_engines_lock:
        engine = SIGNAL_ENGINES.get(key)
        if engine is None:
            directory = Path(os.getenv("SIGNAL_ENGINE_STATE_DIR", str(STRATEGY_TRADES_FILE.parent / "signal-engine-state")))
            engine = PersistentSignalEngine(StrategyConfig(**runtime["effective_strategy"]), directory, key)
            SIGNAL_ENGINES[key] = engine
    return engine


def add_higher_timeframe_context(bars: list[dict[str, Any]], higher_bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not bars or not higher_bars:
        return bars

    higher_index = 0
    enriched = []
    for bar in bars:
        bar_close_time = int(bar.get("close_time") or bar.get("open_time") or 0)
        while (
            higher_index + 1 < len(higher_bars)
            and int(
                higher_bars[higher_index + 1].get("close_time") or higher_bars[higher_index + 1].get("open_time") or 0
            )
            <= bar_close_time
        ):
            higher_index += 1

        higher_close_time = int(
            higher_bars[higher_index].get("close_time") or higher_bars[higher_index].get("open_time") or 0
        )
        higher = higher_bars[higher_index] if higher_close_time <= bar_close_time else {}
        enriched.append(
            {
                **bar,
                "higher_close": higher.get("close"),
                "higher_ema20": higher.get("ema20"),
                "higher_ema60": higher.get("ema60"),
                "higher_ema200": higher.get("ema200"),
                "higher_macd": higher.get("macd"),
                "higher_trend": higher.get("trend", "unknown"),
            }
        )
    return enriched


def build_project_signal_bars(symbol: str, interval: str) -> list[dict[str, Any]]:
    bars = calculate_indicators(
        [bar for bar in fetch_klines(symbol, interval, KLINE_LIMIT) if int(bar["close_time"]) < time.time() * 1000]
    )
    if not bars:
        return []
    if interval in {HIGHER_TREND_INTERVAL, "1d"}:
        enriched_bars = add_higher_timeframe_context(bars, bars)
    else:
        higher_limit = max(120, min(1000, len(bars) // 8 + 120))
        higher_bars = calculate_indicators(
            [
                bar
                for bar in fetch_klines(symbol, HIGHER_TREND_INTERVAL, higher_limit)
                if int(bar["close_time"]) < time.time() * 1000
            ]
        )
        enriched_bars = add_higher_timeframe_context(bars, higher_bars)

    ready_bars = [bar for bar in enriched_bars if bar.get("close") is not None and bar.get("macd") is not None]
    if ready_bars:
        ready_bars[-1].update(fetch_microstructure_state(symbol, interval))
    return ready_bars


def detect_project_signal(symbol: str, interval: str, *, engine_namespace="watchlist", include_td=True) -> dict[str, Any]:
    if market_data_source() != "binance_usdm":
        raise ValueError("当前产品要求 Binance USDT 合约行情，请使用统一启动器")
    normalized_interval = normalize_interval(interval)
    ready_bars = build_project_signal_bars(symbol, normalized_interval)
    runtime = execution_settings(RUNTIME_SETTINGS.get())
    signal = signal_engine(symbol, normalized_interval, runtime, engine_namespace).detect(ready_bars)
    signal["runtime_config"] = runtime
    signal["config_revision"] = runtime["config_revision"]
    signal["config_engine_version"] = runtime["config_engine_version"]
    signal["engine_version"] = runtime["engine_version"]
    signal["execution_revision"] = runtime["execution_revision"]
    latest = ready_bars[-1] if ready_bars else {}
    close_time = latest.get("close_time") or latest.get("open_time")
    td_signals: dict[str, int] = {}
    if include_td and close_time is not None:
        try:
            td_signals = get_td_signals(symbol, int(close_time))
        except (
            http.client.IncompleteRead,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            OSError,
            ValueError,
        ):
            td_signals = {}

    signal.setdefault("signal", "wait")
    signal.setdefault("signal_name", "K线不足" if len(ready_bars) < 100 else "等待MACD背驰")
    signal.setdefault("price", latest.get("close"))
    signal.setdefault("kline_close_time", close_time)
    signal.setdefault("created_at", time.time())
    signal["type"] = "chanlun"
    signal["symbol"] = symbol
    signal["interval"] = normalized_interval
    signal["td_signals"] = td_signals
    signal["td_summary"] = " ".join(f"{key}:{td_signal_label(value)}" for key, value in td_signals.items())
    return signal


def detect_chanlun_signal(symbol: str, interval: str) -> dict[str, Any]:
    bars = calculate_indicators(fetch_klines(symbol, interval, KLINE_LIMIT))
    ready_bars = [bar for bar in bars if bar.get("close") is not None and bar.get("macd") is not None]
    if len(ready_bars) < 100:
        return build_wait_signal(symbol, interval, "K线不足")

    current_bar = ready_bars[-1]
    current_bar.update(fetch_microstructure_state(symbol, interval))
    window = ready_bars[-200:]
    closes = [bar["close"] for bar in window]
    macd_values = [bar["macd"] for bar in window]
    macd_fast_values = [bar.get("macd_fast") for bar in window]

    price_peaks, price_troughs = find_local_extremes(closes)
    macd_peaks, macd_troughs = find_local_extremes(macd_values)
    macd_fast_peaks, macd_fast_troughs = find_local_extremes(macd_fast_values)

    bullish_div, bullish_strength, bullish_info = detect_bullish_divergence(
        price_troughs, macd_troughs, macd_fast_troughs
    )
    bearish_div, bearish_strength, bearish_info = detect_bearish_divergence(price_peaks, macd_peaks, macd_fast_peaks)
    td_signals = get_td_signals(symbol, int(current_bar["close_time"]))
    td_summary = " ".join(f"{key}:{td_signal_label(value)}" for key, value in td_signals.items())
    ma_direction = latest_ma_direction(ready_bars)
    macd_direction = latest_macd_direction(ready_bars)

    base_signal = {
        "type": "chanlun",
        "symbol": symbol,
        "interval": interval,
        "price": current_bar["close"],
        "kline_close_time": current_bar["close_time"],
        "td_signals": td_signals,
        "td_summary": td_summary,
        "created_at": time.time(),
    }

    def bars_waited_since(close_time: Any) -> int:
        try:
            created_close_time = int(close_time)
        except (TypeError, ValueError):
            return 0
        return sum(1 for bar in ready_bars if int(bar.get("close_time", 0)) > created_close_time)

    def build_buy_response(
        candidate: dict[str, Any],
        signal: str,
        signal_name: str,
        filter_message: str,
        confirm_bars: int,
        higher: dict[str, Any] | None = None,
        signal_grade: str = "weak",
    ) -> dict[str, Any]:
        higher_state = higher or {}
        structure = candidate.get("structure")
        score_info = score_signal(
            "long", candidate["strength"], signal == "long", current_bar, higher_state, "熔断" in signal_name, structure
        )
        stop_anchor = float(
            structure.get("stop_anchor", candidate["price_low"]) if structure else candidate["price_low"]
        )
        combined_filter = f"{filter_message}；结构：{structure['text']}" if structure else filter_message
        return {
            **base_signal,
            "signal": signal,
            "signal_name": signal_name,
            "strength": candidate["strength"],
            "filter": combined_filter,
            "stop_loss": stop_anchor - (current_bar.get("atr") or 0) * ATR_STOP_MULTIPLIER,
            "divergence_time": candidate["divergence_time"],
            "signal_ready_time": candidate.get("signal_ready_time", candidate["divergence_time"]),
            "divergence_type": candidate["divergence_type"],
            "structure_factors": structure.get("factors", []) if structure else [],
            "structure_score": int(structure.get("score", 0) if structure else 0),
            "structure_text": structure.get("text", "结构因子不足") if structure else "结构因子不足",
            "confirm_bars": confirm_bars,
            "higher_trend": higher_state.get("trend", "unknown"),
            "signal_grade": score_info["grade"],
            "signal_score": score_info["score"],
            "signal_score_max": score_info["max_score"],
            "score_text": score_info["text"],
        }

    def build_sell_response(
        candidate: dict[str, Any], signal: str, signal_name: str, filter_message: str, confirm_bars: int = 0
    ) -> dict[str, Any]:
        higher = fetch_higher_trend_state(symbol) if interval == DEFAULT_SIGNAL_INTERVAL else {"trend": "unknown"}
        structure = candidate.get("structure")
        score_info = score_signal(
            "short", candidate["strength"], signal == "short", current_bar, higher, "熔断" in signal_name, structure
        )
        stop_anchor = float(
            structure.get("stop_anchor", candidate["price_high"]) if structure else candidate["price_high"]
        )
        combined_filter = f"{filter_message}；结构：{structure['text']}" if structure else filter_message
        return {
            **base_signal,
            "signal": signal,
            "signal_name": signal_name,
            "strength": candidate["strength"],
            "filter": combined_filter,
            "stop_loss": stop_anchor + (current_bar.get("atr") or 0) * ATR_STOP_MULTIPLIER,
            "divergence_time": candidate["divergence_time"],
            "signal_ready_time": candidate.get("signal_ready_time", candidate["divergence_time"]),
            "divergence_type": candidate["divergence_type"],
            "structure_factors": structure.get("factors", []) if structure else [],
            "structure_score": int(structure.get("score", 0) if structure else 0),
            "structure_text": structure.get("text", "结构因子不足") if structure else "结构因子不足",
            "confirm_bars": confirm_bars,
            "higher_trend": higher.get("trend", "unknown"),
            "signal_grade": score_info["grade"],
            "signal_score": score_info["score"],
            "signal_score_max": score_info["max_score"],
            "score_text": score_info["text"],
        }

    pending_buy = get_pending_buy_divergence(symbol, interval)
    if pending_buy:
        confirm_bars = bars_waited_since(pending_buy.get("created_close_time"))
        if confirm_bars > CONFIRM_MAX_BARS:
            clear_pending_buy_divergence(symbol, interval)
            return build_buy_response(
                pending_buy, "filtered_buy", "底背驰确认超时", f"超过 {CONFIRM_MAX_BARS} 根K线未确认", confirm_bars
            )
        confirmed, _confirmation_flags = confirmation_passed(
            "long", ma_direction, macd_direction, DEFAULT_CONFIG.confirmation_mode
        )
        if confirmed:
            clear_pending_buy_divergence(symbol, interval)
            trend_blocked, trend_message, higher = check_buy_trend_breaker(symbol, interval)
            if trend_blocked:
                return build_buy_response(
                    pending_buy, "filtered_buy", "底背驰被高周期趋势熔断", trend_message, confirm_bars, higher
                )
            filter_passed, filter_message = check_buy_filter(ready_bars)
            signal_grade = grade_signal(
                "long", pending_buy["strength"], filter_passed, current_bar, higher, trend_blocked
            )
            signal_name = "MACD底背驰确认开多" if filter_passed else "底背驰被买入过滤"
            return build_buy_response(
                pending_buy,
                "long" if filter_passed else "filtered_buy",
                signal_name,
                filter_message,
                confirm_bars,
                higher,
                signal_grade,
            )
        return build_buy_response(
            pending_buy, "filtered_buy", "底背驰等待确认", confirmation_wait_text("long", DEFAULT_CONFIG), confirm_bars
        )

    if bullish_div and bullish_strength >= MIN_DIVERGENCE_STRENGTH and bullish_info:
        divergence_bar = window[bullish_info["index"]]
        ready_index = min(
            int(bullish_info.get("confirm_index", bullish_info["index"]) or bullish_info["index"]), len(window) - 1
        )
        ready_bar = window[ready_index]
        buy_candidate = {
            "strength": bullish_strength,
            "price_low": bullish_info["price_low"],
            "divergence_time": divergence_bar["close_time"],
            "signal_ready_time": ready_bar.get("close_time", ready_bar.get("open_time")),
            "divergence_type": bullish_info.get("type", "macd"),
            "created_close_time": current_bar["close_time"],
        }
        buy_candidate["structure"] = build_chan_structure_context(
            "long", window, price_peaks, price_troughs, macd_values, bullish_info
        )
        confirmed, _confirmation_flags = confirmation_passed(
            "long", ma_direction, macd_direction, DEFAULT_CONFIG.confirmation_mode
        )
        if not confirmed:
            set_pending_buy_divergence(symbol, interval, buy_candidate)
            return build_buy_response(
                buy_candidate, "filtered_buy", "底背驰等待确认", confirmation_wait_text("long", DEFAULT_CONFIG), 0
            )
        trend_blocked, trend_message, higher = check_buy_trend_breaker(symbol, interval)
        if trend_blocked:
            return build_buy_response(buy_candidate, "filtered_buy", "底背驰被高周期趋势熔断", trend_message, 0, higher)
        filter_passed, filter_message = check_buy_filter(ready_bars)
        signal_grade = grade_signal("long", bullish_strength, filter_passed, current_bar, higher, trend_blocked)
        signal_name = "MACD底背驰确认开多" if filter_passed else "底背驰被买入过滤"
        return build_buy_response(
            buy_candidate,
            "long" if filter_passed else "filtered_buy",
            signal_name,
            filter_message,
            0,
            higher,
            signal_grade,
        )

    if bearish_div and bearish_strength >= MIN_DIVERGENCE_STRENGTH and bearish_info:
        divergence_bar = window[bearish_info["index"]]
        ready_index = min(
            int(bearish_info.get("confirm_index", bearish_info["index"]) or bearish_info["index"]), len(window) - 1
        )
        ready_bar = window[ready_index]
        sell_candidate = {
            "strength": bearish_strength,
            "price_high": bearish_info["price_high"],
            "divergence_time": divergence_bar["close_time"],
            "signal_ready_time": ready_bar.get("close_time", ready_bar.get("open_time")),
            "divergence_type": bearish_info.get("type", "macd"),
            "created_close_time": current_bar["close_time"],
        }
        sell_candidate["structure"] = build_chan_structure_context(
            "short", window, price_peaks, price_troughs, macd_values, bearish_info
        )
        confirmed, _confirmation_flags = confirmation_passed(
            "short", ma_direction, macd_direction, DEFAULT_CONFIG.confirmation_mode
        )
        if not confirmed:
            set_pending_sell_divergence(symbol, interval, sell_candidate)
            return build_sell_response(
                sell_candidate, "filtered_sell", "顶背驰等待确认", confirmation_wait_text("short", DEFAULT_CONFIG)
            )

        clear_pending_sell_divergence(symbol, interval)
        trend_blocked, trend_message = check_sell_trend_breaker(symbol, interval)
        if trend_blocked:
            return build_sell_response(sell_candidate, "filtered_sell", "顶背驰被高周期趋势熔断", trend_message)

        filter_passed, filter_message = check_sell_filter(ready_bars)
        signal_name = "MACD顶背驰开空观察" if filter_passed else "顶背驰被卖出过滤"
        return build_sell_response(
            sell_candidate, "short" if filter_passed else "filtered_sell", signal_name, filter_message
        )

    pending_sell = get_pending_sell_divergence(symbol, interval)
    if pending_sell:
        confirm_bars = bars_waited_since(pending_sell.get("created_close_time"))
        if confirm_bars > CONFIRM_MAX_BARS:
            clear_pending_sell_divergence(symbol, interval)
            return build_sell_response(
                pending_sell, "filtered_sell", "顶背驰确认超时", f"超过 {CONFIRM_MAX_BARS} 根K线未确认", confirm_bars
            )
        confirmed, _confirmation_flags = confirmation_passed(
            "short", ma_direction, macd_direction, DEFAULT_CONFIG.confirmation_mode
        )
        if not confirmed:
            return build_sell_response(
                pending_sell,
                "filtered_sell",
                "顶背驰等待确认",
                confirmation_wait_text("short", DEFAULT_CONFIG),
                confirm_bars,
            )

        clear_pending_sell_divergence(symbol, interval)
        trend_blocked, trend_message = check_sell_trend_breaker(symbol, interval)
        if trend_blocked:
            return build_sell_response(
                pending_sell, "filtered_sell", "顶背驰被高周期趋势熔断", trend_message, confirm_bars
            )

        filter_passed, filter_message = check_sell_filter(ready_bars)
        signal_name = "MACD顶背驰开空观察" if filter_passed else "顶背驰被卖出过滤"
        return build_sell_response(
            pending_sell, "short" if filter_passed else "filtered_sell", signal_name, filter_message, confirm_bars
        )

    return {
        **base_signal,
        "signal": "wait",
        "signal_name": "等待MACD背驰",
        "strength": max(bullish_strength, bearish_strength),
    }


def build_wait_signal(symbol: str, interval: str, reason: str) -> dict[str, Any]:
    return {
        "type": "chanlun",
        "symbol": symbol,
        "interval": interval,
        "signal": "wait",
        "signal_name": reason,
        "price": None,
        "td_signals": {},
        "td_summary": "",
        "created_at": time.time(),
    }


def serialize_backtest_groups(group_stats) -> list[dict[str, Any]]:
    """Keep marginal/predeclared group identities without inventing other dimensions."""
    groups = []
    for row in group_stats.head(12).to_dict("records"):
        group = {}
        for key in ("group_dimensions", "group_value", "signal", "trend", "higher_trend",
                    "signal_grade", "score_bucket", "structure_bucket", "divergence_type", "strength_bucket"):
            value = row.get(key)
            if isinstance(value, str) and value:
                group[key] = value
        for key in ("trades", "avg_score", "win_rate", "avg_return", "avg_confirm_bars"):
            value = row.get(key)
            if isinstance(value, (int, float)) and math.isfinite(value):
                group[key] = int(value) if key == "trades" else float(value)
        groups.append(group)
    return groups


def run_signal_backtest_summary(
    symbol: str,
    interval: str,
    limit: int,
    reward_risk: float,
    max_hold_bars: int,
    fee_rate: float,
    stop_mode: str,
) -> dict[str, Any]:
    try:
        import project_signal_backtest
    except ModuleNotFoundError as exc:
        missing = exc.name or "回测依赖"
        return {
            "ok": False,
            "error": f"缺少依赖包 {missing}，请先运行 python3 -m pip install -r requirements.txt",
        }

    runtime = execution_settings(RUNTIME_SETTINGS.get())
    bars = project_signal_backtest.fetch_exchange_klines("binance_usdm", symbol, interval, limit)
    enriched_bars = project_signal_backtest.fetch_higher_timeframe_context(
        symbol,
        project_signal_backtest.calculate_indicators(bars),
        interval,
        exchange="binance_usdm",
    )
    stop_modes = ["structure_atr", "atr_trailing_after_1r"] if stop_mode == "all" else [stop_mode]
    mode_results = [
        project_signal_backtest.build_stop_mode_result(
            enriched_bars,
            reward_risk,
            max_hold_bars,
            fee_rate,
            mode,
            strategy_config=StrategyConfig(**runtime["effective_strategy"]),
            return_model=LINEAR_USDM_V1,
            legacy_holding_limit=False,
            min_signal_score=runtime["values"]["paper"]["min_signal_score"],
            signal_direction=runtime["values"]["paper"]["signal_direction"],
        )
        for mode in stop_modes
    ]
    primary = mode_results[0]
    execution_metadata = primary["execution_metadata"]
    trades = primary["trades"]
    metrics = primary["metrics"]
    group_stats = primary["group_stats"]
    started_at = enriched_bars[0]["time"].isoformat() if enriched_bars else None
    ended_at = enriched_bars[-1]["time"].isoformat() if enriched_bars else None

    groups = serialize_backtest_groups(group_stats)

    return RESEARCH_ARCHIVE.save(
        {
            "ok": True,
            "market_data_source": "binance_usdm",
            "config_revision": runtime["config_revision"],
            "config_engine_version": runtime["config_engine_version"],
            "engine_version": runtime["engine_version"],
            "execution_revision": runtime["execution_revision"],
            "runtime_config": runtime,
            "symbol": symbol,
            "interval": interval,
            "limit": len(enriched_bars),
            "started_at": started_at,
            "ended_at": ended_at,
            "execution_metadata": execution_metadata,
            "params": {
                "reward_risk": reward_risk,
                "max_hold_bars": max_hold_bars,
                "fee_rate": fee_rate,
                "stop_mode": stop_mode,
                "return_model": execution_metadata["return_model"],
                "holding_limit_convention": execution_metadata["holding_limit_convention"],
                "max_observed_bars": execution_metadata["max_observed_bars"],
            },
            "metrics": metrics,
            "stop_mode_results": [
                {
                    "stop_mode": result["stop_mode"],
                    "metrics": result["metrics"],
                    "execution_metadata": result["execution_metadata"],
                }
                for result in mode_results
            ],
            "groups": groups,
            "sample_trades": [
                {
                    "signal": trade.signal,
                    "stop_mode": trade.stop_mode,
                    "entry_time": trade.entry_time.isoformat(),
                    "entry_price": trade.entry_price,
                    "exit_reason": trade.exit_reason,
                    "outcome": trade.outcome,
                    "return_pct": trade.return_pct,
                    "return_model": trade.return_model,
                    "bars_held": trade.bars_held,
                    "confirm_bars": trade.confirm_bars,
                    "strength": trade.strength,
                    "trend": trade.trend,
                    "higher_trend": trade.higher_trend,
                    "signal_grade": trade.signal_grade,
                    "divergence_type": trade.divergence_type,
                }
                for trade in trades[-10:]
            ],
        }
    )


def paper_universe_snapshot():
    pool = PAPER_UNIVERSE.status()
    with state_lock:
        scan = dict(state.paper_pool_scan)
    return {**pool, "enabled": PAPER_UNIVERSE_ENABLED, "interval": PAPER_UNIVERSE_INTERVAL, "scan": scan}


async def scan_paper_universe_once(stop_event=None):
    from data_client import interval_ms

    if not PAPER_UNIVERSE_ENABLED:
        return
    await asyncio.to_thread(PAPER_UNIVERSE.refresh, lambda: fetch_binance_paper_universe(
        PAPER_UNIVERSE.top_n, PAPER_UNIVERSE.selection))
    pool = PAPER_UNIVERSE.status()
    with state_lock:
        state.module_errors["paper_universe"] = None if pool["ready"] else (pool["error"] or "市值币池尚未就绪")
    if not pool["ready"]:
        return
    width = interval_ms(PAPER_UNIVERSE_INTERVAL)
    closed_at = int(time.time() * 1000 // width) * width - 1
    members = {item["symbol"]: item for item in pool["members"]}
    with state_lock:
        state.paper_pool_results = {key: value for key, value in state.paper_pool_results.items() if key in members}
        pending = [symbol for symbol in members if state.paper_pool_results.get(symbol, {}).get("kline_close_time", 0) < closed_at]
        state.paper_pool_scan = {"running": True, "started_at": time.time(), "eligible_count": len(members),
                                 "checked_count": len(members) - len(pending), "errors": {}, "bar_close_time": closed_at}
    semaphore = asyncio.Semaphore(PAPER_UNIVERSE_WORKERS)

    async def scan(symbol):
        async with semaphore:
            if stop_event and stop_event.is_set():
                return
            try:
                signal = await asyncio.to_thread(detect_project_signal, symbol, PAPER_UNIVERSE_INTERVAL,
                                                 engine_namespace="paper_pool", include_td=False)
                if (signal.get("kline_close_time") or 0) < closed_at:
                    raise ValueError("合约K线尚未更新到本轮已收盘时间")
                if stop_event and stop_event.is_set():
                    return
                event_key = "paper:" + strategy_trade_id(signal)
                signal = {**signal, "paper_universe_snapshot_id": pool["snapshot_id"], "event_key": event_key}
                if RECORD_STRATEGY_TRADES and register_strategy_signal(signal):
                    record_event(signal, event_key)
                with state_lock:
                    state.paper_pool_results[symbol] = signal
                    state.paper_pool_scan["checked_count"] += 1
            except Exception as exc:
                with state_lock:
                    state.paper_pool_scan["errors"][symbol] = type(exc).__name__

    await asyncio.gather(*(scan(symbol) for symbol in pending))
    with state_lock:
        state.paper_pool_scan.update(running=False, last_scan_at=time.time())
        failures = len(state.paper_pool_scan["errors"])
        state.module_errors["paper_pool"] = f"模拟币池 {failures} 个品种扫描失败" if failures else None


async def paper_universe_loop(stop_event):
    while not stop_event.is_set():
        try:
            await scan_paper_universe_once(stop_event)
        except Exception as exc:
            with state_lock:
                state.module_errors["paper_pool"] = f"模拟币池扫描异常（{type(exc).__name__}）"
                state.paper_pool_scan.update(running=False)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except TimeoutError:
            pass


def publish_monitor_faults() -> None:
    """Count completed monitor rounds, independently of notification polling."""
    with state_lock:
        details = list(state.paper_data_details)
        raw_error = state.module_errors.get("paper_data")
    tracking = {}
    journal_error = None
    if FAULT_TRACKER is not None:
        try:
            tracking = FAULT_TRACKER.observe(
                "paper_data", details or ([{"kind": "invalid_data"}] if raw_error else []),
                observation_id=str(time.monotonic_ns()),
            )
            journal_error = tracking.get("journal_error")
        except (OSError, ValueError, TypeError) as error:
            journal_error = type(error).__name__
    with state_lock:
        state.paper_data_fault = tracking
        # A failed journal remains visible and never suppresses a real outage.
        if FAULT_TRACKER is not None:
            state.module_errors["fault_history"] = (
                f"故障记录不可用（{journal_error}）" if journal_error else None
            )
        state.published_fault = (
            "、".join(
                [name for name, error in (("行情", state.last_error), ("市场摘要", state.summary_error)) if error]
                + [MODULE_FAULT_LABELS.get(module, f"监控模块({module})")
                   for module, error in state.module_errors.items()
                   if error and (module != "paper_data" or not tracking
                                 or journal_error or tracking.get("alert_active"))]
            ) or None
        )


async def monitor_loop_async(stop_event: asyncio.Event | None = None) -> None:
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        with state_lock:
            watchlist = list(state.watchlist)
            tracked_symbols = {trade["symbol"] for trade in state.strategy_trades if trade.get("status") == "open"}
            state.observation_round = {"id": uuid.uuid4().hex, "created_at": time.time(), "events": []}
        symbols = sorted({item["symbol"] for item in watchlist} | set(HOURLY_SUMMARY_SYMBOLS) | tracked_symbols)
        observation_complete = False
        try:
            prices = await async_fetch_tickers(symbols)
            with state_lock:
                state.prices = prices
                state.updated_at = time.time()
                state.last_error = None
            await update_strategy_trades_with_prices_async(prices)
            await evaluate_support_alerts_async(watchlist, prices)
            await evaluate_chanlun_signals_async(watchlist)
            await evaluate_indicator_signals_async(watchlist)
            with state_lock:
                observation_complete = not any(state.module_errors.get(name) for name in ("support", "indicators"))
                state.signal_error = (
                    "; ".join(f"{module}: {error}" for module, error in state.module_errors.items() if error) or None
                )
            await send_hourly_market_summary_async(prices)
        except (
            http.client.IncompleteRead,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
        ) as exc:
            with state_lock:
                state.last_error = str(exc) or f"行情监控异常（{type(exc).__name__}）"
                if tracked_symbols:
                    state.module_errors["paper_data"] = f"本轮持仓行情未完成更新（{type(exc).__name__}）"
                    state.paper_data_details = [{
                        "symbol": symbol, "kind": "market_unavailable", "error_type": type(exc).__name__,
                    } for symbol in sorted(tracked_symbols)]
        finally:
            await finish_observation_round_async(observation_complete)
        try:
            await maybe_run_site_monitor_async()
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
            with state_lock:
                previous = dict(state.site_monitor)
                failures = int(previous.get("task_failures") or 0) + 1
                state.site_monitor = {
                    **previous,
                    "enabled": site_monitor_enabled(),
                    "ok": False,
                    "last_error": str(exc) or f"巡检任务异常（{type(exc).__name__}）",
                    "last_run_at": time.time(),
                    "task_failures": failures,
                    "alert_active": failures >= site_monitor_failure_threshold(),
                }
        await asyncio.to_thread(publish_monitor_faults)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=10)
        except TimeoutError:
            pass


async def run_monitor_loop(stop_event: asyncio.Event) -> None:
    try:
        await monitor_loop_async(stop_event)
    finally:
        await close_http_session()


async def evaluate_chanlun_signals_async(watchlist: list[dict[str, Any]]) -> None:
    next_signals = {}
    signal_error = None
    signal_items = [item for item in watchlist if item.get("signal", True)]

    async def load_signal(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
        try:
            signal = await asyncio.to_thread(
                detect_project_signal, item["symbol"], item.get("interval", DEFAULT_SIGNAL_INTERVAL)
            )
            return item, signal, None
        except (
            http.client.IncompleteRead,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
        ) as exc:
            return item, None, f"{item['symbol']}: {type(exc).__name__}: {exc}"

    loaded = await asyncio.gather(*(load_signal(item) for item in signal_items)) if signal_items else []

    for item, signal, error in loaded:
        if error:
            signal_error = f"{signal_error}; {error}" if signal_error else error
            continue
        if signal is None:
            continue

        symbol = item["symbol"]
        interval = item.get("interval", DEFAULT_SIGNAL_INTERVAL)
        state_key = f"{symbol}:{interval}"
        next_signals[state_key] = signal
        if signal.get("signal") not in {"long", "short"}:
            continue
        if os.getenv("SIGNAL_TRACE", "").strip().lower() in {"1", "true", "yes", "on"}:
            print(f"2. 主程序已捕获信号: {signal}", flush=True)

        alert_key = f"macd_td:{symbol}:{interval}:{signal['signal']}:{signal.get('divergence_time')}"
        with state_lock:
            already_sent = alert_key in state.sent_alerts
        if already_sent:
            continue

        message = {**signal, "email": item.get("email", "")}
        record_event(message, alert_key)
        if RECORD_STRATEGY_TRADES:
            register_strategy_signal({**message, "event_key": alert_key})
        await send_alert_notifications_async(message)

    with state_lock:
        state.signals = next_signals
        state.module_errors["strategy"] = signal_error


async def evaluate_indicator_signals_async(watchlist: list[dict[str, Any]]) -> None:
    next_indicator_signals: dict[str, dict[str, Any]] = {}
    indicator_error = None
    symbols = sorted(
        {item["symbol"] for item in watchlist if item.get("signal", True) or item.get("indicator_alert", True)}
    )
    email_by_symbol = {
        item["symbol"]: item.get("email", "")
        for item in watchlist
        if item.get("signal", True) or item.get("indicator_alert", True)
    }
    ma_alert_symbols = {item["symbol"] for item in watchlist if item.get("signal", True)}
    macd_alert_symbols = {item["symbol"] for item in watchlist if item.get("indicator_alert", True)}

    tasks = [(symbol, interval) for symbol in symbols for interval in INDICATOR_INTERVALS]

    async def load_signal(symbol: str, interval: str) -> tuple[tuple[str, str], dict[str, Any] | None, str | None]:
        try:
            return (symbol, interval), await detect_indicator_signal_async(symbol, interval), None
        except (
            http.client.IncompleteRead,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            OSError,
        ) as exc:
            return (symbol, interval), None, f"{symbol}:{interval}: {type(exc).__name__}"

    loaded = await asyncio.gather(*(load_signal(symbol, interval) for symbol, interval in tasks)) if tasks else []
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for key, signal, error in loaded:
        if error is not None:
            indicator_error = error
            continue
        if signal is not None:
            results[key] = signal

    for symbol in symbols:
        symbol_signals = {}
        for interval in INDICATOR_INTERVALS:
            signal = results.get((symbol, interval))
            if signal is None:
                continue

            symbol_signals[interval] = signal
            for indicator_name in ("ma", "macd"):
                if indicator_name == "ma" and symbol not in ma_alert_symbols:
                    continue
                if indicator_name == "macd" and (symbol not in macd_alert_symbols or interval != "4h"):
                    continue
                indicator = signal.get(indicator_name, {})
                direction = indicator.get("direction")
                if direction not in {"up", "down"}:
                    continue

                alert_key = (
                    f"indicator:{symbol}:{interval}:{indicator_name}:{direction}:{signal.get('kline_close_time')}"
                )
                with state_lock:
                    already_sent = alert_key in state.sent_alerts or not state.observation_checkpoint_ok
                if already_sent:
                    continue

                message = {
                    **signal,
                    "indicator": indicator_name,
                    "indicator_name": "均线" if indicator_name == "ma" else "MACD",
                    "indicator_label": indicator.get("label", ""),
                    "direction": direction,
                    "email": email_by_symbol.get(symbol, ""),
                }
                record_event(message, alert_key)
                with state_lock:
                    state.indicator_alert_keys[f"{symbol}:{interval}:{indicator_name}"] = alert_key
                await send_alert_notifications_async(message)

        next_indicator_signals[symbol] = symbol_signals

    with state_lock:
        state.indicator_signals = next_indicator_signals
        state.module_errors["indicators"] = indicator_error
    save_observation_checkpoint()


async def evaluate_support_alerts_async(watchlist: list[dict[str, Any]], prices: dict[str, dict[str, Any]]) -> None:
    next_support_levels: dict[str, dict[str, Any]] = {}
    support_error = None
    tasks = [
        (item, interval, prices.get(item["symbol"], {}).get("lastPrice"))
        for item in watchlist
        for interval in SUPPORT_INTERVALS
    ]

    async def load_level(
        item: dict[str, Any], interval: str, price: float | None
    ) -> tuple[tuple[str, str], dict[str, Any] | None, str | None]:
        try:
            return (item["symbol"], interval), await detect_support_level_async(item["symbol"], interval, price), None
        except (
            http.client.IncompleteRead,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            OSError,
        ) as exc:
            return (item["symbol"], interval), None, f"{item['symbol']}:{interval}: {type(exc).__name__}"

    loaded = (
        await asyncio.gather(*(load_level(item, interval, price) for item, interval, price in tasks)) if tasks else []
    )
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for key, level, error in loaded:
        if error is not None:
            support_error = error
            continue
        if level is not None:
            results[key] = level

    for item in watchlist:
        symbol = item["symbol"]
        symbol_levels = {}
        for interval in SUPPORT_INTERVALS:
            level = results.get((symbol, interval))
            if level is None:
                continue
            symbol_levels[interval] = level
            structure_key = f"{symbol}:{interval}"
            if not item.get("support_alert", True):
                continue
            with state_lock:
                if not state.observation_checkpoint_ok:
                    continue
                previous = state.support_alert_states.get(structure_key)
                current, messages = advance_support(previous, level)
                if current is not None:
                    state.support_alert_states[structure_key] = current
            for message in messages:
                message = {**message, "email": item.get("email", "")}
                alert_key = (f"support:{symbol}:{interval}:{message['support_transition']}:"
                             f"{level.get('kline_close_time')}:{uuid.uuid4().hex}")
                record_event(message, alert_key)
                await send_alert_notifications_async(message)

        next_support_levels[symbol] = symbol_levels

    with state_lock:
        state.support_levels = next_support_levels
        state.module_errors["support"] = support_error
    save_observation_checkpoint()


async def send_hourly_market_summary_async(prices: dict[str, dict[str, Any]]) -> None:
    if not discord_webhook_url():
        return

    now = time.time()
    with state_lock:
        last_summary_at = state.last_summary_at
        if last_summary_at and now - last_summary_at < HOURLY_SUMMARY_INTERVAL_SECONDS:
            return
        state.last_summary_at = now

    try:
        lines = ["**Binance 每小时行情简报**"]
        missing_symbols = [symbol for symbol in HOURLY_SUMMARY_SYMBOLS if symbol not in prices]
        fallback_prices = await async_fetch_tickers(missing_symbols) if missing_symbols else {}
        funding_by_symbol = dict(
            await asyncio.gather(*(_load_funding_summary(symbol) for symbol in HOURLY_SUMMARY_SYMBOLS))
        )
        for symbol in HOURLY_SUMMARY_SYMBOLS:
            ticker = prices.get(symbol) or fallback_prices.get(symbol, {})
            funding = funding_by_symbol.get(symbol, {})
            lines.append(format_market_summary_line(symbol, ticker, funding))

        await asyncio.to_thread(send_discord_message, "\n".join(lines))
        with state_lock:
            state.summary_error = None
    except (
        http.client.IncompleteRead,
        OSError,
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        KeyError,
        RuntimeError,
    ) as exc:
        with state_lock:
            state.summary_error = str(exc)
            state.last_summary_at = None


async def _load_funding_summary(symbol: str) -> tuple[str, dict[str, Any]]:
    return symbol, await async_fetch_funding_rate(symbol)


def format_market_summary_line(symbol: str, ticker: dict[str, Any], funding: dict[str, Any]) -> str:
    price = ticker.get("lastPrice")
    price_change = ticker.get("priceChange")
    price_change_percent = ticker.get("priceChangePercent")
    funding_rate = funding.get("lastFundingRate")
    next_funding_time = funding.get("nextFundingTime")

    price_text = format_decimal(price)
    change_text = format_signed_decimal(price_change)
    percent_text = format_signed_percent(price_change_percent)
    funding_text = format_percent(funding_rate)
    next_funding_text = format_epoch_ms(next_funding_time)

    return (
        f"- {symbol}: 最新价 {price_text}，24h {change_text} ({percent_text})，"
        f"资金费率 {funding_text}，下次资金费 {next_funding_text}"
    )


def format_decimal(value: Any, digits: int = 8) -> str:
    number = parse_float(value)
    if number is None:
        return "--"
    return f"{number:,.{digits}f}".rstrip("0").rstrip(".")


def format_signed_decimal(value: Any, digits: int = 8) -> str:
    number = parse_float(value)
    if number is None:
        return "--"
    sign = "+" if number > 0 else ""
    return f"{sign}{format_decimal(number, digits)}"


def format_percent(value: Any, digits: int = 4) -> str:
    number = parse_float(value)
    if number is None:
        return "--"
    return f"{number * 100:.{digits}f}%"


def format_signed_percent(value: Any, digits: int = 2) -> str:
    number = parse_float(value)
    if number is None:
        return "--"
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.{digits}f}%"


def format_epoch_ms(value: Any) -> str:
    try:
        timestamp = int(value) / 1000
    except (TypeError, ValueError):
        return "--"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def send_alert_notifications(alert: dict[str, Any]) -> None:
    send_discord_alert(alert)


async def send_alert_notifications_async(alert: dict[str, Any]) -> None:
    if alert.get("type") in {"indicator", "support"}:
        with state_lock:
            if state.observation_round is not None:
                return
    await asyncio.to_thread(send_alert_notifications, alert)


def format_alert_text(alert: dict[str, Any]) -> tuple[str, str]:
    if alert.get("type") == "observation_summary":
        return alert["title"], alert["body"]
    if alert.get("type") == "site_monitor":
        check = alert.get("check") or "site monitor"
        status = alert.get("status")
        if status == "recovered":
            title = f"站点监控恢复：{check}"
            body = f"{check} 已恢复正常。"
            if alert.get("url"):
                body += f"\n目标：{alert['url']}"
            return title, body

        title = f"站点监控告警：{check}"
        body_lines = [f"{check} 连续失败 {alert.get('consecutive_failures', '--')} 次。"]
        if alert.get("url"):
            body_lines.append(f"目标：{alert['url']}")
        if alert.get("status_code") is not None:
            body_lines.append(f"HTTP 状态：{alert['status_code']}")
        if alert.get("error"):
            body_lines.append(f"错误：{alert['error']}")
        body_lines.append("该提醒来自自用站点监控探针。")
        return title, "\n".join(body_lines)

    if alert.get("type") == "support":
        title = f"{alert['symbol']} {alert.get('interval', '')} 支撑提醒"
        action = {"broken": "报价跌破原支撑区", "retest": "再次接近支撑", "level_changed": "接近新支撑"}.get(
            alert.get("support_transition"), "接近支撑"
        )
        body = (
            f"{alert['symbol']} {alert.get('interval', '')} {action} {format_decimal(alert.get('support'))}，"
            f"当前价 {format_decimal(alert.get('price'))}，"
            f"距离 {format_signed_percent(alert.get('distance_pct'))}，"
            f"容差 {format_decimal(alert.get('tolerance_pct'), 2)}%。\n\n"
            "该提醒来自本地币种监控面板。"
        )
        return title, body

    if alert.get("type") == "indicator":
        title = f"{alert['symbol']} {alert.get('interval', '')} 指标提醒"
        body = (
            f"{alert['symbol']} {alert.get('interval', '')} 触发{alert.get('indicator_label', alert.get('indicator_name', '指标'))}，"
            f"参考价 {alert['price']:.8g}。\n\n"
            "该提醒来自本地币种监控面板。"
        )
        return title, body

    is_chanlun = alert.get("type") == "chanlun"
    title = f"{alert['symbol']} 缠论信号提醒" if is_chanlun else f"{alert['symbol']} 价格提醒"

    if is_chanlun:
        strength = alert.get("strength")
        strength_text = f"，强度 {strength:.2f}" if isinstance(strength, (int, float)) else ""
        stop_loss = alert.get("stop_loss")
        stop_text = f"，参考止损 {stop_loss:.8g}" if isinstance(stop_loss, (int, float)) else ""
        filter_text = f"\n过滤条件：{alert['filter']}" if alert.get("filter") else ""
        td_text = f"\nTD9：{alert['td_summary']}" if alert.get("td_summary") else ""
        body = (
            f"{alert['symbol']} {alert.get('interval', DEFAULT_SIGNAL_INTERVAL)} 出现{alert['signal_name']}，"
            f"参考价 {alert['price']:.8g}{strength_text}{stop_text}。"
            f"{filter_text}{td_text}\n\n"
            "该提醒来自本地缠论币种监控面板。"
        )
        return title, body

    direction_text = "高于" if alert.get("direction") == "above" else "低于"
    body = (
        f"{alert['symbol']} 当前价格 {alert['price']:.8g} 已{direction_text} "
        f"阈值 {alert['threshold']:.8g}。\n\n"
        "该提醒来自本地币种监控面板。"
    )
    return title, body


def send_email_alert(alert: dict[str, Any]) -> None:
    recipient = alert.get("email")
    if not recipient:
        return

    host = os.getenv("SMTP_HOST")
    username = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM", username or "")
    port = int(os.getenv("SMTP_PORT", "587"))
    use_ssl = os.getenv("SMTP_SSL", "false").lower() in {"1", "true", "yes"}
    if not host or not username or not password or not sender:
        record_email_error(alert, "SMTP is not configured")
        return

    title, body = format_alert_text(alert)
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as smtp:
                smtp.login(username, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(username, password)
                smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        record_email_error(alert, str(exc))


def send_discord_message(content: str) -> None:
    webhook_url = discord_webhook_url()
    if not webhook_url:
        return

    try:
        post_discord(webhook_url, content[:2000], timeout=15)
        with state_lock:
            state.discord_last_ok_at = time.time()
            state.discord_last_error = None
    except (OSError, urllib.error.URLError) as exc:
        with state_lock:
            state.discord_last_error = str(exc)
        raise exc


def send_discord_alert(alert: dict[str, Any]) -> None:
    webhook_url = discord_webhook_url()
    if not webhook_url:
        return

    title, body = format_alert_text(alert)
    try:
        send_discord_message(f"**{title}**\n{body}")
    except (OSError, urllib.error.URLError) as exc:
        record_discord_error(alert, str(exc))


def record_email_error(alert: dict[str, Any], error: str) -> None:
    record_event(
        {
            **alert,
            "email_error": error,
            "created_at": time.time(),
        }
    )


def record_discord_error(alert: dict[str, Any], error: str) -> None:
    record_event(
        {
            **alert,
            "discord_error": error,
            "created_at": time.time(),
        }
    )


def snapshot() -> dict[str, Any]:
    discord_configured = bool(discord_webhook_url())
    site_monitor = site_monitor_snapshot()
    try:
        runtime = RUNTIME_SETTINGS.get()
        settings_error = None
    except ValueError as exc:
        runtime = None
        settings_error = str(exc)
    with state_lock:
        discord_running = discord_configured and state.discord_last_error is None
        discord_status = {
            "configured": discord_configured,
            "running": discord_running,
            "last_ok_at": state.discord_last_ok_at,
            "last_error": state.discord_last_error,
            "label": "运行中" if discord_running else "未配置" if not discord_configured else "异常",
        }
        return {
            "watchlist": state.watchlist,
            "prices": state.prices,
            "signals": state.signals,
            "indicator_signals": state.indicator_signals,
            "support_levels": state.support_levels,
            "events": state.events,
            "strategy_stats": state.strategy_stats,
            "strategy_trades": [
                dict(trade)
                for trade in sorted(
                    state.strategy_trades,
                    key=lambda trade: parse_float(trade.get("closed_at")) or parse_float(trade.get("opened_at")) or 0,
                    reverse=True,
                )[:20]
            ],
            "updated_at": state.updated_at,
            "last_error": settings_error or state.last_error or state.signal_error or state.summary_error,
            "runtime_settings": runtime,
            "module_errors": dict(state.module_errors),
            "paper_data_fault": dict(state.paper_data_fault),
            "last_summary_at": state.last_summary_at,
            "smtp_configured": False,
            "discord_configured": discord_configured,
            "discord_status": discord_status,
            "notification_configured": discord_configured,
            "market_data_source": market_data_source(),
            "signal_tracking": {
                "mode": "paper",
                "enabled": bool(runtime and runtime["values"]["paper"]["enabled"]),
                "positions": [dict(trade) for trade in state.strategy_trades if trade.get("status") == "open"],
                "updated_at": state.updated_at,
            },
            "site_monitor": site_monitor,
        }


def initialize_state() -> None:
    PUBLIC_DIR.mkdir(exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    all_events = load_all_event_history()
    # Existing ledgers resume, but old alerts are not invented as new live tracks.
    try:
        strategy_trades = load_strategy_trades()
    except ValueError:
        with state_lock:
            state.module_errors["ledger"] = "策略交易账本损坏或不可读，已停止写账及新信号"
        raise
    with state_lock:
        state.watchlist = load_watchlist()
        display_events = [event for event in all_events if event.get("type") != "observation_batch"]
        state.events = list(reversed(display_events[-50:]))
        state.sent_alerts = {event["event_key"] for event in display_events if isinstance(event.get("event_key"), str)}
        state.strategy_trades = strategy_trades
        state.strategy_stats = calculate_strategy_stats(strategy_trades)
    with state_lock:
        state.module_errors["ledger"] = None
    restore_observation_checkpoint(all_events)


def initialize_fault_history() -> None:
    global FAULT_TRACKER
    path = Path(os.getenv("MONITOR_FAULT_FILE", str(STRATEGY_TRADES_FILE.parent / "monitor-faults.json")))
    try:
        FAULT_TRACKER = FaultTracker(path)
        restored = FAULT_TRACKER.summary()
        paper_fault = restored["modules"].get("paper_data", {})
        with state_lock:
            state.paper_data_fault = paper_fault
            state.module_errors["fault_history"] = (
                "故障记录仍有未恢复的写入错误" if restored.get("journal_error") else None
            )
            if paper_fault.get("active"):
                state.paper_data_details = paper_fault["details"]
                state.module_errors["paper_data"] = "重启前持仓行情异常，等待新监控轮确认"
            if paper_fault.get("armed"):
                # Startup is not recovery evidence; retain an announced fault
                # until a new completed monitor observation replaces it.
                names = set((state.published_fault or "").split("、")) - {""}
                state.published_fault = "、".join(sorted(names | {MODULE_FAULT_LABELS["paper_data"]}))
    except (OSError, ValueError, TypeError) as error:
        FAULT_TRACKER = None
        with state_lock:
            state.module_errors["fault_history"] = f"故障记录初始化失败（{type(error).__name__}）"


def fault_diagnostics() -> dict[str, Any]:
    """Safe current/recent paper failures without credentials or raw exceptions."""
    with state_lock:
        error = state.module_errors.get("fault_history")
    if FAULT_TRACKER is None:
        return {"available": False, "error": error, "modules": {}, "recent_events": []}
    events = FAULT_TRACKER.events(limit=50)
    return {"available": True, **FAULT_TRACKER.summary(), "recent_events": events,
            "quote_max_age_ms": PAPER_QUOTE_MAX_AGE_MS}


async def state_event_stream(detail: PayloadDetail = "full", delta: bool = False):
    last_data = None
    last_state = None
    sequence = 0
    last_frame_at = time.monotonic()
    while True:
        current_state = project_state(snapshot(), detail)
        data = json.dumps(current_state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        now = time.monotonic()
        if data != last_data:
            if delta:
                if last_state is None:
                    frame = {"protocol": "lianqi-delta-v1", "type": "snapshot", "seq": 0, "state": current_state}
                else:
                    sequence += 1
                    changed = {key: value for key, value in current_state.items()
                               if key not in last_state or json.dumps(value, ensure_ascii=False, sort_keys=True,
                                                                      separators=(",", ":")) != last_state[key]}
                    frame = {"protocol": "lianqi-delta-v1", "type": "patch", "seq": sequence,
                             "set": changed, "remove": sorted(last_state.keys() - current_state.keys())}
                last_state = {key: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                              for key, value in current_state.items()}
                outgoing = json.dumps(frame, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            else:
                outgoing = data
            last_data = data
            last_frame_at = now
            yield f"data: {outgoing}\n\n"
        elif now - last_frame_at >= 15:
            last_frame_at = now
            yield ": heartbeat\n\n"
        await asyncio.sleep(3)


def create_app():
    if FastAPI is None or StreamingResponse is None or StaticFiles is None or CORSMiddleware is None:
        raise RuntimeError("FastAPI runtime is not installed; run python3 -m pip install -r requirements.txt")

    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    research_jobs = BoundedResearchJobs()

    @asynccontextmanager
    async def lifespan(_app):
        initialize_state()
        initialize_fault_history()
        monitor_stop_event = asyncio.Event()
        automatic_task = None
        automatic_lease = None
        automatic_notification_status.update(enabled=env_bool("FEISHU_AUTO_ENABLED", False), running=False)
        try:
            outbox_path = os.getenv("FEISHU_OUTBOX_FILE", str(ROOT / "runtime" / "feishu-outbox.json"))
            automatic_lease = SenderLease(outbox_path)
            box = Outbox(outbox_path, min_send_interval_s=NOTIFICATION_MIN_SEND_INTERVAL_S)
            box.observe(*automatic_notification_inputs())
            commit_observation_rounds()
            box.observe(*automatic_notification_inputs())
            automatic_task = asyncio.create_task(
                automatic_notification_loop(monitor_stop_event, box), name="feishu-notifications"
            )
        except Exception as exc:
            if automatic_lease:
                automatic_lease.close()
            automatic_notification_status["error"] = f"通知初始化失败（{type(exc).__name__}）"
        monitor_task = asyncio.create_task(run_monitor_loop(monitor_stop_event), name="market-monitor")
        paper_task = asyncio.create_task(paper_universe_loop(monitor_stop_event), name="paper-universe") if PAPER_UNIVERSE_ENABLED else None
        try:
            yield
        finally:
            research_jobs.close()
            monitor_stop_event.set()
            if paper_task:
                try:
                    await asyncio.wait_for(paper_task, timeout=20)
                except TimeoutError:
                    paper_task.cancel()
                    await asyncio.gather(paper_task, return_exceptions=True)
            if automatic_task:
                try:
                    await automatic_task
                finally:
                    if automatic_lease:
                        automatic_lease.close()
            try:
                await asyncio.wait_for(monitor_task, timeout=25)
            except TimeoutError:
                monitor_task.cancel()
                await asyncio.gather(monitor_task, return_exceptions=True)
            await close_http_session()

    app = FastAPI(title="Chanlun Crypto Monitor", lifespan=lifespan)
    app.state.research_jobs = research_jobs
    register_jindan_routes(app)

    @app.middleware("http")
    async def local_admin_only(request: Request, call_next):
        private_paths = {"/api/settings", "/api/notifications/config", "/api/notifications/test"}
        if (
            env_bool("ADMIN_LOCAL_ONLY", False)
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.url.path in private_paths
            and (request.client is None or request.client.host not in {"127.0.0.1", "::1"})
        ):
            return JSONResponse({"detail": "运行配置仅允许服务器本机维护"}, status_code=403)
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(WebsiteGZipMiddleware)

    @app.get("/api/state")
    async def api_state(detail: PayloadDetail = "full") -> dict[str, Any]:
        return project_state(snapshot(), detail)

    @app.get("/api/diagnostics/faults")
    async def api_fault_diagnostics() -> dict[str, Any]:
        return await asyncio.to_thread(fault_diagnostics)

    @app.get("/api/health")
    async def api_health() -> dict[str, Any]:
        discord_url = discord_webhook_url()
        site_monitor = site_monitor_snapshot()
        with state_lock:
            discord_last_ok_at = state.discord_last_ok_at
            discord_last_error = state.discord_last_error
        return {
            "ok": True,
            "updated_at": time.time(),
            "started_at": STARTED_AT,
            "uptime_seconds": round(time.time() - STARTED_AT, 3),
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "code_fingerprint": code_fingerprint(),
            "market_data_source": market_data_source(),
            "discord_configured": bool(discord_url),
            "discord_env_file_exists": ENV_FILE.exists(),
            "discord_env_file_has_key": env_file_has_key("DISCORD_WEBHOOK_URL"),
            "discord_value_length": len(discord_url),
            "discord_last_ok_at": discord_last_ok_at,
            "discord_last_error": discord_last_error,
            "reports_dir_exists": REPORTS_DIR.exists(),
            "report_count": len(list_report_files()),
            "execution_mode": "research_only",
            "signal_tracking_enabled": RECORD_STRATEGY_TRADES,
            "site_monitor_enabled": site_monitor["enabled"],
            "site_monitor_ok": site_monitor["ok"],
            "site_monitor_last_run_at": site_monitor["last_run_at"],
            "site_monitor_failed_count": len([item for item in site_monitor["results"] if not item.get("ok")]),
        }

    @app.get("/api/reports")
    async def api_reports() -> dict[str, Any]:
        reports = list_report_files()
        return {
            "count": len(reports),
            "reports": reports,
        }

    @app.get("/api/notifications/status")
    async def notification_status():
        return {
            "channels": feishu_notifications.status(),
            "test_enabled": env_bool("FEISHU_TEST_ENABLED", False),
            "automatic": dict(automatic_notification_status),
        }

    @app.get("/api/binance-demo/status")
    async def binance_demo_status():
        return {"status": "extension_disabled", "mode": "paper", "credentials_required": False}

    notification_test_lock = None
    notification_test_at = 0.0

    @app.post("/api/notifications/test")
    async def notification_test(request: Request):
        nonlocal notification_test_at, notification_test_lock
        if not env_bool("FEISHU_TEST_ENABLED", False):
            raise HTTPException(status_code=403, detail="通知测试未开启")
        if "application/json" not in request.headers.get("content-type", ""):
            raise HTTPException(status_code=415, detail="请使用网站通知测试按钮")
        payload = await request.json()
        channel = payload.get("channel") if isinstance(payload, dict) else None
        if channel not in feishu_notifications.CHANNELS:
            raise HTTPException(status_code=400, detail="未知通知渠道")
        if notification_test_lock is None:
            notification_test_lock = asyncio.Lock()
        async with notification_test_lock:
            if time.monotonic() - notification_test_at < 3:
                raise HTTPException(status_code=429, detail="请等待 3 秒后重试")
            notification_test_at = time.monotonic()
            test_id = f"{channel}-{int(time.time())}"
            site_url = os.getenv("FEISHU_SITE_URL", "").strip()
            link = (
                f"\n返回网站：{site_url}"
                if site_url.startswith("https://")
                else "\n网站仅在本机运行，请在电脑打开 http://localhost:3000/；手机暂不能通过此地址访问。"
            )
            body = f"渠道：{feishu_notifications.CHANNELS[channel]}\n测试编号：{test_id}\n由网站诊断页触发，用于验证通知链路，不是交易信号。{link}"
            result = await asyncio.to_thread(feishu_notifications.send, channel, "通知链路测试", body)
            return {**result, "channel": channel, "test_id": test_id}

    @app.get("/api/site-monitor")
    async def api_site_monitor() -> dict[str, Any]:
        return site_monitor_snapshot()

    @app.get("/api/network-test")
    async def api_network_test(symbol: str = "BTCUSDT") -> dict[str, Any]:
        normalized_symbol = symbol.upper().strip() or "BTCUSDT"
        return await asyncio.to_thread(test_external_dependencies, normalized_symbol, discord_webhook_url())

    @app.put("/api/notifications/config")
    async def save_notification_config(request: Request):
        try:
            payload = await request.json()
            feishu_notifications.save_webhook(payload["channel"], payload["webhook"])
            return {"channels": feishu_notifications.status()}
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="通知配置无效，请检查渠道和飞书地址") from exc

    @app.get("/api/settings")
    async def api_settings():
        try:
            return RUNTIME_SETTINGS.get()
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.put("/api/settings")
    async def save_settings(request: Request):
        try:
            payload = await request.json()
            return RUNTIME_SETTINGS.save(payload["values"], payload["expected_revision"])
        except SettingsConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/paper-trades")
    async def api_paper_trades(detail: PayloadDetail = "full"):
        universe = paper_universe_snapshot()
        with state_lock:
            stats = calculate_strategy_stats(state.strategy_trades)
            mixed = stats["mixed_return_models"]
            comparability_note = stats["comparability_note"]
            if stats["summary_scope"] == "NO_CLOSED_RECORDS":
                comparison_status = "NO_CLOSED_RECORDS"
            elif any(not group["recognized"] for group in stats["return_model_stats"].values()):
                comparison_status = "UNRECOGNIZED_RETURN_MODEL"
            else:
                comparison_status = "MIXED_RETURN_MODELS" if mixed else stats["summary_scope"]
            payload = {
                "mode": "paper",
                "universe": universe,
                "trades": list(state.strategy_trades),
                "stats": stats,
                "comparison_status": comparison_status,
                "comparability_note": comparability_note,
                "retention_note": "展示本地保留的交易记录；收益为逐笔比例，不代表账户净值。" + comparability_note,
            }
        return project_paper_trades(payload, detail)

    @app.get("/api/okx-demo-ledger")
    async def api_okx_demo_ledger():
        return await asyncio.to_thread(load_okx_demo_ledger)

    @app.get("/api/paper-universe")
    async def api_paper_universe():
        return paper_universe_snapshot()

    @app.get("/api/research-runs")
    async def research_runs():
        return {"runs": await asyncio.to_thread(RESEARCH_ARCHIVE.list)}

    @app.get("/api/research-evidence")
    async def research_evidence():
        from research_evidence import list_validation_evidence

        try:
            return await research_jobs.run(list_validation_evidence, REPORTS_DIR)
        except ResearchBusy as exc:
            raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "5"}) from exc

    @app.get("/api/research-runs/{run_id}")
    async def research_run(run_id: str):
        try:
            return await asyncio.to_thread(RESEARCH_ARCHIVE.get, run_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="实验不存在") from exc

    @app.get("/api/backtest")
    async def api_backtest(
        symbol: str = "BTCUSDT",
        interval: str = DEFAULT_SIGNAL_INTERVAL,
        limit: int = 30000,
        reward_risk: float = 2.0,
        max_hold_bars: int = 96,
        fee_rate: float = 0.001,
        stop_mode: str = "structure_atr",
    ) -> dict[str, Any]:
        normalized_symbol = symbol.upper().strip() or "BTCUSDT"
        normalized_interval = normalize_interval(interval)
        normalized_limit = min(max(parse_int(limit, 30000), 300), 50000)
        normalized_reward_risk = min(max(reward_risk or 2.0, 0.3), 5.0)
        normalized_max_hold_bars = min(max(parse_int(max_hold_bars, 96), 1), 2000)
        normalized_fee_rate = min(max(fee_rate, 0.0), 0.02)
        normalized_stop_mode = (
            stop_mode if stop_mode in {"structure_atr", "atr_trailing_after_1r", "all"} else "structure_atr"
        )
        try:
            return await research_jobs.run(
                run_signal_backtest_summary,
                normalized_symbol,
                normalized_interval,
                normalized_limit,
                normalized_reward_risk,
                normalized_max_hold_bars,
                normalized_fee_rate,
                normalized_stop_mode,
            )
        except ResearchBusy as exc:
            raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "5"}) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/events")
    async def api_events(detail: PayloadDetail = "full", delta: bool = False):
        return StreamingResponse(
            state_event_stream(detail, delta=delta),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/watchlist")
    async def api_watchlist(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("watchlist"), list):
                raise HTTPException(status_code=400, detail="watchlist 必须显式提供为数组；清空请提交空数组。")
            items = payload["watchlist"]
            if any(not isinstance(item, dict) or not isinstance(item.get("symbol"), str)
                   or not item["symbol"].strip() for item in items):
                raise HTTPException(status_code=400, detail="每个监控条目必须包含非空字符串 symbol。")
            watchlist = normalize_watchlist(items)
            invalid_symbols = await asyncio.to_thread(invalid_watchlist_symbols, watchlist)
            if invalid_symbols:
                raise HTTPException(status_code=400, detail=f"未知交易对：{', '.join(invalid_symbols[:8])}")
            save_watchlist(watchlist)
            with state_lock:
                state.watchlist = watchlist
                state.sent_alerts.clear()
                state.pending_buy_divergences.clear()
                state.pending_sell_divergences.clear()
                SIGNAL_ENGINES.clear()
            return {"ok": True, "watchlist": watchlist}
        except (json.JSONDecodeError, OSError, AttributeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/")
    async def website_index():
        built = PUBLIC_DIR / "site" / "index.html"
        return FileResponse(built if built.exists() else PUBLIC_DIR / "index.html")

    app.mount("/reports", StaticFiles(directory=str(REPORTS_DIR), html=True), name="reports")
    app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="static")
    return app


def local_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    display_host = local_lan_ip() if host in {"0.0.0.0", "::"} else host
    print(f"Monitoring dashboard: http://{display_host}:{port}")
    if host in {"0.0.0.0", "::"}:
        print("Same Wi-Fi phone URL: use the address above in the phone browser.")
    if uvicorn is None:
        raise RuntimeError("uvicorn is not installed; run python3 -m pip install -r requirements.txt")
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
