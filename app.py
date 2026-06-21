import asyncio
import json
import http.client
import os
import smtplib
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from config import ENV_FILE, config_value, load_config, load_env_file
from data_client import (
    async_fetch_funding_rate,
    async_fetch_open_interest_ratio,
    async_fetch_klines,
    async_fetch_tickers,
    close_http_session,
    fetch_binance_spot_symbols,
    fetch_funding_rate,
    fetch_klines,
    fetch_open_interest_ratio,
    fetch_tickers,
    market_data_source,
    test_external_dependencies,
    test_market_api,
)
from indicators import calculate_indicators, macd, rolling_average
from position_manager import calculate_return_pct, calculate_target_levels, evaluate_bar_exit, evaluate_price_exit
from runtime_utils import append_jsonl, code_fingerprint as build_code_fingerprint, post_discord
from strategy import (
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
    HIGHER_TREND_INTERVAL,
    ProjectSignalEngine,
    score_signal,
    DEFAULT_CONFIG,
)

try:
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse
    from fastapi.staticfiles import StaticFiles
except ModuleNotFoundError:
    uvicorn = None
    FastAPI = None
    HTTPException = None
    Request = None
    CORSMiddleware = None
    StreamingResponse = None
    StaticFiles = None


ROOT = Path(__file__).resolve().parent
CONFIG = load_config()


def config_int(section: str, key: str, default: int, env_name: Optional[str] = None) -> int:
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
OKX_BOT_STATE_FILE = Path(os.getenv("OKX_BOT_STATE_FILE", str(ROOT / "okx_market_cap_bot_state.json"))).expanduser()
OKX_BOT_EVENT_LOG_FILE = Path(os.getenv("OKX_BOT_EVENT_LOG_FILE", str(ROOT / "okx_market_cap_bot_events.jsonl"))).expanduser()
OKX_BOT_LEGACY_STATE_FILE = ROOT / "okx_market_cap_bot_state.json"
OKX_BOT_LEGACY_EVENT_LOG_FILE = ROOT / "okx_market_cap_bot_events.jsonl"
BINANCE_BOT_STATE_FILE = Path(os.getenv("BINANCE_BOT_STATE_FILE", str(ROOT / "binance_strategy_bot_state.json"))).expanduser()
OKX_BOT_SCAN_TIMEOUT_SECONDS = config_int("bot", "scan_timeout_seconds", 300, "BOT_SCAN_TIMEOUT_SECONDS")
OKX_BOT_ERROR_EVENT_TYPES = {"error", "startup_error", "order_error", "scan_error", "scan_timeout", "discord_error"}
OKX_BOT_PRIMARY_ERROR_EVENT_TYPES = OKX_BOT_ERROR_EVENT_TYPES - {"discord_error"}
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
STRATEGY_HISTORY_LIMIT = int(config_value(CONFIG, "app", "strategy_history_limit", 200))
RECORD_STRATEGY_TRADES = bool(config_value(CONFIG, "app", "record_strategy_trades", False))
REPORT_FILE_SUFFIXES = {".html", ".htm"}
SITE_MONITOR_DEFAULT_PORT = os.getenv("PORT", "8080").strip() or "8080"
SITE_MONITOR_DEFAULT_TARGETS = (
    f"health=http://127.0.0.1:{SITE_MONITOR_DEFAULT_PORT}/api/health,"
    f"reports_api=http://127.0.0.1:{SITE_MONITOR_DEFAULT_PORT}/api/reports,"
    f"reports_page=http://127.0.0.1:{SITE_MONITOR_DEFAULT_PORT}/reports.html"
)
SITE_MONITOR_LAST_RESULTS_LIMIT = 20


DEFAULT_WATCHLIST = [
    {"symbol": "BTCUSDT", "email": "", "signal": True, "indicator_alert": True, "support_alert": True, "interval": "15m"},
    {"symbol": "ETHUSDT", "email": "", "signal": True, "indicator_alert": True, "support_alert": True, "interval": "15m"},
]


@dataclass
class MonitorState:
    watchlist: list[dict[str, Any]] = field(default_factory=list)
    prices: dict[str, dict[str, Any]] = field(default_factory=dict)
    signals: dict[str, dict[str, Any]] = field(default_factory=dict)
    indicator_signals: dict[str, dict[str, Any]] = field(default_factory=dict)
    support_levels: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    strategy_trades: list[dict[str, Any]] = field(default_factory=list)
    strategy_stats: dict[str, Any] = field(default_factory=dict)
    pending_buy_divergences: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_sell_divergences: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_error: Optional[str] = None
    signal_error: Optional[str] = None
    summary_error: Optional[str] = None
    discord_last_ok_at: Optional[float] = None
    discord_last_error: Optional[str] = None
    updated_at: Optional[float] = None
    last_summary_at: Optional[float] = None
    sent_alerts: set[str] = field(default_factory=set)
    site_monitor: dict[str, Any] = field(default_factory=dict)


state = MonitorState()
state_lock = threading.Lock()
okx_bot_status_cache_lock = threading.Lock()
SIGNAL_ENGINES: dict[tuple[str, str], ProjectSignalEngine] = {}
_site_monitor_last_run_at: Optional[float] = None
_site_monitor_alert_state: dict[str, dict[str, Any]] = {}
_okx_bot_status_cache: dict[str, Any] = {
    "expires_at": 0.0,
    "signature": None,
    "value": None,
}


def discord_webhook_url() -> str:
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
            "okx_market_cap_bot.py",
            "binance_strategy_bot.py",
            "position_manager.py",
            "runtime_utils.py",
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


def parse_site_monitor_targets(raw_targets: Optional[str] = None) -> list[dict[str, str]]:
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


def tls_days_remaining(url: str, timeout: float) -> Optional[float]:
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


def probe_site_monitor_target(target: dict[str, str], timeout: Optional[float] = None) -> dict[str, Any]:
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
    require_okx_files = env_bool("SITE_MONITOR_REQUIRE_OKX_BOT_FILES", False)
    require_okx_ok = env_bool("SITE_MONITOR_REQUIRE_OKX_BOT_OK", False)

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

    if require_okx_files or require_okx_ok:
        okx_status = load_okx_bot_status()
        ok = True
        error = None
        if require_okx_files and not (okx_status.get("state_exists") and okx_status.get("event_log_exists")):
            ok = False
            error = "OKX bot state/event log is not visible to the web app"
        if ok and require_okx_ok and (not okx_status.get("ok") or okx_status.get("scan_stale")):
            ok = False
            error = okx_status.get("last_error") or "OKX bot scan is unhealthy or stale"
        checks.append(
            {
                "id": "runtime:okx_bot",
                "name": "okx_bot_runtime",
                "type": "runtime",
                "ok": ok,
                "checked_at": now,
                "state_exists": okx_status.get("state_exists"),
                "event_log_exists": okx_status.get("event_log_exists"),
                "last_scan_at": okx_status.get("last_scan_at"),
                "scan_stale": okx_status.get("scan_stale"),
                "error": error,
            }
        )

    return checks


def apply_site_monitor_alert_state(results: list[dict[str, Any]], now: Optional[float] = None) -> list[dict[str, Any]]:
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
    http_results = await asyncio.gather(
        *(asyncio.to_thread(probe_site_monitor_target, target) for target in targets)
    )
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


def get_pending_buy_divergence(symbol: str, interval: str) -> Optional[dict[str, Any]]:
    with state_lock:
        candidate = state.pending_buy_divergences.get(pending_signal_key(symbol, interval))
        return dict(candidate) if candidate else None


def clear_pending_buy_divergence(symbol: str, interval: str) -> None:
    with state_lock:
        state.pending_buy_divergences.pop(pending_signal_key(symbol, interval), None)


def set_pending_sell_divergence(symbol: str, interval: str, candidate: dict[str, Any]) -> None:
    with state_lock:
        state.pending_sell_divergences[pending_signal_key(symbol, interval)] = candidate


def get_pending_sell_divergence(symbol: str, interval: str) -> Optional[dict[str, Any]]:
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
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        trades = data.get("trades", [])
    else:
        trades = data
    return [trade for trade in trades if isinstance(trade, dict)]


def save_strategy_trades(trades: list[dict[str, Any]]) -> None:
    payload = {
        "updated_at": time.time(),
        "trades": trades[-STRATEGY_HISTORY_LIMIT:],
    }
    try:
        atomic_write_text(STRATEGY_TRADES_FILE, json.dumps(payload, ensure_ascii=False, indent=2))
    except OSError:
        with state_lock:
            state.signal_error = "策略交易账本写入失败"


def append_event_to_disk(event: dict[str, Any]) -> None:
    try:
        append_jsonl(EVENT_LOG_FILE, event)
    except OSError:
        with state_lock:
            state.signal_error = "事件日志写入失败"


def record_event(event: dict[str, Any], event_key: Optional[str] = None) -> None:
    stored_event = dict(event)
    stored_event.setdefault("created_at", time.time())
    if event_key:
        stored_event["event_key"] = event_key

    with state_lock:
        if event_key:
            state.sent_alerts.add(event_key)
        state.events.insert(0, stored_event)
        state.events = state.events[:50]

    append_event_to_disk(stored_event)


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
        available = fetch_binance_spot_symbols("USDT")
    except (http.client.IncompleteRead, OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, RuntimeError):
        return []
    return [symbol for symbol in symbols if symbol not in available]


def normalize_interval(value: Any) -> str:
    interval = str(value or DEFAULT_SIGNAL_INTERVAL).strip()
    return interval if interval in {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"} else DEFAULT_SIGNAL_INTERVAL


def parse_float(value: Any) -> Optional[float]:
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


def calculate_strategy_levels(direction: str, entry_price: float, stop_loss: float) -> Optional[dict[str, float]]:
    return calculate_target_levels(direction, entry_price, stop_loss, STRATEGY_REWARD_RISK)


def build_strategy_trade_from_signal(signal: dict[str, Any]) -> Optional[dict[str, Any]]:
    direction = str(signal.get("signal", ""))
    if direction not in {"long", "short"}:
        return None

    entry_price = parse_float(signal.get("price"))
    stop_loss = parse_float(signal.get("stop_loss"))
    if entry_price is None or stop_loss is None:
        return None

    levels = calculate_strategy_levels(direction, entry_price, stop_loss)
    if not levels:
        return None

    opened_at = parse_float(signal.get("created_at")) or time.time()
    return {
        "id": strategy_trade_id(signal),
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
        "risk": levels["risk"],
        "target_price": levels["target_price"],
        "protection_price": levels["protection_price"],
        "opened_at": opened_at,
        "opened_kline_close_time": signal.get("kline_close_time"),
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


def strategy_return_pct(direction: str, entry_price: float, exit_price: float) -> float:
    return calculate_return_pct(direction, entry_price, exit_price, STRATEGY_FEE_RATE)


def refresh_strategy_trade_mark(trade: dict[str, Any], current_price: float) -> None:
    entry_price = parse_float(trade.get("entry_price"))
    if entry_price is None or entry_price <= 0:
        return
    direction = str(trade.get("direction"))
    trade["current_price"] = current_price
    trade["highest_price"] = max(parse_float(trade.get("highest_price")) or entry_price, current_price)
    trade["lowest_price"] = min(parse_float(trade.get("lowest_price")) or entry_price, current_price)
    trade["unrealized_pct"] = strategy_return_pct(direction, entry_price, current_price)


def close_strategy_trade(trade: dict[str, Any], exit_price: float, reason: str, outcome: str) -> None:
    entry_price = parse_float(trade.get("entry_price"))
    if entry_price is None:
        return
    trade["status"] = "closed"
    trade["exit_reason"] = reason
    trade["outcome"] = outcome
    trade["exit_price"] = exit_price
    trade["closed_at"] = time.time()
    trade["return_pct"] = strategy_return_pct(str(trade.get("direction")), entry_price, exit_price)


def evaluate_open_strategy_trade(trade: dict[str, Any], current_price: float) -> bool:
    if trade.get("status") != "open":
        return False

    refresh_strategy_trade_mark(trade, current_price)
    direction = str(trade.get("direction"))
    stop_loss = parse_float(trade.get("stop_loss"))
    target_price = parse_float(trade.get("target_price"))
    protection_price = parse_float(trade.get("protection_price"))
    if stop_loss is None or target_price is None or protection_price is None:
        return False

    decision = evaluate_price_exit(direction, current_price, stop_loss, target_price, protection_price)
    if not decision:
        return False
    close_strategy_trade(trade, decision.exit_price, decision.reason, decision.outcome)
    return True


def evaluate_open_strategy_trade_with_bars(trade: dict[str, Any], bars: list[dict[str, Any]], current_price: Optional[float]) -> bool:
    if trade.get("status") != "open":
        return False

    if current_price is not None:
        refresh_strategy_trade_mark(trade, current_price)

    direction = str(trade.get("direction"))
    stop_loss = parse_float(trade.get("stop_loss"))
    target_price = parse_float(trade.get("target_price"))
    protection_price = parse_float(trade.get("protection_price"))
    opened_close_time = parse_int(trade.get("opened_kline_close_time"), 0)
    if stop_loss is None or target_price is None or protection_price is None:
        return False

    future_bars = [
        bar
        for bar in bars
        if opened_close_time > 0 and parse_int(bar.get("close_time"), 0) > opened_close_time
    ]
    for bar in future_bars:
        high = parse_float(bar.get("high"))
        low = parse_float(bar.get("low"))
        close = parse_float(bar.get("close"))
        if high is None or low is None:
            continue
        if close is not None:
            refresh_strategy_trade_mark(trade, close)

        decision = evaluate_bar_exit(direction, low, high, stop_loss, target_price, protection_price)
        if decision:
            close_strategy_trade(trade, decision.exit_price, decision.reason, decision.outcome)
            return True

    if current_price is None:
        return False
    return evaluate_open_strategy_trade(trade, current_price)


def calculate_strategy_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [trade for trade in trades if trade.get("status") == "closed"]
    open_trades = [trade for trade in trades if trade.get("status") == "open"]
    wins = [trade for trade in closed if trade.get("outcome") == "win"]
    losses = [trade for trade in closed if trade.get("outcome") == "loss"]
    returns = [parse_float(trade.get("return_pct")) or 0.0 for trade in closed]
    protection_wins = [trade for trade in closed if trade.get("exit_reason") == "protection_reached"]
    stop_losses = [trade for trade in closed if trade.get("exit_reason") == "stop_loss"]
    return {
        "total_trades": len(trades),
        "open_trades": len(open_trades),
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(closed) if closed else 0.0,
        "expectancy": sum(returns) / len(returns) if returns else 0.0,
        "total_return": sum(returns),
        "stop_loss_rate": len(stop_losses) / len(closed) if closed else 0.0,
        "protection_rate": len(protection_wins) / len(closed) if closed else 0.0,
    }


def register_strategy_signal(signal: dict[str, Any]) -> None:
    trade = build_strategy_trade_from_signal(signal)
    if not trade:
        return

    changed = False
    with state_lock:
        existing_ids = {str(item.get("id")) for item in state.strategy_trades}
        if trade["id"] not in existing_ids:
            state.strategy_trades.append(trade)
            state.strategy_trades = state.strategy_trades[-STRATEGY_HISTORY_LIMIT:]
            state.strategy_stats = calculate_strategy_stats(state.strategy_trades)
            changed = True
        trades_snapshot = [dict(item) for item in state.strategy_trades]

    if changed:
        save_strategy_trades(trades_snapshot)


async def update_strategy_trades_with_prices_async(prices: dict[str, dict[str, Any]]) -> None:
    changed = False
    with state_lock:
        open_keys = sorted(
            {
                (str(trade.get("symbol", "")).upper(), normalize_interval(trade.get("interval", DEFAULT_SIGNAL_INTERVAL)))
                for trade in state.strategy_trades
                if trade.get("status") == "open"
            }
        )

    async def load_bars(symbol: str, interval: str) -> tuple[tuple[str, str], list[dict[str, Any]]]:
        try:
            return (symbol, interval), await async_fetch_klines(symbol, interval, KLINE_LIMIT)
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, OSError):
            return (symbol, interval), []

    bars_by_key = dict(await asyncio.gather(*(load_bars(symbol, interval) for symbol, interval in open_keys))) if open_keys else {}

    with state_lock:
        for trade in state.strategy_trades:
            if trade.get("status") != "open":
                continue
            symbol = str(trade.get("symbol", "")).upper()
            interval = normalize_interval(trade.get("interval", DEFAULT_SIGNAL_INTERVAL))
            ticker = prices.get(symbol)
            current_price = parse_float((ticker or {}).get("lastPrice"))
            bars = bars_by_key.get((symbol, interval), [])
            if bars and evaluate_open_strategy_trade_with_bars(trade, bars, current_price):
                changed = True
            elif not bars and current_price is not None and evaluate_open_strategy_trade(trade, current_price):
                changed = True
        state.strategy_stats = calculate_strategy_stats(state.strategy_trades)
        trades_snapshot = [dict(item) for item in state.strategy_trades]

    if changed:
        save_strategy_trades(trades_snapshot)


def seed_strategy_trades_from_events(events: list[dict[str, Any]], existing_trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
    return trades[-STRATEGY_HISTORY_LIMIT:]


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
        return True, f"✗ {SELL_TREND_BREAKER_INTERVAL}空头趋势熔断：价格={close:.2f}<EMA60={ema60:.2f}, MACD={macd_value:.4f}<0", higher
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
        return True, f"✗ {SELL_TREND_BREAKER_INTERVAL}多头趋势熔断：价格={close:.2f}>EMA60={ema60:.2f}, MACD={macd_value:.4f}>0"
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


def detect_support_level(symbol: str, interval: str, current_price: Optional[float]) -> dict[str, Any]:
    bars = fetch_klines(symbol, interval, KLINE_LIMIT)
    return build_support_level_from_bars(symbol, interval, current_price, bars)


async def detect_support_level_async(symbol: str, interval: str, current_price: Optional[float]) -> dict[str, Any]:
    bars = await async_fetch_klines(symbol, interval, KLINE_LIMIT)
    return build_support_level_from_bars(symbol, interval, current_price, bars)


def build_support_level_from_bars(symbol: str, interval: str, current_price: Optional[float], bars: list[dict[str, Any]]) -> dict[str, Any]:
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
        except (http.client.IncompleteRead, OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, ValueError):
            signals[interval] = 0
            continue
        if not bars:
            signals[interval] = 0
            continue
        index = nearest_index_by_close_time(bars, timestamp_ms)
        window = bars[max(0, index - 50) : index + 1]
        signals[interval] = td_setup(window, 9) if len(window) >= 50 else 0
    return signals


def signal_engine(symbol: str, interval: str) -> ProjectSignalEngine:
    key = (symbol.upper(), normalize_interval(interval))
    engine = SIGNAL_ENGINES.get(key)
    if engine is None:
        engine = ProjectSignalEngine()
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
            and int(higher_bars[higher_index + 1].get("close_time") or higher_bars[higher_index + 1].get("open_time") or 0) <= bar_close_time
        ):
            higher_index += 1

        higher_close_time = int(higher_bars[higher_index].get("close_time") or higher_bars[higher_index].get("open_time") or 0)
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
    bars = calculate_indicators(fetch_klines(symbol, interval, KLINE_LIMIT))
    if not bars:
        return []
    if interval in {HIGHER_TREND_INTERVAL, "1d"}:
        enriched_bars = add_higher_timeframe_context(bars, bars)
    else:
        higher_limit = max(120, min(1000, len(bars) // 8 + 120))
        higher_bars = calculate_indicators(fetch_klines(symbol, HIGHER_TREND_INTERVAL, higher_limit))
        enriched_bars = add_higher_timeframe_context(bars, higher_bars)

    ready_bars = [bar for bar in enriched_bars if bar.get("close") is not None and bar.get("macd") is not None]
    if ready_bars:
        ready_bars[-1].update(fetch_microstructure_state(symbol, interval))
    return ready_bars


def detect_project_signal(symbol: str, interval: str) -> dict[str, Any]:
    normalized_interval = normalize_interval(interval)
    ready_bars = build_project_signal_bars(symbol, normalized_interval)
    signal = signal_engine(symbol, normalized_interval).detect(ready_bars)
    latest = ready_bars[-1] if ready_bars else {}
    close_time = latest.get("close_time") or latest.get("open_time")
    td_signals: dict[str, int] = {}
    if close_time is not None:
        try:
            td_signals = get_td_signals(symbol, int(close_time))
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, OSError, ValueError):
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
    ready_bars = [
        bar
        for bar in bars
        if bar.get("close") is not None and bar.get("macd") is not None
    ]
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

    bullish_div, bullish_strength, bullish_info = detect_bullish_divergence(price_troughs, macd_troughs, macd_fast_troughs)
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

    def build_buy_response(candidate: dict[str, Any], signal: str, signal_name: str, filter_message: str, confirm_bars: int, higher: Optional[dict[str, Any]] = None, signal_grade: str = "weak") -> dict[str, Any]:
        higher_state = higher or {}
        structure = candidate.get("structure")
        score_info = score_signal("long", candidate["strength"], signal == "long", current_bar, higher_state, "熔断" in signal_name, structure)
        stop_anchor = float(structure.get("stop_anchor", candidate["price_low"]) if structure else candidate["price_low"])
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

    def build_sell_response(candidate: dict[str, Any], signal: str, signal_name: str, filter_message: str, confirm_bars: int = 0) -> dict[str, Any]:
        higher = fetch_higher_trend_state(symbol) if interval == DEFAULT_SIGNAL_INTERVAL else {"trend": "unknown"}
        structure = candidate.get("structure")
        score_info = score_signal("short", candidate["strength"], signal == "short", current_bar, higher, "熔断" in signal_name, structure)
        stop_anchor = float(structure.get("stop_anchor", candidate["price_high"]) if structure else candidate["price_high"])
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
            return build_buy_response(pending_buy, "filtered_buy", "底背驰确认超时", f"超过 {CONFIRM_MAX_BARS} 根K线未确认", confirm_bars)
        confirmed, _confirmation_flags = confirmation_passed("long", ma_direction, macd_direction, DEFAULT_CONFIG.confirmation_mode)
        if confirmed:
            clear_pending_buy_divergence(symbol, interval)
            trend_blocked, trend_message, higher = check_buy_trend_breaker(symbol, interval)
            if trend_blocked:
                return build_buy_response(pending_buy, "filtered_buy", "底背驰被高周期趋势熔断", trend_message, confirm_bars, higher)
            filter_passed, filter_message = check_buy_filter(ready_bars)
            signal_grade = grade_signal("long", pending_buy["strength"], filter_passed, current_bar, higher, trend_blocked)
            signal_name = "MACD底背驰确认开多" if filter_passed else "底背驰被买入过滤"
            return build_buy_response(pending_buy, "long" if filter_passed else "filtered_buy", signal_name, filter_message, confirm_bars, higher, signal_grade)
        return build_buy_response(pending_buy, "filtered_buy", "底背驰等待确认", confirmation_wait_text("long", DEFAULT_CONFIG), confirm_bars)

    if bullish_div and bullish_strength >= MIN_DIVERGENCE_STRENGTH and bullish_info:
        divergence_bar = window[bullish_info["index"]]
        ready_index = min(int(bullish_info.get("confirm_index", bullish_info["index"]) or bullish_info["index"]), len(window) - 1)
        ready_bar = window[ready_index]
        buy_candidate = {
            "strength": bullish_strength,
            "price_low": bullish_info["price_low"],
            "divergence_time": divergence_bar["close_time"],
            "signal_ready_time": ready_bar.get("close_time", ready_bar.get("open_time")),
            "divergence_type": bullish_info.get("type", "macd"),
            "created_close_time": current_bar["close_time"],
        }
        buy_candidate["structure"] = build_chan_structure_context("long", window, price_peaks, price_troughs, macd_values, bullish_info)
        confirmed, _confirmation_flags = confirmation_passed("long", ma_direction, macd_direction, DEFAULT_CONFIG.confirmation_mode)
        if not confirmed:
            set_pending_buy_divergence(symbol, interval, buy_candidate)
            return build_buy_response(buy_candidate, "filtered_buy", "底背驰等待确认", confirmation_wait_text("long", DEFAULT_CONFIG), 0)
        trend_blocked, trend_message, higher = check_buy_trend_breaker(symbol, interval)
        if trend_blocked:
            return build_buy_response(buy_candidate, "filtered_buy", "底背驰被高周期趋势熔断", trend_message, 0, higher)
        filter_passed, filter_message = check_buy_filter(ready_bars)
        signal_grade = grade_signal("long", bullish_strength, filter_passed, current_bar, higher, trend_blocked)
        signal_name = "MACD底背驰确认开多" if filter_passed else "底背驰被买入过滤"
        return build_buy_response(buy_candidate, "long" if filter_passed else "filtered_buy", signal_name, filter_message, 0, higher, signal_grade)

    if bearish_div and bearish_strength >= MIN_DIVERGENCE_STRENGTH and bearish_info:
        divergence_bar = window[bearish_info["index"]]
        ready_index = min(int(bearish_info.get("confirm_index", bearish_info["index"]) or bearish_info["index"]), len(window) - 1)
        ready_bar = window[ready_index]
        sell_candidate = {
            "strength": bearish_strength,
            "price_high": bearish_info["price_high"],
            "divergence_time": divergence_bar["close_time"],
            "signal_ready_time": ready_bar.get("close_time", ready_bar.get("open_time")),
            "divergence_type": bearish_info.get("type", "macd"),
            "created_close_time": current_bar["close_time"],
        }
        sell_candidate["structure"] = build_chan_structure_context("short", window, price_peaks, price_troughs, macd_values, bearish_info)
        confirmed, _confirmation_flags = confirmation_passed("short", ma_direction, macd_direction, DEFAULT_CONFIG.confirmation_mode)
        if not confirmed:
            set_pending_sell_divergence(symbol, interval, sell_candidate)
            return build_sell_response(sell_candidate, "filtered_sell", "顶背驰等待确认", confirmation_wait_text("short", DEFAULT_CONFIG))

        clear_pending_sell_divergence(symbol, interval)
        trend_blocked, trend_message = check_sell_trend_breaker(symbol, interval)
        if trend_blocked:
            return build_sell_response(sell_candidate, "filtered_sell", "顶背驰被高周期趋势熔断", trend_message)

        filter_passed, filter_message = check_sell_filter(ready_bars)
        signal_name = "MACD顶背驰开空观察" if filter_passed else "顶背驰被卖出过滤"
        return build_sell_response(sell_candidate, "short" if filter_passed else "filtered_sell", signal_name, filter_message)

    pending_sell = get_pending_sell_divergence(symbol, interval)
    if pending_sell:
        confirm_bars = bars_waited_since(pending_sell.get("created_close_time"))
        if confirm_bars > CONFIRM_MAX_BARS:
            clear_pending_sell_divergence(symbol, interval)
            return build_sell_response(pending_sell, "filtered_sell", "顶背驰确认超时", f"超过 {CONFIRM_MAX_BARS} 根K线未确认", confirm_bars)
        confirmed, _confirmation_flags = confirmation_passed("short", ma_direction, macd_direction, DEFAULT_CONFIG.confirmation_mode)
        if not confirmed:
            return build_sell_response(pending_sell, "filtered_sell", "顶背驰等待确认", confirmation_wait_text("short", DEFAULT_CONFIG), confirm_bars)

        clear_pending_sell_divergence(symbol, interval)
        trend_blocked, trend_message = check_sell_trend_breaker(symbol, interval)
        if trend_blocked:
            return build_sell_response(pending_sell, "filtered_sell", "顶背驰被高周期趋势熔断", trend_message, confirm_bars)

        filter_passed, filter_message = check_sell_filter(ready_bars)
        signal_name = "MACD顶背驰开空观察" if filter_passed else "顶背驰被卖出过滤"
        return build_sell_response(pending_sell, "short" if filter_passed else "filtered_sell", signal_name, filter_message, confirm_bars)

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

    bars = project_signal_backtest.fetch_binance_klines(symbol, interval, limit)
    enriched_bars = project_signal_backtest.fetch_higher_timeframe_context(
        symbol,
        project_signal_backtest.calculate_indicators(bars),
        interval,
    )
    stop_modes = ["structure_atr", "atr_trailing_after_1r"] if stop_mode == "all" else [stop_mode]
    mode_results = [
        project_signal_backtest.build_stop_mode_result(
            enriched_bars,
            reward_risk,
            max_hold_bars,
            fee_rate,
            mode,
        )
        for mode in stop_modes
    ]
    primary = mode_results[0]
    trades = primary["trades"]
    metrics = primary["metrics"]
    group_stats = primary["group_stats"]
    started_at = enriched_bars[0]["time"].isoformat() if enriched_bars else None
    ended_at = enriched_bars[-1]["time"].isoformat() if enriched_bars else None

    groups = []
    if not group_stats.empty:
        groups = [
            {
                "signal": str(row["signal"]),
                "trend": str(row["trend"]),
                "higher_trend": str(row.get("higher_trend", "unknown")),
                "signal_grade": str(row.get("signal_grade", "weak")),
                "score_bucket": str(row.get("score_bucket", "unknown")),
                "divergence_type": str(row["divergence_type"]),
                "strength_bucket": str(row["strength_bucket"]),
                "trades": int(row["trades"]),
                "avg_score": float(row.get("avg_score", 0)),
                "win_rate": float(row["win_rate"]),
                "avg_return": float(row["avg_return"]),
                "avg_confirm_bars": float(row.get("avg_confirm_bars", 0)),
            }
            for row in group_stats.head(12).to_dict("records")
        ]

    return {
        "ok": True,
        "symbol": symbol,
        "interval": interval,
        "limit": len(enriched_bars),
        "started_at": started_at,
        "ended_at": ended_at,
        "params": {
            "reward_risk": reward_risk,
            "max_hold_bars": max_hold_bars,
            "fee_rate": fee_rate,
            "stop_mode": stop_mode,
        },
        "metrics": metrics,
        "stop_mode_results": [
            {
                "stop_mode": result["stop_mode"],
                "metrics": result["metrics"],
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


async def monitor_loop_async(stop_event: Optional[asyncio.Event] = None) -> None:
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        with state_lock:
            watchlist = list(state.watchlist)
        symbols = sorted({item["symbol"] for item in watchlist} | set(HOURLY_SUMMARY_SYMBOLS))

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
            await send_hourly_market_summary_async(prices)
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, OSError, RuntimeError) as exc:
            with state_lock:
                state.last_error = str(exc)
        try:
            await maybe_run_site_monitor_async()
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
            with state_lock:
                previous = dict(state.site_monitor)
                state.site_monitor = {
                    **previous,
                    "enabled": site_monitor_enabled(),
                    "ok": False,
                    "last_error": str(exc),
                    "last_run_at": time.time(),
                }
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=10)
        except asyncio.TimeoutError:
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

    async def load_signal(item: dict[str, Any]) -> tuple[dict[str, Any], Optional[dict[str, Any]], Optional[str]]:
        try:
            signal = await asyncio.to_thread(detect_project_signal, item["symbol"], item.get("interval", DEFAULT_SIGNAL_INTERVAL))
            return item, signal, None
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, OSError, RuntimeError, ValueError) as exc:
            return item, None, str(exc)

    loaded = await asyncio.gather(*(load_signal(item) for item in signal_items)) if signal_items else []

    for item, signal, error in loaded:
        if error:
            signal_error = error
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
        with state_lock:
            state.signal_error = None
        await send_alert_notifications_async(message)

    with state_lock:
        state.signals = next_signals
        state.signal_error = signal_error


async def evaluate_indicator_signals_async(watchlist: list[dict[str, Any]]) -> None:
    next_indicator_signals: dict[str, dict[str, Any]] = {}
    indicator_error = None
    symbols = sorted(
        {
            item["symbol"]
            for item in watchlist
            if item.get("signal", True) or item.get("indicator_alert", True)
        }
    )
    email_by_symbol = {
        item["symbol"]: item.get("email", "")
        for item in watchlist
        if item.get("signal", True) or item.get("indicator_alert", True)
    }
    ma_alert_symbols = {
        item["symbol"]
        for item in watchlist
        if item.get("signal", True)
    }
    macd_alert_symbols = {
        item["symbol"]
        for item in watchlist
        if item.get("indicator_alert", True)
    }

    tasks = [(symbol, interval) for symbol in symbols for interval in INDICATOR_INTERVALS]

    async def load_signal(symbol: str, interval: str) -> tuple[tuple[str, str], Optional[dict[str, Any]], Optional[str]]:
        try:
            return (symbol, interval), await detect_indicator_signal_async(symbol, interval), None
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, OSError) as exc:
            return (symbol, interval), None, str(exc)

    loaded = await asyncio.gather(*(load_signal(symbol, interval) for symbol, interval in tasks)) if tasks else []
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for key, signal, error in loaded:
        if error:
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

                alert_key = f"indicator:{symbol}:{interval}:{indicator_name}:{direction}:{signal.get('kline_close_time')}"
                with state_lock:
                    already_sent = alert_key in state.sent_alerts
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
                await send_alert_notifications_async(message)

        next_indicator_signals[symbol] = symbol_signals

    with state_lock:
        state.indicator_signals = next_indicator_signals
        if indicator_error:
            state.signal_error = indicator_error


async def evaluate_support_alerts_async(watchlist: list[dict[str, Any]], prices: dict[str, dict[str, Any]]) -> None:
    next_support_levels: dict[str, dict[str, Any]] = {}
    support_error = None
    tasks = [
        (item, interval, prices.get(item["symbol"], {}).get("lastPrice"))
        for item in watchlist
        for interval in SUPPORT_INTERVALS
    ]

    async def load_level(item: dict[str, Any], interval: str, price: Optional[float]) -> tuple[tuple[str, str], Optional[dict[str, Any]], Optional[str]]:
        try:
            return (item["symbol"], interval), await detect_support_level_async(item["symbol"], interval, price), None
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, OSError) as exc:
            return (item["symbol"], interval), None, str(exc)

    loaded = await asyncio.gather(*(load_level(item, interval, price) for item, interval, price in tasks)) if tasks else []
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for key, level, error in loaded:
        if error:
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
            support = level.get("support")
            alert_key = f"support:{symbol}:{interval}:touch:{level.get('kline_close_time')}"
            reset_key = f"support:{symbol}:{interval}:active"
            reset_distance = level.get("tolerance_pct", 0.4) * 2
            distance_pct = level.get("distance_pct")

            if distance_pct is not None and distance_pct > reset_distance:
                with state_lock:
                    state.sent_alerts.discard(reset_key)
                continue
            if not item.get("support_alert", True) or not level.get("touched") or support is None:
                continue
            with state_lock:
                already_sent = reset_key in state.sent_alerts or alert_key in state.sent_alerts
            if already_sent:
                continue

            message = {
                **level,
                "email": item.get("email", ""),
            }
            with state_lock:
                state.sent_alerts.add(reset_key)
            record_event(message, alert_key)
            await send_alert_notifications_async(message)

        next_support_levels[symbol] = symbol_levels

    with state_lock:
        state.support_levels = next_support_levels
        if support_error:
            state.signal_error = support_error


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
            await asyncio.gather(
                *(
                    _load_funding_summary(symbol)
                    for symbol in HOURLY_SUMMARY_SYMBOLS
                )
            )
        )
        for symbol in HOURLY_SUMMARY_SYMBOLS:
            ticker = prices.get(symbol) or fallback_prices.get(symbol, {})
            funding = funding_by_symbol.get(symbol, {})
            lines.append(format_market_summary_line(symbol, ticker, funding))

        await asyncio.to_thread(send_discord_message, "\n".join(lines))
        with state_lock:
            state.summary_error = None
    except (http.client.IncompleteRead, OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, RuntimeError) as exc:
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
    await asyncio.to_thread(send_alert_notifications, alert)


def format_alert_text(alert: dict[str, Any]) -> tuple[str, str]:
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
        body = (
            f"{alert['symbol']} {alert.get('interval', '')} 触及支撑点位 {format_decimal(alert.get('support'))}，"
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


def read_recent_jsonl(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw_lines = deque(path.read_text(encoding="utf-8").splitlines(), maxlen=max(1, limit * 3))
    except OSError:
        return []

    events: list[dict[str, Any]] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events[-limit:]


def okx_bot_state_path() -> Path:
    if OKX_BOT_STATE_FILE.exists() or OKX_BOT_STATE_FILE == OKX_BOT_LEGACY_STATE_FILE:
        return OKX_BOT_STATE_FILE
    if OKX_BOT_LEGACY_STATE_FILE.exists():
        return OKX_BOT_LEGACY_STATE_FILE
    return OKX_BOT_STATE_FILE


def okx_bot_event_log_path() -> Path:
    if OKX_BOT_EVENT_LOG_FILE.exists() or OKX_BOT_EVENT_LOG_FILE == OKX_BOT_LEGACY_EVENT_LOG_FILE:
        return OKX_BOT_EVENT_LOG_FILE
    if OKX_BOT_LEGACY_EVENT_LOG_FILE.exists():
        return OKX_BOT_LEGACY_EVENT_LOG_FILE
    return OKX_BOT_EVENT_LOG_FILE


def path_signature(path: Path) -> tuple[str, Optional[float], Optional[int]]:
    try:
        stat = path.stat()
        return str(path), stat.st_mtime, stat.st_size
    except OSError:
        return str(path), None, None


def load_okx_bot_state_payload() -> tuple[dict[str, Any], Optional[str]]:
    state_path = okx_bot_state_path()
    if not state_path.exists():
        return {}, None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"bot state read failed: {exc}"
    return payload if isinstance(payload, dict) else {}, None


def normalize_bot_position(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": position.get("id"),
        "symbol": position.get("symbol"),
        "interval": position.get("interval"),
        "direction": position.get("direction") or position.get("side"),
        "status": position.get("status"),
        "entry_price": position.get("entry_price"),
        "stop_loss": position.get("stop_loss"),
        "target_price": position.get("target_price") or position.get("take_profit"),
        "protection_price": position.get("protection_price"),
        "opened_at": position.get("opened_at"),
        "size": position.get("size"),
        "notional_usdt": position.get("notional_usdt"),
        "order_symbol": position.get("order_symbol"),
        "exit_price": position.get("exit_price"),
        "exit_reason": position.get("exit_reason"),
        "return_pct": position.get("return_pct"),
    }


def load_binance_bot_positions() -> list[dict[str, Any]]:
    if not BINANCE_BOT_STATE_FILE.exists():
        return []
    try:
        payload = json.loads(BINANCE_BOT_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    positions = payload.get("positions", []) if isinstance(payload, dict) else []
    if not isinstance(positions, list):
        return []
    return [
        {**normalize_bot_position(position), "bot": "binance"}
        for position in positions
        if isinstance(position, dict) and position.get("status") == "open"
    ]


def summarize_okx_bot_status(events: list[dict[str, Any]], state_payload: dict[str, Any], state_error: Optional[str] = None) -> dict[str, Any]:
    now = time.time()
    state_path = okx_bot_state_path()
    event_log_path = okx_bot_event_log_path()
    positions = state_payload.get("positions", []) if isinstance(state_payload, dict) else []
    positions = positions if isinstance(positions, list) else []
    open_positions = [item for item in positions if isinstance(item, dict) and item.get("status") == "open"]

    last_event = events[-1] if events else None
    last_scan = next((event for event in reversed(events) if event.get("type") == "scan"), None)
    last_scan_started = next((event for event in reversed(events) if event.get("type") == "scan_started"), None)
    last_startup = next((event for event in reversed(events) if event.get("type") == "startup"), None)
    last_error_event = next((event for event in reversed(events) if event.get("type") in OKX_BOT_PRIMARY_ERROR_EVENT_TYPES), None)
    if last_error_event is None:
        last_error_event = next((event for event in reversed(events) if event.get("type") in OKX_BOT_ERROR_EVENT_TYPES), None)

    last_scan_at = parse_float((last_scan or {}).get("created_at"))
    last_scan_started_at = parse_float((last_scan_started or {}).get("created_at"))
    last_event_at = parse_float((last_event or {}).get("created_at"))
    configured_timeout = parse_float((last_startup or {}).get("scan_timeout_seconds")) or OKX_BOT_SCAN_TIMEOUT_SECONDS
    scan_grace_seconds = max(60.0, configured_timeout * 0.25)
    running_scan_overdue = (
        last_scan_started_at is not None
        and (last_scan_at is None or last_scan_started_at > last_scan_at)
        and now - last_scan_started_at > configured_timeout + scan_grace_seconds
    )
    scan_stale = last_scan_at is not None and now - last_scan_at > max(configured_timeout * 2, 600)
    health = "running"
    health_label = "运行中"
    if state_error:
        health = "error"
        health_label = "状态异常"
    elif not event_log_path.exists():
        health = "unknown"
        health_label = "未发现日志"
    elif running_scan_overdue:
        health = "stalled"
        health_label = "可能卡住"
    elif scan_stale:
        health = "stale"
        health_label = "扫描过期"

    last_error = state_error
    last_error_at = parse_float((last_error_event or {}).get("created_at"))
    latest_scan_errors = int((last_scan or {}).get("errors", 0) or 0)
    error_is_current = (
        last_error_event is not None
        and (
            last_scan_at is None
            or last_error_at is None
            or last_error_at >= last_scan_at
            or latest_scan_errors > 0
        )
    )
    if error_is_current and last_error_event:
        last_error = last_error_event.get("error") or last_error_event.get("message") or last_error
    normalized_open_positions = [
        {**normalize_bot_position(position), "bot": "okx"}
        for position in open_positions
    ]
    binance_positions = load_binance_bot_positions()
    all_open_positions = normalized_open_positions + binance_positions

    return {
        "ok": bool(event_log_path.exists()) and not running_scan_overdue and not scan_stale and not bool(state_error),
        "health": health,
        "health_label": health_label,
        "state_exists": state_path.exists(),
        "event_log_exists": event_log_path.exists(),
        "state_path": str(state_path),
        "event_log_path": str(event_log_path),
        "open_positions": len(open_positions),
        "position_count": len([item for item in positions if isinstance(item, dict)]),
        "positions": all_open_positions,
        "okx_positions": normalized_open_positions,
        "binance_positions": binance_positions,
        "last_event_at": last_event_at,
        "last_scan_at": last_scan_at,
        "last_scan_started_at": last_scan_started_at,
        "last_startup_at": parse_float((last_startup or {}).get("created_at")),
        "last_event": last_event,
        "last_scan": last_scan,
        "last_startup": last_startup,
        "last_error": last_error,
        "last_error_event": last_error_event,
        "scan_timeout_seconds": configured_timeout,
        "seconds_since_last_scan": round(now - last_scan_at, 3) if last_scan_at is not None else None,
        "running_scan_overdue": running_scan_overdue,
        "scan_stale": scan_stale,
    }


def load_okx_bot_status(force_refresh: bool = False) -> dict[str, Any]:
    state_path = okx_bot_state_path()
    event_log_path = okx_bot_event_log_path()
    signature = (
        path_signature(state_path),
        path_signature(event_log_path),
        path_signature(BINANCE_BOT_STATE_FILE),
    )
    now = time.time()
    with okx_bot_status_cache_lock:
        cached = _okx_bot_status_cache.get("value")
        if (
            not force_refresh
            and cached is not None
            and _okx_bot_status_cache.get("signature") == signature
            and now < float(_okx_bot_status_cache.get("expires_at") or 0)
        ):
            return dict(cached)

    events = read_recent_jsonl(okx_bot_event_log_path(), limit=300)
    state_payload, state_error = load_okx_bot_state_payload()
    status = summarize_okx_bot_status(events, state_payload, state_error)
    with okx_bot_status_cache_lock:
        _okx_bot_status_cache.update(
            {
                "expires_at": now + 2.0,
                "signature": signature,
                "value": dict(status),
            }
        )
    return status


def load_okx_bot_errors(limit: int = 20) -> list[dict[str, Any]]:
    events = read_recent_jsonl(okx_bot_event_log_path(), limit=max(100, limit * 5))
    errors = [event for event in reversed(events) if event.get("type") in OKX_BOT_ERROR_EVENT_TYPES]
    return errors[: max(1, min(limit, 100))]


def load_okx_bot_health_detail() -> dict[str, Any]:
    events = read_recent_jsonl(okx_bot_event_log_path(), limit=300)
    state_payload, state_error = load_okx_bot_state_payload()
    status = summarize_okx_bot_status(events, state_payload, state_error)
    with okx_bot_status_cache_lock:
        _okx_bot_status_cache.update(
            {
                "expires_at": time.time() + 2.0,
                "signature": (
                    path_signature(okx_bot_state_path()),
                    path_signature(okx_bot_event_log_path()),
                    path_signature(BINANCE_BOT_STATE_FILE),
                ),
                "value": dict(status),
            }
        )
    return {
        **status,
        "code_fingerprint": code_fingerprint(),
        "recent_errors": load_okx_bot_errors(10),
        "recent_events": list(reversed(events[-20:])),
    }


def load_okx_bot_status_legacy() -> dict[str, Any]:
    status: dict[str, Any] = {
        "state_exists": OKX_BOT_STATE_FILE.exists(),
        "event_log_exists": OKX_BOT_EVENT_LOG_FILE.exists(),
        "open_positions": 0,
        "position_count": 0,
        "last_event_at": None,
        "last_scan_at": None,
        "last_error": None,
    }

    if OKX_BOT_STATE_FILE.exists():
        try:
            payload = json.loads(OKX_BOT_STATE_FILE.read_text(encoding="utf-8"))
            positions = payload.get("positions", []) if isinstance(payload, dict) else []
            if isinstance(positions, list):
                status["position_count"] = len([item for item in positions if isinstance(item, dict)])
                status["open_positions"] = len(
                    [
                        item
                        for item in positions
                        if isinstance(item, dict) and item.get("status") == "open"
                    ]
                )
        except (OSError, json.JSONDecodeError) as exc:
            status["last_error"] = f"bot state read failed: {exc}"

    if OKX_BOT_EVENT_LOG_FILE.exists():
        try:
            events = []
            for line in OKX_BOT_EVENT_LOG_FILE.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            if events:
                last_event = events[-1]
                status["last_event_at"] = last_event.get("created_at")
                last_scan = next((event for event in reversed(events) if event.get("type") == "scan"), None)
                last_error = next((event for event in reversed(events) if event.get("type") in OKX_BOT_PRIMARY_ERROR_EVENT_TYPES), None)
                if last_error is None:
                    last_error = next((event for event in reversed(events) if event.get("type") in OKX_BOT_ERROR_EVENT_TYPES), None)
                status["last_scan_at"] = (last_scan or {}).get("created_at")
                if last_error:
                    status["last_error"] = last_error.get("error") or status.get("last_error")
        except (OSError, json.JSONDecodeError) as exc:
            status["last_error"] = f"bot event log read failed: {exc}"

    return status


def snapshot() -> dict[str, Any]:
    discord_configured = bool(discord_webhook_url())
    okx_bot_status = load_okx_bot_status()
    site_monitor = site_monitor_snapshot()
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
            "strategy_trades": list(reversed(state.strategy_trades[-20:])),
            "updated_at": state.updated_at,
            "last_error": state.last_error or state.signal_error or state.summary_error,
            "last_summary_at": state.last_summary_at,
            "smtp_configured": False,
            "discord_configured": discord_configured,
            "discord_status": discord_status,
            "notification_configured": discord_configured,
            "market_data_source": market_data_source(),
            "okx_bot_status": okx_bot_status,
            "site_monitor": site_monitor,
        }


def initialize_state() -> None:
    PUBLIC_DIR.mkdir(exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    all_events = load_all_event_history()
    strategy_trades = seed_strategy_trades_from_events(all_events, load_strategy_trades())
    with state_lock:
        state.watchlist = load_watchlist()
        state.events = list(reversed(all_events[-50:]))
        state.sent_alerts = {
            event["event_key"]
            for event in state.events
            if isinstance(event.get("event_key"), str)
        }
        state.strategy_trades = strategy_trades
        state.strategy_stats = calculate_strategy_stats(strategy_trades)
    save_strategy_trades(strategy_trades)


async def state_event_stream():
    while True:
        data = json.dumps(snapshot(), ensure_ascii=False)
        yield f"data: {data}\n\n"
        await asyncio.sleep(3)


def create_app():
    if FastAPI is None or StreamingResponse is None or StaticFiles is None or CORSMiddleware is None:
        raise RuntimeError("FastAPI runtime is not installed; run python3 -m pip install -r requirements.txt")

    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(_app):
        initialize_state()
        monitor_stop_event = asyncio.Event()
        monitor_task = asyncio.create_task(run_monitor_loop(monitor_stop_event), name="market-monitor")
        try:
            yield
        finally:
            monitor_stop_event.set()
            try:
                await asyncio.wait_for(monitor_task, timeout=25)
            except asyncio.TimeoutError:
                monitor_task.cancel()
                await asyncio.gather(monitor_task, return_exceptions=True)
            await close_http_session()

    app = FastAPI(title="Chanlun Crypto Monitor", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/state")
    async def api_state() -> dict[str, Any]:
        return snapshot()

    @app.get("/api/health")
    async def api_health() -> dict[str, Any]:
        discord_url = discord_webhook_url()
        okx_bot_status = load_okx_bot_status()
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
            "okx_bot_state_exists": okx_bot_status["state_exists"],
            "okx_bot_event_log_exists": okx_bot_status["event_log_exists"],
            "okx_bot_open_positions": okx_bot_status["open_positions"],
            "okx_bot_position_count": okx_bot_status["position_count"],
            "okx_bot_last_event_at": okx_bot_status["last_event_at"],
            "okx_bot_last_scan_at": okx_bot_status["last_scan_at"],
            "okx_bot_last_error": okx_bot_status["last_error"],
            "site_monitor_enabled": site_monitor["enabled"],
            "site_monitor_ok": site_monitor["ok"],
            "site_monitor_last_run_at": site_monitor["last_run_at"],
            "site_monitor_failed_count": len([item for item in site_monitor["results"] if not item.get("ok")]),
        }

    @app.get("/api/okx-bot/health")
    async def api_okx_bot_health() -> dict[str, Any]:
        return load_okx_bot_health_detail()

    @app.get("/api/okx-bot/errors")
    async def api_okx_bot_errors(limit: int = 20) -> dict[str, Any]:
        normalized_limit = min(max(parse_int(limit, 20), 1), 100)
        errors = load_okx_bot_errors(normalized_limit)
        return {
            "count": len(errors),
            "errors": errors,
        }

    @app.get("/api/reports")
    async def api_reports() -> dict[str, Any]:
        reports = list_report_files()
        return {
            "count": len(reports),
            "reports": reports,
        }

    @app.get("/api/site-monitor")
    async def api_site_monitor() -> dict[str, Any]:
        return site_monitor_snapshot()

    @app.get("/api/network-test")
    async def api_network_test(symbol: str = "BTCUSDT") -> dict[str, Any]:
        normalized_symbol = symbol.upper().strip() or "BTCUSDT"
        return await asyncio.to_thread(test_external_dependencies, normalized_symbol, discord_webhook_url())

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
        normalized_fee_rate = min(max(fee_rate or 0.001, 0.0), 0.02)
        normalized_stop_mode = stop_mode if stop_mode in {"structure_atr", "atr_trailing_after_1r", "all"} else "structure_atr"
        try:
            return await asyncio.to_thread(
                run_signal_backtest_summary,
                normalized_symbol,
                normalized_interval,
                normalized_limit,
                normalized_reward_risk,
                normalized_max_hold_bars,
                normalized_fee_rate,
                normalized_stop_mode,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/events")
    async def api_events():
        return StreamingResponse(
            state_event_stream(),
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
            watchlist = normalize_watchlist(payload.get("watchlist", []))
            invalid_symbols = invalid_watchlist_symbols(watchlist)
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
