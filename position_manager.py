"""Shared, exchange-agnostic position lifecycle decisions.

This module intentionally contains no network, persistence, notification, or
exchange-specific code. Callers remain responsible for executing an order and
recording state after receiving an exit decision.
"""

from __future__ import annotations

from dataclasses import dataclass

STRUCTURE_ATR_STOP_MODE = "structure_atr"
STRUCTURE_ATR_BREAKEVEN_AFTER_1R_STOP_MODE = "structure_atr_breakeven_after_1r"
ATR_TRAILING_AFTER_1R_STOP_MODE = "atr_trailing_after_1r"
STOP_MODE_CHOICES = (
    STRUCTURE_ATR_STOP_MODE,
    STRUCTURE_ATR_BREAKEVEN_AFTER_1R_STOP_MODE,
    ATR_TRAILING_AFTER_1R_STOP_MODE,
)


@dataclass(frozen=True)
class ExitDecision:
    reason: str
    exit_price: float
    outcome: str


@dataclass(frozen=True)
class PositionLifecycleResult:
    decision: ExitDecision | None
    active_stop: float
    highest_price: float
    lowest_price: float
    protection_activated: bool
    protected_stop_price: float


def calculate_target_levels(
    direction: str,
    entry_price: float,
    stop_loss: float,
    reward_risk: float,
) -> dict[str, float] | None:
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
) -> ExitDecision | None:
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
) -> ExitDecision | None:
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


def classify_return_outcome(
    direction: str,
    entry_price: float,
    exit_price: float,
    fee_rate: float = 0.0,
    epsilon: float = 1e-12,
) -> str:
    """Classify a realized net return without counting fee-adjusted breakeven as a win."""
    net_return = calculate_return_pct(direction, entry_price, exit_price, fee_rate)
    if net_return > epsilon:
        return "win"
    if net_return < -epsilon:
        return "loss"
    return "breakeven"


def breakeven_stop_price(direction: str, entry_price: float, fee_rate: float = 0.0) -> float:
    """Return a fee-adjusted breakeven stop for 1R protection modes."""
    if direction == "long":
        return entry_price * (1 + fee_rate * 2)
    return entry_price / (1 + fee_rate * 2)


def update_trailing_stop(
    direction: str,
    current_stop: float,
    entry_price: float,
    initial_risk: float,
    favorable_price: float,
    atr_value: float | None,
    fee_rate: float = 0.0,
    atr_multiplier: float = 1.2,
) -> float:
    """Move a stop only in the favorable direction after price reaches 1R."""
    if atr_value is None or atr_value <= 0 or initial_risk <= 0:
        return current_stop

    if direction == "long":
        if favorable_price < entry_price + initial_risk:
            return current_stop
        trailing_stop = favorable_price - atr_value * atr_multiplier
        return max(current_stop, trailing_stop, breakeven_stop_price(direction, entry_price, fee_rate))

    if favorable_price > entry_price - initial_risk:
        return current_stop
    trailing_stop = favorable_price + atr_value * atr_multiplier
    return min(current_stop, trailing_stop, breakeven_stop_price(direction, entry_price, fee_rate))


def evaluate_lifecycle_bar(
    direction: str,
    low: float,
    high: float,
    close: float,
    entry_price: float,
    initial_stop_loss: float,
    target_price: float,
    protection_price: float,
    *,
    active_stop: float | None = None,
    highest_price: float | None = None,
    lowest_price: float | None = None,
    protection_activated: bool = False,
    protected_stop_price: float = 0.0,
    stop_mode: str = STRUCTURE_ATR_STOP_MODE,
    fee_rate: float = 0.0,
    atr_value: float | None = None,
    trailing_atr_multiplier: float = 1.2,
) -> PositionLifecycleResult:
    """Evaluate one bar and return both exit decision and updated lifecycle state.

    ``structure_atr`` preserves the legacy behavior where touching the 1R
    protection level immediately exits. The protected modes arm protection at
    1R, move the stop, and keep the trade alive for target/trailing outcomes.
    """
    active_stop = initial_stop_loss if active_stop is None else active_stop
    highest_price = entry_price if highest_price is None else highest_price
    lowest_price = entry_price if lowest_price is None else lowest_price
    highest_price = max(highest_price, high, close)
    lowest_price = min(lowest_price, low, close)

    if stop_mode == STRUCTURE_ATR_STOP_MODE:
        decision = evaluate_bar_exit(direction, low, high, active_stop, target_price, protection_price)
        if decision and decision.reason == "protection_reached":
            protection_activated = True
            protected_stop_price = protection_price
        return PositionLifecycleResult(
            decision,
            active_stop,
            highest_price,
            lowest_price,
            protection_activated,
            protected_stop_price,
        )

    if direction == "long":
        if low <= active_stop:
            reason = "trailing_stop" if protection_activated and stop_mode == ATR_TRAILING_AFTER_1R_STOP_MODE else "protected_stop" if protection_activated else "stop_loss"
            outcome = classify_return_outcome(direction, entry_price, active_stop, fee_rate)
            decision = ExitDecision(reason, active_stop, outcome)
            return PositionLifecycleResult(decision, active_stop, highest_price, lowest_price, protection_activated, protected_stop_price)
        if high >= target_price:
            decision = ExitDecision("take_profit", target_price, "win")
            return PositionLifecycleResult(decision, active_stop, highest_price, lowest_price, protection_activated, protected_stop_price)
        if high >= protection_price:
            protection_activated = True
            if stop_mode == STRUCTURE_ATR_BREAKEVEN_AFTER_1R_STOP_MODE:
                protected_stop_price = breakeven_stop_price(direction, entry_price, fee_rate)
                active_stop = max(active_stop, protected_stop_price)
        if stop_mode == ATR_TRAILING_AFTER_1R_STOP_MODE:
            previous_stop = active_stop
            active_stop = update_trailing_stop(
                direction,
                active_stop,
                entry_price,
                abs(entry_price - initial_stop_loss),
                highest_price,
                atr_value,
                fee_rate,
                trailing_atr_multiplier,
            )
            if active_stop > previous_stop:
                protection_activated = True
                protected_stop_price = max(protected_stop_price, active_stop)
        return PositionLifecycleResult(None, active_stop, highest_price, lowest_price, protection_activated, protected_stop_price)

    if direction == "short":
        if high >= active_stop:
            reason = "trailing_stop" if protection_activated and stop_mode == ATR_TRAILING_AFTER_1R_STOP_MODE else "protected_stop" if protection_activated else "stop_loss"
            outcome = classify_return_outcome(direction, entry_price, active_stop, fee_rate)
            decision = ExitDecision(reason, active_stop, outcome)
            return PositionLifecycleResult(decision, active_stop, highest_price, lowest_price, protection_activated, protected_stop_price)
        if low <= target_price:
            decision = ExitDecision("take_profit", target_price, "win")
            return PositionLifecycleResult(decision, active_stop, highest_price, lowest_price, protection_activated, protected_stop_price)
        if low <= protection_price:
            protection_activated = True
            if stop_mode == STRUCTURE_ATR_BREAKEVEN_AFTER_1R_STOP_MODE:
                protected_stop_price = breakeven_stop_price(direction, entry_price, fee_rate)
                active_stop = min(active_stop, protected_stop_price)
        if stop_mode == ATR_TRAILING_AFTER_1R_STOP_MODE:
            previous_stop = active_stop
            active_stop = update_trailing_stop(
                direction,
                active_stop,
                entry_price,
                abs(initial_stop_loss - entry_price),
                lowest_price,
                atr_value,
                fee_rate,
                trailing_atr_multiplier,
            )
            if active_stop < previous_stop:
                protection_activated = True
                protected_stop_price = active_stop if protected_stop_price == 0.0 else min(protected_stop_price, active_stop)
        return PositionLifecycleResult(None, active_stop, highest_price, lowest_price, protection_activated, protected_stop_price)

    return PositionLifecycleResult(None, active_stop, highest_price, lowest_price, protection_activated, protected_stop_price)
