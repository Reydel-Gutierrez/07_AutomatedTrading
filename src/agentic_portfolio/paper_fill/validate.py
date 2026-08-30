"""Deterministic paper-fill validation. Fail closed. No investment logic."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agentic_portfolio.execution.types import (
    BUY_ACTIONS,
    EXECUTABLE_ACTIONS,
    NON_EXECUTABLE_ACTIONS,
    SELL_ACTIONS,
    SIDE_FOR_ACTION,
    ExecutionStatus,
    OrderPlan,
    OrderType,
    QuoteSnapshot,
)
from agentic_portfolio.execution.validate import estimated_price, held_position, quote_is_stale
from agentic_portfolio.paper_fill.accounting import is_zero_qty
from agentic_portfolio.paper_fill.types import BookDelta, FillStatus, PaperFill, ReconciliationResult
from agentic_portfolio.schemas import PortfolioContext, ReconciliationFinding


class PaperFillValidationError(ValueError):
    """Malformed fill or inconsistent paper book. Engine must fail closed."""


def _close(a: float | None, b: float | None, *, abs_tol: float, rel_tol: float) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b), 1.0))


def resolve_fill_price(
    plan: OrderPlan,
    quote: QuoteSnapshot | None,
    *,
    now: datetime,
    config: dict[str, Any],
) -> tuple[float | None, list[str]]:
    codes: list[str] = []
    max_age = float(config.get("quote_max_age_seconds") or 300)
    if quote is not None:
        stale, stale_code = quote_is_stale(quote, now=now, max_age_seconds=max_age)
        if stale and stale_code:
            codes.append(stale_code)
        else:
            price = estimated_price(quote)
            if price is not None and price > 0:
                return price, codes
            codes.append("MISSING_PRICE")
            return None, codes
    if plan.estimated_price is not None and plan.estimated_price > 0:
        return float(plan.estimated_price), codes
    codes.append("MISSING_PRICE")
    return None, codes


def pretrade_codes(
    plan: OrderPlan,
    ctx: PortfolioContext,
    *,
    filled_ids: set[str],
    quantity: float | None,
    fill_price: float | None,
    config: dict[str, Any],
) -> list[str]:
    codes: list[str] = []
    if plan.order_plan_id in filled_ids:
        codes.append("DUPLICATE_FILL")
    if plan.execution_status != ExecutionStatus.PAPER_ONLY:
        codes.append("NOT_PAPER_ONLY")
    if plan.action in NON_EXECUTABLE_ACTIONS:
        codes.append("NON_EXECUTABLE_ACTION")
    elif plan.action not in EXECUTABLE_ACTIONS:
        codes.append("UNKNOWN_ACTION")
    if plan.stop_orders_created:
        codes.append("STOP_ORDER_NOT_ALLOWED")
    if plan.broker_submitted:
        codes.append("MALFORMED_ORDER_PLAN")
    if plan.live_trade_actions_allowed or plan.auto_execution:
        codes.append("MALFORMED_ORDER_PLAN")
    if not plan.live_execution_blocked:
        codes.append("MALFORMED_ORDER_PLAN")
    if plan.order_type != OrderType.MARKET:
        codes.append("UNSUPPORTED_ORDER_TYPE")
    expected_side = SIDE_FOR_ACTION.get(plan.action)
    if expected_side is not None and plan.order_side != expected_side:
        codes.append("SIDE_ACTION_MISMATCH")
    if quantity is None or quantity <= 0:
        codes.append("MISSING_QUANTITY")
    if fill_price is None or fill_price <= 0:
        if "MISSING_PRICE" not in codes:
            codes.append("MISSING_PRICE")
    if plan.action in SELL_ACTIONS and quantity is not None:
        held = held_position(ctx, plan.symbol)
        held_qty = float(held.quantity or 0.0) if held else 0.0
        if held is None or held_qty <= 0 or quantity > held_qty + float(config.get("quantity_zero_epsilon") or 1e-9):
            codes.append("SELL_CREATES_NEGATIVE_POSITION")
    if plan.action in BUY_ACTIONS and quantity is not None and fill_price is not None:
        if quantity * fill_price > ctx.cash + float(config.get("quantity_notional_abs_tolerance") or 0.01):
            codes.append("BUY_EXCEEDS_AVAILABLE_CASH")
    if ctx.current_nav <= 0:
        codes.append("MALFORMED_ORDER_PLAN")
    return codes


def skip_fill_reason(plan: OrderPlan) -> str | None:
    if plan.action in NON_EXECUTABLE_ACTIONS:
        return "NON_EXECUTABLE_ACTION"
    if plan.execution_status != ExecutionStatus.PAPER_ONLY:
        return "BLOCKED_FROM_LIVE" if plan.execution_status == ExecutionStatus.BLOCKED_FROM_LIVE else "NOT_PAPER_ONLY"
    if plan.action not in EXECUTABLE_ACTIONS:
        return "UNKNOWN_ACTION"
    return None


def reconcile_step(
    plan: OrderPlan,
    fill: PaperFill,
    before: PortfolioContext,
    after: PortfolioContext,
    delta: BookDelta | None,
    *,
    filled_ids: set[str],
    config: dict[str, Any],
) -> ReconciliationResult:
    abs_tol = float(config.get("quantity_notional_abs_tolerance") or 0.01)
    rel_tol = float(config.get("quantity_notional_rel_tolerance") or 1e-6)
    nav_tol = float(config.get("nav_abs_tolerance") or 0.01)
    eps = float(config.get("quantity_zero_epsilon") or 1e-9)
    findings: list[ReconciliationFinding] = []
    checks = {
        "fill_matches_order_plan": True,
        "resulting_quantity_correct": True,
        "cash_movement_correct": True,
        "no_negative_position": True,
        "position_closed_at_zero": True,
        "realized_pnl_consistent": True,
        "nav_accounting_consistent": True,
        "no_duplicate_fill": True,
        "thesis_sleeve_links_intact": True,
    }

    def fail(check: str, code: str, message: str) -> None:
        checks[check] = False
        findings.append(ReconciliationFinding(code=code, symbol=plan.symbol, message=message))

    if fill.order_plan_id != plan.order_plan_id or fill.symbol != plan.symbol:
        fail("fill_matches_order_plan", "FILL_PLAN_MISMATCH", "fill identity does not match OrderPlan")
    if fill.side != plan.order_side:
        fail("fill_matches_order_plan", "FILL_PLAN_MISMATCH", "fill side does not match OrderPlan")
    if fill.status == FillStatus.FILLED:
        if not _close(fill.quantity, plan.quantity, abs_tol=abs_tol, rel_tol=rel_tol):
            fail("fill_matches_order_plan", "FILL_PLAN_MISMATCH", "fill quantity does not match OrderPlan")
        if fill.quantity is not None and fill.fill_price is not None and fill.filled_notional is not None:
            if not _close(fill.quantity * fill.fill_price, fill.filled_notional, abs_tol=abs_tol, rel_tol=rel_tol):
                fail("fill_matches_order_plan", "QUANTITY_NOTIONAL_MISMATCH", "filled notional != qty * price")

    if plan.order_plan_id in filled_ids and fill.status == FillStatus.FILLED:
        fail("no_duplicate_fill", "DUPLICATE_FILL", "OrderPlan already has a paper fill")

    if delta is None:
        if fill.status == FillStatus.FILLED:
            fail("fill_matches_order_plan", "INCONSISTENT_FILL", "FILLED fill is missing a book delta")
        return ReconciliationResult(ok=not findings, findings=findings, checks=checks)

    expected_qty = delta.quantity_before + delta.quantity if plan.action in BUY_ACTIONS else delta.quantity_before - delta.quantity
    if expected_qty < -eps:
        fail("no_negative_position", "SELL_CREATES_NEGATIVE_POSITION", "sell would create a negative position")
        expected_qty = 0.0
    if is_zero_qty(expected_qty, eps=eps):
        expected_qty = 0.0
    if not _close(delta.quantity_after, expected_qty, abs_tol=abs_tol, rel_tol=rel_tol):
        fail("resulting_quantity_correct", "QUANTITY_MISMATCH", "resulting quantity is not plan-consistent")

    held_after = held_position(after, plan.symbol)
    actual_qty = float(held_after.quantity or 0.0) if held_after else 0.0
    if not _close(actual_qty, delta.quantity_after, abs_tol=abs_tol, rel_tol=rel_tol):
        fail("resulting_quantity_correct", "QUANTITY_MISMATCH", "book quantity does not match delta")

    if actual_qty < -eps:
        fail("no_negative_position", "SELL_CREATES_NEGATIVE_POSITION", "book has a negative position")

    expected_closed = is_zero_qty(delta.quantity_after, eps=eps) and plan.action in SELL_ACTIONS
    if expected_closed:
        if held_after is not None and not is_zero_qty(float(held_after.quantity or 0.0), eps=eps):
            fail("position_closed_at_zero", "POSITION_NOT_CLOSED", "SELL/REDUCE left a residual position")
        if not delta.position_closed:
            fail("position_closed_at_zero", "POSITION_NOT_CLOSED", "zero quantity was not closed")
    if held_after is not None and is_zero_qty(float(held_after.quantity or 0.0), eps=eps):
        fail("position_closed_at_zero", "ZERO_POSITION_NOT_REMOVED", "zero-quantity position remained on the book")

    expected_cash = delta.cash_before - delta.filled_notional if plan.action in BUY_ACTIONS else delta.cash_before + delta.filled_notional
    if not _close(delta.cash_after, expected_cash, abs_tol=abs_tol, rel_tol=rel_tol):
        fail("cash_movement_correct", "CASH_MISMATCH", "cash movement does not match fill notional")
    if not _close(after.cash, delta.cash_after, abs_tol=abs_tol, rel_tol=rel_tol):
        fail("cash_movement_correct", "CASH_MISMATCH", "book cash does not match delta")
    if after.cash < -abs_tol:
        fail("cash_movement_correct", "NEGATIVE_CASH", "paper cash went negative")

    if plan.action in SELL_ACTIONS and delta.average_cost_before is not None:
        expected_realized = (delta.fill_price - delta.average_cost_before) * delta.quantity
        if delta.position_closed or _close(delta.average_cost_after, delta.average_cost_before, abs_tol=abs_tol, rel_tol=rel_tol):
            if not _close(delta.realized_pnl, expected_realized, abs_tol=abs_tol, rel_tol=rel_tol):
                fail("realized_pnl_consistent", "REALIZED_PNL_MISMATCH", "realized P&L is not (fill - average cost) * qty")
        prior = float(before.realized_pnl or 0.0)
        after_r = float(after.realized_pnl or 0.0)
        if not _close(after_r, prior + delta.realized_pnl, abs_tol=abs_tol, rel_tol=rel_tol):
            fail("realized_pnl_consistent", "REALIZED_PNL_MISMATCH", "portfolio realized P&L did not accrue the fill")
    if plan.action in BUY_ACTIONS and abs(delta.realized_pnl) > abs_tol:
        fail("realized_pnl_consistent", "REALIZED_PNL_MISMATCH", "BUY/ADD must not realize P&L")

    mv = sum(p.market_value for p in after.positions)
    if not _close(after.current_nav, after.cash + mv, abs_tol=nav_tol, rel_tol=rel_tol):
        fail("nav_accounting_consistent", "NAV_MISMATCH", "NAV != cash + position market value")

    if fill.thesis_id != plan.thesis_id:
        fail("thesis_sleeve_links_intact", "THESIS_LINK_BROKEN", "fill thesis_id does not match OrderPlan")
    held_before = held_position(before, plan.symbol)
    if held_after is not None and held_before is not None:
        if held_before.thesis_id and held_after.thesis_id != held_before.thesis_id:
            fail("thesis_sleeve_links_intact", "THESIS_LINK_BROKEN", "position thesis_id changed")
        if held_before.sleeve and held_after.sleeve != held_before.sleeve:
            fail("thesis_sleeve_links_intact", "SLEEVE_LINK_BROKEN", "position sleeve changed")

    return ReconciliationResult(ok=not findings, findings=findings, checks=checks)


def merge_reconciliation(parts: list[ReconciliationResult]) -> ReconciliationResult:
    if not parts:
        return ReconciliationResult(ok=True)
    checks: dict[str, bool] = {}
    findings: list[ReconciliationFinding] = []
    for part in parts:
        findings.extend(part.findings)
        for key, ok in part.checks.items():
            checks[key] = checks.get(key, True) and ok
    return ReconciliationResult(ok=all(p.ok for p in parts) and not findings, findings=findings, checks=checks)
