from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable
from datetime import timezone
from html import escape
from pathlib import Path
from typing import Any

import pandas as pd

from backtest_statistics import bootstrap_mean_summary
from historical_market_data import fetch_okx_klines_range


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Attribute completed trade outcomes to BTC and market-breadth regimes.",
    )
    parser.add_argument("--run-dir", type=Path, required=True, help="Universe backtest output directory.")
    parser.add_argument("--cache-dir", type=Path, default=Path("runtime/regime-analysis-cache"))
    parser.add_argument("--output-dir", type=Path, help="Defaults to --run-dir.")
    parser.add_argument("--benchmark-symbol", default="BTCUSDT")
    parser.add_argument("--instrument-type", default="SWAP")
    parser.add_argument("--daily-interval", default="1d")
    parser.add_argument("--warmup-days", type=int, default=260)
    parser.add_argument("--min-group-trades", type=int, default=10)
    parser.add_argument("--max-breadth-symbols", type=int, default=0, help="0 means all symbols in universe snapshot.")
    return parser.parse_args()


def as_utc_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def pct(value: float | int | None, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value) * 100:.{digits}f}%"


def num(value: float | int | None, digits: int = 4) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):.{digits}f}"


def ensure_utc(series: pd.Series) -> pd.Series:
    result = pd.to_datetime(series, utc=True, errors="coerce")
    if hasattr(result.dt, "as_unit"):
        return result.dt.as_unit("ns")
    return result


def expanding_percentile(series: pd.Series, min_history: int = 60) -> pd.Series:
    values: list[float] = []
    output: list[float] = []
    for value in series.astype(float):
        if pd.isna(value) or len(values) < min_history:
            output.append(math.nan)
        else:
            output.append(sum(item <= float(value) for item in values) / len(values))
        if not pd.isna(value):
            values.append(float(value))
    return pd.Series(output, index=series.index)


def rows_to_daily_frame(rows: list[dict[str, Any]], symbol: str) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame()
    frame["symbol"] = symbol
    frame["available_at"] = pd.to_datetime(frame["close_time"], unit="ms", utc=True)
    if hasattr(frame["available_at"].dt, "as_unit"):
        frame["available_at"] = frame["available_at"].dt.as_unit("ns")
    frame = frame.sort_values("available_at").drop_duplicates("available_at")
    numeric_cols = ["open", "high", "low", "close", "volume"]
    for col in numeric_cols:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame[["symbol", "available_at", *numeric_cols]]


def add_btc_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["btc_ret_7d"] = result["close"].pct_change(7)
    result["btc_ret_30d"] = result["close"].pct_change(30)
    result["btc_ret_60d"] = result["close"].pct_change(60)
    result["btc_ret_90d"] = result["close"].pct_change(90)
    result["btc_ma50"] = result["close"].rolling(50, min_periods=50).mean()
    result["btc_ma200"] = result["close"].rolling(200, min_periods=200).mean()
    result["btc_above_ma50"] = result["close"] > result["btc_ma50"]
    result["btc_above_ma200"] = result["close"] > result["btc_ma200"]

    previous_close = result["close"].shift(1)
    true_range = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - previous_close).abs(),
            (result["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    result["btc_atr14_pct"] = true_range.rolling(14, min_periods=14).mean() / result["close"]
    result["btc_realized_vol30"] = result["close"].pct_change().rolling(30, min_periods=30).std() * math.sqrt(365)
    close_mean = result["close"].rolling(20, min_periods=20).mean()
    close_std = result["close"].rolling(20, min_periods=20).std()
    result["btc_bb_width20"] = (4 * close_std) / close_mean
    result["btc_vol_pctile"] = expanding_percentile(result["btc_realized_vol30"])
    result["btc_bb_width_pctile"] = expanding_percentile(result["btc_bb_width20"])
    return result


def fetch_daily(symbol: str, start: pd.Timestamp, end: pd.Timestamp, instrument_type: str, cache_dir: Path) -> pd.DataFrame:
    rows = fetch_okx_klines_range(symbol, "1d", start, end, instrument_type, cache_dir)
    return rows_to_daily_frame(rows, symbol)


def add_symbol_metrics(frame: pd.DataFrame, btc_metrics: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["symbol_ret_7d"] = result["close"].pct_change(7)
    result["symbol_ret_30d"] = result["close"].pct_change(30)
    result["symbol_ret_60d"] = result["close"].pct_change(60)
    result["symbol_ret_90d"] = result["close"].pct_change(90)
    result["symbol_ma50"] = result["close"].rolling(50, min_periods=50).mean()
    result["symbol_ma200"] = result["close"].rolling(200, min_periods=200).mean()
    result["symbol_above_ma50"] = result["close"] > result["symbol_ma50"]
    result["symbol_above_ma200"] = result["close"] > result["symbol_ma200"]
    result["symbol_drawdown_30d_high"] = result["close"] / result["close"].rolling(30, min_periods=10).max() - 1
    result["symbol_realized_vol30"] = result["close"].pct_change().rolling(30, min_periods=30).std() * math.sqrt(365)
    result["symbol_vol_pctile"] = expanding_percentile(result["symbol_realized_vol30"])

    btc_reference = btc_metrics[
        ["available_at", "btc_ret_30d", "btc_ret_60d", "btc_ret_90d", "btc_realized_vol30"]
    ].sort_values("available_at")
    result = pd.merge_asof(
        result.sort_values("available_at"),
        btc_reference,
        on="available_at",
        direction="backward",
    )
    result["symbol_rel_btc_30d"] = result["symbol_ret_30d"] - result["btc_ret_30d"]
    result["symbol_rel_btc_60d"] = result["symbol_ret_60d"] - result["btc_ret_60d"]
    result["symbol_rel_btc_90d"] = result["symbol_ret_90d"] - result["btc_ret_90d"]
    result["symbol_vol_vs_btc"] = result["symbol_realized_vol30"] - result["btc_realized_vol30"]
    return result


def fetch_breadth_frame(
    symbols: Iterable[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
    instrument_type: str,
    cache_dir: Path,
    btc_metrics: pd.DataFrame,
) -> tuple[pd.DataFrame, list[dict[str, Any]], pd.DataFrame]:
    metric_frames: list[pd.DataFrame] = []
    symbol_metric_frames: list[pd.DataFrame] = []
    coverage: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            frame = fetch_daily(symbol, start, end, instrument_type, cache_dir)
        except Exception as exc:  # noqa: BLE001 - public data availability varies by listing age.
            coverage.append({"symbol": symbol, "bars": 0, "status": f"error: {exc}"})
            continue
        if frame.empty:
            coverage.append({"symbol": symbol, "bars": 0, "status": "empty"})
            continue
        frame = frame.copy()
        frame["ret_7d"] = frame["close"].pct_change(7)
        frame["ret_30d"] = frame["close"].pct_change(30)
        frame["ma50"] = frame["close"].rolling(50, min_periods=50).mean()
        frame["above_ma50"] = frame["close"] > frame["ma50"]
        symbol_metric_frames.append(add_symbol_metrics(frame, btc_metrics))
        metric_frames.append(frame[["available_at", "symbol", "close", "ret_7d", "ret_30d", "ma50", "above_ma50"]])
        coverage.append(
            {
                "symbol": symbol,
                "bars": int(len(frame)),
                "first": str(frame["available_at"].min()),
                "last": str(frame["available_at"].max()),
                "status": "ok",
            }
        )
    if not metric_frames:
        return pd.DataFrame(), coverage, pd.DataFrame()
    combined = pd.concat(metric_frames, ignore_index=True)
    symbol_metrics = pd.concat(symbol_metric_frames, ignore_index=True) if symbol_metric_frames else pd.DataFrame()
    combined["valid_ma50"] = combined["ma50"].notna()
    grouped = combined.groupby("available_at", dropna=False)
    breadth = grouped.agg(
        breadth_n=("symbol", "nunique"),
        breadth_ma50_n=("valid_ma50", "sum"),
        breadth_above_ma50=("above_ma50", "mean"),
        breadth_median_ret7=("ret_7d", "median"),
        breadth_median_ret30=("ret_30d", "median"),
    ).reset_index()
    breadth.loc[breadth["breadth_ma50_n"] < 10, "breadth_above_ma50"] = math.nan
    return breadth.sort_values("available_at"), coverage, symbol_metrics.sort_values(["symbol", "available_at"])


def bucket_direction(ret30: Any, above_ma50: Any, prefix: str = "btc") -> str:
    if pd.isna(ret30) or pd.isna(above_ma50):
        return "unknown"
    if bool(above_ma50) and float(ret30) > 0:
        return f"{prefix}_uptrend"
    if (not bool(above_ma50)) and float(ret30) < 0:
        return f"{prefix}_downtrend"
    return f"{prefix}_mixed"


def bucket_percentile(value: Any, low: float = 0.33, high: float = 0.67, prefix: str = "") -> str:
    if pd.isna(value):
        return f"{prefix}unknown" if prefix else "unknown"
    value = float(value)
    if value <= low:
        label = "low"
    elif value >= high:
        label = "high"
    else:
        label = "mid"
    return f"{prefix}{label}" if prefix else label


def bucket_breadth(value: Any) -> str:
    if pd.isna(value):
        return "breadth_unknown"
    value = float(value)
    if value >= 0.55:
        return "breadth_strong"
    if value <= 0.45:
        return "breadth_weak"
    return "breadth_neutral"


def bucket_relative_strength(value: Any, prefix: str) -> str:
    if pd.isna(value):
        return f"{prefix}_unknown"
    value = float(value)
    if value >= 0.05:
        return f"{prefix}_strong"
    if value <= -0.05:
        return f"{prefix}_weak"
    return f"{prefix}_neutral"


def bucket_drawdown(value: Any) -> str:
    if pd.isna(value):
        return "symbol_drawdown_unknown"
    value = float(value)
    if value >= -0.10:
        return "symbol_near_30d_high"
    if value <= -0.25:
        return "symbol_deep_drawdown"
    return "symbol_mid_drawdown"


def enrich_trades(trades: pd.DataFrame, btc: pd.DataFrame, breadth: pd.DataFrame, symbol_metrics: pd.DataFrame) -> pd.DataFrame:
    result = trades.copy()
    result["entry_time"] = ensure_utc(result["entry_time"])
    result["exit_time"] = ensure_utc(result["exit_time"])
    result["return_pct"] = pd.to_numeric(result["return_pct"], errors="coerce")
    result["tp_win"] = result["exit_reason"].astype(str).eq("take_profit")
    result["positive_return"] = result["return_pct"] > 0
    result = result.sort_values("entry_time")

    btc_cols = [
        "available_at",
        "close",
        "btc_ret_7d",
        "btc_ret_30d",
        "btc_ret_60d",
        "btc_ret_90d",
        "btc_ma50",
        "btc_ma200",
        "btc_above_ma50",
        "btc_above_ma200",
        "btc_atr14_pct",
        "btc_realized_vol30",
        "btc_bb_width20",
        "btc_vol_pctile",
        "btc_bb_width_pctile",
    ]
    btc_join = btc[btc_cols].rename(columns={"close": "btc_close", "available_at": "btc_daily_available_at"})
    result = pd.merge_asof(
        result,
        btc_join.sort_values("btc_daily_available_at"),
        left_on="entry_time",
        right_on="btc_daily_available_at",
        direction="backward",
    )
    if not breadth.empty:
        result = pd.merge_asof(
            result,
            breadth.sort_values("available_at"),
            left_on="entry_time",
            right_on="available_at",
            direction="backward",
        ).rename(columns={"available_at": "breadth_available_at"})
    else:
        result["breadth_available_at"] = pd.NaT
        result["breadth_n"] = math.nan
        result["breadth_ma50_n"] = math.nan
        result["breadth_above_ma50"] = math.nan
        result["breadth_median_ret7"] = math.nan
        result["breadth_median_ret30"] = math.nan

    symbol_metric_cols = [
        "symbol",
        "available_at",
        "symbol_ret_7d",
        "symbol_ret_30d",
        "symbol_ret_60d",
        "symbol_ret_90d",
        "symbol_ma50",
        "symbol_ma200",
        "symbol_above_ma50",
        "symbol_above_ma200",
        "symbol_drawdown_30d_high",
        "symbol_realized_vol30",
        "symbol_vol_pctile",
        "symbol_rel_btc_30d",
        "symbol_rel_btc_60d",
        "symbol_rel_btc_90d",
        "symbol_vol_vs_btc",
    ]
    result["__row_id"] = range(len(result))
    merged_parts: list[pd.DataFrame] = []
    if not symbol_metrics.empty:
        available_symbol_metrics = symbol_metrics[symbol_metric_cols].rename(columns={"available_at": "symbol_daily_available_at"})
        for symbol, trade_part in result.groupby("symbol", sort=False):
            metric_part = available_symbol_metrics[available_symbol_metrics["symbol"].eq(symbol)].drop(columns=["symbol"])
            if metric_part.empty:
                missing = trade_part.copy()
                missing["symbol_daily_available_at"] = pd.NaT
                for col in symbol_metric_cols:
                    if col not in {"symbol", "available_at"}:
                        missing[col] = math.nan
                merged_parts.append(missing)
                continue
            merged_parts.append(
                pd.merge_asof(
                    trade_part.sort_values("entry_time"),
                    metric_part.sort_values("symbol_daily_available_at"),
                    left_on="entry_time",
                    right_on="symbol_daily_available_at",
                    direction="backward",
                )
            )
        result = pd.concat(merged_parts, ignore_index=True).sort_values("__row_id").drop(columns=["__row_id"])
    else:
        result["symbol_daily_available_at"] = pd.NaT
        for col in symbol_metric_cols:
            if col not in {"symbol", "available_at"}:
                result[col] = math.nan
        result = result.drop(columns=["__row_id"])

    result["btc_direction_bucket"] = [
        bucket_direction(ret30, above, "btc") for ret30, above in zip(result["btc_ret_30d"], result["btc_above_ma50"])
    ]
    result["btc_ma50_bucket"] = result["btc_above_ma50"].map({True: "btc_above_ma50", False: "btc_below_ma50"}).fillna("btc_ma50_unknown")
    result["btc_ma200_bucket"] = result["btc_above_ma200"].map({True: "btc_above_ma200", False: "btc_below_ma200"}).fillna("btc_ma200_unknown")
    result["btc_vol_bucket"] = result["btc_vol_pctile"].map(lambda value: bucket_percentile(value, prefix="btc_vol_"))
    result["btc_bb_width_bucket"] = result["btc_bb_width_pctile"].map(lambda value: bucket_percentile(value, prefix="btc_bb_width_"))
    result["breadth_bucket"] = result["breadth_above_ma50"].map(bucket_breadth)
    result["breadth_ret30_bucket"] = result["breadth_median_ret30"].map(
        lambda value: "breadth_ret30_unknown"
        if pd.isna(value)
        else ("breadth_ret30_up" if float(value) > 0 else "breadth_ret30_down")
    )
    result["symbol_direction_bucket"] = [
        bucket_direction(ret30, above, "symbol") for ret30, above in zip(result["symbol_ret_30d"], result["symbol_above_ma50"])
    ]
    result["symbol_ma50_bucket"] = result["symbol_above_ma50"].map({True: "symbol_above_ma50", False: "symbol_below_ma50"}).fillna("symbol_ma50_unknown")
    result["symbol_ma200_bucket"] = result["symbol_above_ma200"].map({True: "symbol_above_ma200", False: "symbol_below_ma200"}).fillna("symbol_ma200_unknown")
    result["symbol_vol_bucket"] = result["symbol_vol_pctile"].map(lambda value: bucket_percentile(value, prefix="symbol_vol_"))
    result["symbol_drawdown_bucket"] = result["symbol_drawdown_30d_high"].map(bucket_drawdown)
    result["symbol_rel_btc_30d_bucket"] = result["symbol_rel_btc_30d"].map(lambda value: bucket_relative_strength(value, "symbol_rel30"))
    result["symbol_rel_btc_60d_bucket"] = result["symbol_rel_btc_60d"].map(lambda value: bucket_relative_strength(value, "symbol_rel60"))
    return result


def aggregate(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {
            "trades": 0,
            "tp_rate": math.nan,
            "positive_rate": math.nan,
            "avg_return_pct": math.nan,
            "median_return_pct": math.nan,
            "sum_return_pct": math.nan,
            "stop_loss_rate": math.nan,
            "protected_stop_rate": math.nan,
            "take_profit_rate": math.nan,
        }
    exits = frame["exit_reason"].astype(str)
    return {
        "trades": int(len(frame)),
        "tp_rate": float(frame["tp_win"].mean()),
        "positive_rate": float(frame["positive_return"].mean()),
        "avg_return_pct": float(frame["return_pct"].mean()),
        "median_return_pct": float(frame["return_pct"].median()),
        "sum_return_pct": float(frame["return_pct"].sum()),
        "stop_loss_rate": float(exits.eq("stop_loss").mean()),
        "protected_stop_rate": float(exits.eq("protected_stop").mean()),
        "take_profit_rate": float(exits.eq("take_profit").mean()),
    }


def grouped_stats(frame: pd.DataFrame, bucket_cols: list[str], min_group_trades: int) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for bucket_col in bucket_cols:
        for bucket_value, bucket_frame in frame.groupby(bucket_col, dropna=False):
            if len(bucket_frame) < min_group_trades:
                continue
            overall = aggregate(bucket_frame)
            row = {"bucket": bucket_col, "value": str(bucket_value), **overall}
            for sample in ["fit", "holdout"]:
                sample_stats = aggregate(bucket_frame[bucket_frame["sample"].astype(str).eq(sample)])
                for key, value in sample_stats.items():
                    row[f"{sample}_{key}"] = value
            row["holdout_minus_fit_avg_return_pct"] = row["holdout_avg_return_pct"] - row["fit_avg_return_pct"]
            rows.append(row)
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    return result.sort_values(["holdout_avg_return_pct", "avg_return_pct"], ascending=[False, False])


def environment_shift(frame: pd.DataFrame) -> pd.DataFrame:
    metrics = [
        "btc_ret_7d",
        "btc_ret_30d",
        "btc_ret_60d",
        "btc_ret_90d",
        "btc_realized_vol30",
        "btc_vol_pctile",
        "btc_atr14_pct",
        "btc_bb_width20",
        "breadth_above_ma50",
        "breadth_median_ret7",
        "breadth_median_ret30",
        "symbol_ret_30d",
        "symbol_ret_60d",
        "symbol_rel_btc_30d",
        "symbol_rel_btc_60d",
        "symbol_realized_vol30",
        "symbol_vol_pctile",
        "symbol_drawdown_30d_high",
        "symbol_vol_vs_btc",
    ]
    rows: list[dict[str, Any]] = []
    for metric in metrics:
        row: dict[str, Any] = {"metric": metric}
        for sample in ["fit", "holdout"]:
            values = pd.to_numeric(frame.loc[frame["sample"].astype(str).eq(sample), metric], errors="coerce").dropna()
            row[f"{sample}_n"] = int(values.shape[0])
            row[f"{sample}_mean"] = float(values.mean()) if not values.empty else math.nan
            row[f"{sample}_median"] = float(values.median()) if not values.empty else math.nan
        row["holdout_minus_fit_mean"] = row["holdout_mean"] - row["fit_mean"]
        rows.append(row)
    return pd.DataFrame(rows)


def bucket_distribution(frame: pd.DataFrame, bucket_cols: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for bucket_col in bucket_cols:
        values = sorted(str(item) for item in frame[bucket_col].dropna().unique())
        for value in values:
            row = {"bucket": bucket_col, "value": value}
            for sample in ["fit", "holdout"]:
                sample_frame = frame[frame["sample"].astype(str).eq(sample)]
                denominator = len(sample_frame)
                count = int(sample_frame[bucket_col].astype(str).eq(value).sum())
                row[f"{sample}_count"] = count
                row[f"{sample}_share"] = count / denominator if denominator else math.nan
            row["holdout_minus_fit_share"] = row["holdout_share"] - row["fit_share"]
            rows.append(row)
    return pd.DataFrame(rows)


def symbol_stats(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for symbol, symbol_frame in frame.groupby("symbol"):
        stats = aggregate(symbol_frame)
        rows.append({"symbol": symbol, **stats})
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    return result.sort_values(["avg_return_pct", "trades"], ascending=[True, False])


def bootstrap_mean(values: pd.Series, seed: int = 20260624, iterations: int = 5000) -> dict[str, Any]:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if len(clean) == 0:
        return {
            "n": 0,
            "mean_return_pct": math.nan,
            "ci025_return_pct": math.nan,
            "ci975_return_pct": math.nan,
            "p_nonpositive": math.nan,
            "double_cost_proxy_mean_pct": math.nan,
        }
    bootstrap = bootstrap_mean_summary(clean.tolist(), seed=seed, iterations=iterations)
    return {
        "n": int(len(clean)),
        "mean_return_pct": bootstrap["mean"],
        "ci025_return_pct": bootstrap["ci_low"],
        "ci975_return_pct": bootstrap["ci_high"],
        "p_nonpositive": bootstrap["p_nonpositive"],
        "double_cost_proxy_mean_pct": bootstrap["mean"] - 0.002,
    }


def candidate_checks(frame: pd.DataFrame, group_stats_frame: pd.DataFrame) -> pd.DataFrame:
    if group_stats_frame.empty:
        return pd.DataFrame()
    candidates = group_stats_frame[
        (group_stats_frame["fit_trades"] >= 10)
        & (group_stats_frame["holdout_trades"] >= 10)
        & (group_stats_frame["fit_avg_return_pct"] > 0)
        & (group_stats_frame["holdout_avg_return_pct"] > 0)
    ]
    rows: list[dict[str, Any]] = []
    for index, row in candidates.reset_index(drop=True).iterrows():
        bucket = str(row["bucket"])
        value = str(row["value"])
        candidate_frame = frame[frame[bucket].astype(str).eq(value)]
        for scope, scope_frame in [
            ("all", candidate_frame),
            ("fit", candidate_frame[candidate_frame["sample"].astype(str).eq("fit")]),
            ("holdout", candidate_frame[candidate_frame["sample"].astype(str).eq("holdout")]),
        ]:
            rows.append(
                {
                    "candidate": int(index + 1),
                    "bucket": bucket,
                    "value": value,
                    "scope": scope,
                    **bootstrap_mean(scope_frame["return_pct"], seed=20260624 + index * 10 + len(scope)),
                }
            )
    return pd.DataFrame(rows)


def format_stats_table(frame: pd.DataFrame) -> pd.DataFrame:
    table = frame.copy()
    for col in table.columns:
        if col.endswith("_rate") or col.endswith("_share") or col in {
            "avg_return_pct",
            "median_return_pct",
            "sum_return_pct",
            "fit_avg_return_pct",
            "holdout_avg_return_pct",
            "holdout_minus_fit_avg_return_pct",
            "mean_return_pct",
            "ci025_return_pct",
            "ci975_return_pct",
            "double_cost_proxy_mean_pct",
        }:
            table[col] = table[col].map(lambda value: pct(value))
        elif col.endswith("_mean") or col.endswith("_median") or col == "holdout_minus_fit_mean":
            table[col] = table[col].map(lambda value: pct(value) if pd.notna(value) and abs(float(value)) < 2 else num(value))
    return table


def df_html(frame: pd.DataFrame, max_rows: int = 20) -> str:
    if frame.empty:
        return "<p class='muted'>无数据。</p>"
    return format_stats_table(frame.head(max_rows)).to_html(index=False, escape=False, classes="data-table")


def make_summary(enriched: pd.DataFrame, group_stats: pd.DataFrame, shift: pd.DataFrame, coverage: list[dict[str, Any]]) -> dict[str, Any]:
    overall = aggregate(enriched)
    fit = aggregate(enriched[enriched["sample"].astype(str).eq("fit")])
    holdout = aggregate(enriched[enriched["sample"].astype(str).eq("holdout")])
    coverage_ok = [row for row in coverage if row.get("status") == "ok"]
    promising = group_stats[
        (group_stats["fit_trades"] >= 10)
        & (group_stats["holdout_trades"] >= 10)
        & (group_stats["fit_avg_return_pct"] > 0)
        & (group_stats["holdout_avg_return_pct"] > 0)
    ] if not group_stats.empty else pd.DataFrame()
    largest_shift = (
        shift.assign(abs_holdout_minus_fit_mean=shift["holdout_minus_fit_mean"].abs())
        .sort_values("abs_holdout_minus_fit_mean", ascending=False)
        .drop(columns=["abs_holdout_minus_fit_mean"])
        .head(5)
        .to_dict("records")
        if not shift.empty
        else []
    )
    return {
        "overall": overall,
        "fit": fit,
        "holdout": holdout,
        "coverage": {
            "symbols_requested": len(coverage),
            "symbols_ok": len(coverage_ok),
            "symbols_failed": len(coverage) - len(coverage_ok),
        },
        "promising_regime_count": int(len(promising)),
        "largest_environment_shifts": largest_shift,
    }


def write_html(
    path: Path,
    summary: dict[str, Any],
    shift: pd.DataFrame,
    distribution: pd.DataFrame,
    group_stats: pd.DataFrame,
    candidate_check_frame: pd.DataFrame,
    symbols: pd.DataFrame,
    coverage: list[dict[str, Any]],
    run_id: str,
) -> None:
    overall = summary["overall"]
    fit = summary["fit"]
    holdout = summary["holdout"]
    promising_note = (
        "发现 fit 与 holdout 均为正期望的单维环境切片，但仍需重跑样本外验证。"
        if summary["promising_regime_count"]
        else "未发现同时满足 fit 与 holdout 正期望且样本数足够的单维宏观/个币环境切片。"
    )
    worst_symbols = symbols.head(12) if not symbols.empty else symbols
    best_symbols = symbols.sort_values(["avg_return_pct", "trades"], ascending=[False, False]).head(12) if not symbols.empty else symbols
    failed_coverage = pd.DataFrame([row for row in coverage if row.get("status") != "ok"]).head(20)

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>底分型 Long 环境归因报告 - {escape(run_id)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #172033; }}
    h1, h2 {{ color: #101828; }}
    .card {{ background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 12px; padding: 16px 18px; margin: 16px 0; }}
    .metric-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
    .metric {{ background: white; border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px; }}
    .metric .label {{ color: #667085; font-size: 13px; }}
    .metric .value {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
    table.data-table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin: 10px 0 24px; }}
    table.data-table th, table.data-table td {{ border: 1px solid #e5e7eb; padding: 7px 8px; text-align: right; }}
    table.data-table th:first-child, table.data-table td:first-child,
    table.data-table th:nth-child(2), table.data-table td:nth-child(2) {{ text-align: left; }}
    table.data-table th {{ background: #eef2f7; }}
    .muted {{ color: #667085; }}
    .warn {{ color: #b42318; font-weight: 700; }}
    .ok {{ color: #067647; font-weight: 700; }}
  </style>
</head>
<body>
  <h1>底分型确认 Long：fit vs holdout 宏观与个币环境归因</h1>
  <p class="muted">Run: {escape(run_id)}。本报告只用于假设生成，不是上线规则，也没有修改生产配置。</p>

  <div class="card">
    <h2>结论先看</h2>
    <p><span class="warn">当前证据仍不支持上线。</span>{escape(promising_note)}</p>
    <p>这一步的价值在于定位“信号失效时的环境”，不是继续调参。下一轮如果要验证，应只选择 1-2 个宏观/个币过滤假设重新跑 universe backtest。</p>
  </div>

  <div class="metric-grid">
    <div class="metric"><div class="label">总交易</div><div class="value">{overall["trades"]}</div></div>
    <div class="metric"><div class="label">总体期望/笔</div><div class="value">{pct(overall["avg_return_pct"])}</div></div>
    <div class="metric"><div class="label">fit 期望/笔</div><div class="value">{pct(fit["avg_return_pct"])}</div></div>
    <div class="metric"><div class="label">holdout 期望/笔</div><div class="value">{pct(holdout["avg_return_pct"])}</div></div>
    <div class="metric"><div class="label">fit TP率</div><div class="value">{pct(fit["take_profit_rate"])}</div></div>
    <div class="metric"><div class="label">holdout TP率</div><div class="value">{pct(holdout["take_profit_rate"])}</div></div>
  </div>

  <h2>fit 与 holdout 的环境均值差异</h2>
  <p class="muted">环境值按交易入场时可见的最近一根已完成 BTC/市场/个币日线对齐；不使用未来日线。</p>
  {df_html(shift, 30)}

  <h2>环境桶分布变化</h2>
  {df_html(distribution.sort_values("holdout_minus_fit_share", ascending=False), 40)}

  <h2>单维宏观/个币环境切片表现</h2>
  <p class="muted">只看单维桶，最小样本数阈值为脚本参数；这是“假设生成表”，不能直接生成生产规则。</p>
  {df_html(group_stats, 40)}

  <h2>候选切片 bootstrap 压力检查</h2>
  <p class="muted">仅检查 fit 与 holdout 都为正期望且样本数足够的单维切片。double-cost proxy 使用额外 0.20% 成本压力近似。</p>
  {df_html(candidate_check_frame, 20)}

  <h2>亏损与盈利品种线索</h2>
  <h3>Bottom symbols</h3>
  {df_html(worst_symbols, 12)}
  <h3>Top symbols</h3>
  {df_html(best_symbols, 12)}

  <h2>市场广度数据覆盖</h2>
  <p>请求品种 {summary["coverage"]["symbols_requested"]} 个，成功 {summary["coverage"]["symbols_ok"]} 个，失败/无数据 {summary["coverage"]["symbols_failed"]} 个。</p>
  {df_html(failed_coverage, 20)}
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def main() -> None:
    args = parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    output_dir = (args.output_dir or run_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    trades_path = run_dir / "trades.csv"
    manifest_path = run_dir / "manifest.json"
    universe_path = run_dir / "universe_snapshot.json"
    if not trades_path.exists():
        raise FileNotFoundError(trades_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    universe = json.loads(universe_path.read_text(encoding="utf-8")) if universe_path.exists() else []
    run_id = str(manifest.get("run_id") or run_dir.name)

    trades = pd.read_csv(trades_path)
    trades["entry_time"] = ensure_utc(trades["entry_time"])
    start = as_utc_timestamp(trades["entry_time"].min()) - pd.Timedelta(days=args.warmup_days)
    end = as_utc_timestamp(trades["entry_time"].max()) + pd.Timedelta(days=3)

    btc_daily = fetch_daily(args.benchmark_symbol, start, end, args.instrument_type, args.cache_dir)
    if btc_daily.empty:
        raise RuntimeError(f"No benchmark daily data for {args.benchmark_symbol}")
    btc_metrics = add_btc_metrics(btc_daily)

    universe_symbols = [str(item.get("symbol") or "").upper() for item in universe if item.get("symbol")]
    if not universe_symbols:
        universe_symbols = sorted(str(item).upper() for item in trades["symbol"].dropna().unique())
    if args.max_breadth_symbols > 0:
        universe_symbols = universe_symbols[: args.max_breadth_symbols]
    breadth, coverage, symbol_metrics = fetch_breadth_frame(
        universe_symbols,
        start,
        end,
        args.instrument_type,
        args.cache_dir,
        btc_metrics,
    )

    enriched = enrich_trades(trades, btc_metrics, breadth, symbol_metrics)
    bucket_cols = [
        "btc_direction_bucket",
        "btc_ma50_bucket",
        "btc_ma200_bucket",
        "btc_vol_bucket",
        "btc_bb_width_bucket",
        "breadth_bucket",
        "breadth_ret30_bucket",
        "symbol_direction_bucket",
        "symbol_ma50_bucket",
        "symbol_ma200_bucket",
        "symbol_vol_bucket",
        "symbol_drawdown_bucket",
        "symbol_rel_btc_30d_bucket",
        "symbol_rel_btc_60d_bucket",
    ]
    group_stats_frame = grouped_stats(enriched, bucket_cols, args.min_group_trades)
    shift = environment_shift(enriched)
    distribution = bucket_distribution(enriched, bucket_cols)
    symbols = symbol_stats(enriched)
    candidate_check_frame = candidate_checks(enriched, group_stats_frame)
    summary = make_summary(enriched, group_stats_frame, shift, coverage)
    summary["candidate_checks"] = candidate_check_frame.to_dict("records")
    summary["run_id"] = run_id
    summary["inputs"] = {
        "trades_path": str(trades_path),
        "benchmark_symbol": args.benchmark_symbol,
        "instrument_type": args.instrument_type,
        "start": str(start),
        "end": str(end),
        "min_group_trades": args.min_group_trades,
        "generated_at": pd.Timestamp.now(tz=timezone.utc).isoformat(),
    }

    enriched.to_csv(output_dir / "trades_with_regime.csv", index=False)
    group_stats_frame.to_csv(output_dir / "regime_group_stats.csv", index=False)
    candidate_check_frame.to_csv(output_dir / "regime_candidate_checks.csv", index=False)
    shift.to_csv(output_dir / "regime_environment_shift.csv", index=False)
    distribution.to_csv(output_dir / "regime_bucket_distribution.csv", index=False)
    symbols.to_csv(output_dir / "regime_symbol_outcomes.csv", index=False)
    pd.DataFrame(coverage).to_csv(output_dir / "regime_data_coverage.csv", index=False)
    (output_dir / "regime_attribution_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    write_html(
        output_dir / "regime_attribution_report.html",
        summary,
        shift,
        distribution,
        group_stats_frame,
        candidate_check_frame,
        symbols,
        coverage,
        run_id,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
