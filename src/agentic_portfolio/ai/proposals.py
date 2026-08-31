"""LIVE proposals stop before broker placement. Risk Gate is still required."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from agentic_portfolio.ai.errors import PlacementForbidden
from agentic_portfolio.ai.safety import LIVE_ORDER_PLACEMENT, assert_proposal_only, refuse_placement
from agentic_portfolio.ai.store import AIArtifactStore
from agentic_portfolio.ai.types import (
    ACTION_TO_DECISION,
    LiveProposal,
    PortfolioDecisionResult,
    ProposalStatus,
    RecommendedAction,
)
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.paths import project_root
from agentic_portfolio.risk_gate import evaluate
from agentic_portfolio.runtime import LIVE_SOURCE_OF_TRUTH, RuntimeMode
from agentic_portfolio.schemas import (
    ClassificationStatus,
    Decision,
    LiquidityInputs,
    PortfolioContext,
    ProposedAction,
    SecurityClass,
    Sleeve,
    to_dict,
)

RISK_UP = {RecommendedAction.BUY_CANDIDATE}


def cap_dollars(suggested: float | None, context: PortfolioContext) -> float:
    cash = float(context.cash or 0.0)
    bp = float(context.buying_power or 0.0)
    nav = float(context.current_nav or 0.0)
    hard = min(x for x in (cash, bp, nav) if x >= 0)
    if suggested is None:
        return max(0.0, hard)
    return max(0.0, min(float(suggested), hard))


def to_risk_action(
    decision: PortfolioDecisionResult,
    context: PortfolioContext,
    *,
    security_class: SecurityClass = SecurityClass.INDIVIDUAL_EQUITY,
    sleeve: Sleeve | None = Sleeve.CORE_GROWTH,
    price: float | None = None,
    liquidity: LiquidityInputs | None = None,
) -> ProposedAction | None:
    mapped = ACTION_TO_DECISION[decision.action]
    if mapped in {Decision.WATCH, Decision.REJECT, Decision.NO_ACTION, Decision.HOLD} and decision.action != RecommendedAction.HOLD:
        if mapped in {Decision.WATCH, Decision.REJECT}:
            return None
    nav = context.current_nav
    existing = sum(p.market_value for p in context.positions if p.symbol.upper() == decision.ticker)
    current_pct = (existing / nav) if nav else 0.0
    desired_pct = (decision.suggested_allocation_pct or 0.0) / 100.0
    mapped_decision = mapped
    if mapped_decision == Decision.BUY and existing > 0:
        mapped_decision = Decision.ADD
    elif mapped_decision == Decision.ADD and existing <= 0:
        mapped_decision = Decision.BUY
    notional = None
    resulting = current_pct
    if mapped_decision in {Decision.BUY, Decision.ADD}:
        resulting = desired_pct
        notional = cap_dollars(decision.suggested_max_dollars, context)
        if notional <= 0 and nav:
            notional = max(0.0, desired_pct * nav - existing)
            notional = cap_dollars(notional, context)
    elif mapped_decision in {Decision.SELL, Decision.REDUCE}:
        resulting = desired_pct
        notional = max(0.0, existing - desired_pct * nav)
    return ProposedAction(
        symbol=decision.ticker,
        decision=mapped_decision,
        security_class=security_class,
        classification_status=ClassificationStatus.VALIDATED if security_class else ClassificationStatus.PARTIAL,
        sleeve=sleeve,
        current_price=price,
        proposed_notional=notional,
        expected_resulting_position_pct=resulting,
        liquidity=liquidity or LiquidityInputs(median_daily_dollar_volume_20d=1e12),
    )


def create_proposal(
    decision: PortfolioDecisionResult,
    context: PortfolioContext,
    *,
    store: AIArtifactStore,
    runtime_mode: RuntimeMode | str,
    snapshot_id: str | None = None,
    screening_id: str | None = None,
    research_id: str | None = None,
    security_class: SecurityClass = SecurityClass.INDIVIDUAL_EQUITY,
    sleeve: Sleeve | None = Sleeve.CORE_GROWTH,
    price: float | None = None,
    now: datetime | None = None,
    live_trade_actions_allowed: bool = False,
    auto_execution: bool = False,
    journal=None,
    root=None,
) -> LiveProposal:
    stamp = now or datetime.now(timezone.utc)
    mode = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode)
    assert_proposal_only(live_trade_actions_allowed=live_trade_actions_allowed, auto_execution=auto_execution)
    if LIVE_ORDER_PLACEMENT:
        refuse_placement("place_equity_order", root=root)
    action = to_risk_action(decision, context, security_class=security_class, sleeve=sleeve, price=price)
    status = ProposalStatus.PROPOSED
    verdict = None
    reasons: list[str] = []
    rejection = decision.rejection_reason
    if action is None:
        status = ProposalStatus.WATCH if decision.action in {RecommendedAction.WATCH, RecommendedAction.HOLD, RecommendedAction.REJECT} else ProposalStatus.REJECTED
        if decision.action == RecommendedAction.REJECT:
            status = ProposalStatus.REJECTED
        rejection = rejection or f"no ProposedAction for {decision.action.value}"
    else:
        risk = evaluate(context, action)
        verdict = risk.verdict
        reasons = [r.value if hasattr(r, "value") else str(r) for r in (risk.reasons or [])]
        if str(getattr(verdict, "value", verdict)) not in {"PASS", "REQUIRES_ENHANCED_REVIEW"}:
            status = ProposalStatus.RISK_BLOCKED
            rejection = rejection or "risk_gate_blocked"
        if action.decision in {Decision.BUY, Decision.ADD} and cap_dollars(action.proposed_notional, context) <= 0:
            status = ProposalStatus.REJECTED
            rejection = "no_buying_power"
    proposal = LiveProposal(
        proposal_id=str(uuid4()),
        ticker=decision.ticker,
        action=decision.action,
        decision=ACTION_TO_DECISION[decision.action],
        status=status,
        confidence=decision.confidence,
        rationale=decision.rationale,
        suggested_allocation_pct=decision.suggested_allocation_pct,
        suggested_max_dollars=decision.suggested_max_dollars,
        capped_max_dollars=cap_dollars(decision.suggested_max_dollars, context),
        reassessment_conditions=list(decision.reassessment_conditions),
        risk_notes=list(decision.risk_notes),
        risk_verdict=verdict,
        risk_reasons=reasons,
        runtime_mode=mode,
        source_of_truth=LIVE_SOURCE_OF_TRUTH if mode == RuntimeMode.LIVE.value else "isolated_paper_book",
        paper_environment=mode != RuntimeMode.LIVE.value,
        live_order_placement=False,
        placement_attempted=False,
        context_id=decision.context_id,
        screening_id=screening_id,
        research_id=research_id or None,
        decision_id=decision.decision_id,
        snapshot_id=snapshot_id,
        provider=decision.provider,
        model=decision.model,
        cost=decision.cost if isinstance(decision.cost, Decimal) else Decimal(str(decision.cost or 0)),
        created_at=stamp.isoformat(),
        rejection_reason=rejection,
        nav=context.current_nav,
        cash=context.cash,
        buying_power=context.buying_power,
    )
    store.save_proposal(proposal.proposal_id, {**to_dict(proposal), "created_at": proposal.created_at})
    append_jsonl(
        {
            "type": "LIVE_AI_PROPOSAL" if mode == RuntimeMode.LIVE.value else "PAPER_AI_PROPOSAL",
            "proposal_id": proposal.proposal_id,
            "ticker": proposal.ticker,
            "status": proposal.status.value,
            "placement_attempted": False,
            "live_order_placement": False,
        },
        journal or ((root or project_root()) / "logs" / "ai_proposals.jsonl"),
    )
    return proposal


def attempt_placement(proposal: LiveProposal, *, root=None) -> None:
    del proposal
    refuse_placement("place_equity_order", root=root)
    raise PlacementForbidden("unreachable")
