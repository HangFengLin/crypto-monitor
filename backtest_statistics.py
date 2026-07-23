from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

import pandas as pd

DEFAULT_BOOTSTRAP_SEED = 20260620
DEFAULT_BOOTSTRAP_ITERATIONS = 2000


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Return a Wilson score interval for a binomial proportion."""
    if total <= 0:
        return 0.0, 0.0
    proportion = successes / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    centre = (proportion + z2 / (2.0 * total)) / denominator
    margin = z * math.sqrt((proportion * (1.0 - proportion) + z2 / (4.0 * total)) / total) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _weekly_values(records: Iterable[dict[str, Any]], value_key: str, time_key: str) -> dict[str, list[float]]:
    weeks: dict[str, list[float]] = defaultdict(list)
    for record in records:
        value = record.get(value_key)
        timestamp = pd.to_datetime(record.get(time_key), utc=True, errors="coerce")
        if value is None or pd.isna(timestamp):
            continue
        week = timestamp.tz_convert("UTC").tz_localize(None).to_period("W-SUN").start_time.isoformat()
        weeks[week].append(float(value))
    return dict(weeks)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _confidence_bounds(estimates: list[float], confidence: float) -> tuple[float, float]:
    if not estimates:
        return 0.0, 0.0
    estimates.sort()
    tail = (1.0 - confidence) / 2.0
    low_index = min(len(estimates) - 1, max(0, int(math.floor(tail * (len(estimates) - 1)))))
    high_index = min(len(estimates) - 1, max(0, int(math.ceil((1.0 - tail) * (len(estimates) - 1)))))
    return estimates[low_index], estimates[high_index]


def bootstrap_mean_summary(
    values: Iterable[Any],
    *,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    confidence: float = 0.95,
) -> dict[str, float]:
    """Bootstrap a simple iid mean and return a standard summary payload."""
    clean: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            clean.append(number)
    if not clean:
        return {"n": 0.0, "mean": 0.0, "ci_low": 0.0, "ci_high": 0.0, "p_nonpositive": 1.0}

    rng = random.Random(seed)
    estimates = [_mean([rng.choice(clean) for _item in clean]) for _ in range(max(1, iterations))]
    ci_low, ci_high = _confidence_bounds(estimates, confidence)
    return {
        "n": float(len(clean)),
        "mean": _mean(clean),
        "ci_low": ci_low,
        "ci_high": ci_high,
        "p_nonpositive": sum(value <= 0 for value in estimates) / len(estimates),
    }


def weekly_block_bootstrap_summary(
    records: Iterable[dict[str, Any]],
    *,
    value_key: str = "return_pct",
    time_key: str = "exit_time",
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    confidence: float = 0.95,
) -> dict[str, float]:
    """Bootstrap a mean by resampling UTC calendar weeks as dependence blocks."""
    weeks = _weekly_values(records, value_key, time_key)
    if not weeks:
        return {"n": 0.0, "mean": 0.0, "ci_low": 0.0, "ci_high": 0.0, "p_nonpositive": 1.0}
    keys = sorted(weeks)
    observed = [value for values in weeks.values() for value in values]
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(max(1, iterations)):
        sample: list[float] = []
        for _block in keys:
            sample.extend(weeks[rng.choice(keys)])
        estimates.append(_mean(sample))
    ci_low, ci_high = _confidence_bounds(estimates, confidence)
    return {
        "n": float(len(observed)),
        "mean": _mean(observed),
        "ci_low": ci_low,
        "ci_high": ci_high,
        "p_nonpositive": sum(value <= 0 for value in estimates) / len(estimates),
    }


def weekly_block_bootstrap_mean(
    records: Iterable[dict[str, Any]],
    *,
    value_key: str = "return_pct",
    time_key: str = "exit_time",
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Bootstrap the mean by resampling UTC calendar weeks as dependence blocks."""
    summary = weekly_block_bootstrap_summary(
        records,
        value_key=value_key,
        time_key=time_key,
        iterations=iterations,
        seed=seed,
        confidence=confidence,
    )
    return summary["ci_low"], summary["ci_high"]


def weekly_block_bootstrap_difference(
    candidate_records: Iterable[dict[str, Any]],
    baseline_records: Iterable[dict[str, Any]],
    *,
    value_key: str = "return_pct",
    time_key: str = "exit_time",
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    confidence: float = 0.95,
) -> dict[str, float]:
    """Compare two samples by resampling aligned UTC-week blocks."""
    candidate = _weekly_values(candidate_records, value_key, time_key)
    baseline = _weekly_values(baseline_records, value_key, time_key)
    keys = sorted(set(candidate) | set(baseline))
    if not keys:
        return {"delta": 0.0, "ci_low": 0.0, "ci_high": 0.0, "p_value": 1.0}

    def flattened_mean(source: dict[str, list[float]], sampled: Sequence[str]) -> float:
        values = [value for key in sampled for value in source.get(key, [])]
        return sum(values) / len(values) if values else 0.0

    observed_candidate = [value for values in candidate.values() for value in values]
    observed_baseline = [value for values in baseline.values() for value in values]
    observed = (sum(observed_candidate) / len(observed_candidate) if observed_candidate else 0.0) - (
        sum(observed_baseline) / len(observed_baseline) if observed_baseline else 0.0
    )
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(max(1, iterations)):
        sampled = [rng.choice(keys) for _block in keys]
        estimates.append(flattened_mean(candidate, sampled) - flattened_mean(baseline, sampled))
    ci_low, ci_high = _confidence_bounds(estimates, confidence)
    non_positive = sum(value <= 0 for value in estimates)
    non_negative = sum(value >= 0 for value in estimates)
    # The +1 correction prevents a finite Monte Carlo run from reporting an
    # impossible exact p=0, which matters when BH-FDR is applied downstream.
    p_value = min(1.0, 2.0 * (min(non_positive, non_negative) + 1) / (len(estimates) + 1))
    return {
        "delta": observed,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "p_value": p_value,
    }


def benjamini_hochberg(p_values: Sequence[float], alpha: float = 0.10) -> list[dict[str, float | bool]]:
    """Return BH adjusted q-values and rejection flags in input order."""
    count = len(p_values)
    if not count:
        return []
    ordered = sorted(enumerate(float(max(0.0, min(1.0, value))) for value in p_values), key=lambda item: item[1])
    adjusted = [1.0] * count
    running = 1.0
    for reverse_rank, (index, p_value) in enumerate(reversed(ordered), start=1):
        rank = count - reverse_rank + 1
        running = min(running, p_value * count / rank)
        adjusted[index] = min(1.0, running)
    return [{"q_value": adjusted[index], "reject": adjusted[index] <= alpha} for index in range(count)]


def add_trade_buckets(frame: pd.DataFrame) -> pd.DataFrame:
    """Add stable exploratory buckets without mutating the caller's frame."""
    enriched = frame.copy()
    enriched["strength_bucket"] = pd.cut(
        enriched["strength"], bins=[-math.inf, 0.4, 0.7, math.inf], labels=["低强度", "中强度", "高强度"], include_lowest=True
    )
    enriched["score_bucket"] = pd.cut(
        enriched["signal_score"], bins=[-math.inf, 6, 9, math.inf], labels=["低分", "中分", "高分"], include_lowest=True
    )
    enriched["structure_bucket"] = pd.cut(
        enriched["structure_score"], bins=[-math.inf, 0, 1, math.inf], labels=["无结构", "弱结构", "强结构"], include_lowest=True
    )
    return enriched


def grouped_trade_statistics(frame: pd.DataFrame, dimensions: Sequence[str], min_trades: int = 30) -> pd.DataFrame:
    """Build one declared grouping table with uncertainty and a hard sample floor."""
    if frame.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    grouper: Any = dimensions[0] if len(dimensions) == 1 else list(dimensions)
    for keys, part in frame.groupby(grouper, observed=True, dropna=False):
        key_values = (keys,) if len(dimensions) == 1 else tuple(keys)
        total = len(part)
        if total < min_trades:
            continue
        successes = int((part["return_pct"] > 0).sum())
        ci_low, ci_high = wilson_interval(successes, total)
        row: dict[str, Any] = {
            "group_dimensions": " × ".join(dimensions),
            "group_value": " × ".join(str(value) for value in key_values),
            "trades": total,
            "win_rate": successes / total,
            "win_rate_ci_low": ci_low,
            "win_rate_ci_high": ci_high,
            "avg_return": float(part["return_pct"].mean()),
            "avg_score": float(part["signal_score"].mean()),
            "avg_structure": float(part["structure_score"].mean()),
            "avg_confirm_bars": float(part["confirm_bars"].mean()),
        }
        for dimension, value in zip(dimensions, key_values):
            row[dimension] = value
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["avg_return", "trades"], ascending=[False, False]).reset_index(drop=True)


def build_exploratory_group_tables(
    trade_frame: pd.DataFrame,
    *,
    min_trades: int = 30,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return report-safe marginal/predeclared groups and a raw full 8-D table."""
    if trade_frame.empty:
        return pd.DataFrame(), pd.DataFrame()
    frame = add_trade_buckets(trade_frame)
    marginal_dimensions = [
        "signal",
        "trend",
        "higher_trend",
        "signal_grade",
        "score_bucket",
        "structure_bucket",
        "divergence_type",
        "strength_bucket",
    ]
    declared_pairs = [("higher_trend", "structure_bucket"), ("signal", "score_bucket"), ("divergence_type", "strength_bucket")]
    tables = [grouped_trade_statistics(frame, [dimension], min_trades) for dimension in marginal_dimensions]
    tables.extend(grouped_trade_statistics(frame, pair, min_trades) for pair in declared_pairs)
    report_safe = pd.concat([table for table in tables if not table.empty], ignore_index=True) if any(not table.empty for table in tables) else pd.DataFrame()

    full_dimensions = marginal_dimensions
    full = grouped_trade_statistics(frame, full_dimensions, min_trades=1)
    return report_safe, full
