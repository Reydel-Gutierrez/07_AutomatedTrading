from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any

from agentic_portfolio.correlation import CorrelationObservation, CorrelationStatus
from agentic_portfolio.sectors import CanonicalSector, SectorStatus


class ClassificationStatus(str, Enum):
    VALIDATED = "VALIDATED"
    VERIFIED = "VALIDATED"  # alias — risk-gate verified
    PARTIAL = "PARTIAL"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"


class SecurityClass(str, Enum):
    BROAD_MARKET_INDEX_ETF = "BROAD_MARKET_INDEX_ETF"
    OTHER_DIVERSIFIED_ETF = "OTHER_DIVERSIFIED_ETF"
    INDIVIDUAL_EQUITY = "INDIVIDUAL_EQUITY"


class Sleeve(str, Enum):
    CORE_GROWTH = "CORE_GROWTH"
    OPPORTUNISTIC = "OPPORTUNISTIC"
    TACTICAL = "TACTICAL"
    SPECULATIVE = "SPECULATIVE"


class RiskState(str, Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    RISK_REDUCTION = "RISK_REDUCTION"
    DEFENSIVE = "DEFENSIVE"
    HALTED = "HALTED"


class Decision(str, Enum):
    BUY = "BUY"
    ADD = "ADD"
    SELL = "SELL"
    REDUCE = "REDUCE"
    HOLD = "HOLD"
    WATCH = "WATCH"
    REJECT = "REJECT"
    NO_ACTION = "NO_ACTION"


class GateVerdict(str, Enum):
    PASS = "PASS"
    REQUIRES_ENHANCED_REVIEW = "REQUIRES_ENHANCED_REVIEW"
    RISK_REDUCING_ONLY = "RISK_REDUCING_ONLY"
    HALTED = "HALTED"
    FAIL = "FAIL"


class BreachKind(str, Enum):
    NONE = "NONE"
    PROPOSED_ACTION_BREACH = "PROPOSED_ACTION_BREACH"
    PASSIVE_MARKET_DRIFT_BREACH = "PASSIVE_MARKET_DRIFT_BREACH"


class ProvenanceKind(str, Enum):
    MCP_OBSERVED_FACT = "MCP_OBSERVED_FACT"
    DERIVED_DETERMINISTIC_VALUE = "DERIVED_DETERMINISTIC_VALUE"
    MISSING = "MISSING"
    CONFLICTING = "CONFLICTING"


class FactOrigin(str, Enum):
    """Origin of a market fact. LIVE AI may only consume MCP_OBSERVED or DERIVED."""

    MCP_OBSERVED = "MCP_OBSERVED"
    DERIVED = "DERIVED"
    MISSING = "MISSING"
    UNAVAILABLE = "UNAVAILABLE"
    FIXTURE = "FIXTURE"
    TEST = "TEST"
    DEMO = "DEMO"
    SAMPLE = "SAMPLE"
    SYNTHETIC = "SYNTHETIC"
    PAPER = "PAPER"
    MOCK = "MOCK"


class FreshnessStatus(str, Enum):
    FRESH = "FRESH"
    LAST_SESSION = "LAST_SESSION"
    OFF_HOURS = "OFF_HOURS"
    INDICATIVE = "INDICATIVE"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"


class CandidateValidationStatus(str, Enum):
    VALID = "VALID"
    INVALID_IDENTITY = "INVALID_IDENTITY"
    MISSING_IDENTITY = "MISSING_IDENTITY"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    STALE_QUOTE = "STALE_QUOTE"
    MISSING_QUOTE = "MISSING_QUOTE"
    MISSING_LIQUIDITY = "MISSING_LIQUIDITY"
    STALE_FUNDAMENTALS = "STALE_FUNDAMENTALS"
    UNSUPPORTED_SECURITY_TYPE = "UNSUPPORTED_SECURITY_TYPE"
    SYNTHETIC_DATA_DETECTED = "SYNTHETIC_DATA_DETECTED"


class EmbeddedSectorStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class SleeveAssignmentStatus(str, Enum):
    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    REDUCING = "REDUCING"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    WATCH = "WATCH"


class ThesisStatus(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    STRENGTHENED = "STRENGTHENED"
    UNCHANGED = "UNCHANGED"
    WEAKENED = "WEAKENED"
    INVALIDATED = "INVALIDATED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


class PositionRegistryStatus(str, Enum):
    REGISTERED = "REGISTERED"
    UNREGISTERED_POSITION = "UNREGISTERED_POSITION"


class RefreshReason(str, Enum):
    STALE = "STALE"
    HIGH_CONCENTRATION_CAPACITY = "HIGH_CONCENTRATION_CAPACITY"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    CONFLICTING_OBSERVATIONS = "CONFLICTING_OBSERVATIONS"
    MATERIAL_FUND_CHANGE = "MATERIAL_FUND_CHANGE"
    HUMAN_REQUEST = "HUMAN_REQUEST"
    SESSION_START = "SESSION_START"
    MISSING = "MISSING"
    INITIAL = "INITIAL"


def to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, list):
        return [to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj


@dataclass
class EvidenceValue:
    """One ClassificationEvidence field with provenance.

    AI interpretation must not be stored as MCP_OBSERVED_FACT.
    """

    value: Any = None
    source: str | None = None
    observed_at: str | None = None
    provenance: ProvenanceKind = ProvenanceKind.MISSING
    confidence: str | None = None
    status: str | None = None


@dataclass
class ProvenanceFact:
    """A single market fact with source, time, and freshness. Missing stays unavailable."""

    value: Any = None
    source: str | None = None
    as_of: str | None = None
    freshness: FreshnessStatus = FreshnessStatus.UNAVAILABLE
    origin: FactOrigin = FactOrigin.MISSING
    unavailable: bool = True
    notes: list[str] = field(default_factory=list)
    session: str | None = None

    def for_ai(self) -> dict[str, Any]:
        out = {
            "value": None if self.unavailable else self.value,
            "unavailable": bool(self.unavailable or self.value is None),
            "source": self.source,
            "as_of": self.as_of,
            "freshness": self.freshness.value if isinstance(self.freshness, FreshnessStatus) else self.freshness,
            "origin": self.origin.value if isinstance(self.origin, FactOrigin) else self.origin,
            "notes": list(self.notes or []),
        }
        if self.session:
            out["session"] = self.session
        return out


@dataclass
class ClassificationEvidence:
    """Verifiable fund/equity characteristics. None means unknown — not a free pass."""

    instrument_kind: str | None = None  # "etf" | "equity"
    is_leveraged: bool | None = None
    is_inverse: bool | None = None
    is_thematic: bool | None = None
    is_sector_or_industry_fund: bool | None = None
    is_narrow_factor: bool | None = None
    is_single_stock_fund: bool | None = None
    underlying_index: str | None = None
    fund_mandate: str | None = None
    constituent_count: int | None = None
    max_sector_weight: float | None = None
    top10_weight: float | None = None
    seed_list_match: bool = False
    # ETF embedded sector weights — never invented.
    embedded_sector_weights: dict[str, float] | None = None
    embedded_sector_exposure_status: EmbeddedSectorStatus = EmbeddedSectorStatus.UNKNOWN
    underlying_index_definitionally_broad: bool | None = None
    sector_label_raw: str | None = None
    industry_label_raw: str | None = None
    legal_name: str | None = None
    description: str | None = None
    conflict_notes: list[str] = field(default_factory=list)
    provenance: dict[str, EvidenceValue] = field(default_factory=dict)


@dataclass
class CacheMetadata:
    created_at: str | None = None
    refreshed_at: str | None = None
    expires_at: str | None = None
    stale: bool = False
    source_version: str | None = None
    refresh_reason: str | None = None
    field_ttls: dict[str, str] = field(default_factory=dict)


@dataclass
class LiquidityEvidence:
    median_daily_dollar_volume_20d: float | None = None
    recent_dollar_volume: float | None = None
    bid_ask_spread_pct: float | None = None
    average_volume_proxy: float | None = None
    status: str = "UNKNOWN"
    provenance: ProvenanceKind = ProvenanceKind.MISSING
    source: str | None = None
    observed_at: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class ClassificationResult:
    security_class: SecurityClass
    status: ClassificationStatus
    effective_class_for_ceiling: SecurityClass
    confidence: str
    reasons: list[str] = field(default_factory=list)
    seed_list_used: bool = False
    symbol: str = ""
    instrument_type: str | None = None
    evidence: ClassificationEvidence | None = None
    sector: CanonicalSector = CanonicalSector.UNKNOWN
    sector_status: SectorStatus = SectorStatus.UNKNOWN
    liquidity: LiquidityEvidence | None = None
    observed_at: str | None = None
    cache: CacheMetadata | None = None

    @property
    def classification_status(self) -> ClassificationStatus:
        return self.status


@dataclass
class Position:
    symbol: str
    market_value: float
    quantity: float = 0.0
    average_cost: float | None = None
    current_price: float | None = None
    sleeve: Sleeve | None = None
    security_class: SecurityClass | None = None
    classification_status: ClassificationStatus | None = None
    sector: str | None = None
    sector_status: SectorStatus | None = None
    unrealized_pnl: float | None = None
    registry_status: PositionRegistryStatus = PositionRegistryStatus.REGISTERED
    thesis_id: str | None = None


@dataclass
class OpenOrder:
    order_id: str
    symbol: str
    side: str
    state: str
    notional: float | None = None


@dataclass
class SpyBenchmark:
    price: float | None = None
    period_return: float | None = None
    portfolio_return: float | None = None
    excess_return: float | None = None


@dataclass
class PortfolioContext:
    timestamp: str
    account_number: str
    current_nav: float
    cash: float
    buying_power: float
    cash_allocation_pct: float
    positions: list[Position]
    holdings_count: int
    sleeve_market_values: dict[str, float]
    sleeve_allocation_pct: dict[str, float]
    sector_exposure: dict[str, float]
    sector_allocation_pct: dict[str, float]
    open_orders: list[OpenOrder]
    realized_pnl: float | None
    unrealized_pnl: float | None
    start_of_day_nav: float | None
    daily_portfolio_return: float | None
    daily_risk_halt: bool
    high_water_mark: float
    cash_flow_adjusted_hwm: float
    external_capital_flow: float
    current_drawdown: float
    risk_state: RiskState
    spy: SpyBenchmark | None
    facts_note: str = "NAV, cash, BP, positions, orders are observed facts. Classification is deterministic. Sleeves are persisted assignments."
    trading_session_id: str | None = None
    session_fail_safe: bool = False
    correlation: CorrelationObservation | None = None


@dataclass
class LiquidityInputs:
    median_daily_dollar_volume_20d: float | None = None
    recent_dollar_volume: float | None = None
    bid_ask_spread_pct: float | None = None


@dataclass
class ProposedAction:
    symbol: str
    decision: Decision
    security_class: SecurityClass
    classification_status: ClassificationStatus
    sleeve: Sleeve | None
    current_price: float | None = None
    proposed_notional: float | None = None
    expected_resulting_position_pct: float | None = None
    expected_resulting_sleeve_pct: float | None = None
    expected_resulting_sector_pct: float | None = None
    sector: str | None = None
    thesis_id: str | None = None
    investment_thesis_review_complete: bool = False
    risk_review_complete: bool = False
    enhanced_concentration_review_complete: bool = False
    high_concentration_review_complete: bool = False
    sector_concentration_review_complete: bool = False
    speculative_liquidity_review_complete: bool = False
    opportunistic_enhanced_risk_review_complete: bool = False
    add_justified_only_by_lower_price: bool = False
    explicitly_risk_reducing: bool = False
    human_authorized_halted_execution: bool = False
    liquidity: LiquidityInputs = field(default_factory=LiquidityInputs)
    correlation_with_book: float | None = None
    correlation: CorrelationObservation | None = None
    position_registry_status: PositionRegistryStatus | None = None
    sleeve_reclassification_pending: bool = False


@dataclass
class GateReason:
    code: str
    message: str
    kind: BreachKind = BreachKind.PROPOSED_ACTION_BREACH


@dataclass
class RiskGateResult:
    verdict: GateVerdict
    execution_permitted: bool
    recommendation_permitted: bool
    reasons: list[GateReason]
    required_reviews: list[str]
    applicable_position_ceiling_pct: float | None
    snapshot_id: str | None = None
    journal_record: dict[str, Any] = field(default_factory=dict)


@dataclass
class SleeveRecord:
    symbol: str
    sleeve: Sleeve
    assigned_at: str
    thesis_id: str | None = None
    status: SleeveAssignmentStatus = SleeveAssignmentStatus.PROPOSED
    source_decision_id: str | None = None
    last_reviewed_at: str | None = None
    quantity: float | None = None
    market_value: float | None = None


@dataclass
class SleeveReclassificationEvent:
    decision_id: str
    symbol: str
    old_sleeve: Sleeve
    new_sleeve: Sleeve
    reason: str
    new_thesis_id: str | None
    review_flag: str
    timestamp: str
    approved: bool
    review: str | None = None


@dataclass
class ThesisReview:
    review_id: str
    review_type: str  # INVESTMENT_THESIS_REVIEW | RISK_REVIEW | SLEEVE_RECLASSIFICATION_REVIEW
    reviewed_at: str
    session_id: str | None = None
    notes: str | None = None
    decision_id: str | None = None


@dataclass
class ExitPolicy:
    """How the position would later be exited. Never creates broker stop orders."""

    thesis_based: bool = True
    mandatory_fixed_stop_loss: bool = False
    price_invalidation: str | None = None
    event_invalidation: str | None = None
    technical_invalidation: str | None = None
    risk_invalidation: str | None = None
    broker_stop_orders_created: bool = False
    notes: str | None = None


@dataclass
class ThesisRecord:
    thesis_id: str
    symbol: str
    sleeve: Sleeve
    created_at: str
    updated_at: str
    status: ThesisStatus
    decision: Decision | None = None
    expected_horizon: str | None = None
    thesis_summary: str | None = None
    bull_case: str | None = None
    base_case: str | None = None
    bear_case: str | None = None
    catalysts: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    invalidation_conditions: list[str] = field(default_factory=list)
    review_triggers: list[str] = field(default_factory=list)
    exit_policy: ExitPolicy | None = None
    why_position_should_exist: str | None = None
    research_id: str | None = None
    desired_allocation_pct: float | None = None
    confidence: str | None = None
    supporting_evidence_refs: list[str] = field(default_factory=list)
    review_history: list[ThesisReview] = field(default_factory=list)
    last_price_observed: float | None = None
    last_price_observed_at: str | None = None


@dataclass
class ReconciliationFinding:
    code: str
    symbol: str | None
    message: str
    auto_repair: bool = False
    details: dict[str, Any] = field(default_factory=dict)


class CandidateStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    SHORTLISTED = "SHORTLISTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    PROMOTED_TO_RESEARCH = "PROMOTED_TO_RESEARCH"
    WATCHING = "WATCHING"
    RESEARCH_COMPLETE = "RESEARCH_COMPLETE"
    RESEARCH_INCONCLUSIVE = "RESEARCH_INCONCLUSIVE"


class DiscoveryPriority(str, Enum):
    """Research-queue urgency. URGENT_RESEARCH means research quickly — not buy quickly."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT_RESEARCH = "URGENT_RESEARCH"


class SignalType(str, Enum):
    FUNDAMENTAL = "FUNDAMENTAL"
    VALUATION = "VALUATION"
    PRICE_ACTION = "PRICE_ACTION"
    MOMENTUM = "MOMENTUM"
    VOLUME = "VOLUME"
    VOLATILITY = "VOLATILITY"
    CATALYST = "CATALYST"
    NEWS = "NEWS"
    EARNINGS = "EARNINGS"
    SEC_FILING = "SEC_FILING"
    SECTOR = "SECTOR"
    MARKET_REGIME = "MARKET_REGIME"
    RELATIVE_STRENGTH = "RELATIVE_STRENGTH"
    QUALITY = "QUALITY"
    LIQUIDITY = "LIQUIDITY"
    RISK_FLAG = "RISK_FLAG"


class SignalDirection(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"
    MIXED = "MIXED"


class DiscoveryChannel(str, Enum):
    CORE_QUALITY_DISCOVERY = "CORE_QUALITY_DISCOVERY"
    OPPORTUNISTIC_DISLOCATION_DISCOVERY = "OPPORTUNISTIC_DISLOCATION_DISCOVERY"
    TACTICAL_SETUP_DISCOVERY = "TACTICAL_SETUP_DISCOVERY"
    SPECULATIVE_ASYMMETRY_DISCOVERY = "SPECULATIVE_ASYMMETRY_DISCOVERY"


class Freshness(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    EXPIRED = "EXPIRED"


class ResearchQueueStatus(str, Enum):
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    RESEARCHING = "RESEARCHING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    NEED_MORE_DATA = "NEED_MORE_DATA"
    INCONCLUSIVE = "INCONCLUSIVE"
    DROPPED = "DROPPED"
    EXPIRED = "EXPIRED"


class MarketRegimeStatus(str, Enum):
    OBSERVED = "OBSERVED"
    UNKNOWN = "UNKNOWN"


class LiquidityStatus(str, Enum):
    ADEQUATE = "ADEQUATE"
    PARTIAL = "PARTIAL"
    THIN = "THIN"
    UNUSABLE = "UNUSABLE"
    UNKNOWN = "UNKNOWN"


@dataclass
class DiscoverySignal:
    """Structured discovery evidence. Scores are derived from these, not from free text."""

    signal_type: SignalType
    name: str
    value: Any = None
    direction: SignalDirection = SignalDirection.NEUTRAL
    strength: float = 0.0
    observed_at: str | None = None
    source: str | None = None
    evidence_ref: str | None = None


@dataclass
class SleeveHypothesis:
    """Provisional sleeve only. Not a persisted ACTIVE sleeve assignment."""

    sleeve: Sleeve
    reason: str
    confidence: str = "LOW"


@dataclass
class MarketRegime:
    """Thin regime input. UNKNOWN when evidence is missing — never fabricated."""

    status: MarketRegimeStatus = MarketRegimeStatus.UNKNOWN
    trend: str | None = None
    volatility: str | None = None
    breadth: str | None = None
    risk_on_off: str | None = None
    spy_trend: str | None = None
    sector_leadership: list[str] = field(default_factory=list)
    observed_at: str | None = None
    confidence: str | None = None
    source: str | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def unknown(cls, *, observed_at: str | None = None, reason: str = "regime_evidence_unavailable") -> MarketRegime:
        return cls(
            status=MarketRegimeStatus.UNKNOWN,
            observed_at=observed_at,
            confidence=None,
            notes=[reason],
        )


@dataclass
class Candidate:
    candidate_id: str
    symbol: str
    discovered_at: str
    discovery_source: str
    provisional_sleeve: Sleeve
    security_class: SecurityClass | None = None
    classification_status: ClassificationStatus | None = None
    current_price: float | None = None
    market_cap: float | None = None
    sector: str | None = None
    liquidity_status: str = LiquidityStatus.UNKNOWN.value
    discovery_score: float = 0.0
    priority: DiscoveryPriority = DiscoveryPriority.LOW
    reasons: list[str] = field(default_factory=list)
    signals: list[DiscoverySignal] = field(default_factory=list)
    supporting_evidence_refs: list[str] = field(default_factory=list)
    known_risks: list[str] = field(default_factory=list)
    event_flags: list[str] = field(default_factory=list)
    freshness: Freshness = Freshness.FRESH
    status: CandidateStatus = CandidateStatus.DISCOVERED
    sleeve_reason: str | None = None
    sleeve_confidence: str | None = None
    primary_provisional_sleeve: Sleeve | None = None
    alternative_sleeves: list[SleeveHypothesis] = field(default_factory=list)
    discovery_sources: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    research_questions: list[str] = field(default_factory=list)
    initial_observations: list[str] = field(default_factory=list)
    rejection_reason: str | None = None
    rejection_evidence: list[str] = field(default_factory=list)
    action_blocked_reason: str | None = None
    industry: str | None = None
    thesis_type: str | None = None
    expires_at: str | None = None
    score_breakdown: dict[str, float] = field(default_factory=dict)
    overlap_penalty: float = 0.0
    required_research_areas: list[str] = field(default_factory=list)
    comparison_group_id: str | None = None
    overlap_warnings: list[str] = field(default_factory=list)
    deferred_due_to_overlap: bool = False


@dataclass
class ResearchQueueEntry:
    queue_id: str
    candidate_id: str
    symbol: str
    provisional_sleeve: Sleeve
    discovery_score: float
    priority: DiscoveryPriority
    why_research_warranted: str
    required_research_areas: list[str] = field(default_factory=list)
    freshness_deadline: str | None = None
    status: ResearchQueueStatus = ResearchQueueStatus.QUEUED
    enqueued_at: str | None = None
    notes: str | None = None
    comparison_group_id: str | None = None
    overlap_warnings: list[str] = field(default_factory=list)
    deferred_due_to_research_queue_overlap: bool = False
    research_id: str | None = None
    attempt_count: int = 0
    last_error: str | None = None
    last_attempt_at: str | None = None
    claimed_at: str | None = None
    skipped_reason: str | None = None
    evidence_fingerprint: str | None = None
    research_generation: int = 1


@dataclass
class DiscoveryRun:
    run_id: str
    started_at: str
    completed_at: str | None = None
    market_session_context: dict[str, Any] = field(default_factory=dict)
    risk_state: str | None = None
    sources_queried: list[str] = field(default_factory=list)
    symbols_evaluated: list[str] = field(default_factory=list)
    candidates_created: list[str] = field(default_factory=list)
    candidates_rejected: list[str] = field(default_factory=list)
    candidates_promoted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    data_freshness: str | None = None
    conclusion: str | None = None
    regime_status: str | None = None
    theses_created: int = 0
    buy_actions_created: int = 0
    execution_attempted: bool = False
