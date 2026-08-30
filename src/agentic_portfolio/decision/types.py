"""Thesis + portfolio-decision types. Facts stay in Python; AI fills interpretation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentic_portfolio.research.types import FrozenPortfolioFacts, FrozenRiskLimits, ResearchReport
from agentic_portfolio.schemas import (
    Decision,
    ExitPolicy,
    PortfolioContext,
    ProposedAction,
    RiskGateResult,
    Sleeve,
    ThesisRecord,
    ThesisStatus,
)

CASH_SYMBOL = "CASH"
SPY_SYMBOL = "SPY"
RISK_UP = {Decision.BUY, Decision.ADD}


@dataclass
class NameDecision:
    symbol: str
    decision: Decision
    desired_allocation_pct: float | None = None
    rationale: str | None = None
    why_preferable_to_cash: str | None = None
    why_preferable_to_spy: str | None = None
    why_preferable_to_alternatives: str | None = None
    thesis_id: str | None = None
    research_id: str | None = None


@dataclass
class PortfolioComparison:
    ranking: list[str] = field(default_factory=list)
    vs_cash: str | None = None
    vs_spy: str | None = None
    notes: str | None = None


@dataclass
class DecisionPacket:
    """Frozen facts for the reasoner. AI cannot alter these."""

    packet_id: str
    assembled_at: str
    reports: list[dict[str, Any]] = field(default_factory=list)
    portfolio_facts: FrozenPortfolioFacts | None = None
    risk_limits: FrozenRiskLimits = field(default_factory=FrozenRiskLimits)
    existing_theses: list[dict[str, Any]] = field(default_factory=list)
    existing_holdings: list[dict[str, Any]] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=lambda: [CASH_SYMBOL, SPY_SYMBOL])
    policy_context: dict[str, Any] = field(default_factory=dict)
    sleeve_exit_requirements: dict[str, Any] = field(default_factory=dict)


@dataclass
class DecisionReasoningRequest:
    packet: dict[str, Any]
    reports: list[dict[str, Any]]
    portfolio_context: dict[str, Any]
    existing_theses: list[dict[str, Any]]
    policy_context: dict[str, Any]
    alternatives: list[str]
    instructions: str


@dataclass
class GatedAction:
    proposed_action: ProposedAction
    risk: RiskGateResult
    thesis_id: str | None = None


@dataclass
class DecisionResult:
    packet: DecisionPacket
    theses: list[ThesisRecord] = field(default_factory=list)
    decisions: list[NameDecision] = field(default_factory=list)
    comparison: PortfolioComparison | None = None
    gated_actions: list[GatedAction] = field(default_factory=list)
    reports: list[ResearchReport] = field(default_factory=list)
    context: PortfolioContext | None = None
    batch_id: str | None = None
    execution_attempted: bool = False
    theses_activated: int = 0
    broker_stop_orders_created: int = 0
    unsupported_claims: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)


def sleeve_of(value: str | Sleeve | None, fallback: Sleeve) -> Sleeve:
    if value is None:
        return fallback
    if isinstance(value, Sleeve):
        return value
    return Sleeve(str(value))


def status_forced_draft(status: ThesisStatus | str | None) -> ThesisStatus:
    return ThesisStatus.DRAFT
