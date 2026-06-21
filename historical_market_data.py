from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

from data_client import OKX_BASE_URL, OKX_HISTORY_CANDLES_PATH, interval_ms, normalize_okx_inst_id, okx_bar, read_json_url


BINANCE_DATA_BASE = "https://data.binance.vision/data/futures/um"
BINANCE_FUTURES_BASE = "https://fapi.binance.com"
OKX_FUNDING_HISTORY_PATH = "/api/v5/public/funding-rate-history"
OKX_HISTORICAL_DOWNLOAD_LINK = "/priapi/v5/broker/public/trade-data/download-link"


def utc_millis(value: date | datetime | str | pd.Timestamp) -> int:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return int(timestamp.timestamp() * 1000)


def pandas_frequency(interval: str) -> str:
    """Translate exchange interval syntax to unambiguous pandas offsets."""
    if interval.endswith("m") and interval[:-1].isdigit():
        return f"{interval[:-1]}min"
    if interval.endswith("h") and interval[:-1].isdigit():
        return f"{interval[:-1]}h"
    if interval.endswith("d") and interval[:-1].isdigit():
        return f"{interval[:-1]}D"
    return interval


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _range_cache_path(cache_dir: Optional[Path], parts: list[str], start_ms: int, end_ms: int) -> Optional[Path]:
    if cache_dir is None:
        return None
    return cache_dir.joinpath(*parts, f"{start_ms}_{end_ms}.json")


def _load_cached_rows(path: Optional[Path]) -> Optional[list[dict[str, Any]]]:
    if path is None or not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else None


def _post_json_url(url: str, payload: dict[str, Any], *, timeout: int = 30, attempts: int = 8) -> Any:
    """POST JSON to an official public endpoint with bounded 429 backoff."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    last_error: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                data=body,
                headers={
                    "User-Agent": "crypto-strategy-validation/1.0",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            retry_after: Optional[float] = None
            if exc.code == 429 and exc.headers:
                try:
                    retry_after = float(exc.headers.get("Retry-After"))
                except (TypeError, ValueError):
                    retry_after = None
            time.sleep(retry_after if retry_after is not None else min(30.0, 0.75 * (2**attempt)))
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(min(30.0, 0.5 * (2**attempt)))
    if last_error:
        raise last_error
    raise TimeoutError("POST request failed")


def _ascii_url(url: str) -> str:
    """Percent-encode non-ASCII archive path segments without touching queries."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            urllib.parse.quote(parts.path, safe="/%"),
            parts.query,
            parts.fragment,
        )
    )


def download_cached(url: str, destination: Path, *, checksum_url: Optional[str] = None, attempts: int = 6) -> Optional[Path]:
    """Download an immutable public archive once and verify its published checksum."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0:
        return destination
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    last_error: Optional[BaseException] = None
    for attempt in range(attempts):
        retry_after: Optional[float] = None
        try:
            request = urllib.request.Request(_ascii_url(url), headers={"User-Agent": "crypto-strategy-validation/1.0"})
            with urllib.request.urlopen(request, timeout=30) as response, temporary.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
            if checksum_url:
                checksum_request = urllib.request.Request(_ascii_url(checksum_url), headers={"User-Agent": "crypto-strategy-validation/1.0"})
                with urllib.request.urlopen(checksum_request, timeout=20) as response:
                    expected = response.read().decode("utf-8").strip().split()[0].lower()
                actual = sha256_file(temporary)
                if expected and actual != expected:
                    raise ValueError(f"checksum mismatch for {url}: {actual} != {expected}")
            temporary.replace(destination)
            return destination
        except urllib.error.HTTPError as exc:
            temporary.unlink(missing_ok=True)
            if exc.code == 404:
                return None
            last_error = exc
            if exc.code == 429 and exc.headers:
                try:
                    retry_after = float(exc.headers.get("Retry-After"))
                except (TypeError, ValueError):
                    retry_after = None
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            temporary.unlink(missing_ok=True)
            last_error = exc
        time.sleep(retry_after if retry_after is not None else min(30.0, 0.5 * (2**attempt)))
    if last_error:
        raise RuntimeError(f"download failed: {url}: {last_error}") from last_error
    return None


def _month_starts(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    current = start.tz_localize(None).to_period("M").start_time.tz_localize("UTC")
    last = end.tz_localize(None).to_period("M").start_time.tz_localize("UTC")
    months = []
    while current <= last:
        months.append(current)
        current = current + pd.offsets.MonthBegin(1)
    return months


def _days(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    return list(pd.date_range(start.normalize(), end.normalize(), freq="D", tz="UTC"))


def _read_zip_rows(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not names:
            return []
        with archive.open(names[0]) as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8")
            return list(csv.reader(text))


def _parse_binance_kline_rows(rows: Iterable[list[str]], start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    for row in rows:
        if not row or not str(row[0]).lstrip("-").isdigit() or len(row) < 7:
            continue
        open_time = int(row[0])
        if open_time < start_ms or open_time >= end_ms:
            continue
        bars.append(
            {
                "open_time": open_time,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "close_time": int(row[6]),
                "time": pd.to_datetime(open_time, unit="ms", utc=True),
            }
        )
    return bars


def fetch_binance_um_klines(
    symbol: str,
    interval: str,
    start: date | datetime | str | pd.Timestamp,
    end: date | datetime | str | pd.Timestamp,
    cache_dir: Path,
) -> list[dict[str, Any]]:
    """Load Binance USD-M futures klines from monthly archives with daily fallback."""
    start_ts = pd.Timestamp(start, tz="UTC") if pd.Timestamp(start).tzinfo is None else pd.Timestamp(start).tz_convert("UTC")
    end_ts = pd.Timestamp(end, tz="UTC") if pd.Timestamp(end).tzinfo is None else pd.Timestamp(end).tz_convert("UTC")
    start_ms, end_ms = utc_millis(start_ts), utc_millis(end_ts)
    archive_root = cache_dir / "binance" / "um" / "klines" / symbol.upper() / interval
    bars: list[dict[str, Any]] = []
    current_month = pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None).to_period("M")
    for month in _month_starts(start_ts, end_ts):
        label = month.strftime("%Y-%m")
        filename = f"{symbol.upper()}-{interval}-{label}.zip"
        url = f"{BINANCE_DATA_BASE}/monthly/klines/{symbol.upper()}/{interval}/{filename}"
        month_period = month.tz_localize(None).to_period("M")
        # The current UTC month cannot have a complete monthly archive yet.
        # Going straight to immutable daily packages makes a fully populated
        # cache genuinely reusable without a network probe for a known 404.
        path = None
        if month_period < current_month:
            path = download_cached(url, archive_root / "monthly" / filename, checksum_url=f"{url}.CHECKSUM")
        if path is not None:
            bars.extend(_parse_binance_kline_rows(_read_zip_rows(path), start_ms, end_ms))
            continue
        # A completed month with any listed-contract data has an immutable
        # monthly package. A 404 therefore means the contract did not yet
        # exist; probing every daily URL only creates hundreds of avoidable
        # requests. Daily fallback is reserved for the still-open UTC month.
        if month_period < current_month:
            continue
        month_end = min(end_ts, month + pd.offsets.MonthBegin(1))
        for day in _days(max(start_ts, month), month_end - pd.Timedelta(milliseconds=1)):
            day_label = day.strftime("%Y-%m-%d")
            daily_name = f"{symbol.upper()}-{interval}-{day_label}.zip"
            daily_url = f"{BINANCE_DATA_BASE}/daily/klines/{symbol.upper()}/{interval}/{daily_name}"
            daily_path = download_cached(daily_url, archive_root / "daily" / daily_name, checksum_url=f"{daily_url}.CHECKSUM")
            if daily_path is not None:
                bars.extend(_parse_binance_kline_rows(_read_zip_rows(daily_path), start_ms, end_ms))
    deduplicated = {int(bar["open_time"]): bar for bar in bars}
    return [deduplicated[key] for key in sorted(deduplicated)]


def fetch_binance_funding_history(
    symbol: str,
    start: date | datetime | str | pd.Timestamp,
    end: date | datetime | str | pd.Timestamp,
    cache_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    start_ms, end_ms = utc_millis(start), utc_millis(end)
    cache_path = _range_cache_path(cache_dir, ["binance", "um", "funding", symbol.upper()], start_ms, end_ms)
    cached = _load_cached_rows(cache_path)
    if cached is not None:
        return cached
    cursor = start_ms
    rows: list[dict[str, Any]] = []
    while cursor < end_ms:
        params = urllib.parse.urlencode({"symbol": symbol.upper(), "startTime": cursor, "endTime": end_ms - 1, "limit": 1000})
        batch = read_json_url(f"{BINANCE_FUTURES_BASE}/fapi/v1/fundingRate?{params}")
        if not batch:
            break
        rows.extend(
            {"timestamp": int(item["fundingTime"]), "funding_rate": float(item["fundingRate"]), "source": "binance_um"}
            for item in batch
            if start_ms <= int(item["fundingTime"]) < end_ms
        )
        next_cursor = int(batch[-1]["fundingTime"]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.08)
    result = sorted({row["timestamp"]: row for row in rows}.values(), key=lambda row: row["timestamp"])
    if cache_path is not None:
        write_json(cache_path, result)
    return result


def fetch_okx_klines_range(
    symbol: str,
    interval: str,
    start: date | datetime | str | pd.Timestamp,
    end: date | datetime | str | pd.Timestamp,
    instrument_type: str = "SWAP",
    cache_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    start_ms, end_ms = utc_millis(start), utc_millis(end)
    inst_id = normalize_okx_inst_id(symbol, instrument_type)
    cache_path = _range_cache_path(cache_dir, ["okx", instrument_type.lower(), "klines", inst_id, interval], start_ms, end_ms)
    cached = _load_cached_rows(cache_path)
    if cached is not None:
        for bar in cached:
            bar["time"] = pd.Timestamp(bar["time"])
        return cached
    # Start at the requested historical boundary instead of paging backwards
    # from "now" when reproducing an older experiment.
    after: Optional[int] = end_ms
    rows: list[list[Any]] = []
    while True:
        query: dict[str, Any] = {"instId": inst_id, "bar": okx_bar(interval), "limit": 100}
        if after is not None:
            query["after"] = after
        payload = read_json_url(f"{OKX_BASE_URL}{OKX_HISTORY_CANDLES_PATH}?{urllib.parse.urlencode(query)}")
        if str(payload.get("code")) != "0":
            raise RuntimeError(f"OKX candles failed: {payload.get('msg') or payload}")
        batch = payload.get("data", [])
        if not batch:
            break
        rows.extend(batch)
        earliest = int(batch[-1][0])
        if earliest < start_ms:
            break
        if after is not None and earliest >= after:
            break
        after = earliest
        time.sleep(0.08)
    bars = []
    for row in rows:
        open_time = int(row[0])
        if not (start_ms <= open_time < end_ms):
            continue
        bars.append(
            {
                "open_time": open_time,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "close_time": open_time + interval_ms(interval) - 1,
                "time": pd.to_datetime(open_time, unit="ms", utc=True),
            }
        )
    result = [dict(item) for _, item in sorted({bar["open_time"]: bar for bar in bars}.items())]
    if cache_path is not None:
        write_json(cache_path, result)
    return result


def fetch_okx_funding_history(
    symbol: str,
    start: date | datetime | str | pd.Timestamp,
    end: date | datetime | str | pd.Timestamp,
    instrument_type: str = "SWAP",
    cache_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    start_ms, end_ms = utc_millis(start), utc_millis(end)
    inst_id = normalize_okx_inst_id(symbol, instrument_type)
    cache_path = _range_cache_path(cache_dir, ["okx", instrument_type.lower(), "funding_v2", inst_id], start_ms, end_ms)
    cached = _load_cached_rows(cache_path)
    if cached is not None:
        return cached
    rows = fetch_okx_funding_archives(inst_id, start, end, cache_dir)
    # Monthly archives are immutable and cover completed months. The public
    # REST endpoint supplies the current tail only; using it as the sole source
    # would truncate a two-year study to roughly three months.
    rest_start_ms = max(start_ms, max((int(row["timestamp"]) for row in rows), default=start_ms - 1) + 1)
    after: Optional[int] = end_ms
    while True:
        query: dict[str, Any] = {"instId": inst_id, "limit": 100}
        if after is not None:
            query["after"] = after
        payload = read_json_url(f"{OKX_BASE_URL}{OKX_FUNDING_HISTORY_PATH}?{urllib.parse.urlencode(query)}")
        if str(payload.get("code")) != "0":
            raise RuntimeError(f"OKX funding failed: {payload.get('msg') or payload}")
        batch = payload.get("data", [])
        if not batch:
            break
        for item in batch:
            timestamp = int(item["fundingTime"])
            if rest_start_ms <= timestamp < end_ms:
                rows.append({"timestamp": timestamp, "funding_rate": float(item["fundingRate"]), "source": "okx"})
        earliest = int(batch[-1]["fundingTime"])
        if earliest < rest_start_ms:
            break
        if after is not None and earliest >= after:
            break
        after = earliest
        time.sleep(0.08)
    result = sorted({row["timestamp"]: row for row in rows}.values(), key=lambda row: row["timestamp"])
    if cache_path is not None:
        write_json(cache_path, result)
    return result


def fetch_okx_funding_archives(
    inst_id: str,
    start: date | datetime | str | pd.Timestamp,
    end: date | datetime | str | pd.Timestamp,
    cache_dir: Optional[Path],
) -> list[dict[str, Any]]:
    """Download OKX's official monthly funding-rate archives by calendar year."""
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    start_ts = start_ts.tz_localize("UTC") if start_ts.tzinfo is None else start_ts.tz_convert("UTC")
    end_ts = end_ts.tz_localize("UTC") if end_ts.tzinfo is None else end_ts.tz_convert("UTC")
    start_ms, end_ms = utc_millis(start_ts), utc_millis(end_ts)
    inst_family = inst_id.removesuffix("-SWAP")
    rows: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for year in range(start_ts.year, end_ts.year + 1):
        year_start = pd.Timestamp(year=year, month=1, day=1, tz="UTC")
        year_end = pd.Timestamp(year=year, month=12, day=31, tz="UTC")
        query_start = max(start_ts.normalize(), year_start)
        query_end = min(end_ts - pd.Timedelta(milliseconds=1), year_end)
        if query_end < query_start:
            continue
        payload = {
            "module": "3",
            "instType": "SWAP",
            "instQueryParam": {"instFamilyList": [inst_family]},
            "dateQuery": {
                "dateAggrType": "monthly",
                "begin": str(utc_millis(query_start)),
                "end": str(utc_millis(query_end)),
            },
        }
        response = _post_json_url(f"{OKX_BASE_URL}{OKX_HISTORICAL_DOWNLOAD_LINK}", payload)
        if str(response.get("code")) != "0":
            raise RuntimeError(f"OKX historical funding links failed: {response.get('msg') or response}")
        details = (response.get("data") or {}).get("details") or []
        for detail in details:
            for group in detail.get("groupDetails") or []:
                url = str(group.get("url") or "")
                filename = str(group.get("filename") or Path(urllib.parse.urlsplit(url).path).name)
                if not url or not filename or url in seen_urls:
                    continue
                seen_urls.add(url)
                if cache_dir is None:
                    root = Path("/tmp") / "strategy-validation-okx-funding" / inst_id
                else:
                    root = cache_dir / "okx" / "swap" / "funding_archives" / inst_id
                path = download_cached(url, root / filename, checksum_url=None)
                if path is not None:
                    rows.extend(_parse_okx_funding_archive(path, start_ms, end_ms))
        time.sleep(0.15)
    return sorted({row["timestamp"]: row for row in rows}.values(), key=lambda row: row["timestamp"])


def _parse_okx_funding_archive(path: Path, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for row in _read_zip_rows(path):
        if len(row) < 3 or not str(row[2]).isdigit():
            continue
        timestamp = int(row[2])
        if start_ms <= timestamp < end_ms:
            parsed.append(
                {
                    "timestamp": timestamp,
                    "funding_rate": float(row[1]),
                    "source": "okx_historical_archive",
                }
            )
    return parsed


def fetch_binance_oi_metrics(
    symbol: str,
    signal_dates: Iterable[date | datetime | str | pd.Timestamp],
    cache_dir: Path,
) -> list[dict[str, Any]]:
    """Load only signal-day OI archives plus the prior day needed by the rolling baseline."""
    days: set[pd.Timestamp] = set()
    for value in signal_dates:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        normalized = timestamp.normalize()
        days.add(normalized)
        days.add(normalized - pd.Timedelta(days=1))
    rows: list[dict[str, Any]] = []
    root = cache_dir / "binance" / "um" / "metrics" / symbol.upper()
    for day in sorted(days):
        label = day.strftime("%Y-%m-%d")
        filename = f"{symbol.upper()}-metrics-{label}.zip"
        url = f"{BINANCE_DATA_BASE}/daily/metrics/{symbol.upper()}/{filename}"
        path = download_cached(url, root / filename, checksum_url=f"{url}.CHECKSUM")
        if path is None:
            continue
        for index, row in enumerate(_read_zip_rows(path)):
            if index == 0 and row and row[0] == "create_time":
                continue
            if len(row) < 4:
                continue
            timestamp = pd.to_datetime(row[0], utc=True, errors="coerce")
            if pd.isna(timestamp):
                continue
            rows.append(
                {
                    "timestamp": int(timestamp.timestamp() * 1000),
                    "open_interest": float(row[2]),
                    "open_interest_value": float(row[3]),
                    "source": "binance_vision_um_metrics",
                }
            )
    return sorted({row["timestamp"]: row for row in rows}.values(), key=lambda row: row["timestamp"])


def enrich_microstructure(
    bars: list[dict[str, Any]],
    funding_rows: Iterable[dict[str, Any]],
    oi_rows: Iterable[dict[str, Any]],
    interval: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """As-of align funding and rolling OI without any future backfill."""
    if not bars:
        return [], {"bars": 0, "funding_coverage": 0.0, "oi_coverage": 0.0}
    funding = pd.DataFrame(list(funding_rows))
    oi = pd.DataFrame(list(oi_rows))
    if not funding.empty:
        funding = funding.sort_values("timestamp")
    if not oi.empty:
        oi["time"] = pd.to_datetime(oi["timestamp"], unit="ms", utc=True)
        series = oi.set_index("time")["open_interest"].sort_index().resample(pandas_frequency(interval), label="right", closed="right").last().dropna()
        baseline = series.shift(1).rolling(19, min_periods=19).mean()
        ratios = (series / baseline).dropna()
        oi_times = [int(value.timestamp() * 1000) for value in ratios.index]
        oi_values = ratios.tolist()
    else:
        oi_times, oi_values = [], []

    funding_records = funding.to_dict("records") if not funding.empty else []
    funding_index = -1
    oi_index = -1
    funding_value: Optional[float] = None
    oi_value: Optional[float] = None
    enriched: list[dict[str, Any]] = []
    funding_count = 0
    oi_count = 0
    for bar in sorted(bars, key=lambda item: int(item["open_time"])):
        # Binance/OKX bars close one millisecond before the next bar opens.
        # Using open+interval here would incorrectly admit a funding/OI record
        # published exactly at the next open into the just-closed signal bar.
        decision_time = int(bar.get("close_time") or (int(bar.get("open_time", 0)) + interval_ms(interval) - 1))
        while funding_index + 1 < len(funding_records) and int(funding_records[funding_index + 1]["timestamp"]) <= decision_time:
            funding_index += 1
            funding_value = float(funding_records[funding_index]["funding_rate"])
        while oi_index + 1 < len(oi_times) and oi_times[oi_index + 1] <= decision_time:
            oi_index += 1
            oi_value = float(oi_values[oi_index])
        item = dict(bar)
        funding_is_fresh = (
            funding_index >= 0
            and decision_time - int(funding_records[funding_index]["timestamp"]) <= int(pd.Timedelta(hours=12).total_seconds() * 1000)
        )
        if funding_value is not None and funding_is_fresh:
            item["funding_rate"] = funding_value
            funding_count += 1
        oi_is_fresh = oi_index >= 0 and decision_time - oi_times[oi_index] <= interval_ms(interval)
        if oi_value is not None and oi_is_fresh:
            item["open_interest_ratio"] = oi_value
            oi_count += 1
        enriched.append(item)
    total = len(enriched)
    return enriched, {
        "bars": total,
        "funding_coverage": funding_count / total if total else 0.0,
        "oi_coverage": oi_count / total if total else 0.0,
        "funding_rows": len(funding_records),
        "oi_rows": len(oi_times),
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
