"""Human Approval Packet types. Packaging only; never places a live order."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agentic_portfolio.decision.types import NameDecision, PortfolioComparison
from agentic_portfolio.execution.types import ExecutionStatus, OrderPlan, QuoteSnapshot
from agentic_portfolio.monitoring.types import MonitoredPosition
from agentic_portfolio.research.types import ResearchReport
from agentic_portfolio.schemas import (
    BreachKind,
    ClassificationStatus,
    Decision,
    GateReason,
    GateVerdict,
    LiquidityInputs,
    PortfolioContext,
    PositionRegistryStatus,
    ProposedAction,
    RiskGateResult,
    SecurityClass,
    Sleeve,
    ThesisRecord,
)


class ApprovalStatus(str, Enum):
    PENDING_HUMAN_APPROVAL = "PENDING_HUMAN_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


OPEN_STATUSES = {ApprovalStatus.PENDING_HUMAN_APPROVAL, ApprovalStatus.APPROVED}
TERMINAL_STATUSES = {ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED, ApprovalStatus.SUPERSEDED}
HUMAN_DECISIONS = {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}


@dataclass
class StatusEvent:
    status: ApprovalStatus
    at: str
    reason: str | None = None
    note: str | None = None


@dataclass
class OrderPlanSummary:
    order_plan_id: str
    execution_status: str
    side: str | None
    order_type: str | None
    time_in_force: str | None
    quantity: float | None
    notional: float | None
    estimated_price: float | None
    estimated_position_pct_after: float | None
    blocked_reasons: list[str] = field(default_factory=list)
    live_execution_blocked: bool = True
    stop_orders_created: int = 0
    broker_submitted: bool = False


@dataclass
class EvidenceRefs:
    order_plan_id: str | None = None
    source_decision_id: str | None = None
    thesis_id: str | None = None
    research_id: str | None = None
    risk_evaluation_id: str | None = None
    monitoring_run_id: str | None = None
    supporting_evidence_refs: list[str] = field(default_factory=list)


@dataclass
class ApprovalSnapshot:
    """Frozen book/quote/risk identity used later for expiry."""

    nav: float | None = None
    cash: float | None = None
    cash_allocation_pct: float | None = None
    holdings_count: int | None = None
    position_pct: float | None = None
    position_quantity: float | None = None
    risk_state: str | None = None
    daily_risk_halt: bool | None = None
    quote_observed_at: str | None = None
    research_id: str | None = None
    research_freshness: str | None = None
    thesis_id: str | None = None
    thesis_updated_at: str | None = None
    source_decision_id: str | None = None


@dataclass
class ApprovalPacket:
    approval_id: str
    symbol: str
    action: Decision
    desired_allocation_pct: float | None
    current_allocation_pct: float | None
    order_notional: float | None
    order_quantity: float | None
    current_price: float | None
    sleeve: Sleeve | None
    thesis_summary: str | None
    why_now: str | None
    why_not_cash: str | None
    why_not_spy: str | None
    bull_case: str | None
    base_case: str | None
    bear_case: str | None
    key_risks: list[str]
    invalidation_exit_policy: str | None
    expected_horizon: str | None
    portfolio_effect: str | None
    sector_concentration_effect: str | None
    risk_gate_verdict: str | None
    enhanced_review_requirements: list[str]
    order_plan_summary: OrderPlanSummary
    evidence_refs: EvidenceRefs
    created_at: str
    status: ApprovalStatus
    snapshot: ApprovalSnapshot = field(default_factory=ApprovalSnapshot)
    status_history: list[StatusEvent] = field(default_factory=list)
    expiry_reasons: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    human_note: str | None = None
    decided_at: str | None = None
    monitoring_state: str | None = None
    sector: str | None = None
    live_execution_blocked: bool = True
    broker_submitted: bool = False
    live_trade_actions_allowed: bool = False
    auto_execution: bool = False
    approved_does_not_place_order: bool = True
    stop_orders_created: int = 0


@dataclass
class SkippedApproval:
    symbol: str
    action: Decision
    reason: str
    order_plan_id: str | None = None


@dataclass
class ApprovalResult:
    run_id: str
    packets: list[ApprovalPacket] = field(default_factory=list)
    skipped: list[SkippedApproval] = field(default_factory=list)
    superseded: list[ApprovalPacket] = field(default_factory=list)
    context: PortfolioContext | None = None
    execution_attempted: bool = False
    broker_orders_submitted: int = 0
    broker_stop_orders_created: int = 0
    live_execution_attempted: bool = False
    live_trade_actions_allowed: bool = False
    auto_execution: bool = False
    validation_errors: list[str] = field(default_factory=list)

    @property
    def pending(self) -> list[ApprovalPacket]:
        return [p for p in self.packets if p.status == ApprovalStatus.PENDING_HUMAN_APPROVAL]


@dataclass
class ApprovalRequest:
    plan: OrderPlan
    action: ProposedAction
    risk: RiskGateResult
    context: PortfolioContext
    thesis: ThesisRecord | None = None
    decision: NameDecision | None = None
    comparison: PortfolioComparison | None = None
    report: ResearchReport | None = None
    monitoring: MonitoredPosition | dict[str, Any] | None = None
    quote: QuoteSnapshot | None = None
    monitoring_run_id: str | None = None


@dataclass
class ApprovalMarketView:
    """Current facts used to expire or supersede an existing packet."""

    quote: QuoteSnapshot | None = None
    context: PortfolioContext | None = None
    research: ResearchReport | None = None
    thesis: ThesisRecord | None = None
    newer_decision_id: str | None = None


def proposed_action_from_dict(raw: dict[str, Any]) -> ProposedAction:
    liq = raw.get("liquidity") or {}
    sleeve = raw.get("sleeve")
    sector = raw.get("sector")
    registry = raw.get("position_registry_status")
    return ProposedAction(
        symbol=str(raw["symbol"]).upper(),
        decision=Decision(raw["decision"]),
        security_class=SecurityClass(raw["security_class"]),
        classification_status=ClassificationStatus(raw.get("classification_status") or "VALIDATED"),
        sleeve=Sleeve(sleeve) if sleeve else None,
        current_price=raw.get("current_price"),
        proposed_notional=raw.get("proposed_notional"),
        expected_resulting_position_pct=raw.get("expected_resulting_position_pct"),
        expected_resulting_sleeve_pct=raw.get("expected_resulting_sleeve_pct"),
        expected_resulting_sector_pct=raw.get("expected_resulting_sector_pct"),
        sector=sector,
        thesis_id=raw.get("thesis_id"),
        investment_thesis_review_complete=bool(raw.get("investment_thesis_review_complete")),
        risk_review_complete=bool(raw.get("risk_review_complete")),
        enhanced_concentration_review_complete=bool(raw.get("enhanced_concentration_review_complete")),
        high_concentration_review_complete=bool(raw.get("high_concentration_review_complete")),
        sector_concentration_review_complete=bool(raw.get("sector_concentration_review_complete")),
        speculative_liquidity_review_complete=bool(raw.get("speculative_liquidity_review_complete")),
        opportunistic_enhanced_risk_review_complete=bool(raw.get("opportunistic_enhanced_risk_review_complete")),
        add_justified_only_by_lower_price=bool(raw.get("add_justified_only_by_lower_price")),
        explicitly_risk_reducing=bool(raw.get("explicitly_risk_reducing")),
        human_authorized_halted_execution=bool(raw.get("human_authorized_halted_execution")),
        liquidity=LiquidityInputs(
            median_daily_dollar_volume_20d=liq.get("median_daily_dollar_volume_20d"),
            recent_dollar_volume=liq.get("recent_dollar_volume"),
            bid_ask_spread_pct=liq.get("bid_ask_spread_pct"),
        ),
        position_registry_status=PositionRegistryStatus(registry) if registry else None,
        sleeve_reclassification_pending=bool(raw.get("sleeve_reclassification_pending")),
    )


def risk_from_dict(raw: dict[str, Any]) -> RiskGateResult:
    reasons: list[GateReason] = []
    for item in raw.get("reasons") or []:
        kind = item.get("kind") or "NONE"
        reasons.append(
            GateReason(
                code=str(item.get("code") or "UNKNOWN"),
                message=str(item.get("message") or ""),
                kind=BreachKind(kind) if kind in BreachKind._value2member_map_ else BreachKind.NONE,
            )
        )
    return RiskGateResult(
        verdict=GateVerdict(raw["verdict"]),
        execution_permitted=bool(raw.get("execution_permitted", False)),
        recommendation_permitted=bool(raw.get("recommendation_permitted", False)),
        reasons=reasons,
        required_reviews=list(raw.get("required_reviews") or []),
        applicable_position_ceiling_pct=raw.get("applicable_position_ceiling_pct"),
        snapshot_id=raw.get("snapshot_id"),
        journal_record=dict(raw.get("journal_record") or {}),
    )


def packet_from_dict(raw: dict[str, Any]) -> ApprovalPacket:
    summary = raw.get("order_plan_summary") or {}
    refs = raw.get("evidence_refs") or {}
    snap = raw.get("snapshot") or {}
    history = []
    for item in raw.get("status_history") or []:
        history.append(
            StatusEvent(
                status=ApprovalStatus(item["status"]),
                at=item["at"],
                reason=item.get("reason"),
                note=item.get("note"),
            )
        )
    sleeve = raw.get("sleeve")
    return ApprovalPacket(
        approval_id=str(raw["approval_id"]),
        symbol=str(raw["symbol"]).upper(),
        action=Decision(raw["action"]),
        desired_allocation_pct=raw.get("desired_allocation_pct"),
        current_allocation_pct=raw.get("current_allocation_pct"),
        order_notional=raw.get("order_notional"),
        order_quantity=raw.get("order_quantity"),
        current_price=raw.get("current_price"),
        sleeve=Sleeve(sleeve) if sleeve else None,
        thesis_summary=raw.get("thesis_summary"),
        why_now=raw.get("why_now"),
        why_not_cash=raw.get("why_not_cash"),
        why_not_spy=raw.get("why_not_spy"),
        bull_case=raw.get("bull_case"),
        base_case=raw.get("base_case"),
        bear_case=raw.get("bear_case"),
        key_risks=list(raw.get("key_risks") or []),
        invalidation_exit_policy=raw.get("invalidation_exit_policy"),
        expected_horizon=raw.get("expected_horizon"),
        portfolio_effect=raw.get("portfolio_effect"),
        sector_concentration_effect=raw.get("sector_concentration_effect"),
        risk_gate_verdict=raw.get("risk_gate_verdict"),
        enhanced_review_requirements=list(raw.get("enhanced_review_requirements") or []),
        order_plan_summary=OrderPlanSummary(
            order_plan_id=str(summary.get("order_plan_id") or raw.get("order_plan_id") or ""),
            execution_status=str(summary.get("execution_status") or ExecutionStatus.PAPER_ONLY.value),
            side=summary.get("side"),
            order_type=summary.get("order_type"),
            time_in_force=summary.get("time_in_force"),
            quantity=summary.get("quantity"),
            notional=summary.get("notional"),
            estimated_price=summary.get("estimated_price"),
            estimated_position_pct_after=summary.get("estimated_position_pct_after"),
            blocked_reasons=list(summary.get("blocked_reasons") or []),
            live_execution_blocked=bool(summary.get("live_execution_blocked", True)),
            stop_orders_created=int(summary.get("stop_orders_created") or 0),
            broker_submitted=False,
        ),
        evidence_refs=EvidenceRefs(
            order_plan_id=refs.get("order_plan_id"),
            source_decision_id=refs.get("source_decision_id"),
            thesis_id=refs.get("thesis_id"),
            research_id=refs.get("research_id"),
            risk_evaluation_id=refs.get("risk_evaluation_id"),
            monitoring_run_id=refs.get("monitoring_run_id"),
            supporting_evidence_refs=list(refs.get("supporting_evidence_refs") or []),
        ),
        created_at=str(raw["created_at"]),
        status=ApprovalStatus(raw["status"]),
        snapshot=ApprovalSnapshot(
            nav=snap.get("nav"),
            cash=snap.get("cash"),
            cash_allocation_pct=snap.get("cash_allocation_pct"),
            holdings_count=snap.get("holdings_count"),
            position_pct=snap.get("position_pct"),
            position_quantity=snap.get("position_quantity"),
            risk_state=snap.get("risk_state"),
            daily_risk_halt=snap.get("daily_risk_halt"),
            quote_observed_at=snap.get("quote_observed_at"),
            research_id=snap.get("research_id"),
            research_freshness=snap.get("research_freshness"),
            thesis_id=snap.get("thesis_id"),
            thesis_updated_at=snap.get("thesis_updated_at"),
            source_decision_id=snap.get("source_decision_id"),
        ),
        status_history=history,
        expiry_reasons=list(raw.get("expiry_reasons") or []),
        superseded_by=raw.get("superseded_by"),
        human_note=raw.get("human_note"),
        decided_at=raw.get("decided_at"),
        monitoring_state=raw.get("monitoring_state"),
        sector=raw.get("sector"),
        live_execution_blocked=True,
        broker_submitted=False,
        live_trade_actions_allowed=False,
        auto_execution=False,
        approved_does_not_place_order=True,
        stop_orders_created=0,
    )
