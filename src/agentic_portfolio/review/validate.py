"""Fail-closed eligibility for Robinhood review-only. Never places."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agentic_portfolio.approval.types import ApprovalMarketView, ApprovalPacket, ApprovalStatus
from agentic_portfolio.approval.validate import expiry_codes
from agentic_portfolio.execution.types import BUY_ACTIONS, EXECUTABLE_ACTIONS, OrderPlan, OrderSide, QuoteSnapshot
from agentic_portfolio.execution.validate import estimated_price, risk_permits
from agentic_portfolio.review.types import EXPIRED_CODES, ReviewRequest, ReviewStatus
from agentic_portfolio.schemas import ProposedAction, RiskGateResult


class ReviewValidationError(ValueError):
    """Malformed review request. Fail closed."""


def _close(a: float | None, b: float | None, *, abs_tol: float, rel_tol: float) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b), 1.0))


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").replace("$", "").strip())
        except ValueError:
            return None
    return None


def quantity_str(quantity: float, *, decimals: int = 6) -> str:
    text = f"{quantity:.{decimals}f}".rstrip("0").rstrip(".")
    return text if text else "0"


def robinhood_side(plan: OrderPlan) -> str:
    if plan.order_side == OrderSide.BUY or plan.action in BUY_ACTIONS:
        return "buy"
    return "sell"


def build_review_payload(
    plan: OrderPlan,
    account_number: str,
    *,
    market_hours: str = "regular_hours",
    decimals: int = 6,
) -> dict[str, Any]:
    order_type = plan.order_type.value if plan.order_type else "market"
    payload: dict[str, Any] = {
        "account_number": str(account_number),
        "symbol": plan.symbol.upper(),
        "side": robinhood_side(plan),
        "type": order_type,
        "time_in_force": plan.time_in_force.value if plan.time_in_force else "gfd",
        "market_hours": market_hours,
    }
    if plan.quantity is not None and plan.quantity > 0:
        payload["quantity"] = quantity_str(float(plan.quantity), decimals=decimals)
    elif plan.notional is not None and plan.notional > 0 and order_type == "market":
        payload["dollar_amount"] = f"{float(plan.notional):.2f}"
    if order_type in {"limit", "stop_limit"} and plan.estimated_price:
        payload["limit_price"] = f"{float(plan.estimated_price):.2f}"
    return payload


def approval_block_reason(packet: ApprovalPacket) -> tuple[str | None, ReviewStatus | None]:
    if packet.status == ApprovalStatus.APPROVED:
        return None, None
    if packet.status == ApprovalStatus.EXPIRED:
        return "APPROVAL_EXPIRED", ReviewStatus.REVIEW_EXPIRED
    if packet.status == ApprovalStatus.SUPERSEDED:
        return "APPROVAL_SUPERSEDED", ReviewStatus.REVIEW_EXPIRED
    if packet.status == ApprovalStatus.REJECTED:
        return "APPROVAL_REJECTED", ReviewStatus.REVIEW_FAILED
    return "APPROVAL_NOT_APPROVED", ReviewStatus.REVIEW_FAILED


def local_fail_codes(
    req: ReviewRequest,
    *,
    now: datetime,
    config: dict[str, Any],
    risk: RiskGateResult,
) -> tuple[list[str], ReviewStatus | None]:
    """Return (codes, status). status is None when local checks pass."""
    packet = req.packet
    plan = req.plan
    action = req.action
    reason, status = approval_block_reason(packet)
    if reason:
        return [reason], status

    codes: list[str] = []
    if plan.symbol.upper() != packet.symbol.upper() or action.symbol.upper() != packet.symbol.upper():
        codes.append("PLAN_PACKET_MISMATCH")
    if plan.action not in EXECUTABLE_ACTIONS or action.decision not in EXECUTABLE_ACTIONS:
        codes.append("NON_EXECUTABLE_ACTION")
    if plan.action != packet.action or action.decision != packet.action:
        codes.append("PLAN_PACKET_MISMATCH")
    if plan.order_plan_id != (packet.evidence_refs.order_plan_id or packet.order_plan_summary.order_plan_id):
        codes.append("PLAN_PACKET_MISMATCH")
    if packet.broker_submitted or plan.broker_submitted:
        codes.append("ALREADY_SUBMITTED")

    view = ApprovalMarketView(
        quote=req.quote,
        context=req.context,
        research=req.research,
        thesis=req.thesis,
        newer_decision_id=req.newer_decision_id,
    )
    drift, _superseded = expiry_codes(packet, view, now=now, config=config)
    codes.extend(drift)
    codes.extend(_quote_material_codes(packet, req.quote, config))

    ok, risk_code = risk_permits(action, risk)
    if not ok and risk_code:
        codes.append(risk_code)

    seen: set[str] = set()
    unique: list[str] = []
    for code in codes:
        if code not in seen:
            seen.add(code)
            unique.append(code)
    if not unique:
        return [], None
    if any(code in EXPIRED_CODES for code in unique) and not any(
        code in {"PLAN_PACKET_MISMATCH", "ALREADY_SUBMITTED", "RISK_GATE_NOT_PERMITTED", "RISK_REDUCING_ONLY_BLOCKS_BUY"}
        for code in unique
    ):
        return unique, ReviewStatus.REVIEW_EXPIRED
    return unique, ReviewStatus.REVIEW_FAILED


def _quote_material_codes(packet: ApprovalPacket, quote: QuoteSnapshot | None, config: dict[str, Any]) -> list[str]:
    if quote is None:
        return []
    price = estimated_price(quote)
    frozen = packet.current_price or packet.order_plan_summary.estimated_price
    if price is None or frozen is None or frozen <= 0:
        return []
    lim = float((config.get("material_change") or {}).get("quote_change_pct") or 0.01)
    if abs(price - float(frozen)) / float(frozen) > lim:
        return ["QUOTE_MATERIAL_CHANGE"]
    return []


def plan_vs_review_codes(
    plan: OrderPlan,
    payload: dict[str, Any],
    parsed: dict[str, Any],
    *,
    config: dict[str, Any],
) -> list[str]:
    """Fail closed when the broker review disagrees with the approved OrderPlan."""
    vs = config.get("review_vs_plan") or {}
    qty_rel = float(vs.get("quantity_rel_tolerance") or 0.01)
    notional_rel = float(vs.get("notional_rel_tolerance") or 0.02)
    price_rel = float(vs.get("price_rel_tolerance") or 0.02)
    codes: list[str] = []
    rh_symbol = parsed.get("symbol") or payload.get("symbol")
    if rh_symbol and str(rh_symbol).upper() != plan.symbol.upper():
        codes.append("REVIEW_DIFFERS_FROM_ORDER_PLAN")
    rh_side = str(parsed.get("side") or payload.get("side") or "").lower()
    expected_side = robinhood_side(plan)
    if rh_side and rh_side not in {expected_side, "sell_to_close" if expected_side == "sell" else expected_side}:
        if rh_side != expected_side and not (expected_side == "sell" and rh_side in {"sell", "sell_to_close"}):
            codes.append("REVIEW_DIFFERS_FROM_ORDER_PLAN")
    rh_type = str(parsed.get("order_type") or parsed.get("type") or payload.get("type") or "").lower()
    expected_type = (plan.order_type.value if plan.order_type else "market").lower()
    if rh_type and rh_type != expected_type:
        codes.append("REVIEW_DIFFERS_FROM_ORDER_PLAN")
    rh_qty = _as_float(parsed.get("quantity"))
    if rh_qty is not None and plan.quantity is not None:
        if not _close(rh_qty, float(plan.quantity), abs_tol=1e-6, rel_tol=qty_rel):
            codes.append("REVIEW_DIFFERS_FROM_ORDER_PLAN")
    est = parsed.get("estimated_cost")
    if est is None:
        est = parsed.get("estimated_proceeds")
    if est is not None and plan.notional is not None:
        if not _close(float(est), float(plan.notional), abs_tol=0.01, rel_tol=notional_rel):
            codes.append("REVIEW_DIFFERS_FROM_ORDER_PLAN")
    rh_limit = _as_float(parsed.get("limit_price"))
    if rh_limit is not None and plan.estimated_price and expected_type in {"limit", "stop_limit"}:
        if not _close(rh_limit, float(plan.estimated_price), abs_tol=0.01, rel_tol=price_rel):
            codes.append("REVIEW_DIFFERS_FROM_ORDER_PLAN")
    seen: set[str] = set()
    unique: list[str] = []
    for code in codes:
        if code not in seen:
            seen.add(code)
            unique.append(code)
    return unique


def parse_review_response(raw: Any) -> dict[str, Any]:
    """Normalize an unknown Robinhood review payload into comparable fields."""
    data = raw if isinstance(raw, dict) else {"raw": raw}
    nested = data.get("data") if isinstance(data.get("data"), dict) else data
    order = nested.get("order") if isinstance(nested.get("order"), dict) else {}
    quote_data = nested.get("quote_data") if isinstance(nested.get("quote_data"), dict) else {}
    warnings = _as_messages(nested.get("warnings") or nested.get("alerts") or nested.get("notices") or order.get("warnings"))
    errors = _as_messages(nested.get("errors") or nested.get("error") or order.get("errors") or nested.get("blocking_alerts"))
    if nested.get("rejected") is True or order.get("rejected") is True:
        errors.append("rejected")
    status = str(nested.get("status") or order.get("status") or data.get("status") or "").lower()
    if status in {"rejected", "failed", "blocked", "error"}:
        errors.append(status)
    estimated_cost = _as_float(
        nested.get("estimated_cost")
        or nested.get("estimated_total")
        or nested.get("cost")
        or order.get("estimated_cost")
    )
    estimated_proceeds = _as_float(
        nested.get("estimated_proceeds") or nested.get("proceeds") or order.get("estimated_proceeds")
    )
    quantity = _as_float(order.get("quantity") or nested.get("quantity"))
    last = _as_float(quote_data.get("last_trade_price") or quote_data.get("last_non_reg_trade_price"))
    side = order.get("side") or nested.get("side")
    if estimated_cost is None and estimated_proceeds is None and last is not None and quantity:
        total = round(last * quantity, 2)
        if str(side or "").lower() == "buy":
            estimated_cost = total
        else:
            estimated_proceeds = total
    limit_price = _as_float(order.get("limit_price") or nested.get("limit_price"))
    return {
        "symbol": order.get("symbol") or nested.get("symbol"),
        "side": order.get("side") or nested.get("side"),
        "order_type": order.get("type") or nested.get("type"),
        "quantity": quantity,
        "limit_price": limit_price,
        "estimated_cost": estimated_cost,
        "estimated_proceeds": estimated_proceeds,
        "warnings": warnings,
        "errors": errors,
        "status": status or None,
    }


def _as_messages(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        text = value.get("message") or value.get("detail") or value.get("code") or str(value)
        return [str(text)]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_as_messages(item))
        return out
    return [str(value)]


def status_from_parsed(parsed: dict[str, Any], mismatch: list[str]) -> ReviewStatus:
    if parsed.get("errors"):
        return ReviewStatus.REVIEW_REJECTED
    if mismatch:
        return ReviewStatus.REVIEW_FAILED
    if parsed.get("warnings"):
        return ReviewStatus.REVIEW_READY
    return ReviewStatus.REVIEW_ACCEPTED
