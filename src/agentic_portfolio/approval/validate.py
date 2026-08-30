"""Deterministic eligibility and expiry for Human Approval Packets."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agentic_portfolio.approval.types import (
    OPEN_STATUSES,
    ApprovalMarketView,
    ApprovalPacket,
    ApprovalStatus,
    StatusEvent,
)
from agentic_portfolio.execution.types import EXECUTABLE_ACTIONS, ExecutionStatus, OrderPlan
from agentic_portfolio.execution.validate import held_market_value, held_quantity, quote_is_stale
from agentic_portfolio.research.types import ResearchFreshness, ResearchStatus
from agentic_portfolio.schemas import GateVerdict, PortfolioContext, ProposedAction, RiskGateResult

STALE_RESEARCH = {
    ResearchFreshness.STALE.value,
    ResearchStatus.RESEARCH_STALE.value,
    "EXPIRED",
}


class ApprovalValidationError(ValueError):
    """Malformed approval packet. Fail closed."""


def skip_reason(plan: OrderPlan, action: ProposedAction, risk: RiskGateResult) -> str | None:
    if action.decision not in EXECUTABLE_ACTIONS or plan.action not in EXECUTABLE_ACTIONS:
        return "NON_EXECUTABLE_ACTION"
    if plan.execution_status != ExecutionStatus.PAPER_ONLY:
        return "NOT_PAPER_ONLY"
    if plan.blocked_reasons:
        return "NOT_PAPER_ONLY"
    if not risk.recommendation_permitted or risk.verdict == GateVerdict.FAIL:
        return "RISK_GATE_NOT_PERMITTED"
    if plan.symbol.upper() != action.symbol.upper():
        return "PLAN_ACTION_MISMATCH"
    if plan.action != action.decision:
        return "PLAN_ACTION_MISMATCH"
    return None


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _close(a: float | None, b: float | None, *, abs_tol: float, rel_tol: float) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b), 1.0))


def current_position_pct(context: PortfolioContext, symbol: str) -> float:
    if not context.current_nav:
        return 0.0
    return held_market_value(context, symbol) / context.current_nav


def expiry_codes(
    packet: ApprovalPacket,
    view: ApprovalMarketView,
    *,
    now: datetime,
    config: dict[str, Any],
) -> tuple[list[str], str | None]:
    """Return (codes, superseded_by). Newer decisions supersede; other drift expires."""
    if packet.status not in OPEN_STATUSES:
        return [], None
    codes: list[str] = []
    superseded_by: str | None = None
    material = config.get("material_change") or {}
    max_age = float(config.get("quote_max_age_seconds") or 300)

    quote = view.quote
    if quote is None:
        snap_ts = _parse_ts(packet.snapshot.quote_observed_at)
        if snap_ts is None or (now - snap_ts).total_seconds() > max_age:
            codes.append("STALE_QUOTE")
    else:
        stale, _reason = quote_is_stale(quote, now=now, max_age_seconds=max_age)
        if stale:
            codes.append("STALE_QUOTE")

    research = view.research
    if research is not None:
        freshness = str(getattr(research.freshness, "value", research.freshness) or "")
        status = str(getattr(research.research_status, "value", research.research_status) or "")
        snap_research = packet.snapshot.research_id or packet.evidence_refs.research_id
        if snap_research and research.research_id != snap_research:
            codes.append("STALE_RESEARCH")
        elif freshness in STALE_RESEARCH or status in STALE_RESEARCH:
            if (packet.snapshot.research_freshness or "") not in STALE_RESEARCH:
                codes.append("STALE_RESEARCH")
        stale_after = _parse_ts(getattr(research, "stale_after", None))
        if stale_after is not None and now > stale_after:
            codes.append("STALE_RESEARCH")

    thesis = view.thesis
    if thesis is not None:
        snap_id = packet.snapshot.thesis_id or packet.evidence_refs.thesis_id
        if snap_id and thesis.thesis_id != snap_id:
            codes.append("STALE_THESIS")
        else:
            snap_updated = _parse_ts(packet.snapshot.thesis_updated_at)
            current_updated = _parse_ts(thesis.updated_at)
            if snap_updated and current_updated and current_updated > snap_updated:
                codes.append("STALE_THESIS")

    ctx = view.context
    snap = packet.snapshot
    if ctx is not None and snap.nav is not None:
        nav_lim = float(material.get("nav_change_pct") or 0.02)
        if snap.nav and abs(ctx.current_nav - snap.nav) / max(abs(snap.nav), 1e-9) > nav_lim:
            codes.append("PORTFOLIO_MATERIAL_CHANGE")
        cash_lim = float(material.get("cash_pct_change") or 0.02)
        if snap.cash_allocation_pct is not None and abs(ctx.cash_allocation_pct - snap.cash_allocation_pct) > cash_lim:
            codes.append("PORTFOLIO_MATERIAL_CHANGE")
        count_lim = int(material.get("holdings_count_change") or 1)
        if snap.holdings_count is not None and abs(ctx.holdings_count - snap.holdings_count) >= count_lim:
            codes.append("PORTFOLIO_MATERIAL_CHANGE")
        pos_lim = float(material.get("position_pct_change") or 0.005)
        qty_rel = float(material.get("quantity_rel_tolerance") or 0.01)
        now_pct = current_position_pct(ctx, packet.symbol)
        if snap.position_pct is not None and abs(now_pct - snap.position_pct) > pos_lim:
            codes.append("PORTFOLIO_MATERIAL_CHANGE")
        now_qty = held_quantity(ctx, packet.symbol)
        if snap.position_quantity is not None and not _close(
            now_qty, snap.position_quantity, abs_tol=1e-6, rel_tol=qty_rel
        ):
            codes.append("PORTFOLIO_MATERIAL_CHANGE")
        if snap.risk_state is not None and ctx.risk_state.value != snap.risk_state:
            codes.append("RISK_STATE_CHANGED")
        if snap.daily_risk_halt is not None and bool(ctx.daily_risk_halt) != bool(snap.daily_risk_halt):
            codes.append("RISK_STATE_CHANGED")

    newer = view.newer_decision_id
    if newer and snap.source_decision_id and str(newer) != str(snap.source_decision_id):
        superseded_by = str(newer)
        codes.append("SUPERSEDED_BY_NEWER_DECISION")

    seen: set[str] = set()
    unique: list[str] = []
    for code in codes:
        if code not in seen:
            seen.add(code)
            unique.append(code)
    return unique, superseded_by


def apply_freshness(
    packet: ApprovalPacket,
    view: ApprovalMarketView,
    *,
    now: datetime,
    config: dict[str, Any],
) -> ApprovalPacket:
    codes, superseded_by = expiry_codes(packet, view, now=now, config=config)
    if not codes or packet.status not in OPEN_STATUSES:
        return packet
    ts = now.isoformat()
    if "SUPERSEDED_BY_NEWER_DECISION" in codes:
        packet.status = ApprovalStatus.SUPERSEDED
        packet.superseded_by = superseded_by
        packet.expiry_reasons = list(codes)
        packet.status_history.append(StatusEvent(status=ApprovalStatus.SUPERSEDED, at=ts, reason=";".join(codes)))
        return packet
    packet.status = ApprovalStatus.EXPIRED
    packet.expiry_reasons = list(codes)
    packet.status_history.append(StatusEvent(status=ApprovalStatus.EXPIRED, at=ts, reason=";".join(codes)))
    return packet


def can_record_human_decision(packet: ApprovalPacket, status: ApprovalStatus) -> str | None:
    if status not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
        return "INVALID_HUMAN_DECISION"
    if packet.status != ApprovalStatus.PENDING_HUMAN_APPROVAL:
        return "NOT_PENDING"
    return None
