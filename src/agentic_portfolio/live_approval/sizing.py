"""Deterministic live-order sizing. Never invent a trade amount."""

from __future__ import annotations

from typing import Any

from agentic_portfolio.live_approval.types import LiveApproval, LiveApprovalStatus
from agentic_portfolio.runtime import live_placement_enabled


MISSING_ORDER_SIZING = "missing_order_sizing"


def as_positive_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:
        return None
    return number


def resolve_order_sizing(
    *,
    proposed_notional: Any = None,
    desired_allocation_pct: Any = None,
    nav: Any = None,
) -> tuple[float | None, float | None, str | None]:
    """Return (dollars, pct, fail_reason). Never invent an amount."""
    dollars = as_positive_float(proposed_notional)
    pct = as_positive_float(desired_allocation_pct)
    nav_value = as_positive_float(nav)
    if dollars is None and pct is not None and nav_value is not None:
        dollars = nav_value * (pct / 100.0)
    if dollars is None:
        return None, pct, MISSING_ORDER_SIZING
    return dollars, pct, None


def sizing_from_watch(item: Any) -> tuple[Any, Any]:
    """Read persisted watch/plan sizing. Ignore quote payloads."""
    plan = getattr(item, "conditional_plan", None)
    notional = getattr(item, "proposed_notional", None)
    pct = getattr(item, "desired_allocation_pct", None)
    if plan is not None:
        if as_positive_float(notional) is None:
            notional = getattr(plan, "proposed_notional", None)
        if as_positive_float(pct) is None:
            pct = getattr(plan, "desired_allocation_pct", None)
    return notional, pct


def snapshot_execution_flags(*, placement_enabled: bool | None = None) -> dict[str, Any]:
    """Current runtime execution context. AUTO_EXECUTION stays false."""
    placement = live_placement_enabled() if placement_enabled is None else bool(placement_enabled)
    return {
        "LIVE_ORDER_PLACEMENT": placement,
        "live_execution_blocked": not placement,
        "live_trade_actions_allowed": placement,
        "auto_execution": False,
    }


def pending_is_reusable(
    item: LiveApproval,
    *,
    dollars: float | None,
    placement_enabled: bool,
) -> bool:
    """Reuse only a well-formed PENDING packet whose execution flags match now.

    Malformed (null/non-positive dollars) or stale blocked packets must be
    retired, not mutated into an executable approval.
    """
    if item.status is not LiveApprovalStatus.PENDING:
        return False
    existing = as_positive_float(item.proposed_dollar_amount)
    if existing is None:
        return False
    wanted = as_positive_float(dollars)
    if wanted is not None and abs(existing - wanted) > 0.01:
        return False
    flags = snapshot_execution_flags(placement_enabled=placement_enabled)
    if bool(item.LIVE_ORDER_PLACEMENT) != bool(flags["LIVE_ORDER_PLACEMENT"]):
        return False
    if bool(item.live_execution_blocked) != bool(flags["live_execution_blocked"]):
        return False
    if bool(item.live_trade_actions_allowed) != bool(flags["live_trade_actions_allowed"]):
        return False
    return True
