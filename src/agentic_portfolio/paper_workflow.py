"""Paper research workflow (no broker execution).

symbol → read-only evidence → ClassificationEvidence → deterministic classify
  → proposed sleeve → thesis record → ProposedAction → Portfolio Context
  → Deterministic Risk Gate → journal

Candidate Discovery does not call this workflow. Discovery may only enqueue
research. This paper path is Research/Thesis/Risk — still no broker orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentic_portfolio.adapters.robinhood_read import (
    FORBIDDEN_MCP_TOOLS,
    RobinhoodSecurityBundle,
    adapt_classification_evidence,
    adapt_liquidity_evidence,
)
from agentic_portfolio.classification import classify
from agentic_portfolio.evidence_cache import put_classification
from agentic_portfolio.journal import append_jsonl, append_risk_decision
from agentic_portfolio.risk_gate import evaluate
from agentic_portfolio.schemas import (
    ClassificationResult,
    Decision,
    LiquidityInputs,
    PortfolioContext,
    ProposedAction,
    RefreshReason,
    RiskGateResult,
    Sleeve,
    SleeveAssignmentStatus,
    ThesisRecord,
    ThesisStatus,
    to_dict,
)
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry

CLASSIFICATION_HINT = (
    "get_equity_tradability",
    "get_equity_fundamentals",
    "search",
    "get_equity_quotes",
)


@dataclass
class PaperWorkflowResult:
    symbol: str
    evidence: dict[str, Any]
    classification: ClassificationResult
    sleeve: Sleeve | None
    thesis: ThesisRecord | None
    proposed_action: ProposedAction | None
    context: PortfolioContext
    risk: RiskGateResult
    journal_path: Path | None = None
    mcp_tools_used: list[str] = field(default_factory=list)
    execution_attempted: bool = False


def run_paper_research_workflow(
    *,
    symbol: str,
    bundle: RobinhoodSecurityBundle,
    context: PortfolioContext,
    sleeves: SleeveRegistry,
    theses: ThesisRegistry,
    proposed_sleeve: Sleeve,
    decision: Decision,
    proposed_notional: float | None,
    thesis_fields: dict[str, Any] | None = None,
    action_kwargs: dict[str, Any] | None = None,
    classify_policy: dict | None = None,
    cache_path: Path | None = None,
    journal_path: Path | None = None,
    activate_thesis: bool = True,
) -> PaperWorkflowResult:
    """Paper-only. Raises if a forbidden MCP tool name is present on the bundle."""
    _assert_no_execution_payload(bundle)

    evidence = adapt_classification_evidence(bundle, classify_policy)
    liquidity = adapt_liquidity_evidence(bundle)
    classification = classify(symbol, evidence, classify_policy)
    classification.liquidity = liquidity
    put_classification(
        symbol,
        classification,
        reason=RefreshReason.INITIAL,
        source_version=bundle.source_version,
        path=cache_path,
    )

    sleeve_rec = sleeves.get(symbol)
    if sleeve_rec is None:
        sleeve_rec = sleeves.assign(
            symbol=symbol,
            sleeve=proposed_sleeve,
            status=SleeveAssignmentStatus.PROPOSED,
            source_decision_id=str(uuid4()),
        )
    elif sleeve_rec.sleeve != proposed_sleeve:
        proposed_sleeve = sleeve_rec.sleeve

    fields = dict(thesis_fields or {})
    thesis = theses.create(
        symbol=symbol,
        sleeve=sleeve_rec.sleeve,
        status=ThesisStatus.ACTIVE if activate_thesis else ThesisStatus.DRAFT,
        decision=decision,
        expected_horizon=fields.get("expected_horizon"),
        thesis_summary=fields.get("thesis_summary"),
        bull_case=fields.get("bull_case") or fields.get("bull_thesis"),
        bear_case=fields.get("bear_case") or fields.get("bear_thesis"),
        catalysts=fields.get("catalysts"),
        risks=fields.get("risks"),
        invalidation_conditions=fields.get("invalidation_conditions") or fields.get("invalidation_criteria"),
        confidence=fields.get("confidence") or fields.get("confidence_level"),
        supporting_evidence_refs=fields.get("supporting_evidence_refs"),
    )
    if activate_thesis:
        theses.add_review(thesis.thesis_id, review_type="INVESTMENT_THESIS_REVIEW", session_id=context.trading_session_id)
        theses.add_review(thesis.thesis_id, review_type="RISK_REVIEW", session_id=context.trading_session_id)
        sleeves.assign(
            symbol=symbol,
            sleeve=sleeve_rec.sleeve,
            thesis_id=thesis.thesis_id,
            status=SleeveAssignmentStatus.ACTIVE,
            source_decision_id=thesis.thesis_id,
        )
        sleeve_rec = sleeves.get(symbol)

    ak = dict(action_kwargs or {})
    sector = classification.sector.value if classification.sector.value != "UNKNOWN" else ak.get("sector")
    action = ProposedAction(
        symbol=symbol.upper(),
        decision=decision,
        security_class=classification.effective_class_for_ceiling,
        classification_status=classification.status,
        sleeve=sleeve_rec.sleeve if sleeve_rec else proposed_sleeve,
        current_price=ak.get("current_price"),
        proposed_notional=proposed_notional,
        expected_resulting_position_pct=ak.get("expected_resulting_position_pct"),
        expected_resulting_sleeve_pct=ak.get("expected_resulting_sleeve_pct"),
        expected_resulting_sector_pct=ak.get("expected_resulting_sector_pct"),
        sector=None if sector == "UNKNOWN" else sector,
        thesis_id=thesis.thesis_id,
        investment_thesis_review_complete=activate_thesis,
        risk_review_complete=activate_thesis,
        enhanced_concentration_review_complete=bool(ak.get("enhanced_concentration_review_complete")),
        high_concentration_review_complete=bool(ak.get("high_concentration_review_complete")),
        sector_concentration_review_complete=bool(ak.get("sector_concentration_review_complete")),
        speculative_liquidity_review_complete=bool(ak.get("speculative_liquidity_review_complete")),
        liquidity=ak.get("liquidity")
        or LiquidityInputs(
            median_daily_dollar_volume_20d=ak.get("median_daily_dollar_volume_20d") or 1e12,
        ),
    )
    for key in (
        "enhanced_concentration_review_complete",
        "high_concentration_review_complete",
        "sector_concentration_review_complete",
        "speculative_liquidity_review_complete",
        "opportunistic_enhanced_risk_review_complete",
        "explicitly_risk_reducing",
        "add_justified_only_by_lower_price",
        "current_price",
        "liquidity",
        "correlation_with_book",
        "expected_resulting_position_pct",
        "expected_resulting_sleeve_pct",
        "expected_resulting_sector_pct",
        "sector",
    ):
        if key in ak and ak[key] is not None:
            setattr(action, key, ak[key])

    risk = evaluate(context, action, sleeves=sleeves, theses=theses)
    jp = append_risk_decision(risk, journal_path)
    if journal_path:
        append_jsonl(
            {
                "type": "paper_research_workflow",
                "symbol": symbol.upper(),
                "classification": to_dict(classification),
                "sleeve": (sleeve_rec.sleeve.value if sleeve_rec else proposed_sleeve.value),
                "thesis_id": thesis.thesis_id,
                "verdict": risk.verdict.value,
                "execution_attempted": False,
                "mcp_tools_used": list(CLASSIFICATION_HINT),
            },
            journal_path,
        )

    return PaperWorkflowResult(
        symbol=symbol.upper(),
        evidence=to_dict(evidence),
        classification=classification,
        sleeve=sleeve_rec.sleeve if sleeve_rec else proposed_sleeve,
        thesis=thesis,
        proposed_action=action,
        context=context,
        risk=risk,
        journal_path=jp,
        mcp_tools_used=list(CLASSIFICATION_HINT),
        execution_attempted=False,
    )


def _assert_no_execution_payload(bundle: RobinhoodSecurityBundle) -> None:
    blob = str(bundle)
    for tool in FORBIDDEN_MCP_TOOLS:
        if tool in blob:
            raise RuntimeError(f"paper workflow refused execution tool mention: {tool}")
