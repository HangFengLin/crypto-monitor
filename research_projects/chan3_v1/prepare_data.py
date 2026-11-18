"""Download and audit the frozen 2020-2023 CHAN3_V1 dataset.

This script does not compute strategy returns.  It only downloads official
Binance USD-M archives, validates continuity/OHLC/funding coverage, writes CSVs
and a hash manifest, and produces DATA_AUDIT.md.

Run from the repository root:
    .venv/bin/python research_projects/chan3_v1/prepare_data.py
"""
from __future__ import annotations

import bisect
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from historical_market_data import (  # noqa: E402
    download_cached,
    fetch_binance_um_klines,
    sha256_file,
)

START = pd.Timestamp("2020-01-01T00:00:00Z")
END = pd.Timestamp("2023-10-01T00:00:00Z")
SYMBOLS = ("BTCUSDT", "ETHUSDT")
INTERVAL = "15m"
BASE = "https://data.binance.vision/data/futures/um"
HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
DATA = HERE / "data"
STEP_MS = 15 * 60 * 1000
FUNDING_STEP_MS = 8 * 60 * 60 * 1000


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def month_starts(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    return list(pd.date_range(start=start, end=end, freq="MS", inclusive="left"))


def load_funding(symbol: str) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    archives: list[dict] = []
    for month in month_starts(START, END):
        label = month.strftime("%Y-%m")
        filename = f"{symbol}-fundingRate-{label}.zip"
        url = f"{BASE}/monthly/fundingRate/{symbol}/{filename}"
        dest = CACHE / "binance" / "um" / "funding" / symbol / "monthly" / filename
        path = download_cached(url, dest, checksum_url=f"{url}.CHECKSUM")
        if path is None:
            continue
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".csv")]
            if not names:
                raise RuntimeError(f"no CSV in {path}")
            with archive.open(names[0]) as stream:
                import io

                text = io.TextIOWrapper(stream, encoding="utf-8")
                for row in csv.DictReader(text):
                    timestamp = int(row["calc_time"])
                    if not (int(START.timestamp() * 1000) <= timestamp < int(END.timestamp() * 1000)):
                        continue
                    rows.append(
                        {
                            "timestamp": timestamp,
                            "funding_rate": float(row["last_funding_rate"]),
                        }
                    )
        archives.append(
            {
                "path": str(path.relative_to(HERE)),
                "sha256": sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    deduped = {row["timestamp"]: row for row in rows}
    return [deduped[key] for key in sorted(deduped)], archives


def audit_bars(bars: list[dict], symbol: str) -> dict:
    if not bars:
        raise RuntimeError(f"no bars for {symbol}")
    times = [int(bar["open_time"]) for bar in bars]
    duplicates = len(times) - len(set(times))
    gaps: list[dict] = []
    for previous, current in zip(times, times[1:]):
        difference = current - previous
        if difference != STEP_MS:
            gaps.append(
                {
                    "after": previous,
                    "before": current,
                    "delta_ms": difference,
                    "missing_bars": max(0, difference // STEP_MS - 1),
                }
            )
    bad_ohlc = 0
    for bar in bars:
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        if not (h >= max(o, c) and l <= min(o, c) and l > 0 and h > 0):
            bad_ohlc += 1
    expected = (times[-1] - times[0]) // STEP_MS + 1
    return {
        "symbol": symbol,
        "rows": len(bars),
        "first_open_time": times[0],
        "last_open_time": times[-1],
        "first_utc": pd.to_datetime(times[0], unit="ms", utc=True).isoformat(),
        "last_utc": pd.to_datetime(times[-1], unit="ms", utc=True).isoformat(),
        "expected_rows_between_endpoints": expected,
        "missing_rows": expected - len(bars),
        "duplicates": duplicates,
        "bad_ohlc": bad_ohlc,
        "gaps": gaps[:20],
        "gap_count": len(gaps),
    }


def audit_funding(rows: list[dict], bars: list[dict], symbol: str) -> dict:
    if not rows:
        raise RuntimeError(f"no funding for {symbol}")
    times = [row["timestamp"] for row in rows]
    first = int(bars[0]["open_time"])
    last = int(bars[-1]["open_time"])
    boundaries = list(range((first // FUNDING_STEP_MS) * FUNDING_STEP_MS, last + 1, FUNDING_STEP_MS))

    def has_settlement(boundary: int) -> bool:
        pos = bisect.bisect_left(times, boundary)
        return any(
            0 <= idx < len(times) and abs(times[idx] - boundary) <= 60_000
            for idx in (pos - 1, pos)
        )

    missing = [b for b in boundaries if not has_settlement(b)]
    return {
        "symbol": symbol,
        "rows": len(rows),
        "first_utc": pd.to_datetime(times[0], unit="ms", utc=True).isoformat(),
        "last_utc": pd.to_datetime(times[-1], unit="ms", utc=True).isoformat(),
        "expected_settlements": len(boundaries),
        "missing_settlements": len(missing),
        "missing_examples": missing[:10],
        "duplicates": len(times) - len(set(times)),
    }


def write_bars_csv(path: Path, bars: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["open_time", "open", "high", "low", "close", "volume", "close_time"])
        for bar in bars:
            writer.writerow(
                [
                    int(bar["open_time"]),
                    f"{float(bar['open']):.10f}",
                    f"{float(bar['high']):.10f}",
                    f"{float(bar['low']):.10f}",
                    f"{float(bar['close']):.10f}",
                    f"{float(bar['volume']):.10f}",
                    int(bar["close_time"]),
                ]
            )


def write_funding_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "funding_rate"])
        for row in rows:
            writer.writerow([int(row["timestamp"]), f"{float(row['funding_rate']):.10f}"])


def main() -> int:
    started = datetime.now(timezone.utc).isoformat()
    DATA.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "project": "CHAN3_V1",
        "created_at_utc": started,
        "source": "Binance official USD-M monthly kline and fundingRate archives",
        "start_utc": START.isoformat(),
        "end_exclusive_utc": END.isoformat(),
        "interval": INTERVAL,
        "symbols": {},
        "archives": [],
    }
    prior_use = (
        "仓库内 2024 年以前的使用记录只检索到 2023-10 之后："
        "strategy_validation 的 2023-12/2024-01 冒烟测试、T28 的 2023-11 预热、"
        "金丹 V3 的 2023-11 起预热。本数据在 2023-09-30 结束，与该边界不重叠。"
    )
    for symbol in SYMBOLS:
        print(f"[prepare] {symbol}: downloading 15m klines", flush=True)
        bars = fetch_binance_um_klines(symbol, INTERVAL, START, END, CACHE)
        bar_audit = audit_bars(bars, symbol)
        if bar_audit["missing_rows"] or bar_audit["duplicates"] or bar_audit["bad_ohlc"]:
            raise RuntimeError(f"{symbol} bar audit failed: {bar_audit}")
        write_bars_csv(DATA / f"{symbol}_15m.csv", bars)

        print(f"[prepare] {symbol}: downloading funding archives", flush=True)
        funding, funding_archives = load_funding(symbol)
        funding_audit = audit_funding(funding, bars, symbol)
        if funding_audit["duplicates"]:
            raise RuntimeError(f"{symbol} duplicate funding rows")
        write_funding_csv(DATA / f"{symbol}_funding.csv", funding)

        kline_archives = []
        for month in month_starts(START, END):
            label = month.strftime("%Y-%m")
            path = CACHE / "binance" / "um" / "klines" / symbol / INTERVAL / "monthly" / (
                f"{symbol}-{INTERVAL}-{label}.zip"
            )
            if not path.exists():
                raise RuntimeError(f"missing kline archive after download: {path}")
            kline_archives.append(
                {
                    "path": str(path.relative_to(HERE)),
                    "sha256": sha256(path),
                    "bytes": path.stat().st_size,
                }
            )
        manifest["symbols"][symbol] = {
            "bars": bar_audit,
            "funding": funding_audit,
            "bars_csv": str((DATA / f"{symbol}_15m.csv").relative_to(HERE)),
            "funding_csv": str((DATA / f"{symbol}_funding.csv").relative_to(HERE)),
            "kline_archives": kline_archives,
            "funding_archives": funding_archives,
        }
        manifest["archives"].extend(kline_archives + funding_archives)
        print(
            f"[prepare] {symbol}: {bar_audit['rows']} bars, "
            f"{funding_audit['rows']} funding rows, gaps={bar_audit['gap_count']}",
            flush=True,
        )

    manifest["prior_use_audit"] = prior_use
    manifest_path = HERE / "data_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "# CHAN3_V1 数据审计",
        "",
        f"- 窗口：{START.isoformat()} 至 {END.isoformat()}（右端不含）",
        f"- 品种：{', '.join(SYMBOLS)}；周期：{INTERVAL}",
        "- 来源：Binance 官方 USD-M 月度 K 线与资金费归档，逐文件校验官方 CHECKSUM。",
        f"- 生成时间：{started}",
        "",
        "## 此前使用边界",
        "",
        prior_use,
        "",
        "检索范围：`reports/`、`research_projects/`、`docs/` 中的日期/协议/清单；"
        "未发现本窗口 2020-01-01 至 2023-09-30 的收益研究记录。",
        "",
        "## 覆盖",
        "",
        "| 品种 | K 线行数 | 首根 UTC | 末根 UTC | 缺行 | 缺口段 | 重复 | 坏 OHLC | 资金费行 | 缺结算 |",
        "|---|---:|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for symbol in SYMBOLS:
        item = manifest["symbols"][symbol]
        b = item["bars"]
        f = item["funding"]
        lines.append(
            f"| {symbol} | {b['rows']} | {b['first_utc']} | {b['last_utc']} | "
            f"{b['missing_rows']} | {b['gap_count']} | {b['duplicates']} | {b['bad_ohlc']} | "
            f"{f['rows']} | {f['missing_settlements']} |"
        )
    lines += [
        "",
        "## 处理规则",
        "",
        "- 不做插值、不补零；缺失窗口的信号/交易直接跳过。",
        "- 资金费按 00:00/08:00/16:00 UTC 结算；缺失结算会把该笔交易标记并排除。",
        "- 所有月度归档 SHA-256 与覆盖统计在 `data_manifest.json`。",
        "",
    ]
    (HERE / "DATA_AUDIT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[prepare] wrote {manifest_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
