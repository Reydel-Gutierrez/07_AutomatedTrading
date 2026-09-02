"""Portfolio-level AI decision. Advisory. Risk Gate remains the authority."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from agentic_portfolio.ai.context import AIContext, overlay_broker_facts
from agentic_portfolio.ai.errors import AIError, BudgetDenied, BudgetExhausted
from agentic_portfolio.ai.gateway import AIGateway
from agentic_portfolio.ai.schemas import decision_from_payload
from agentic_portfolio.ai.types import (
    AIConfidence,
    DeepResearchResult,
    ModelRole,
    PortfolioDecisionResult,
    RecommendedAction,
)
from agentic_portfolio.schemas import PortfolioContext

DECISION_INSTRUCTIONS = """You are the portfolio decision reasoner.

Python has frozen the LIVE/PAPER account facts and a deep-research result.
You interpret. You do not trade.

Hard rules:
- Never rewrite NAV, cash, buying power, or positions.
- suggested_allocation_pct is percent of current NAV, not dollars.
- suggested_max_dollars is a cap hint; Python will clip it to cash/buying power.
- CASH is a valid alternative. Unused sleeve capacity is not a mandate to buy.
- action must be REJECT, WATCH, BUY_CANDIDATE, HOLD, REDUCE, or EXIT.
- BUY_CANDIDATE is a candidate for Risk Gate, not an order.
- Prefer WATCH/REJECT when evidence is thin. Do not invent a size just because cash exists.
"""


def _blocked(ctx: AIContext, reason: str) -> PortfolioDecisionResult:
    return PortfolioDecisionResult(
        ticker=ctx.ticker,
        action=RecommendedAction.REJECT,
        confidence=AIConfidence.LOW,
        rationale=reason,
        suggested_allocation_pct=None,
        suggested_max_dollars=None,
        reassessment_conditions=[],
        risk_notes=[reason],
        context_id=ctx.context_id,
        decision_id=str(uuid4()),
        runtime_mode=ctx.runtime_mode,
        rejection_reason=reason,
        operational_failure=True,
    )


def decide_candidate(
    gateway: AIGateway,
    ctx: AIContext,
    portfolio: PortfolioContext,
    research: DeepResearchResult,
    *,
    persist=None,
    now: datetime | None = None,
    critical: bool = False,
) -> PortfolioDecisionResult:
    stamp = now or datetime.now(timezone.utc)
    blob = overlay_broker_facts(
        {
            "context": ctx.to_prompt_dict(),
            "research": research.__dict__,
        },
        portfolio,
    )
    messages = [
        {"role": "system", "content": DECISION_INSTRUCTIONS},
        {"role": "user", "content": json.dumps(blob, default=str)},
    ]
    purpose = "portfolio_reassessment" if critical else "portfolio_decision"
    try:
        result = gateway.complete_structured(
            role=ModelRole.RESEARCH,
            purpose=purpose,
            schema_name="portfolio_decision",
            messages=messages,
            ticker=ctx.ticker,
            critical=critical,
        )
    except (BudgetDenied, BudgetExhausted, AIError) as exc:
        return _blocked(ctx, str(exc))
    row = decision_from_payload(
        result.payload,
        provider=result.provider,
        model=result.model,
        cost=result.actual_cost,
        context_id=ctx.context_id,
        decision_id=str(uuid4()),
        runtime_mode=ctx.runtime_mode,
    )
    if row.ticker != ctx.ticker:
        row.ticker = ctx.ticker
        row.rejection_reason = "ticker_mismatch_overlaid"
    if persist is not None and not row.operational_failure:
        persist.save_decision(
            row.decision_id,
            {**row.__dict__, "created_at": stamp.isoformat(), "research_id": research.research_id},
        )
    return row
