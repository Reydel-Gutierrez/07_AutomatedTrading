"""Permanent Deep Research types.

Facts, derived metrics, and AI interpretations stay distinct.
Research never emits BUY ProposedActions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agentic_portfolio.schemas import Candidate, PortfolioContext, SecurityClass, Sleeve


class ResearchStatus(str, Enum):
    RESEARCH_PENDING = "RESEARCH_PENDING"
    RESEARCHING = "RESEARCHING"
    RESEARCH_COMPLETE = "RESEARCH_COMPLETE"
    RESEARCH_INCONCLUSIVE = "RESEARCH_INCONCLUSIVE"
    RESEARCH_REJECTED = "RESEARCH_REJECTED"
    RESEARCH_STALE = "RESEARCH_STALE"


class ResearchFreshness(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    RESEARCH_REFRESH_REQUIRED = "RESEARCH_REFRESH_REQUIRED"


class ResearchSubjectKind(str, Enum):
    NEW_CANDIDATE = "NEW_CANDIDATE"
    EXISTING_POSITION_REVIEW = "EXISTING_POSITION_REVIEW"


class ResearchConclusion(str, Enum):
    ADVANCE_TO_THESIS = "ADVANCE_TO_THESIS"
    KEEP_WATCHING = "KEEP_WATCHING"
    REJECT = "REJECT"
    NEED_MORE_DATA = "NEED_MORE_DATA"


class DislocationVerdict(str, Enum):
    LIKELY_DISLOCATION = "LIKELY_DISLOCATION"
    MIXED = "MIXED"
    LIKELY_DETERIORATION = "LIKELY_DETERIORATION"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class EvidenceKind(str, Enum):
    OBSERVED_FACT = "OBSERVED_FACT"
    DETERMINISTIC_DERIVED_METRIC = "DETERMINISTIC_DERIVED_METRIC"
    AI_INTERPRETATION = "AI_INTERPRETATION"


class ResearchConfidence(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class EarningsEffectKind(str, Enum):
    ONE_TIME_EFFECT = "ONE_TIME_EFFECT"
    STRUCTURAL_CHANGE = "STRUCTURAL_CHANGE"
    UNCERTAIN = "UNCERTAIN"


class RefreshTrigger(str, Enum):
    ELAPSED_TIME = "ELAPSED_TIME"
    EARNINGS_EVENT = "EARNINGS_EVENT"
    MAJOR_NEWS = "MAJOR_NEWS"
    MATERIAL_FILING = "MATERIAL_FILING"
    PRICE_MOVE = "PRICE_MOVE"
    REGIME_CHANGE = "REGIME_CHANGE"
    THESIS_CONCERN = "THESIS_CONCERN"
    PERIODIC_REVIEW = "PERIODIC_REVIEW"
    HUMAN_REQUEST = "HUMAN_REQUEST"


@dataclass
class EvidenceItem:
    """One research evidence item with provenance.

    OBSERVED_FACT: retrieved value, never rewritten by AI.
    DETERMINISTIC_DERIVED_METRIC: computed from facts (growth, margins, yield).
    AI_INTERPRETATION: qualitative judgment citing evidence_refs.
    """

    evidence_id: str
    kind: EvidenceKind
    name: str
    value: Any = None
    source: str | None = None
    observed_at: str | None = None
    data_type: str = "unknown"
    raw_ref: str | None = None
    derived: bool = False
    freshness: str | None = None
    notes: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class ScenarioCase:
    case: str
    summary: str
    major_assumptions: list[str] = field(default_factory=list)
    expected_business_outcome: str | None = None
    major_risk: str | None = None
    attractiveness_implication: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    price_target: float | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class DislocationAssessment:
    verdict: DislocationVerdict
    reasoning: str
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class FrozenPortfolioFacts:
    """Immutable portfolio snapshot for research. AI cannot alter these."""

    current_nav: float
    cash: float
    buying_power: float
    cash_allocation_pct: float
    holdings_count: int
    positions: list[dict[str, Any]] = field(default_factory=list)
    sleeve_allocation_pct: dict[str, float] = field(default_factory=dict)
    sector_allocation_pct: dict[str, float] = field(default_factory=dict)
    risk_state: str | None = None
    high_water_mark: float | None = None
    current_drawdown: float | None = None
    daily_risk_halt: bool = False
    existing_thesis_ids: list[str] = field(default_factory=list)


@dataclass
class FrozenRiskLimits:
    """Policy ceilings copied for audit. Research cannot modify them."""

    source: str = "config/portfolio_policy.json"
    limits: dict[str, Any] = field(default_factory=dict)


@dataclass
class FrozenClassification:
    security_class: str | None = None
    classification_status: str | None = None
    sector: str | None = None
    industry: str | None = None
    reasons: list[str] = field(default_factory=list)


@dataclass
class ResearchEvidencePacket:
    """Normalized evidence prepared by Python for the ResearchReasoner."""

    packet_id: str
    candidate_id: str
    symbol: str
    assembled_at: str
    subject_kind: ResearchSubjectKind
    provisional_sleeve: Sleeve
    facts: list[EvidenceItem] = field(default_factory=list)
    derived_metrics: list[EvidenceItem] = field(default_factory=list)
    sources_observed: list[str] = field(default_factory=list)
    sources_unavailable: list[str] = field(default_factory=list)
    classification: FrozenClassification = field(default_factory=FrozenClassification)
    portfolio_facts: FrozenPortfolioFacts | None = None
    risk_limits: FrozenRiskLimits = field(default_factory=FrozenRiskLimits)
    sleeve_research_questions: list[str] = field(default_factory=list)
    policy_context: dict[str, Any] = field(default_factory=dict)
    missing_information: list[str] = field(default_factory=list)
    comparison_group_id: str | None = None
    comparison_peer_symbols: list[str] = field(default_factory=list)
    discovery_score: float | None = None
    technical_weight: str | None = None
    investment_question: str | None = None
    existing_thesis_id: str | None = None
    completeness: str = "PARTIAL"

    def fact_ids(self) -> set[str]:
        return {e.evidence_id for e in self.facts}

    def derived_ids(self) -> set[str]:
        return {e.evidence_id for e in self.derived_metrics}

    def all_evidence_ids(self) -> set[str]:
        return self.fact_ids() | self.derived_ids()

    def fact_by_name(self, name: str) -> EvidenceItem | None:
        for item in self.facts:
            if item.name == name:
                return item
        return None


@dataclass
class ResearchReport:
    research_id: str
    candidate_id: str
    symbol: str
    started_at: str
    completed_at: str | None = None
    provisional_sleeve: Sleeve = Sleeve.CORE_GROWTH
    security_class: SecurityClass | None = None
    sector: str | None = None
    industry: str | None = None
    market_price: float | None = None
    research_status: ResearchStatus = ResearchStatus.RESEARCH_PENDING
    subject_kind: ResearchSubjectKind = ResearchSubjectKind.NEW_CANDIDATE
    executive_summary: str | None = None
    business_summary: str | None = None
    investment_question: str | None = None
    fundamental_analysis: str | None = None
    financial_analysis: str | None = None
    valuation_analysis: str | None = None
    earnings_analysis: str | None = None
    competitive_analysis: str | None = None
    technical_context: str | None = None
    market_context: str | None = None
    sector_context: str | None = None
    news_analysis: str | None = None
    filing_analysis: str | None = None
    catalyst_analysis: str | None = None
    risk_analysis: str | None = None
    bull_case: ScenarioCase | None = None
    base_case: ScenarioCase | None = None
    bear_case: ScenarioCase | None = None
    temporary_dislocation_assessment: DislocationAssessment | None = None
    fundamental_deterioration_assessment: DislocationAssessment | None = None
    key_catalysts: list[str] = field(default_factory=list)
    key_risks: list[str] = field(default_factory=list)
    invalidation_candidates: list[str] = field(default_factory=list)
    expected_horizon: str | None = None
    missing_information: list[str] = field(default_factory=list)
    conflicting_evidence: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    facts: list[EvidenceItem] = field(default_factory=list)
    derived_metrics: list[EvidenceItem] = field(default_factory=list)
    ai_interpretations: list[EvidenceItem] = field(default_factory=list)
    confidence: ResearchConfidence = ResearchConfidence.LOW
    research_conclusion: ResearchConclusion | None = None
    recommended_next_step: str | None = None
    observed_at: str | None = None
    freshness: ResearchFreshness = ResearchFreshness.FRESH
    thesis_id: str | None = None
    packet_id: str | None = None
    comparison_group_id: str | None = None
    discovery_score: float | None = None
    unsupported_claims: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    sources_observed: list[str] = field(default_factory=list)
    sources_unavailable: list[str] = field(default_factory=list)
    earnings_effect_kind: str | None = None
    refresh_triggers: list[str] = field(default_factory=list)
    proposed_actions_created: int = 0
    buy_actions_created: int = 0
    execution_attempted: bool = False
    risk_limits_unchanged: bool = True
    portfolio_facts_unchanged: bool = True
    classification_unchanged: bool = True
    stale_after: str | None = None
    research_source: str | None = None
    provider: str | None = None
    model: str | None = None
    ai_call_id: str | None = None
    estimated_cost: float | None = None
    actual_cost: float | None = None
    evidence_fingerprint: str | None = None
    research_generation: int = 1
    runtime_mode: str | None = None
    production_artifact: bool = True


@dataclass
class ComparisonDimension:
    name: str
    ranking: list[str] = field(default_factory=list)
    notes: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    uncertainty: str | None = None


@dataclass
class ResearchComparison:
    comparison_id: str
    comparison_group_id: str | None
    symbols: list[str]
    research_ids: list[str]
    dimensions: list[ComparisonDimension] = field(default_factory=list)
    relative_conclusion: str | None = None
    created_at: str | None = None
    evidence_quality_notes: str | None = None
    portfolio_overlap_notes: str | None = None
    unsupported_claims: list[str] = field(default_factory=list)


@dataclass
class ResearchReasoningRequest:
    """Provider-agnostic payload. Cursor is not required at runtime."""

    candidate: dict[str, Any]
    packet: dict[str, Any]
    portfolio_context: dict[str, Any]
    policy_context: dict[str, Any]
    sleeve_questions: list[str]
    instructions: str
    comparison_peers: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ResearchResult:
    report: ResearchReport
    packet: ResearchEvidencePacket
    candidate: Candidate | None = None
    context: PortfolioContext | None = None
    proposed_actions_created: int = 0
    buy_actions_created: int = 0
    execution_attempted: bool = False
    comparison: ResearchComparison | None = None
