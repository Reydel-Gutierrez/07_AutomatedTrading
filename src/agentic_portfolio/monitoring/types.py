"""Position monitoring types. Facts stay in Python; AI fills interpretation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agentic_portfolio.decision.types import DecisionResult, GatedAction
from agentic_portfolio.research.types import FrozenPortfolioFacts, ResearchReport
from agentic_portfolio.schemas import Decision, PortfolioContext, Sleeve, ThesisRecord, ThesisStatus


class MonitoringState(str, Enum):
    HEALTHY = "HEALTHY"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    RESEARCH_REFRESH_REQUIRED = "RESEARCH_REFRESH_REQUIRED"
    THESIS_WEAKENED = "THESIS_WEAKENED"
    THESIS_INVALIDATED = "THESIS_INVALIDATED"
    EXIT_CONDITION_TRIGGERED = "EXIT_CONDITION_TRIGGERED"


class TriggerKind(str, Enum):
    PRICE_MOVE = "PRICE_MOVE"
    EARNINGS_EVENT = "EARNINGS_EVENT"
    MAJOR_NEWS = "MAJOR_NEWS"
    MATERIAL_FILING = "MATERIAL_FILING"
    RESEARCH_STALE = "RESEARCH_STALE"
    RESEARCH_REFRESH_REQUIRED = "RESEARCH_REFRESH_REQUIRED"
    THESIS_REVIEW_TRIGGER = "THESIS_REVIEW_TRIGGER"
    THESIS_INVALIDATION_CANDIDATE = "THESIS_INVALIDATION_CANDIDATE"
    EXIT_POLICY_CONDITION = "EXIT_POLICY_CONDITION"
    TACTICAL_PRICE_OR_TECHNICAL = "TACTICAL_PRICE_OR_TECHNICAL"
    SPECULATIVE_RISK_OR_CATALYST = "SPECULATIVE_RISK_OR_CATALYST"
    OPPORTUNISTIC_DISLOCATION_REVIEW = "OPPORTUNISTIC_DISLOCATION_REVIEW"
    PORTFOLIO_RISK_STATE = "PORTFOLIO_RISK_STATE"
    MISSING_THESIS = "MISSING_THESIS"
    MISSING_RESEARCH = "MISSING_RESEARCH"


MONITORING_ACTIONS = {
    Decision.HOLD,
    Decision.ADD,
    Decision.REDUCE,
    Decision.SELL,
    Decision.NO_ACTION,
}

PRICE_ONLY_KINDS = {
    TriggerKind.PRICE_MOVE,
    TriggerKind.RESEARCH_STALE,
    TriggerKind.RESEARCH_REFRESH_REQUIRED,
}

THESIS_STATUS_FROM_MONITOR = {
    ThesisStatus.UNCHANGED,
    ThesisStatus.STRENGTHENED,
    ThesisStatus.WEAKENED,
    ThesisStatus.INVALIDATED,
}


@dataclass
class PositionObservation:
    """Injected read-only facts for one held name."""

    symbol: str
    current_price: float | None = None
    previous_close: float | None = None
    reference_price: float | None = None
    price_move_pct: float | None = None
    earnings_event: bool = False
    earnings_rows: list[dict[str, Any]] = field(default_factory=list)
    major_news: bool = False
    news_items: list[dict[str, Any]] = field(default_factory=list)
    material_filing: bool = False
    filings: list[dict[str, Any]] = field(default_factory=list)
    technicals: dict[str, Any] = field(default_factory=dict)
    price_invalidation_observed: bool = False
    technical_invalidation_observed: bool = False
    catalyst_failed: bool = False
    risk_event: bool = False
    fundamental_invalidation_observed: bool = False
    sources_observed: list[str] = field(default_factory=list)


@dataclass
class Trigger:
    kind: TriggerKind
    reason: str
    evidence_refs: list[str] = field(default_factory=list)
    sleeve_specific: bool = False


@dataclass
class MonitoringFacts:
    symbol: str
    sleeve: Sleeve | None
    position_pct: float
    current_price: float | None = None
    reference_price: float | None = None
    price_move_pct: float | None = None
    earnings_event: bool = False
    major_news: bool = False
    material_filing: bool = False
    research_freshness: str | None = None
    refresh_triggers: list[str] = field(default_factory=list)
    thesis_id: str | None = None
    thesis_status: str | None = None
    invalidation_conditions: list[str] = field(default_factory=list)
    review_triggers: list[str] = field(default_factory=list)
    exit_policy: dict[str, Any] | None = None
    predefined_price_or_technical: bool = False
    predefined_risk_or_catalyst: bool = False
    portfolio_risk_state: str | None = None
    daily_risk_halt: bool = False
    technicals: dict[str, Any] = field(default_factory=dict)
    missing_thesis: bool = False
    missing_research: bool = False
    sources_observed: list[str] = field(default_factory=list)


@dataclass
class MonitoringPacket:
    packet_id: str
    assembled_at: str
    facts: MonitoringFacts
    triggers: list[Trigger]
    preliminary_state: MonitoringState
    policy_context: dict[str, Any] = field(default_factory=dict)
    portfolio_facts: FrozenPortfolioFacts | None = None
    research_brief: dict[str, Any] | None = None
    thesis: dict[str, Any] | None = None
    sleeve_behavior: dict[str, Any] = field(default_factory=dict)


@dataclass
class MonitoringReasoningRequest:
    packet: dict[str, Any]
    facts: dict[str, Any]
    triggers: list[dict[str, Any]]
    thesis: dict[str, Any] | None
    research_brief: dict[str, Any] | None
    portfolio_context: dict[str, Any]
    policy_context: dict[str, Any]
    instructions: str


@dataclass
class ThesisReassessment:
    symbol: str
    thesis_id: str | None
    prior_status: str | None
    new_status: ThesisStatus
    monitoring_state: MonitoringState
    recommended_action: Decision
    desired_allocation_pct: float | None = None
    rationale: str | None = None
    opportunistic_verdict: str | None = None
    tactical_invalidation_detected: bool = False
    speculative_invalidation_detected: bool = False
    exit_condition_triggered: bool = False
    research_refresh_needed: bool = False
    broker_stop_orders_created: bool = False
    core_price_not_used_as_invalidation: bool = False
    unsupported_claims: list[str] = field(default_factory=list)


@dataclass
class MonitoredPosition:
    symbol: str
    facts: MonitoringFacts
    triggers: list[Trigger]
    preliminary_state: MonitoringState
    state: MonitoringState
    reassessment: ThesisReassessment | None = None
    research: ResearchReport | None = None
    thesis: ThesisRecord | None = None
    decision_result: DecisionResult | None = None
    gated_actions: list[GatedAction] = field(default_factory=list)
    recommended_action: Decision = Decision.NO_ACTION
    broker_stop_orders_created: int = 0
    execution_attempted: bool = False
    research_refresh_requested: bool = False


@dataclass
class MonitoringResult:
    run_id: str
    packet_ids: list[str] = field(default_factory=list)
    positions: list[MonitoredPosition] = field(default_factory=list)
    context: PortfolioContext | None = None
    execution_attempted: bool = False
    broker_stop_orders_created: int = 0
    theses_activated: int = 0
    unsupported_claims: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
