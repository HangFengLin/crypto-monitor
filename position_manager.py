"""Shared, exchange-agnostic position lifecycle decisions.

This module intentionally contains no network, persistence, notification, or
exchange-specific code. Callers remain responsible for executing an order and
recording state after receiving an exit decision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

LEGACY_RATIO_V1 = "legacy_ratio_v1"
LINEAR_USDM_V1 = "linear_usdm_v1"
RETURN_MODEL_CHOICES = (LEGACY_RATIO_V1, LINEAR_USDM_V1)

STRUCTURE_ATR_STOP_MODE = "structure_atr"
STRUCTURE_ATR_TARGET_ONLY_STOP_MODE = "structure_atr_target_only"
STRUCTURE_ATR_BREAKEVEN_AFTER_1R_STOP_MODE = "structure_atr_breakeven_after_1r"
ATR_TRAILING_AFTER_1R_STOP_MODE = "atr_trailing_after_1r"
PARTIAL_1R_BREAKEVEN_STOP_MODE = "partial_1r_breakeven"
STOP_MODE_CHOICES = (
    STRUCTURE_ATR_STOP_MODE,
    STRUCTURE_ATR_BREAKEVEN_AFTER_1R_STOP_MODE,
    ATR_TRAILING_AFTER_1R_STOP_MODE,
    PARTIAL_1R_BREAKEVEN_STOP_MODE,
    STRUCTURE_ATR_TARGET_ONLY_STOP_MODE,
)


@dataclass(frozen=True)
class ExitDecision:
    reason: str
    exit_price: float
    outcome: str


@dataclass(frozen=True)
class PartialExit:
    reason: str
    exit_price: float
    fraction: float
    outcome: str


@dataclass(frozen=True)
class PositionLifecycleResult:
    decision: ExitDecision | None
    active_stop: float
    highest_price: float
    lowest_price: float
    protection_activated: bool
    protected_stop_price: float
    remaining_fraction: float = 1.0
    partial_taken: bool = False
    partial_exits: tuple[PartialExit, ...] = ()


def _validate_return_model(return_model: str) -> None:
    if return_model not in RETURN_MODEL_CHOICES:
        raise ValueError(f"unsupported return model: {return_model!r}")


def _validate_positive_price(name: str, value: float) -> None:
    try:
        valid = not isinstance(value, bool) and math.isfinite(value) and value > 0
    except (TypeError, OverflowError):
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a finite positive price")


def _validate_linear_inputs(direction: str, entry_price: float, fee_rate: float) -> None:
    if direction not in {"long", "short"}:
        raise ValueError(f"unsupported position direction: {direction!r}")
    _validate_positive_price("entry_price", entry_price)
    try:
        valid_fee = not isinstance(fee_rate, bool) and math.isfinite(fee_rate) and 0 <= fee_rate < 1
    except (TypeError, OverflowError):
        valid_fee = False
    if not valid_fee:
        raise ValueError("fee_rate must be finite and satisfy 0 <= fee_rate < 1")


def _versioned_exit_decision(
    decision: ExitDecision | None,
    direction: str,
    entry_price: float | None,
    fee_rate: float,
    return_model: str,
) -> ExitDecision | None:
    if decision is None or return_model == LEGACY_RATIO_V1:
        return decision
    if entry_price is None:
        raise ValueError("entry_price must be a finite positive price")
    return ExitDecision(
        decision.reason,
        decision.exit_price,
        classify_return_outcome(direction, entry_price, decision.exit_price, fee_rate,
                                return_model=return_model),
    )


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
    *,
    entry_price: float | None = None,
    fee_rate: float = 0.0,
    return_model: str = LEGACY_RATIO_V1,
) -> ExitDecision | None:
    """Evaluate one completed bar, conservatively checking stop loss first."""
    _validate_return_model(return_model)
    if return_model == LINEAR_USDM_V1:
        if entry_price is None:
            raise ValueError("entry_price must be a finite positive price")
        _validate_linear_inputs(direction, entry_price, fee_rate)
        for name, price in (("low", low), ("high", high), ("stop_loss", stop_loss),
                            ("target_price", target_price), ("protection_price", protection_price)):
            _validate_positive_price(name, price)
    decision = None
    if direction == "long":
        if low <= stop_loss:
            decision = ExitDecision("stop_loss", stop_loss, "loss")
        elif high >= target_price:
            decision = ExitDecision("take_profit", target_price, "win")
        elif high >= protection_price:
            decision = ExitDecision("protection_reached", protection_price, "win")
    elif direction == "short":
        if high >= stop_loss:
            decision = ExitDecision("stop_loss", stop_loss, "loss")
        elif low <= target_price:
            decision = ExitDecision("take_profit", target_price, "win")
        elif low <= protection_price:
            decision = ExitDecision("protection_reached", protection_price, "win")
    return _versioned_exit_decision(decision, direction, entry_price, fee_rate, return_model)


def evaluate_price_exit(
    direction: str,
    current_price: float,
    stop_loss: float,
    target_price: float,
    protection_price: float,
    *,
    entry_price: float | None = None,
    fee_rate: float = 0.0,
    return_model: str = LEGACY_RATIO_V1,
) -> ExitDecision | None:
    """Evaluate a point-in-time price using the same trigger priority as bars."""
    return evaluate_bar_exit(
        direction,
        current_price,
        current_price,
        stop_loss,
        target_price,
        protection_price,
        entry_price=entry_price,
        fee_rate=fee_rate,
        return_model=return_model,
    )


def calculate_return_pct(
    direction: str,
    entry_price: float,
    exit_price: float,
    fee_rate: float = 0.0,
    *,
    return_model: str = LEGACY_RATIO_V1,
) -> float:
    """Return PnL divided by entry notional, or the unchanged legacy ratio.

    Linear USD-M charges fees on each fill's notional. The default remains the
    historical ratio formula so callers and persisted trades must opt in.
    """
    _validate_return_model(return_model)
    if return_model == LINEAR_USDM_V1:
        _validate_linear_inputs(direction, entry_price, fee_rate)
        _validate_positive_price("exit_price", exit_price)
        price_ratio = exit_price / entry_price
        direction_sign = 1 if direction == "long" else -1
        net_return = direction_sign * (exit_price - entry_price) / entry_price - fee_rate * (1 + price_ratio)
        if not math.isfinite(net_return):
            raise ValueError("linear return must be finite")
        return net_return
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
    *,
    return_model: str = LEGACY_RATIO_V1,
) -> str:
    """Classify a realized net return without counting fee-adjusted breakeven as a win."""
    net_return = calculate_return_pct(direction, entry_price, exit_price, fee_rate,
                                      return_model=return_model)
    if net_return > epsilon:
        return "win"
    if net_return < -epsilon:
        return "loss"
    return "breakeven"


def breakeven_stop_price(
    direction: str,
    entry_price: float,
    fee_rate: float = 0.0,
    *,
    return_model: str = LEGACY_RATIO_V1,
) -> float:
    """Return a fee-adjusted breakeven stop for 1R protection modes."""
    _validate_return_model(return_model)
    if return_model == LINEAR_USDM_V1:
        _validate_linear_inputs(direction, entry_price, fee_rate)
        if direction == "long":
            price = entry_price * ((1 + fee_rate) / (1 - fee_rate))
        else:
            price = entry_price * ((1 - fee_rate) / (1 + fee_rate))
        _validate_positive_price("breakeven_stop_price", price)
        return price
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
    *,
    return_model: str = LEGACY_RATIO_V1,
) -> float:
    """Move a stop only in the favorable direction after price reaches 1R."""
    _validate_return_model(return_model)
    if return_model == LINEAR_USDM_V1:
        _validate_linear_inputs(direction, entry_price, fee_rate)
        _validate_positive_price("current_stop", current_stop)
        _validate_positive_price("favorable_price", favorable_price)
    if atr_value is None or atr_value <= 0 or initial_risk <= 0:
        return current_stop

    if direction == "long":
        if favorable_price < entry_price + initial_risk:
            return current_stop
        trailing_stop = favorable_price - atr_value * atr_multiplier
        return max(current_stop, trailing_stop, breakeven_stop_price(
            direction, entry_price, fee_rate, return_model=return_model))

    if favorable_price > entry_price - initial_risk:
        return current_stop
    trailing_stop = favorable_price + atr_value * atr_multiplier
    return min(current_stop, trailing_stop, breakeven_stop_price(
        direction, entry_price, fee_rate, return_model=return_model))


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
    return_model: str = LEGACY_RATIO_V1,
    remaining_fraction: float = 1.0,
    partial_taken: bool = False,
) -> PositionLifecycleResult:
    """Evaluate one bar and return both exit decision and updated lifecycle state.

    ``structure_atr`` preserves the legacy behavior where touching the 1R
    protection level immediately exits. The protected modes arm protection at
    1R, move the stop, and keep the trade alive for target/trailing outcomes.
    ``structure_atr_target_only`` retains the initial stop and full quantity
    until stop or target, ignoring 1R protection and ATR updates. Callers own
    holding limits, gap execution, slippage and funding.
    ``partial_1r_breakeven`` requires linear accounting and emits only this
    call's new half-quantity fill. Its final decision consumes the input
    remaining fraction minus those new fills; callers retain prior fills and
    prevent completed-bar replay. Its updated protected stop starts next bar.
    """
    _validate_return_model(return_model)
    active_stop = initial_stop_loss if active_stop is None else active_stop
    highest_price = entry_price if highest_price is None else highest_price
    lowest_price = entry_price if lowest_price is None else lowest_price
    if return_model == LINEAR_USDM_V1:
        _validate_linear_inputs(direction, entry_price, fee_rate)
        for name, price in (("low", low), ("high", high), ("close", close),
                            ("initial_stop_loss", initial_stop_loss), ("active_stop", active_stop),
                            ("target_price", target_price), ("protection_price", protection_price),
                            ("highest_price", highest_price), ("lowest_price", lowest_price)):
            _validate_positive_price(name, price)
    highest_price = max(highest_price, high, close)
    lowest_price = min(lowest_price, low, close)

    if stop_mode == STRUCTURE_ATR_TARGET_ONLY_STOP_MODE:
        if (active_stop != initial_stop_loss or protection_activated is not False
                or protected_stop_price != 0.0 or isinstance(remaining_fraction, bool)
                or remaining_fraction != 1.0 or partial_taken is not False):
            raise ValueError("target-only mode requires an unchanged initial stop and full unprotected quantity")
        # Equal target/protection prices make the protection branch unreachable;
        # retain shared stop-first priority and versioned fee classification.
        decision = evaluate_bar_exit(
            direction, low, high, initial_stop_loss, target_price, target_price,
            entry_price=entry_price, fee_rate=fee_rate, return_model=return_model,
        )
        return PositionLifecycleResult(
            decision, initial_stop_loss, highest_price, lowest_price, False, 0.0,
        )

    if stop_mode == PARTIAL_1R_BREAKEVEN_STOP_MODE:
        if return_model != LINEAR_USDM_V1:
            raise ValueError("partial exits require linear_usdm_v1 accounting")
        if (isinstance(remaining_fraction, bool)
                or not isinstance(remaining_fraction, (int, float))
                or not math.isfinite(remaining_fraction)
                or not isinstance(partial_taken, bool)
                or remaining_fraction != (0.5 if partial_taken else 1.0)):
            raise ValueError("partial state must be full/unreduced or half/already reduced")
        risk = entry_price - initial_stop_loss if direction == "long" else initial_stop_loss - entry_price
        one_r_price = entry_price + risk if direction == "long" else entry_price - risk
        if risk <= 0 or not math.isclose(protection_price, one_r_price, rel_tol=1e-12):
            raise ValueError("partial mode requires a valid initial stop and its fixed 1R protection price")
        if ((direction == "long" and target_price < protection_price)
                or (direction == "short" and target_price > protection_price)):
            raise ValueError("partial mode target must be at or beyond 1R")
        breakeven_stop = breakeven_stop_price(direction, entry_price, fee_rate, return_model=return_model)
        if partial_taken:
            if ((direction == "long" and active_stop < breakeven_stop)
                    or (direction == "short" and active_stop > breakeven_stop)):
                raise ValueError("remaining half must retain its fee-adjusted protected stop")
            protection_activated = True
            protected_stop_price = active_stop
        partial_exits: tuple[PartialExit, ...] = ()

        def partial_result(decision: ExitDecision | None = None) -> PositionLifecycleResult:
            assert active_stop is not None
            return PositionLifecycleResult(
                decision, active_stop, highest_price, lowest_price,
                protection_activated, protected_stop_price,
                remaining_fraction=0.0 if decision is not None else remaining_fraction,
                partial_taken=partial_taken, partial_exits=partial_exits,
            )

        stop_touched = low <= active_stop if direction == "long" else high >= active_stop
        if stop_touched:
            reason = "protected_stop" if protection_activated else "stop_loss"
            return partial_result(ExitDecision(
                reason, active_stop,
                classify_return_outcome(direction, entry_price, active_stop, fee_rate,
                                        return_model=return_model),
            ))
        one_r_touched = high >= protection_price if direction == "long" else low <= protection_price
        if not partial_taken and one_r_touched:
            partial_exits = (PartialExit(
                "partial_1r", protection_price, 0.5,
                classify_return_outcome(direction, entry_price, protection_price, fee_rate,
                                        return_model=return_model),
            ),)
            remaining_fraction -= 0.5
            partial_taken = True
            protection_activated = True
            active_stop = max(active_stop, breakeven_stop) if direction == "long" else min(active_stop, breakeven_stop)
            protected_stop_price = active_stop
            # The new stop is next-bar state; do not retest this bar's range.
        target_touched = high >= target_price if direction == "long" else low <= target_price
        if target_touched:
            return partial_result(ExitDecision(
                "take_profit", target_price,
                classify_return_outcome(direction, entry_price, target_price, fee_rate,
                                        return_model=return_model),
            ))
        return partial_result()

    if stop_mode == STRUCTURE_ATR_STOP_MODE:
        decision = evaluate_bar_exit(direction, low, high, active_stop, target_price, protection_price,
                                     entry_price=entry_price, fee_rate=fee_rate, return_model=return_model)
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
            outcome = classify_return_outcome(direction, entry_price, active_stop, fee_rate,
                                              return_model=return_model)
            decision = ExitDecision(reason, active_stop, outcome)
            return PositionLifecycleResult(decision, active_stop, highest_price, lowest_price, protection_activated, protected_stop_price)
        if high >= target_price:
            decision = _versioned_exit_decision(ExitDecision("take_profit", target_price, "win"),
                                                direction, entry_price, fee_rate, return_model)
            return PositionLifecycleResult(decision, active_stop, highest_price, lowest_price, protection_activated, protected_stop_price)
        if high >= protection_price:
            protection_activated = True
            if stop_mode == STRUCTURE_ATR_BREAKEVEN_AFTER_1R_STOP_MODE:
                protected_stop_price = breakeven_stop_price(direction, entry_price, fee_rate,
                                                            return_model=return_model)
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
                return_model=return_model,
            )
            if active_stop > previous_stop:
                protection_activated = True
                protected_stop_price = max(protected_stop_price, active_stop)
        return PositionLifecycleResult(None, active_stop, highest_price, lowest_price, protection_activated, protected_stop_price)

    if direction == "short":
        if high >= active_stop:
            reason = "trailing_stop" if protection_activated and stop_mode == ATR_TRAILING_AFTER_1R_STOP_MODE else "protected_stop" if protection_activated else "stop_loss"
            outcome = classify_return_outcome(direction, entry_price, active_stop, fee_rate,
                                              return_model=return_model)
            decision = ExitDecision(reason, active_stop, outcome)
            return PositionLifecycleResult(decision, active_stop, highest_price, lowest_price, protection_activated, protected_stop_price)
        if low <= target_price:
            decision = _versioned_exit_decision(ExitDecision("take_profit", target_price, "win"),
                                                direction, entry_price, fee_rate, return_model)
            return PositionLifecycleResult(decision, active_stop, highest_price, lowest_price, protection_activated, protected_stop_price)
        if low <= protection_price:
            protection_activated = True
            if stop_mode == STRUCTURE_ATR_BREAKEVEN_AFTER_1R_STOP_MODE:
                protected_stop_price = breakeven_stop_price(direction, entry_price, fee_rate,
                                                            return_model=return_model)
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
                return_model=return_model,
            )
            if active_stop < previous_stop:
                protection_activated = True
                protected_stop_price = active_stop if protected_stop_price == 0.0 else min(protected_stop_price, active_stop)
        return PositionLifecycleResult(None, active_stop, highest_price, lowest_price, protection_activated, protected_stop_price)

    return PositionLifecycleResult(None, active_stop, highest_price, lowest_price, protection_activated, protected_stop_price)
