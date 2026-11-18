"""Causal, deliberately simplified chanlun third-buy research primitives.

The implementation is intentionally narrow:

* raw closed 15m bars, no inclusion merging;
* a fractal needs two bars on each side and is known at the close of i+2;
* a stroke connects alternating confirmed fractals at least four bars apart;
* a central zone is the overlap of the three strokes before the departure;
* a third buy is an upward departure above ZG followed by a pullback whose
  low stays strictly above ZG.

No function in this module uses a bar after the signal bar to decide whether a
signal exists.  Trade simulation may of course use later bars to resolve the
trade.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Iterable, Sequence

EIGHT_HOURS_MS = 8 * 60 * 60 * 1000


@dataclass(frozen=True)
class Fractal:
    kind: str  # "top" or "bottom"
    index: int
    price: float
    confirm_index: int


@dataclass(frozen=True)
class Stroke:
    start: Fractal
    end: Fractal
    high: float
    low: float
    confirm_index: int

    @property
    def direction(self) -> str:
        return "up" if self.end.price > self.start.price else "down"


@dataclass(frozen=True)
class Signal:
    symbol: str
    signal_index: int
    entry_index: int
    zone_zg: float
    zone_zd: float
    departure_high: float
    pullback_low: float
    stop: float
    atr: float


@dataclass
class Bars:
    open_time: list[int]
    open: list[float]
    high: list[float]
    low: list[float]
    close: list[float]
    close_time: list[int]

    def __len__(self) -> int:
        return len(self.open_time)


@dataclass
class Trade:
    symbol: str
    signal_index: int
    entry_index: int
    exit_index: int
    entry_time: int
    exit_time: int
    entry: float
    stop: float
    target: float
    exit: float
    reason: str
    gross_pct: float
    fee_pct: float
    funding_pct: float
    net_pct: float
    risk_pct: float
    r_multiple: float
    funding_missing: bool = False
    truncated: bool = False
    excluded: bool = False


def find_fractals(highs: Sequence[float], lows: Sequence[float], wing: int = 2) -> list[Fractal]:
    """Return confirmed swing fractals in confirmation order.

    A top at i requires strictly higher highs than the ``wing`` bars on each
    side; a bottom is symmetric.  The fractal is not returned until i+wing, so
    a caller that only looks at confirmations cannot peek at the future.
    """
    if wing < 1:
        raise ValueError("wing must be >= 1")
    n = len(highs)
    if len(lows) != n:
        raise ValueError("highs and lows must have equal length")
    out: list[Fractal] = []
    for i in range(wing, n - wing):
        left = range(i - wing, i)
        right = range(i + 1, i + wing + 1)
        if all(highs[i] > highs[j] for j in left) and all(highs[i] > highs[j] for j in right):
            out.append(Fractal("top", i, float(highs[i]), i + wing))
        if all(lows[i] < lows[j] for j in left) and all(lows[i] < lows[j] for j in right):
            out.append(Fractal("bottom", i, float(lows[i]), i + wing))
    out.sort(key=lambda f: (f.confirm_index, f.index))
    return out


def wilder_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[float | None]:
    """Wilder ATR; values before the first full period are None."""
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("OHLC lengths must match")
    if n == 0:
        return []
    tr: list[float | None] = [None] * n
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    atr: list[float | None] = [None] * n
    if n <= period:
        return atr
    first = sum(float(tr[i]) for i in range(1, period + 1)) / period
    atr[period] = first
    for i in range(period + 1, n):
        atr[i] = (float(atr[i - 1]) * (period - 1) + float(tr[i])) / period
    return atr


def _make_stroke(start: Fractal, end: Fractal) -> Stroke:
    return Stroke(
        start=start,
        end=end,
        high=max(start.price, end.price),
        low=min(start.price, end.price),
        confirm_index=end.confirm_index,
    )


def detect_third_buy(
    strokes: Sequence[Stroke],
    atr: Sequence[float | None],
    *,
    atr_buffer: float = 0.25,
    symbol: str = "",
) -> Signal | None:
    """Check the last five strokes for the frozen third-buy pattern.

    Strokes are ordered oldest to newest.  The last five are:
    zone (three), departure (one up), pullback (one down).  The signal time is
    the confirmation of the pullback's ending bottom fractal.
    """
    if len(strokes) < 5:
        return None
    zone = strokes[-5:-2]
    departure = strokes[-2]
    pullback = strokes[-1]
    zg = min(s.high for s in zone)
    zd = max(s.low for s in zone)
    if not zg > zd:
        return None
    if departure.end.kind != "top" or departure.high <= zg:
        return None
    if pullback.end.kind != "bottom" or pullback.low <= zg:
        return None
    t = pullback.confirm_index
    if t < 0 or t >= len(atr) or atr[t] is None:
        return None
    stop = zg - atr_buffer * float(atr[t])
    if not math.isfinite(stop) or stop <= 0:
        return None
    return Signal(
        symbol=symbol,
        signal_index=t,
        entry_index=t + 1,
        zone_zg=float(zg),
        zone_zd=float(zd),
        departure_high=float(departure.high),
        pullback_low=float(pullback.low),
        stop=float(stop),
        atr=float(atr[t]),
    )


def build_strokes_and_signals(
    fractals: Sequence[Fractal],
    atr: Sequence[float | None],
    *,
    min_gap: int = 4,
    atr_buffer: float = 0.25,
    symbol: str = "",
) -> tuple[list[Stroke], list[Signal]]:
    """Incrementally build strokes and emit causal third-buy signals.

    Same-type fractals replace the last pivot only if more extreme.  An
    opposite fractal closer than ``min_gap`` is discarded.  A signal is emitted
    only when a new opposite stroke is appended; a same-type extension updates
    the last stroke for future patterns but does not emit a duplicate signal.
    """
    if min_gap < 1:
        raise ValueError("min_gap must be >= 1")
    pivots: list[Fractal] = []
    strokes: list[Stroke] = []
    signals: list[Signal] = []
    for f in sorted(fractals, key=lambda x: (x.confirm_index, x.index)):
        if not pivots:
            pivots.append(f)
            continue
        last = pivots[-1]
        if f.kind == last.kind:
            more_extreme = (f.kind == "top" and f.price > last.price) or (
                f.kind == "bottom" and f.price < last.price
            )
            if not more_extreme:
                continue
            pivots[-1] = f
            if strokes and strokes[-1].end.index == last.index and strokes[-1].end.kind == last.kind:
                strokes[-1] = _make_stroke(strokes[-1].start, f)
            continue
        if abs(f.index - last.index) < min_gap:
            continue
        pivots.append(f)
        strokes.append(_make_stroke(last, f))
        signal = detect_third_buy(
            strokes, atr, atr_buffer=atr_buffer, symbol=symbol
        )
        if signal is not None:
            signals.append(signal)
    return strokes, signals


def _nearest_funding(
    times: Sequence[int], rates: Sequence[float], target: int, tolerance_ms: int = 60_000
) -> float | None:
    if not times:
        return None
    pos = bisect_left(times, target)
    for idx in (pos - 1, pos):
        if 0 <= idx < len(times) and abs(times[idx] - target) <= tolerance_ms:
            return float(rates[idx])
    return None


def funding_cost_pct(
    entry_time: int,
    exit_time: int,
    funding_times: Sequence[int],
    funding_rates: Sequence[float],
    *,
    include_entry_boundary: bool = True,
) -> tuple[float, bool]:
    """Sum funding rates crossed while long.

    Positive rates are paid by longs.  Missing expected 8h settlements are
    reported rather than silently treated as zero.
    """
    if exit_time < entry_time:
        raise ValueError("exit_time before entry_time")
    first = (entry_time // EIGHT_HOURS_MS) * EIGHT_HOURS_MS
    if not include_entry_boundary or first < entry_time:
        first += EIGHT_HOURS_MS
    total = 0.0
    missing = False
    boundary = first
    while boundary <= exit_time:
        rate = _nearest_funding(funding_times, funding_rates, boundary)
        if rate is None:
            missing = True
        else:
            total += rate
        boundary += EIGHT_HOURS_MS
    return total, missing


def signal_entry_status(
    signal: Signal,
    bars: Bars,
    *,
    horizon_bars: int = 96,
    slippage: float = 0.0002,
) -> str:
    """Classify an entry without looking at post-entry returns.

    A gap back to or below ZG invalidates the third-buy premise before entry,
    even if the gap is still above the initial stop.
    """
    if signal.entry_index + horizon_bars > len(bars):
        return "no_forward_window"
    entry = float(bars.open[signal.entry_index]) * (1.0 + slippage)
    if entry <= signal.stop:
        return "entry_below_stop"
    if entry <= signal.zone_zg:
        return "entry_reentered_zone"
    return "candidate"


def simulate_trade(
    bars: Bars,
    signal: Signal,
    funding_times: Sequence[int],
    funding_rates: Sequence[float],
    *,
    fee_rate: float = 0.001,
    slippage: float = 0.0002,
    max_hold_bars: int = 96,
) -> Trade | None:
    """Resolve one signal into a trade; return None only for invalid entry."""
    i = signal.entry_index
    n = len(bars)
    if i >= n or i + max_hold_bars > n:
        # Do not score a trade that cannot complete its frozen holding window.
        return None
    entry_raw = float(bars.open[i])
    entry = entry_raw * (1.0 + slippage)
    stop = float(signal.stop)
    if entry <= stop:
        return None
    target = entry + 2.0 * (entry - stop)
    last = min(i + max_hold_bars - 1, n - 1)
    exit_price: float | None = None
    exit_index = last
    reason = "time"
    truncated = last < i + max_hold_bars - 1

    for j in range(i, last + 1):
        o = float(bars.open[j])
        h = float(bars.high[j])
        l = float(bars.low[j])
        # Opening gap is known before the bar's range.
        if o <= stop:
            exit_price = o * (1.0 - slippage)
            exit_index = j
            reason = "stop_gap"
            break
        if o >= target:
            exit_price = target * (1.0 - slippage)
            exit_index = j
            reason = "target_gap"
            break
        # Conservative intrabar rule: stop wins if both levels are inside.
        if l <= stop and h >= target:
            exit_price = stop * (1.0 - slippage)
            exit_index = j
            reason = "stop"
            break
        if l <= stop:
            exit_price = stop * (1.0 - slippage)
            exit_index = j
            reason = "stop"
            break
        if h >= target:
            exit_price = target * (1.0 - slippage)
            exit_index = j
            reason = "target"
            break

    if exit_price is None:
        exit_price = float(bars.close[last]) * (1.0 - slippage)
        exit_index = last
        reason = "time"

    entry_time = int(bars.open_time[i])
    exit_time = int(bars.close_time[exit_index])
    funding_pct, funding_missing = funding_cost_pct(
        entry_time, exit_time, funding_times, funding_rates
    )
    gross_pct = exit_price / entry - 1.0
    fee_pct = fee_rate * (1.0 + exit_price / entry)
    net_pct = gross_pct - fee_pct - funding_pct
    risk_pct = (entry - stop) / entry
    r_multiple = net_pct / risk_pct if risk_pct > 0 else float("nan")
    return Trade(
        symbol=signal.symbol,
        signal_index=signal.signal_index,
        entry_index=i,
        exit_index=exit_index,
        entry_time=entry_time,
        exit_time=exit_time,
        entry=entry,
        stop=stop,
        target=target,
        exit=exit_price,
        reason=reason,
        gross_pct=gross_pct,
        fee_pct=fee_pct,
        funding_pct=funding_pct,
        net_pct=net_pct,
        risk_pct=risk_pct,
        r_multiple=r_multiple,
        funding_missing=funding_missing,
        truncated=truncated,
        excluded=funding_missing,
    )


def baseline_returns(
    bars: Bars,
    funding_times: Sequence[int],
    funding_rates: Sequence[float],
    *,
    horizon_bars: int = 96,
    fee_rate: float = 0.001,
    slippage: float = 0.0002,
) -> list[dict[str, float | int]]:
    """Unconditional long: next open to close ``horizon_bars`` later.

    This is a market-drift baseline, not a stop/target strategy.  Every
    eligible bar is included, so the observations overlap; block bootstrap by
    week is used for inference.
    """
    out: list[dict[str, float | int]] = []
    n = len(bars)
    last_entry = n - horizon_bars
    for i in range(0, last_entry):
        entry_index = i + 1
        exit_index = i + horizon_bars
        if exit_index >= n:
            break
        entry = float(bars.open[entry_index]) * (1.0 + slippage)
        exit_price = float(bars.close[exit_index]) * (1.0 - slippage)
        if entry <= 0 or exit_price <= 0:
            continue
        entry_time = int(bars.open_time[entry_index])
        exit_time = int(bars.close_time[exit_index])
        funding_pct, missing = funding_cost_pct(
            entry_time, exit_time, funding_times, funding_rates
        )
        gross = exit_price / entry - 1.0
        fees = fee_rate * (1.0 + exit_price / entry)
        net = gross - fees - funding_pct
        out.append(
            {
                "entry_index": entry_index,
                "exit_index": exit_index,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "gross_pct": gross,
                "fee_pct": fees,
                "funding_pct": funding_pct,
                "net_pct": net,
                "funding_missing": bool(missing),
            }
        )
    return out


def week_key(open_time_ms: int) -> tuple[int, int]:
    dt = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
    iso = dt.isocalendar()
    return int(iso.year), int(iso.week)


def _mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else float("nan")


def block_bootstrap_mean(
    values_by_week: dict[tuple[int, int], list[float]],
    *,
    iterations: int = 2000,
    seed: int = 20261030,
    confidence: float = 0.95,
) -> dict[str, float]:
    """Week-block percentile interval for a mean."""
    keys = sorted(values_by_week)
    if not keys:
        return {"low": float("nan"), "high": float("nan"), "mean": float("nan")}
    import random

    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(iterations):
        picked = [keys[rng.randrange(len(keys))] for _ in range(len(keys))]
        values = [v for key in picked for v in values_by_week[key]]
        if values:
            means.append(sum(values) / len(values))
    means.sort()
    alpha = (1.0 - confidence) / 2.0
    low = means[int(alpha * (len(means) - 1))]
    high = means[int((1.0 - alpha) * (len(means) - 1))]
    return {
        "low": low,
        "high": high,
        "mean": _mean(v for vals in values_by_week.values() for v in vals),
    }


def block_bootstrap_difference(
    signal_by_week: dict[tuple[int, int], list[float]],
    baseline_by_week: dict[tuple[int, int], list[float]],
    *,
    iterations: int = 2000,
    seed: int = 20261030,
    confidence: float = 0.95,
) -> dict[str, float]:
    """Paired week-block interval for signal mean minus baseline mean.

    Only weeks containing at least one signal are used, so the comparison is
    against the market in the same weeks rather than against a different
    regime.
    """
    keys = sorted(k for k in signal_by_week if baseline_by_week.get(k))
    if not keys:
        return {"low": float("nan"), "high": float("nan"), "mean": float("nan")}
    import random

    rng = random.Random(seed)
    diffs: list[float] = []
    for _ in range(iterations):
        picked = [keys[rng.randrange(len(keys))] for _ in range(len(keys))]
        sig = [v for key in picked for v in signal_by_week[key]]
        base = [v for key in picked for v in baseline_by_week[key]]
        if sig and base:
            diffs.append(sum(sig) / len(sig) - sum(base) / len(base))
    diffs.sort()
    alpha = (1.0 - confidence) / 2.0
    observed_sig = [v for key in keys for v in signal_by_week[key]]
    observed_base = [v for key in keys for v in baseline_by_week[key]]
    return {
        "low": diffs[int(alpha * (len(diffs) - 1))],
        "high": diffs[int((1.0 - alpha) * (len(diffs) - 1))],
        "mean": _mean(observed_sig) - _mean(observed_base),
    }
