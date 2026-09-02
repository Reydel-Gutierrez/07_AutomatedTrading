"""Typed AI artifacts. Advisory only. Broker facts stay in Python."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from agentic_portfolio.runtime import RuntimeMode
from agentic_portfolio.schemas import Decision, GateVerdict


class ModelRole(str, Enum):
    SCREENING = "screening"
    RESEARCH = "research"
    ESCALATION = "escalation"
    FALLBACK = "fallback"


class BudgetMode(str, Enum):
    NORMAL = "NORMAL"
    CONSERVING = "CONSERVING"
    CRITICAL = "CRITICAL"
    EXHAUSTED = "EXHAUSTED"


class RecommendedAction(str, Enum):
    REJECT = "REJECT"
    WATCH = "WATCH"
    BUY_CANDIDATE = "BUY_CANDIDATE"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"


class AIConfidence(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ProposalStatus(str, Enum):
    DRAFT = "DRAFT"
    PROPOSED = "PROPOSED"
    RISK_BLOCKED = "RISK_BLOCKED"
    REJECTED = "REJECTED"
    WATCH = "WATCH"
    STALE = "STALE"
    BLOCKED_NO_FACTS = "BLOCKED_NO_FACTS"
    BLOCKED_BUDGET = "BLOCKED_BUDGET"
    BLOCKED_AI = "BLOCKED_AI"


CONFIDENCE_RANK = {AIConfidence.LOW: 0, AIConfidence.MEDIUM: 1, AIConfidence.HIGH: 2}

ACTION_TO_DECISION = {
    RecommendedAction.REJECT: Decision.REJECT,
    RecommendedAction.WATCH: Decision.WATCH,
    RecommendedAction.BUY_CANDIDATE: Decision.BUY,
    RecommendedAction.HOLD: Decision.HOLD,
    RecommendedAction.REDUCE: Decision.REDUCE,
    RecommendedAction.EXIT: Decision.SELL,
}


def parse_confidence(raw: Any) -> AIConfidence:
    text = str(raw or "LOW").strip().upper()
    if text in AIConfidence.__members__:
        return AIConfidence(text)
    return AIConfidence.LOW


def parse_recommended_action(raw: Any) -> RecommendedAction:
    text = str(raw or "").strip().upper()
    if text == "SELL":
        return RecommendedAction.EXIT
    if text == "BUY":
        return RecommendedAction.BUY_CANDIDATE
    if text in RecommendedAction.__members__:
        return RecommendedAction(text)
    raise ValueError(f"unsupported recommended_action: {raw}")


@dataclass
class ScreeningResult:
    ticker: str
    score: float
    classification: str
    catalyst_summary: str
    risk_flags: list[str]
    worth_deep_research: bool
    confidence: AIConfidence
    provider: str | None = None
    model: str | None = None
    cost: Decimal = Decimal("0")
    context_id: str | None = None
    screening_id: str | None = None
    runtime_mode: str = RuntimeMode.PAPER.value
    rejection_reason: str | None = None
    operational_failure: bool = False


@dataclass
class DeepResearchResult:
    ticker: str
    thesis: str
    bull_case: str
    bear_case: str
    catalysts: list[str]
    risks: list[str]
    valuation_observations: str
    technical_observations: str
    confidence: AIConfidence
    recommended_action: RecommendedAction
    provider: str | None = None
    model: str | None = None
    cost: Decimal = Decimal("0")
    context_id: str | None = None
    research_id: str | None = None
    runtime_mode: str = RuntimeMode.PAPER.value
    rejection_reason: str | None = None
    operational_failure: bool = False


@dataclass
class PortfolioDecisionResult:
    ticker: str
    action: RecommendedAction
    confidence: AIConfidence
    rationale: str
    suggested_allocation_pct: float | None
    suggested_max_dollars: float | None
    reassessment_conditions: list[str]
    risk_notes: list[str]
    provider: str | None = None
    model: str | None = None
    cost: Decimal = Decimal("0")
    context_id: str | None = None
    decision_id: str | None = None
    runtime_mode: str = RuntimeMode.PAPER.value
    rejection_reason: str | None = None
    operational_failure: bool = False


@dataclass
class UsageRecord:
    timestamp: str
    provider: str
    model: str
    purpose: str
    ticker: str | None
    input_tokens: int
    output_tokens: int
    estimated_cost: Decimal
    actual_cost: Decimal
    cumulative_daily_cost: Decimal
    cumulative_monthly_cost: Decimal
    role: str | None = None
    runtime_mode: str | None = None
    reservation_id: str | None = None
    blocked: bool = False
    reason: str | None = None


@dataclass
class BudgetStatus:
    month: str
    mode: BudgetMode
    cap: Decimal
    spent: Decimal
    reserved: Decimal
    remaining: Decimal
    pct_used: float
    calls_month: int
    calls_today: int
    spend_by_provider: dict[str, Decimal] = field(default_factory=dict)
    spend_by_model: dict[str, Decimal] = field(default_factory=dict)
    daily_spent: Decimal = Decimal("0")


@dataclass
class GatewayResult:
    payload: dict[str, Any]
    provider: str
    model: str
    role: ModelRole
    purpose: str
    ticker: str | None
    input_tokens: int
    output_tokens: int
    estimated_cost: Decimal
    actual_cost: Decimal
    reservation_id: str
    blocked: bool = False
    reason: str | None = None
    fallback_used: bool = False


@dataclass
class LiveProposal:
    proposal_id: str
    ticker: str
    action: RecommendedAction
    decision: Decision
    status: ProposalStatus
    confidence: AIConfidence
    rationale: str
    suggested_allocation_pct: float | None
    suggested_max_dollars: float | None
    capped_max_dollars: float | None
    reassessment_conditions: list[str]
    risk_notes: list[str]
    risk_verdict: GateVerdict | str | None
    risk_reasons: list[str]
    runtime_mode: str
    source_of_truth: str
    paper_environment: bool
    live_order_placement: bool
    placement_attempted: bool
    context_id: str | None
    screening_id: str | None
    research_id: str | None
    decision_id: str | None
    snapshot_id: str | None
    provider: str | None
    model: str | None
    cost: Decimal
    created_at: str
    rejection_reason: str | None = None
    nav: float | None = None
    cash: float | None = None
    buying_power: float | None = None
