"""Shared, exchange-agnostic position lifecycle decisions.

This module intentionally contains no network, persistence, notification, or
exchange-specific code. Callers remain responsible for executing an order and
recording state after receiving an exit decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ExitDecision:
    reason: str
    exit_price: float
    outcome: str


def calculate_target_levels(
    direction: str,
    entry_price: float,
    stop_loss: float,
    reward_risk: float,
) -> Optional[dict[str, float]]:
    """Return risk, target, and protection levels for a valid position."""
    if direction == "long":
        risk = entry_price - stop_loss
        if risk <= 0:
            return None
        return {
            "risk": risk,
            "target_price": entry_price + risk * reward_risk,
            "protection_price": entry_price + risk,
        }
    if direction == "short":
        risk = stop_loss - entry_price
        if risk <= 0:
            return None
        return {
            "risk": risk,
            "target_price": entry_price - risk * reward_risk,
            "protection_price": entry_price - risk,
        }
    return None


def evaluate_bar_exit(
    direction: str,
    low: float,
    high: float,
    stop_loss: float,
    target_price: float,
    protection_price: float,
) -> Optional[ExitDecision]:
    """Evaluate one completed bar, conservatively checking stop loss first."""
    if direction == "long":
        if low <= stop_loss:
            return ExitDecision("stop_loss", stop_loss, "loss")
        if high >= target_price:
            return ExitDecision("take_profit", target_price, "win")
        if high >= protection_price:
            return ExitDecision("protection_reached", protection_price, "win")
    elif direction == "short":
        if high >= stop_loss:
            return ExitDecision("stop_loss", stop_loss, "loss")
        if low <= target_price:
            return ExitDecision("take_profit", target_price, "win")
        if low <= protection_price:
            return ExitDecision("protection_reached", protection_price, "win")
    return None


def evaluate_price_exit(
    direction: str,
    current_price: float,
    stop_loss: float,
    target_price: float,
    protection_price: float,
) -> Optional[ExitDecision]:
    """Evaluate a point-in-time price using the same trigger priority as bars."""
    return evaluate_bar_exit(
        direction,
        current_price,
        current_price,
        stop_loss,
        target_price,
        protection_price,
    )


def calculate_return_pct(
    direction: str,
    entry_price: float,
    exit_price: float,
    fee_rate: float = 0.0,
) -> float:
    """Calculate long/short return after a symmetric entry and exit fee."""
    if direction == "long":
        gross_return = exit_price / entry_price - 1
    elif direction == "short":
        gross_return = entry_price / exit_price - 1
    else:
        raise ValueError(f"unsupported position direction: {direction!r}")
    return gross_return - fee_rate * 2
