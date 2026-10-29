"""Pure state transitions for support observations, independent of trade exits."""

import math
from typing import Any


def _finite_number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        return None
    return float(value)


def _previous_state(previous: dict | None) -> dict | None:
    if not isinstance(previous, dict) or previous.get("status") not in {"near", "far", "broken"}:
        return None
    support = _finite_number(previous.get("support"), positive=True)
    tolerance = _finite_number(previous.get("tolerance_pct"))
    if support is None or tolerance is None:
        return None
    return {"support": support, "status": previous["status"], "tolerance_pct": tolerance}


def _event(level: dict, support: float, tolerance: float, transition: str, status: str) -> dict:
    return {
        **level,
        "type": "support",
        "support": support,
        "tolerance_pct": tolerance,
        "distance_pct": (level["price"] - support) / support * 100,
        "touched": status == "near",
        "support_transition": transition,
        "support_status": status,
    }


def advance_support(previous: dict | None, level: dict) -> tuple[dict | None, list[dict]]:
    """Advance one symbol/interval's structure without changing either input.

    A near structure rearms only beyond twice its upper tolerance. A break is
    the current quote below the lower tolerance band, not a confirmed bar close.
    """
    price = _finite_number(level.get("price"), positive=True)
    support = _finite_number(level.get("support"), positive=True)
    tolerance = _finite_number(level.get("tolerance_pct"))
    if price is None or support is None or tolerance is None:
        return previous, []

    old = _previous_state(previous)
    same_structure = old is not None and math.isclose(old["support"], support, rel_tol=1e-9)
    if same_structure:
        # Keep one stable identity when only floating-point representation moves.
        support = old["support"]

    lower = support * (1 - tolerance / 100)
    upper = support * (1 + tolerance / 100)
    reset = support * (1 + 2 * tolerance / 100)
    if not all(math.isfinite(value) for value in (lower, upper, reset)):
        return previous, []

    events = []
    if old is not None and not same_structure and old["status"] != "broken":
        if price < old["support"] * (1 - old["tolerance_pct"] / 100):
            events.append(_event(level, old["support"], old["tolerance_pct"], "broken", "broken"))

    if price < lower:
        status = "broken"
    elif price <= upper or (same_structure and old["status"] == "near" and price <= reset):
        status = "near"
    else:
        status = "far"

    if status == "broken" and not (same_structure and old["status"] == "broken"):
        events.append(_event(level, support, tolerance, "broken", status))
    elif status == "near" and not (same_structure and old["status"] == "near"):
        transition = "first_touch" if old is None else "retest" if same_structure else "level_changed"
        events.append(_event(level, support, tolerance, transition, status))

    return {"support": support, "status": status, "tolerance_pct": tolerance}, events
