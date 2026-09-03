"""Send-time revalidation. Approval is not permission to execute stale state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from agentic_portfolio.live_approval.types import LiveApproval, LiveApprovalStatus
from agentic_portfolio.live_execution.sizing import held_quantity_from_context
from agentic_portfolio.policy import load_live_execution_config
from agentic_portfolio.schemas import PortfolioContext, ThesisStatus


EXPIRED_APPROVAL = {
    LiveApprovalStatus.EXPIRED,
    LiveApprovalStatus.REJECTED,
    LiveApprovalStatus.CANCELLED,
}


def revalidate_for_send(
    approval: LiveApproval,
    *,
    context: PortfolioContext,
    quote: float | None,
    tradable: bool | None,
    regular_hours_open: bool,
    thesis_status: str | None = None,
    open_orders: list[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
    config: dict | None = None,
    connected: bool = True,
) -> list[str]:
    """Return material-change codes. Empty list means PASS."""
    cfg = (config or load_live_execution_config()).get("revalidation") or {}
    codes: list[str] = []
    stamp = now or datetime.now(timezone.utc)
    if not connected:
        codes.append("BROKER_DISCONNECTED")
    if approval.status in EXPIRED_APPROVAL:
        codes.append("APPROVAL_EXPIRED")
    if approval.expires_at:
        try:
            expires = datetime.fromisoformat(str(approval.expires_at).replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if stamp >= expires:
                codes.append("APPROVAL_EXPIRED")
        except ValueError:
            codes.append("APPROVAL_EXPIRED")
    if cfg.get("require_regular_hours", True) and not regular_hours_open:
        codes.append("MARKET_CLOSED")
    if cfg.get("require_tradable", True) and tradable is False:
        codes.append("SYMBOL_NOT_TRADABLE")
    if quote is None:
        codes.append("QUOTE_UNAVAILABLE")
    approved_quote = approval.quote_at_proposal or approval.current_quote
    max_move = float(cfg.get("max_quote_move_pct") or 0.03)
    if quote is not None and approved_quote:
        if abs(float(quote) - float(approved_quote)) / max(abs(float(approved_quote)), 1e-9) > max_move:
            codes.append("QUOTE_MOVED_MATERIALLY")
    impact = dict(approval.portfolio_impact or {})
    nav_at = _f(approval.nav_at_proposal or impact.get("nav"))
    cash_at = _f(impact.get("cash"))
    bp_at = _f(impact.get("buying_power"))
    nav_now = float(context.current_nav or 0)
    if nav_at and nav_now:
        if abs(nav_now - nav_at) / max(abs(nav_at), 1e-9) > float(cfg.get("max_nav_change_pct") or 0.10):
            codes.append("NAV_CHANGED_MATERIALLY")
    action = str(approval.proposed_action).upper()
    if bp_at is not None and context.buying_power is not None:
        if abs(float(context.buying_power) - bp_at) / max(abs(bp_at), 1e-9) > float(cfg.get("max_buying_power_change_pct") or 0.15):
            codes.append("BUYING_POWER_CHANGED")
        # Selling does not consume buying power. proposed_allocation_pct is a
        # target remaining allocation for REDUCE, not a buy notional.
        if action in {"BUY", "ADD"} and float(context.buying_power) + 1e-6 < _needed_notional(approval, nav_now):
            codes.append("INSUFFICIENT_BUYING_POWER")
    if cash_at is not None and context.cash is not None:
        if abs(float(context.cash) - cash_at) / max(abs(cash_at), 1e-9) > float(cfg.get("max_cash_change_pct") or 0.15):
            if action in {"BUY", "ADD"}:
                codes.append("CASH_CHANGED_MATERIALLY")
    needed = _needed_notional(approval, nav_now)
    if action in {"BUY", "ADD"}:
        if context.cash is not None and needed > float(context.cash) + 1e-6:
            codes.append("INSUFFICIENT_CASH")
        if context.buying_power is not None and needed > float(context.buying_power) + 1e-6:
            codes.append("INSUFFICIENT_BUYING_POWER")
        approved_pct = approval.proposed_allocation_pct
        if approved_pct and nav_now and needed:
            resulting = (needed / nav_now) * 100.0
            if abs(resulting - float(approved_pct)) > float(cfg.get("max_allocation_delta_pct_points") or 1.0):
                codes.append("INTENDED_POSITION_PCT_CHANGED")
    if action in {"SELL", "REDUCE"}:
        held = held_quantity_from_context(context, approval.ticker, quote)
        if held <= 0:
            codes.append("POSITION_CHANGED")
    if thesis_status and str(thesis_status).upper() in {ThesisStatus.INVALIDATED.value, ThesisStatus.REJECTED.value, ThesisStatus.CLOSED.value}:
        codes.append("THESIS_INVALIDATED")
    if cfg.get("reject_duplicate_open_order", True):
        for order in open_orders or list(context.open_orders or []):
            symbol = getattr(order, "symbol", None) or (order.get("symbol") if isinstance(order, Mapping) else None)
            if str(symbol or "").upper() == approval.ticker.upper():
                codes.append("DUPLICATE_OPEN_ORDER")
                break
    risk = str((approval.risk_gate_result or {}).get("verdict") or "").upper()
    if risk in {"HALTED", "FAIL", "RISK_REDUCING_ONLY"} and str(approval.proposed_action).upper() in {"BUY", "ADD"}:
        codes.append("RISK_STATE_BLOCKS")
    if str(getattr(context.risk_state, "value", context.risk_state) or "").upper() in {"HALTED"}:
        if str(approval.proposed_action).upper() in {"BUY", "ADD"}:
            codes.append("RISK_REGIME_CHANGED")
    return list(dict.fromkeys(codes))


def _needed_notional(approval: LiveApproval, nav: float) -> float:
    dollars = _f(approval.proposed_dollar_amount) or 0.0
    pct = _f(approval.proposed_allocation_pct)
    if pct and nav:
        from_pct = nav * (pct / 100.0)
        dollars = min(dollars, from_pct) if dollars else from_pct
    return float(dollars or 0.0)


def _f(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
