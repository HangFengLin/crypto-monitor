from __future__ import annotations

import math
from typing import Any, Optional

import pandas as pd

try:
    import pandas_ta as ta
except ModuleNotFoundError:
    ta = None


def _series(values: list[Optional[float]]) -> pd.Series:
    return pd.Series(values, dtype="float64")


def _to_list(series: pd.Series) -> list[Optional[float]]:
    return [None if pd.isna(value) else float(value) for value in series.tolist()]


def _frame(bars: list[dict[str, Any]]) -> pd.DataFrame:
    data = pd.DataFrame(bars).copy()
    for column in ("open", "high", "low", "close", "volume"):
        if column in data:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    return data


def ema(values: list[Optional[float]], period: int) -> list[Optional[float]]:
    close = _series(values)
    return _to_list(close.ewm(span=period, adjust=False, min_periods=period).mean())


def macd(values: list[Optional[float]], fast: int, slow: int, signal: int) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    close = _series(values)
    if ta is not None:
        result = ta.macd(close, fast=fast, slow=slow, signal=signal)
        macd_line = result[f"MACD_{fast}_{slow}_{signal}"]
        signal_line = result[f"MACDs_{fast}_{slow}_{signal}"]
        hist = result[f"MACDh_{fast}_{slow}_{signal}"]
    else:
        macd_line = close.ewm(span=fast, adjust=False, min_periods=fast).mean() - close.ewm(span=slow, adjust=False, min_periods=slow).mean()
        signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
        hist = macd_line - signal_line
    return _to_list(macd_line), _to_list(signal_line), _to_list(hist)


def rsi(values: list[Optional[float]], period: int = 14) -> list[Optional[float]]:
    close = _series(values)
    if ta is not None:
        return _to_list(ta.rsi(close, length=period))
    change = close.diff()
    gain = change.clip(lower=0)
    loss = -change.clip(upper=0)
    average_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    average_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = average_gain / average_loss
    return _to_list(100 - (100 / (1 + rs)))


def mfi(bars: list[dict[str, Any]], period: int = 14) -> list[Optional[float]]:
    data = _frame(bars)
    if ta is not None:
        return _to_list(ta.mfi(data["high"], data["low"], data["close"], data["volume"], length=period))
    typical = (data["high"] + data["low"] + data["close"]) / 3
    raw_flow = typical * data["volume"]
    positive = raw_flow.where(typical > typical.shift(1), 0.0)
    negative = raw_flow.where(typical < typical.shift(1), 0.0)
    money_ratio = positive.rolling(period, min_periods=period).sum() / negative.rolling(period, min_periods=period).sum()
    return _to_list(100 - (100 / (1 + money_ratio)))


def atr(bars: list[dict[str, Any]], period: int = 14) -> list[Optional[float]]:
    data = _frame(bars)
    if ta is not None:
        return _to_list(ta.atr(data["high"], data["low"], data["close"], length=period))
    previous_close = data["close"].shift(1)
    true_range = pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - previous_close).abs(),
            (data["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return _to_list(true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean())


def adx(bars: list[dict[str, Any]], period: int = 14) -> list[Optional[float]]:
    data = _frame(bars)
    if ta is not None:
        result = ta.adx(data["high"], data["low"], data["close"], length=period)
        return _to_list(result[f"ADX_{period}"])

    up_move = data["high"].diff()
    down_move = -data["low"].diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    true_range = pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - data["close"].shift(1)).abs(),
            (data["low"] - data["close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    smoothed_tr = true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / smoothed_tr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / smoothed_tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return _to_list(dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean())


def chop(bars: list[dict[str, Any]], period: int = 14) -> list[Optional[float]]:
    data = _frame(bars)
    previous_close = data["close"].shift(1)
    true_range = pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - previous_close).abs(),
            (data["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    high_max = data["high"].rolling(period, min_periods=period).max()
    low_min = data["low"].rolling(period, min_periods=period).min()
    price_range = (high_max - low_min).replace(0, pd.NA)
    ratio = true_range.rolling(period, min_periods=period).sum() / price_range
    ratio = ratio.where(ratio > 0)
    value = 100 * ratio.map(lambda item: math.log10(item) if pd.notna(item) and item > 0 else pd.NA) / math.log10(period)
    return _to_list(value)


def rolling_std(values: list[Optional[float]], period: int) -> list[Optional[float]]:
    return _to_list(_series(values).rolling(period, min_periods=period).std(ddof=0))


def obv(values: list[Optional[float]], volumes: list[Optional[float]]) -> list[Optional[float]]:
    close = _series(values)
    volume = _series(volumes).fillna(0)
    direction = close.diff().apply(lambda value: 1 if value > 0 else -1 if value < 0 else 0)
    return _to_list((direction * volume).cumsum())


def rolling_average(values: list[Optional[float]], period: int) -> list[Optional[float]]:
    return _to_list(_series(values).rolling(period, min_periods=period).mean())


def calculate_indicators(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not bars:
        return []

    data = _frame(bars)
    close = data["close"]
    volume = data["volume"]
    macd_line, macd_signal, macd_hist = macd(close.tolist(), 12, 26, 9)
    macd_fast, _, _ = macd(close.tolist(), 8, 17, 6)

    data["macd"] = macd_line
    data["macd_signal"] = macd_signal
    data["macd_hist"] = macd_hist
    data["macd_fast"] = macd_fast
    data["ema20"] = ema(close.tolist(), 20)
    data["ema60"] = ema(close.tolist(), 60)
    data["ema200"] = ema(close.tolist(), 200)
    data["rsi"] = rsi(close.tolist(), 14)
    data["mfi"] = mfi(bars, 14)
    data["atr"] = atr(bars, 14)
    data["adx"] = adx(bars, 14)
    data["chop"] = chop(bars, 14)

    atr_ma50 = data["atr"].rolling(50, min_periods=50).mean()
    bb_mid = close.rolling(20, min_periods=20).mean()
    bb_std = close.rolling(20, min_periods=20).std(ddof=0)
    bb_upper = bb_mid + bb_std * 2
    bb_lower = bb_mid - bb_std * 2
    bb_width = bb_upper - bb_lower
    data["atr_pct"] = data["atr"] / close
    data["atr_ratio"] = data["atr"] / atr_ma50
    data["bb_width"] = bb_width / bb_mid
    data["bb_percent"] = (close - bb_lower) / bb_width
    data["obv"] = obv(close.tolist(), volume.tolist())
    data["obv_ma20"] = data["obv"].rolling(20, min_periods=20).mean()
    data["obv_slope"] = data["obv"] - data["obv"].shift(5)
    data["volume_ma20"] = volume.rolling(20, min_periods=20).mean()
    data["volume_ratio"] = volume / data["volume_ma20"]
    data["trend"] = "sideways"
    data.loc[data["ema20"] > data["ema60"], "trend"] = "up"
    data.loc[data["ema20"] < data["ema60"], "trend"] = "down"

    records = data.to_dict("records")
    return [
        {key: None if pd.isna(value) else value for key, value in record.items()}
        for record in records
    ]
