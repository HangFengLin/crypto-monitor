from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Optional

from config import config_value, load_config
from indicators import rolling_average


_CONFIG = load_config()
MA_FAST_PERIOD = int(config_value(_CONFIG, "strategy", "ma_fast_period", 5))
MA_SLOW_PERIOD = int(config_value(_CONFIG, "strategy", "ma_slow_period", 10))
MIN_DIVERGENCE_STRENGTH = float(config_value(_CONFIG, "strategy", "min_divergence_strength", 0.25))
BUY_RSI_THRESHOLD = float(config_value(_CONFIG, "strategy", "buy_rsi_threshold", 40))
BUY_VOLUME_RATIO = float(config_value(_CONFIG, "strategy", "buy_volume_ratio", 0.8))
SELL_RSI_THRESHOLD = float(config_value(_CONFIG, "strategy", "sell_rsi_threshold", 60))
SELL_VOLUME_RATIO = float(config_value(_CONFIG, "strategy", "sell_volume_ratio", 0.8))
CONFIRM_MAX_BARS = int(config_value(_CONFIG, "strategy", "confirm_max_bars", 12))
CONFIRMATION_MODE = str(config_value(_CONFIG, "strategy", "confirmation_mode", "either"))
EXTREME_WINDOW = int(config_value(_CONFIG, "strategy", "extreme_window", 3))
MAX_EXTREME_LAG_BARS = int(config_value(_CONFIG, "strategy", "max_extreme_lag_bars", 3))
HIGHER_TREND_INTERVAL = str(config_value(_CONFIG, "strategy", "higher_trend_interval", "4h"))
REQUIRE_HIGHER_TREND_ALIGNMENT = bool(config_value(_CONFIG, "strategy", "require_higher_trend_alignment", True))
VOLUME_CONFIRMATION_ENABLED = bool(config_value(_CONFIG, "strategy", "volume_confirmation_enabled", True))
VOLUME_CONTRACTION_RATIO = float(config_value(_CONFIG, "strategy", "volume_contraction_ratio", 0.75))
VOLUME_BREAKOUT_RATIO = float(config_value(_CONFIG, "strategy", "volume_breakout_ratio", 1.5))
OBV_CONFIRMATION_ENABLED = bool(config_value(_CONFIG, "strategy", "obv_confirmation_enabled", True))
ATR_STOP_MULTIPLIER = float(config_value(_CONFIG, "strategy", "atr_stop_multiplier", 1.5))
CHOP_FILTER_ENABLED = bool(config_value(_CONFIG, "strategy", "chop_filter_enabled", True))
MAX_CHOP = float(config_value(_CONFIG, "strategy", "max_chop", 61.8))
MIN_BB_WIDTH = float(config_value(_CONFIG, "strategy", "min_bb_width", 0.012))
MICROSTRUCTURE_ENABLED = bool(config_value(_CONFIG, "strategy", "microstructure_enabled", True))
REQUIRE_MICROSTRUCTURE = bool(config_value(_CONFIG, "strategy", "require_microstructure", False))
LONG_FUNDING_RATE_MAX = float(config_value(_CONFIG, "strategy", "long_funding_rate_max", -0.0001))
SHORT_FUNDING_RATE_MIN = float(config_value(_CONFIG, "strategy", "short_funding_rate_min", 0.0001))
OPEN_INTEREST_SURGE_RATIO = float(config_value(_CONFIG, "strategy", "open_interest_surge_ratio", 1.1))
MFI_LONG_MAX = float(config_value(_CONFIG, "strategy", "mfi_long_max", 35))
MFI_SHORT_MIN = float(config_value(_CONFIG, "strategy", "mfi_short_min", 65))
ADX_LONG_MAX = float(config_value(_CONFIG, "strategy", "adx_long_max", 28))
ADX_SHORT_MAX = float(config_value(_CONFIG, "strategy", "adx_short_max", 32))
BB_LONG_MAX = float(config_value(_CONFIG, "strategy", "bb_long_max", 0.35))
BB_SHORT_MIN = float(config_value(_CONFIG, "strategy", "bb_short_min", 0.65))
BB_WIDTH_SCORE_MIN = float(config_value(_CONFIG, "strategy", "bb_width_score_min", 0.012))
BB_WIDTH_SCORE_MAX = float(config_value(_CONFIG, "strategy", "bb_width_score_max", 0.12))
ATR_RATIO_MIN = float(config_value(_CONFIG, "strategy", "atr_ratio_min", 0.7))
ATR_RATIO_MAX = float(config_value(_CONFIG, "strategy", "atr_ratio_max", 1.8))
ATR_PCT_MAX = float(config_value(_CONFIG, "strategy", "atr_pct_max", 0.035))
EMA200_DISTANCE_MAX = float(config_value(_CONFIG, "strategy", "ema200_distance_max", 0.08))
STRONG_SCORE = int(config_value(_CONFIG, "strategy", "strong_score", 10))
NORMAL_SCORE = int(config_value(_CONFIG, "strategy", "normal_score", 7))


@dataclass(frozen=True)
class StrategyConfig:
    ma_fast_period: int = MA_FAST_PERIOD
    ma_slow_period: int = MA_SLOW_PERIOD
    min_divergence_strength: float = MIN_DIVERGENCE_STRENGTH
    buy_rsi_threshold: float = BUY_RSI_THRESHOLD
    buy_volume_ratio: float = BUY_VOLUME_RATIO
    sell_rsi_threshold: float = SELL_RSI_THRESHOLD
    sell_volume_ratio: float = SELL_VOLUME_RATIO
    confirm_max_bars: int = CONFIRM_MAX_BARS
    confirmation_mode: str = CONFIRMATION_MODE
    extreme_window: int = EXTREME_WINDOW
    max_extreme_lag_bars: int = MAX_EXTREME_LAG_BARS
    require_higher_trend_alignment: bool = REQUIRE_HIGHER_TREND_ALIGNMENT
    volume_confirmation_enabled: bool = VOLUME_CONFIRMATION_ENABLED
    volume_contraction_ratio: float = VOLUME_CONTRACTION_RATIO
    volume_breakout_ratio: float = VOLUME_BREAKOUT_RATIO
    obv_confirmation_enabled: bool = OBV_CONFIRMATION_ENABLED
    atr_stop_multiplier: float = ATR_STOP_MULTIPLIER
    chop_filter_enabled: bool = CHOP_FILTER_ENABLED
    max_chop: float = MAX_CHOP
    min_bb_width: float = MIN_BB_WIDTH
    microstructure_enabled: bool = MICROSTRUCTURE_ENABLED
    require_microstructure: bool = REQUIRE_MICROSTRUCTURE
    long_funding_rate_max: float = LONG_FUNDING_RATE_MAX
    short_funding_rate_min: float = SHORT_FUNDING_RATE_MIN
    open_interest_surge_ratio: float = OPEN_INTEREST_SURGE_RATIO
    mfi_long_max: float = MFI_LONG_MAX
    mfi_short_min: float = MFI_SHORT_MIN
    adx_long_max: float = ADX_LONG_MAX
    adx_short_max: float = ADX_SHORT_MAX
    bb_long_max: float = BB_LONG_MAX
    bb_short_min: float = BB_SHORT_MIN
    bb_width_score_min: float = BB_WIDTH_SCORE_MIN
    bb_width_score_max: float = BB_WIDTH_SCORE_MAX
    atr_ratio_min: float = ATR_RATIO_MIN
    atr_ratio_max: float = ATR_RATIO_MAX
    atr_pct_max: float = ATR_PCT_MAX
    ema200_distance_max: float = EMA200_DISTANCE_MAX
    strong_score: int = STRONG_SCORE
    normal_score: int = NORMAL_SCORE


DEFAULT_CONFIG = StrategyConfig()

NUMERIC_BAR_KEYS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "macd",
    "macd_signal",
    "macd_hist",
    "macd_fast",
    "ema20",
    "ema60",
    "ema200",
    "rsi",
    "mfi",
    "atr",
    "adx",
    "chop",
    "atr_pct",
    "atr_ratio",
    "bb_width",
    "bb_percent",
    "obv",
    "obv_ma20",
    "obv_slope",
    "volume_ma20",
    "volume_ratio",
    "higher_close",
    "higher_ema20",
    "higher_ema60",
    "higher_ema200",
    "higher_macd",
    "funding_rate",
    "lastFundingRate",
    "open_interest_ratio",
    "oi_ratio",
}


def _finite_float(value: Any) -> Optional[float]:
    if value in ("", None) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def clean_strategy_bars(bars: list[dict[str, Any]], required: tuple[str, ...] = ("open", "high", "low", "close", "volume")) -> list[dict[str, Any]]:
    cleaned = []
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        next_bar = dict(bar)
        valid = True
        for key in NUMERIC_BAR_KEYS:
            if key not in next_bar:
                continue
            number = _finite_float(next_bar.get(key))
            if key in required and number is None:
                valid = False
                break
            next_bar[key] = number
        if valid:
            cleaned.append(next_bar)
    return cleaned


def trace_strategy_signal(signal: dict[str, Any]) -> dict[str, Any]:
    if os.getenv("SIGNAL_TRACE", "").strip().lower() in {"1", "true", "yes", "on"} and signal.get("signal") in {"long", "short"}:
        print(f"1. 策略已生成信号: {signal}", flush=True)
    return signal


def find_local_extremes(values: list[Optional[float]], window: int = 3) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    peaks = []
    troughs = []
    for index in range(window, len(values) - window):
        current = _finite_float(values[index])
        if current is None:
            continue
        left = [_finite_float(values[index - offset]) for offset in range(1, window + 1)]
        right = [_finite_float(values[index + offset]) for offset in range(1, window + 1)]
        if any(value is None for value in left + right):
            continue
        if all(current > value for value in left + right if value is not None):
            peaks.append((index, current))
        if all(current < value for value in left + right if value is not None):
            troughs.append((index, current))
    return peaks, troughs


def match_extrema_by_time(
    price_points: list[tuple[int, float]],
    oscillator_points: list[tuple[int, float]],
    max_lag_bars: int = MAX_EXTREME_LAG_BARS,
) -> list[tuple[tuple[int, float], tuple[int, float]]]:
    matched: list[tuple[tuple[int, float], tuple[int, float]]] = []
    used: set[int] = set()
    for price_point in price_points:
        candidates = [
            (oscillator_index, oscillator_point)
            for oscillator_index, oscillator_point in enumerate(oscillator_points)
            if oscillator_index not in used and abs(oscillator_point[0] - price_point[0]) <= max_lag_bars
        ]
        if not candidates:
            continue
        oscillator_index, oscillator_point = min(candidates, key=lambda item: abs(item[1][0] - price_point[0]))
        used.add(oscillator_index)
        matched.append((price_point, oscillator_point))
    return matched


def _divergence_strength(price1: float, price2: float, osc1: float, osc2: float, price_weight: float, osc_weight: float, cap: float) -> float:
    price_base = max(abs(price1), 1e-12)
    osc_base = max(abs(osc1), 1e-12)
    strength = abs((price2 - price1) / price_base) * price_weight + abs((osc2 - osc1) / osc_base) * osc_weight
    return min(cap, strength)


def _safe_sum_abs(values: list[Optional[float]]) -> float:
    return sum(abs(value) for value in values if value is not None)


def _latest_strokes(peaks: list[tuple[int, float]], troughs: list[tuple[int, float]]) -> list[dict[str, Any]]:
    points = sorted(
        [{"index": index, "price": price, "mark": "peak"} for index, price in peaks]
        + [{"index": index, "price": price, "mark": "trough"} for index, price in troughs],
        key=lambda item: item["index"],
    )
    if not points:
        return []

    alternating = [points[0]]
    for point in points[1:]:
        previous = alternating[-1]
        if point["mark"] != previous["mark"]:
            alternating.append(point)
            continue
        if point["mark"] == "peak" and point["price"] >= previous["price"]:
            alternating[-1] = point
        if point["mark"] == "trough" and point["price"] <= previous["price"]:
            alternating[-1] = point

    strokes = []
    for start, end in zip(alternating, alternating[1:]):
        direction = "down" if start["mark"] == "peak" and end["mark"] == "trough" else "up"
        high = max(start["price"], end["price"])
        low = min(start["price"], end["price"])
        bars = max(1, int(end["index"] - start["index"]))
        change_pct = abs(end["price"] - start["price"]) / start["price"] if start["price"] else 0
        strokes.append(
            {
                "start": start,
                "end": end,
                "direction": direction,
                "high": high,
                "low": low,
                "bars": bars,
                "change_pct": change_pct,
                "speed": change_pct / bars,
            }
        )
    return strokes


def _latest_zone(strokes: list[dict[str, Any]], count: int = 3) -> Optional[dict[str, float]]:
    if len(strokes) < count:
        return None
    selected = strokes[-count:]
    lower = max(stroke["low"] for stroke in selected)
    upper = min(stroke["high"] for stroke in selected)
    if lower >= upper:
        return None
    return {"lower": lower, "upper": upper, "mid": (lower + upper) / 2, "width": upper - lower}


def build_chan_structure_context(
    direction: str,
    window: list[dict[str, Any]],
    price_peaks: list[tuple[int, float]],
    price_troughs: list[tuple[int, float]],
    macd_values: list[Optional[float]],
    divergence_info: dict[str, Any],
) -> dict[str, Any]:
    """Approximate CZSC-style structure with local fractals, strokes, and a recent overlap zone."""
    factors: list[str] = []
    score = 0
    divergence_index = int(divergence_info["index"])
    strokes = _latest_strokes(price_peaks, price_troughs)
    zone = _latest_zone(strokes)

    if direction == "long":
        matching_trough = next((item for item in reversed(price_troughs) if item[0] == divergence_index), None)
        if matching_trough is not None:
            score += 1
            factors.append("底分型确认")
        down_strokes = [stroke for stroke in strokes if stroke["direction"] == "down" and stroke["end"]["index"] <= divergence_index]
        if len(down_strokes) >= 2:
            previous, latest = down_strokes[-2], down_strokes[-1]
            previous_area = _safe_sum_abs(macd_values[previous["start"]["index"] : previous["end"]["index"] + 1])
            latest_area = _safe_sum_abs(macd_values[latest["start"]["index"] : latest["end"]["index"] + 1])
            if latest["end"]["price"] < previous["end"]["price"] and (latest_area < previous_area or latest["speed"] < previous["speed"]):
                score += 1
                factors.append("下跌笔力度衰竭")
        if zone:
            latest_price = float(divergence_info["price_low"])
            tolerance = max(zone["width"] * 0.35, latest_price * 0.003)
            if zone["lower"] - tolerance <= latest_price <= zone["mid"]:
                score += 1
                factors.append("中枢下沿背驰")
        stop_anchor = min([stroke["low"] for stroke in strokes[-3:]], default=divergence_info["price_low"])
    else:
        matching_peak = next((item for item in reversed(price_peaks) if item[0] == divergence_index), None)
        if matching_peak is not None:
            score += 1
            factors.append("顶分型确认")
        up_strokes = [stroke for stroke in strokes if stroke["direction"] == "up" and stroke["end"]["index"] <= divergence_index]
        if len(up_strokes) >= 2:
            previous, latest = up_strokes[-2], up_strokes[-1]
            previous_area = _safe_sum_abs(macd_values[previous["start"]["index"] : previous["end"]["index"] + 1])
            latest_area = _safe_sum_abs(macd_values[latest["start"]["index"] : latest["end"]["index"] + 1])
            if latest["end"]["price"] > previous["end"]["price"] and (latest_area < previous_area or latest["speed"] < previous["speed"]):
                score += 1
                factors.append("上涨笔力度衰竭")
        if zone:
            latest_price = float(divergence_info["price_high"])
            tolerance = max(zone["width"] * 0.35, latest_price * 0.003)
            if zone["mid"] <= latest_price <= zone["upper"] + tolerance:
                score += 1
                factors.append("中枢上沿背驰")
        stop_anchor = max([stroke["high"] for stroke in strokes[-3:]], default=divergence_info["price_high"])

    return {
        "score": score,
        "factors": factors,
        "text": "，".join(factors) if factors else "结构因子不足",
        "zone": zone,
        "stop_anchor": stop_anchor,
    }


def detect_bullish_divergence(
    price_troughs: list[tuple[int, float]],
    macd_troughs: list[tuple[int, float]],
    macd_fast_troughs: Optional[list[tuple[int, float]]] = None,
    max_lag_bars: int = MAX_EXTREME_LAG_BARS,
    extreme_window: int = EXTREME_WINDOW,
) -> tuple[bool, float, Optional[dict[str, Any]]]:
    if len(price_troughs) < 2:
        return False, 0, None

    matched_macd = match_extrema_by_time(price_troughs, macd_troughs, max_lag_bars)
    if len(matched_macd) >= 2:
        (price1, macd1), (price2, macd2) = matched_macd[-2], matched_macd[-1]
        if price2[1] < price1[1] and macd2[1] > macd1[1]:
            strength = _divergence_strength(price1[1], price2[1], macd1[1], macd2[1], 50, 50, 1.0)
            return True, strength, {
                "price_low": price2[1],
                "price_prev_low": price1[1],
                "index": price2[0],
                "oscillator_index": macd2[0],
                "confirm_index": max(price2[0], macd2[0]) + extreme_window,
                "strength": strength,
                "type": "macd",
            }

    matched_fast = match_extrema_by_time(price_troughs, macd_fast_troughs or [], max_lag_bars)
    if len(matched_fast) >= 2:
        (price1, fast1), (price2, fast2) = matched_fast[-2], matched_fast[-1]
        if price2[1] < price1[1] and fast2[1] > fast1[1]:
            strength = _divergence_strength(price1[1], price2[1], fast1[1], fast2[1], 30, 30, 0.8)
            return True, strength, {
                "price_low": price2[1],
                "price_prev_low": price1[1],
                "index": price2[0],
                "oscillator_index": fast2[0],
                "confirm_index": max(price2[0], fast2[0]) + extreme_window,
                "strength": strength,
                "type": "fast_macd",
            }
    return False, 0, None


def detect_bearish_divergence(
    price_peaks: list[tuple[int, float]],
    macd_peaks: list[tuple[int, float]],
    macd_fast_peaks: Optional[list[tuple[int, float]]] = None,
    max_lag_bars: int = MAX_EXTREME_LAG_BARS,
    extreme_window: int = EXTREME_WINDOW,
) -> tuple[bool, float, Optional[dict[str, Any]]]:
    if len(price_peaks) < 2:
        return False, 0, None

    matched_macd = match_extrema_by_time(price_peaks, macd_peaks, max_lag_bars)
    if len(matched_macd) >= 2:
        (price1, macd1), (price2, macd2) = matched_macd[-2], matched_macd[-1]
        if price2[1] > price1[1] and macd2[1] < macd1[1]:
            strength = _divergence_strength(price1[1], price2[1], macd1[1], macd2[1], 50, 50, 1.0)
            return True, strength, {
                "price_high": price2[1],
                "price_prev_high": price1[1],
                "index": price2[0],
                "oscillator_index": macd2[0],
                "confirm_index": max(price2[0], macd2[0]) + extreme_window,
                "strength": strength,
                "type": "macd",
            }

    matched_fast = match_extrema_by_time(price_peaks, macd_fast_peaks or [], max_lag_bars)
    if len(matched_fast) >= 2:
        (price1, fast1), (price2, fast2) = matched_fast[-2], matched_fast[-1]
        if price2[1] > price1[1] and fast2[1] < fast1[1]:
            strength = _divergence_strength(price1[1], price2[1], fast1[1], fast2[1], 30, 30, 0.8)
            return True, strength, {
                "price_high": price2[1],
                "price_prev_high": price1[1],
                "index": price2[0],
                "oscillator_index": fast2[0],
                "confirm_index": max(price2[0], fast2[0]) + extreme_window,
                "strength": strength,
                "type": "fast_macd",
            }
    return False, 0, None


def check_buy_filter(bars: list[dict[str, Any]], config: StrategyConfig = DEFAULT_CONFIG) -> tuple[bool, str]:
    if not bars:
        return False, "条件不足"
    latest = bars[-1]
    conditions = []
    if latest.get("rsi") is not None and latest["rsi"] < config.buy_rsi_threshold:
        conditions.append(f"RSI={latest['rsi']:.1f}<{config.buy_rsi_threshold:g}")
    if latest.get("ema60") is not None and latest.get("close") is not None and latest["close"] < latest["ema60"]:
        conditions.append(f"价格={latest['close']:.2f}<EMA60={latest['ema60']:.2f}")
    if latest.get("volume_ratio") is not None and latest["volume_ratio"] > config.buy_volume_ratio:
        conditions.append(f"放量{latest['volume_ratio']:.2f}倍")
    if len(conditions) >= 2:
        return True, "✓ " + ", ".join(conditions)
    return False, "✗ " + ", ".join(conditions) if conditions else "条件不足"


def check_sell_filter(bars: list[dict[str, Any]], config: StrategyConfig = DEFAULT_CONFIG) -> tuple[bool, str]:
    if not bars:
        return False, "条件不足"
    latest = bars[-1]
    conditions = []
    if latest.get("rsi") is not None and latest["rsi"] > config.sell_rsi_threshold:
        conditions.append(f"RSI={latest['rsi']:.1f}>{config.sell_rsi_threshold:g}")
    if latest.get("ema60") is not None and latest.get("close") is not None and latest["close"] > latest["ema60"]:
        conditions.append(f"价格={latest['close']:.2f}>EMA60={latest['ema60']:.2f}")
    if latest.get("volume_ratio") is not None and latest["volume_ratio"] > config.sell_volume_ratio:
        conditions.append(f"放量{latest['volume_ratio']:.2f}倍")
    if len(conditions) >= 2:
        return True, "✓ " + ", ".join(conditions)
    return False, "✗ " + ", ".join(conditions) if conditions else "条件不足"


def cross_direction(previous_fast: Optional[float], current_fast: Optional[float], previous_slow: Optional[float], current_slow: Optional[float]) -> str:
    if previous_fast is None or current_fast is None or previous_slow is None or current_slow is None:
        return "none"
    if previous_fast <= previous_slow and current_fast > current_slow:
        return "up"
    if previous_fast >= previous_slow and current_fast < current_slow:
        return "down"
    return "none"


def latest_ma_direction(bars: list[dict[str, Any]], config: StrategyConfig = DEFAULT_CONFIG) -> str:
    closes = [bar.get("close") for bar in bars]
    ma_fast = rolling_average(closes, config.ma_fast_period)
    ma_slow = rolling_average(closes, config.ma_slow_period)
    if len(bars) < 2:
        return "none"
    previous_index = len(bars) - 2
    current_index = len(bars) - 1
    return cross_direction(ma_fast[previous_index], ma_fast[current_index], ma_slow[previous_index], ma_slow[current_index])


def latest_macd_direction(bars: list[dict[str, Any]]) -> str:
    if len(bars) < 2:
        return "none"
    previous = bars[-2]
    current = bars[-1]
    return cross_direction(previous.get("macd"), current.get("macd"), previous.get("macd_signal"), current.get("macd_signal"))


def confirmation_passed(direction: str, ma_direction: str, macd_direction: str, mode: str = CONFIRMATION_MODE) -> tuple[bool, dict[str, bool]]:
    target = "up" if direction == "long" else "down"
    flags = {"ma": ma_direction == target, "macd": macd_direction == target}
    normalized_mode = (mode or "either").strip().lower()
    if normalized_mode == "both":
        return all(flags.values()), flags
    if normalized_mode == "ma_only":
        return flags["ma"], flags
    if normalized_mode == "macd_only":
        return flags["macd"], flags
    return any(flags.values()), flags


def confirmation_wait_text(direction: str, config: StrategyConfig) -> str:
    cross_name = "上穿" if direction == "long" else "下穿"
    mode_text = {
        "both": "MA 与 MACD 同时",
        "ma_only": "MA",
        "macd_only": "MACD",
        "either": "MA 或 MACD",
    }.get((config.confirmation_mode or "either").strip().lower(), "MA 或 MACD")
    return f"等待 {mode_text} {cross_name}"


def higher_state_from_bar(bar: dict[str, Any]) -> dict[str, Any]:
    return {
        "trend": bar.get("higher_trend", "unknown"),
        "close": bar.get("higher_close"),
        "ema20": bar.get("higher_ema20"),
        "ema60": bar.get("higher_ema60"),
        "ema200": bar.get("higher_ema200"),
        "macd": bar.get("higher_macd"),
    }


def check_higher_timeframe_alignment(direction: str, bar: dict[str, Any], config: StrategyConfig = DEFAULT_CONFIG, higher_label: str = HIGHER_TREND_INTERVAL) -> tuple[bool, str]:
    if not config.require_higher_trend_alignment:
        return True, "高周期顺势未强制"

    higher = higher_state_from_bar(bar)
    close = higher.get("close")
    ema20 = higher.get("ema20")
    ema60 = higher.get("ema60")
    ema200 = higher.get("ema200")
    macd_value = higher.get("macd")

    if close is None or ema20 is None or ema60 is None:
        return False, f"{higher_label}高周期数据不足"

    if direction == "long":
        aligned = close > ema60 and ema20 > ema60 and (ema200 is None or close > ema200) and (macd_value is None or macd_value >= 0)
        if aligned:
            return True, f"✓ {higher_label}多头共振：EMA20>EMA60，价格在EMA60上方"
        return False, f"✗ {higher_label}未多头共振：价格={close:.2f}, EMA20={ema20:.2f}, EMA60={ema60:.2f}"

    aligned = close < ema60 and ema20 < ema60 and (ema200 is None or close < ema200) and (macd_value is None or macd_value <= 0)
    if aligned:
        return True, f"✓ {higher_label}空头共振：EMA20<EMA60，价格在EMA60下方"
    return False, f"✗ {higher_label}未空头共振：价格={close:.2f}, EMA20={ema20:.2f}, EMA60={ema60:.2f}"


def check_buy_trend_breaker(bar: dict[str, Any], higher_label: str = HIGHER_TREND_INTERVAL) -> tuple[bool, str]:
    higher_close = bar.get("higher_close")
    higher_ema60 = bar.get("higher_ema60")
    higher_macd = bar.get("higher_macd")
    if higher_close is None or higher_ema60 is None or higher_macd is None:
        return False, "高周期数据不足"
    if higher_close < higher_ema60 and higher_macd < 0:
        return True, f"✗ {higher_label}空头趋势熔断：价格={higher_close:.2f}<EMA60={higher_ema60:.2f}, MACD={higher_macd:.4f}<0"
    return False, "高周期未熔断"


def check_regime_filter(bar: dict[str, Any], config: StrategyConfig = DEFAULT_CONFIG) -> tuple[bool, str]:
    if not config.chop_filter_enabled:
        return True, "震荡过滤未启用"

    reasons = []
    chop_value = bar.get("chop")
    if chop_value is not None and chop_value >= config.max_chop:
        reasons.append(f"CHOP={chop_value:.1f}>={config.max_chop:g}")
    bb_width = bar.get("bb_width")
    if bb_width is not None and bb_width <= config.min_bb_width:
        reasons.append(f"布林带宽={bb_width:.2%}<={config.min_bb_width:.2%}")
    if reasons:
        return False, "震荡过滤： " + ", ".join(reasons)
    return True, "震荡过滤通过"


def check_volume_confirmation(direction: str, bar: dict[str, Any], config: StrategyConfig = DEFAULT_CONFIG) -> tuple[bool, str]:
    if not config.volume_confirmation_enabled:
        return True, "量价确认未启用"

    confirmations = []
    volume_ratio = bar.get("volume_ratio")
    if volume_ratio is not None and volume_ratio <= config.volume_contraction_ratio:
        confirmations.append(f"缩量{volume_ratio:.2f}倍")
    if volume_ratio is not None and volume_ratio >= config.volume_breakout_ratio:
        confirmations.append(f"爆量{volume_ratio:.2f}倍")

    obv_slope = bar.get("obv_slope")
    if config.obv_confirmation_enabled and obv_slope is not None:
        if direction == "long" and obv_slope > 0:
            confirmations.append("OBV回升")
        if direction == "short" and obv_slope < 0:
            confirmations.append("OBV回落")

    if confirmations:
        return True, "✓ " + ", ".join(confirmations)
    return False, "✗ 无缩量/爆量/OBV确认"


def check_microstructure_confirmation(direction: str, bar: dict[str, Any], config: StrategyConfig = DEFAULT_CONFIG) -> tuple[bool, str]:
    if not config.microstructure_enabled:
        return True, "微观结构未启用"

    funding_rate = bar.get("funding_rate") or bar.get("lastFundingRate")
    oi_ratio = bar.get("open_interest_ratio") or bar.get("oi_ratio")
    confirmations = []

    if direction == "long" and funding_rate is not None and funding_rate <= config.long_funding_rate_max:
        confirmations.append(f"资金费率{funding_rate:.4%}")
    if direction == "short" and funding_rate is not None and funding_rate >= config.short_funding_rate_min:
        confirmations.append(f"资金费率{funding_rate:.4%}")
    if oi_ratio is not None and oi_ratio >= config.open_interest_surge_ratio:
        confirmations.append(f"OI激增{oi_ratio:.2f}倍")

    if confirmations:
        return True, "✓ " + ", ".join(confirmations)
    if funding_rate is None and oi_ratio is None:
        return (not config.require_microstructure), "微观结构数据缺失"
    return (not config.require_microstructure), "微观结构未触发"


def check_entry_context(direction: str, bar: dict[str, Any], config: StrategyConfig = DEFAULT_CONFIG) -> tuple[bool, str]:
    checks = [
        check_regime_filter(bar, config),
        check_higher_timeframe_alignment(direction, bar, config),
        check_volume_confirmation(direction, bar, config),
        check_microstructure_confirmation(direction, bar, config),
    ]
    blocked = [text for passed, text in checks if not passed]
    if blocked:
        return False, "；".join(blocked)
    return True, "；".join(text for _passed, text in checks)


def score_signal(
    direction: str,
    strength: float,
    filter_passed: bool,
    bar: dict[str, Any],
    higher: Optional[dict[str, Any]] = None,
    trend_blocked: bool = False,
    structure: Optional[dict[str, Any]] = None,
    config: StrategyConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    max_score = 20
    if trend_blocked:
        return {"score": 0, "max_score": max_score, "grade": "weak", "text": "高周期趋势熔断"}

    higher_state = higher if higher is not None else higher_state_from_bar(bar)
    score = 0
    reasons = []
    if strength >= 0.7:
        score += 2
        reasons.append("背驰强")
    elif strength >= 0.4:
        score += 1
        reasons.append("背驰中等")
    if filter_passed:
        score += 1
        reasons.append("原过滤通过")
    volume_ratio = bar.get("volume_ratio")
    if volume_ratio is not None and volume_ratio > 1.0:
        score += 1
        reasons.append(f"放量{volume_ratio:.2f}倍")
    rsi_value = bar.get("rsi")
    if direction == "long" and rsi_value is not None and rsi_value < config.buy_rsi_threshold:
        score += 1
        reasons.append(f"RSI低位{rsi_value:.1f}")
    if direction == "short" and rsi_value is not None and rsi_value > config.sell_rsi_threshold:
        score += 1
        reasons.append(f"RSI高位{rsi_value:.1f}")
    mfi_value = bar.get("mfi")
    if direction == "long" and mfi_value is not None and mfi_value < config.mfi_long_max:
        score += 1
        reasons.append(f"MFI低位{mfi_value:.1f}")
    if direction == "short" and mfi_value is not None and mfi_value > config.mfi_short_min:
        score += 1
        reasons.append(f"MFI高位{mfi_value:.1f}")
    bad_higher_trend = "down" if direction == "long" else "up"
    higher_trend = higher_state.get("trend")
    if higher_trend and higher_trend != "unknown" and higher_trend != bad_higher_trend:
        score += 1
        reasons.append(f"高周期{higher_trend}")
    bad_local_trend = "down" if direction == "long" else "up"
    local_trend = bar.get("trend")
    if local_trend and local_trend != "unknown" and local_trend != bad_local_trend:
        score += 1
        reasons.append(f"本周期{local_trend}")
    atr_ratio = bar.get("atr_ratio")
    atr_pct = bar.get("atr_pct")
    if atr_ratio is not None and atr_pct is not None and config.atr_ratio_min <= atr_ratio <= config.atr_ratio_max and atr_pct <= config.atr_pct_max:
        score += 1
        reasons.append(f"波动适中 ATR{atr_pct:.2%}/{atr_ratio:.2f}x")
    adx_value = bar.get("adx")
    if adx_value is not None:
        if direction == "long" and adx_value < config.adx_long_max:
            score += 1
            reasons.append(f"ADX适合反弹{adx_value:.1f}")
        elif direction == "short" and adx_value < config.adx_short_max:
            score += 1
            reasons.append(f"ADX未极强{adx_value:.1f}")
    bb_percent = bar.get("bb_percent")
    if direction == "long" and bb_percent is not None and bb_percent <= config.bb_long_max:
        score += 1
        reasons.append(f"布林低位{bb_percent:.2f}")
    if direction == "short" and bb_percent is not None and bb_percent >= config.bb_short_min:
        score += 1
        reasons.append(f"布林高位{bb_percent:.2f}")
    bb_width = bar.get("bb_width")
    if bb_width is not None and config.bb_width_score_min <= bb_width <= config.bb_width_score_max:
        score += 1
        reasons.append(f"带宽适中{bb_width:.2%}")
    chop_value = bar.get("chop")
    if chop_value is not None and chop_value < config.max_chop:
        score += 1
        reasons.append(f"CHOP可交易{chop_value:.1f}")
    obv_slope = bar.get("obv_slope")
    if direction == "long" and obv_slope is not None and obv_slope > 0:
        score += 1
        reasons.append("OBV回升")
    if direction == "short" and obv_slope is not None and obv_slope < 0:
        score += 1
        reasons.append("OBV回落")
    ema200 = bar.get("ema200")
    close = bar.get("close")
    if ema200 is not None and close is not None and ema200:
        ema200_distance = (close - ema200) / ema200
        if direction == "long" and ema200_distance > -config.ema200_distance_max:
            score += 1
            reasons.append(f"EMA200距离{ema200_distance:.2%}")
        if direction == "short" and ema200_distance < config.ema200_distance_max:
            score += 1
            reasons.append(f"EMA200距离{ema200_distance:.2%}")
    funding_rate = bar.get("funding_rate") or bar.get("lastFundingRate")
    oi_ratio = bar.get("open_interest_ratio") or bar.get("oi_ratio")
    if direction == "long" and funding_rate is not None and funding_rate <= config.long_funding_rate_max:
        score += 1
        reasons.append(f"负资金费{funding_rate:.4%}")
    if direction == "short" and funding_rate is not None and funding_rate >= config.short_funding_rate_min:
        score += 1
        reasons.append(f"正资金费{funding_rate:.4%}")
    if oi_ratio is not None and oi_ratio >= config.open_interest_surge_ratio:
        score += 1
        reasons.append(f"OI放大{oi_ratio:.2f}x")
    if structure:
        structure_score = int(structure.get("score", 0) or 0)
        if structure_score > 0:
            score += min(3, structure_score)
            reasons.extend(str(item) for item in structure.get("factors", []) if item)
    grade = "strong" if score >= config.strong_score else "normal" if score >= config.normal_score else "weak"
    return {"score": score, "max_score": max_score, "grade": grade, "text": "，".join(reasons) if reasons else "因子不足"}


def grade_signal(
    direction: str,
    strength: float,
    filter_passed: bool,
    bar: dict[str, Any],
    higher: Optional[dict[str, Any]] = None,
    trend_blocked: bool = False,
    config: StrategyConfig = DEFAULT_CONFIG,
) -> str:
    return str(score_signal(direction, strength, filter_passed, bar, higher, trend_blocked, config=config)["grade"])


class ProjectSignalEngine:
    def __init__(self, config: StrategyConfig = DEFAULT_CONFIG) -> None:
        self.config = config
        self.pending_buy: Optional[dict[str, Any]] = None
        self.pending_sell: Optional[dict[str, Any]] = None

    @staticmethod
    def bars_waited_since(ready_bars: list[dict[str, Any]], close_time: Any, fallback: int = 0) -> int:
        try:
            created_close_time = int(close_time)
        except (TypeError, ValueError):
            return fallback
        return sum(1 for bar in ready_bars if int(bar.get("close_time", 0) or 0) > created_close_time)

    def detect(self, ready_bars: list[dict[str, Any]]) -> dict[str, Any]:
        ready_bars = clean_strategy_bars(ready_bars, required=("open", "high", "low", "close", "volume", "macd"))
        if len(ready_bars) < 100:
            return {"signal": "wait", "signal_name": "K线不足"}

        current_bar = ready_bars[-1]
        window = ready_bars[-200:]
        closes = [bar["close"] for bar in window]
        macd_values = [bar["macd"] for bar in window]
        macd_fast_values = [bar.get("macd_fast") for bar in window]

        price_peaks, price_troughs = find_local_extremes(closes, self.config.extreme_window)
        macd_peaks, macd_troughs = find_local_extremes(macd_values, self.config.extreme_window)
        macd_fast_peaks, macd_fast_troughs = find_local_extremes(macd_fast_values, self.config.extreme_window)
        bullish_div, bullish_strength, bullish_info = detect_bullish_divergence(
            price_troughs,
            macd_troughs,
            macd_fast_troughs,
            self.config.max_extreme_lag_bars,
            self.config.extreme_window,
        )
        bearish_div, bearish_strength, bearish_info = detect_bearish_divergence(
            price_peaks,
            macd_peaks,
            macd_fast_peaks,
            self.config.max_extreme_lag_bars,
            self.config.extreme_window,
        )
        ma_direction = latest_ma_direction(ready_bars, self.config)
        macd_direction = latest_macd_direction(ready_bars)

        if self.pending_buy:
            fallback_waited = len(ready_bars) - int(self.pending_buy.get("ready_index", len(ready_bars)) or len(ready_bars))
            bars_waited = self.bars_waited_since(ready_bars, self.pending_buy.get("created_close_time"), fallback_waited)
            if bars_waited > self.config.confirm_max_bars:
                expired = self.pending_buy
                self.pending_buy = None
                return self.build_buy_response(expired, "filtered_buy", "底背驰确认超时", f"超过 {self.config.confirm_max_bars} 根K线未确认", current_bar, bars_waited)
            confirmed, _confirmation_flags = confirmation_passed("long", ma_direction, macd_direction, self.config.confirmation_mode)
            if confirmed:
                candidate = self.pending_buy
                self.pending_buy = None
                context_passed, context_text = check_entry_context("long", current_bar, self.config)
                if not context_passed:
                    return self.build_buy_response(candidate, "filtered_buy", "底背驰被共振/量价/震荡过滤", context_text, current_bar, bars_waited)
                filter_passed, filter_text = check_buy_filter(ready_bars, self.config)
                combined_text = f"{context_text}；{filter_text}"
                return self.build_buy_response(candidate, "long" if filter_passed else "filtered_buy", "MACD底背驰确认开多" if filter_passed else "底背驰被买入过滤", combined_text, current_bar, bars_waited)
            return self.build_buy_response(self.pending_buy, "filtered_buy", "底背驰等待确认", confirmation_wait_text("long", self.config), current_bar, bars_waited)

        if bullish_div and bullish_strength >= self.config.min_divergence_strength and bullish_info:
            divergence_bar = window[bullish_info["index"]]
            ready_index = min(int(bullish_info.get("confirm_index", bullish_info["index"]) or bullish_info["index"]), len(window) - 1)
            ready_bar = window[ready_index]
            candidate = {
                "strength": bullish_strength,
                "price_low": bullish_info["price_low"],
                "divergence_time": divergence_bar.get("close_time", divergence_bar.get("open_time")),
                "signal_ready_time": ready_bar.get("close_time", ready_bar.get("open_time")),
                "divergence_type": bullish_info.get("type", "macd"),
                "ready_index": len(ready_bars),
                "created_close_time": current_bar.get("close_time", current_bar.get("open_time")),
            }
            candidate["structure"] = build_chan_structure_context("long", window, price_peaks, price_troughs, macd_values, bullish_info)
            confirmed, _confirmation_flags = confirmation_passed("long", ma_direction, macd_direction, self.config.confirmation_mode)
            if not confirmed:
                self.pending_buy = candidate
                return self.build_buy_response(candidate, "filtered_buy", "底背驰等待确认", confirmation_wait_text("long", self.config), current_bar, 0)
            context_passed, context_text = check_entry_context("long", current_bar, self.config)
            if not context_passed:
                return self.build_buy_response(candidate, "filtered_buy", "底背驰被共振/量价/震荡过滤", context_text, current_bar, 0)
            filter_passed, filter_text = check_buy_filter(ready_bars, self.config)
            combined_text = f"{context_text}；{filter_text}"
            return self.build_buy_response(candidate, "long" if filter_passed else "filtered_buy", "MACD底背驰确认开多" if filter_passed else "底背驰被买入过滤", combined_text, current_bar, 0)

        if bearish_div and bearish_strength >= self.config.min_divergence_strength and bearish_info:
            divergence_bar = window[bearish_info["index"]]
            ready_index = min(int(bearish_info.get("confirm_index", bearish_info["index"]) or bearish_info["index"]), len(window) - 1)
            ready_bar = window[ready_index]
            candidate = {
                "strength": bearish_strength,
                "price_high": bearish_info["price_high"],
                "divergence_time": divergence_bar.get("close_time", divergence_bar.get("open_time")),
                "signal_ready_time": ready_bar.get("close_time", ready_bar.get("open_time")),
                "divergence_type": bearish_info.get("type", "macd"),
                "ready_index": len(ready_bars),
                "created_close_time": current_bar.get("close_time", current_bar.get("open_time")),
            }
            candidate["structure"] = build_chan_structure_context("short", window, price_peaks, price_troughs, macd_values, bearish_info)
            confirmed, _confirmation_flags = confirmation_passed("short", ma_direction, macd_direction, self.config.confirmation_mode)
            if not confirmed:
                self.pending_sell = candidate
                return self.build_sell_response(candidate, "filtered_sell", "顶背驰等待确认", confirmation_wait_text("short", self.config), current_bar, 0)
            context_passed, context_text = check_entry_context("short", current_bar, self.config)
            if not context_passed:
                return self.build_sell_response(candidate, "filtered_sell", "顶背驰被共振/量价/震荡过滤", context_text, current_bar, 0)
            filter_passed, filter_text = check_sell_filter(ready_bars, self.config)
            combined_text = f"{context_text}；{filter_text}"
            return self.build_sell_response(candidate, "short" if filter_passed else "filtered_sell", "MACD顶背驰开空观察" if filter_passed else "顶背驰被卖出过滤", combined_text, current_bar, 0)

        if self.pending_sell:
            fallback_waited = len(ready_bars) - int(self.pending_sell.get("ready_index", len(ready_bars)) or len(ready_bars))
            bars_waited = self.bars_waited_since(ready_bars, self.pending_sell.get("created_close_time"), fallback_waited)
            if bars_waited > self.config.confirm_max_bars:
                expired = self.pending_sell
                self.pending_sell = None
                return self.build_sell_response(expired, "filtered_sell", "顶背驰确认超时", f"超过 {self.config.confirm_max_bars} 根K线未确认", current_bar, bars_waited)
            confirmed, _confirmation_flags = confirmation_passed("short", ma_direction, macd_direction, self.config.confirmation_mode)
            if not confirmed:
                return self.build_sell_response(self.pending_sell, "filtered_sell", "顶背驰等待确认", confirmation_wait_text("short", self.config), current_bar, bars_waited)
            candidate = self.pending_sell
            self.pending_sell = None
            context_passed, context_text = check_entry_context("short", current_bar, self.config)
            if not context_passed:
                return self.build_sell_response(candidate, "filtered_sell", "顶背驰被共振/量价/震荡过滤", context_text, current_bar, bars_waited)
            filter_passed, filter_text = check_sell_filter(ready_bars, self.config)
            combined_text = f"{context_text}；{filter_text}"
            return self.build_sell_response(candidate, "short" if filter_passed else "filtered_sell", "MACD顶背驰开空观察" if filter_passed else "顶背驰被卖出过滤", combined_text, current_bar, bars_waited)

        return {"signal": "wait", "signal_name": "等待MACD背驰", "strength": max(bullish_strength, bearish_strength)}

    def build_sell_response(self, candidate: dict[str, Any], signal: str, name: str, filter_text: str, current_bar: dict[str, Any], confirm_bars: int) -> dict[str, Any]:
        structure = candidate.get("structure")
        score_info = score_signal("short", candidate["strength"], signal == "short", current_bar, trend_blocked=("熔断" in name or "过滤" in name), structure=structure, config=self.config)
        stop_anchor = float(structure.get("stop_anchor", candidate["price_high"]) if structure else candidate["price_high"])
        combined_filter = f"{filter_text}；结构：{structure['text']}" if structure else filter_text
        return trace_strategy_signal({
            "signal": signal,
            "signal_name": name,
            "strength": candidate["strength"],
            "filter": combined_filter,
            "stop_loss": stop_anchor + (current_bar.get("atr") or 0) * self.config.atr_stop_multiplier,
            "divergence_time": candidate["divergence_time"],
            "signal_ready_time": candidate.get("signal_ready_time", candidate["divergence_time"]),
            "divergence_type": candidate["divergence_type"],
            "structure_factors": structure.get("factors", []) if structure else [],
            "structure_score": int(structure.get("score", 0) if structure else 0),
            "structure_text": structure.get("text", "结构因子不足") if structure else "结构因子不足",
            "price": current_bar["close"],
            "confirm_bars": confirm_bars,
            "higher_trend": str(current_bar.get("higher_trend", "unknown")),
            "signal_grade": score_info["grade"],
            "signal_score": score_info["score"],
            "signal_score_max": score_info["max_score"],
            "score_text": score_info["text"],
        })

    def build_buy_response(self, candidate: dict[str, Any], signal: str, name: str, filter_text: str, current_bar: dict[str, Any], confirm_bars: int) -> dict[str, Any]:
        structure = candidate.get("structure")
        score_info = score_signal("long", candidate["strength"], signal == "long", current_bar, trend_blocked=("熔断" in name or "过滤" in name), structure=structure, config=self.config)
        stop_anchor = float(structure.get("stop_anchor", candidate["price_low"]) if structure else candidate["price_low"])
        combined_filter = f"{filter_text}；结构：{structure['text']}" if structure else filter_text
        return trace_strategy_signal({
            "signal": signal,
            "signal_name": name,
            "strength": candidate["strength"],
            "filter": combined_filter,
            "stop_loss": stop_anchor - (current_bar.get("atr") or 0) * self.config.atr_stop_multiplier,
            "divergence_time": candidate["divergence_time"],
            "signal_ready_time": candidate.get("signal_ready_time", candidate["divergence_time"]),
            "divergence_type": candidate["divergence_type"],
            "structure_factors": structure.get("factors", []) if structure else [],
            "structure_score": int(structure.get("score", 0) if structure else 0),
            "structure_text": structure.get("text", "结构因子不足") if structure else "结构因子不足",
            "price": current_bar["close"],
            "confirm_bars": confirm_bars,
            "higher_trend": str(current_bar.get("higher_trend", "unknown")),
            "signal_grade": score_info["grade"],
            "signal_score": score_info["score"],
            "signal_score_max": score_info["max_score"],
            "score_text": score_info["text"],
        })
