"""Primary AI deep research. Advisory. Never a BUY ProposedAction by itself."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from agentic_portfolio.ai.context import AIContext, overlay_broker_facts
from agentic_portfolio.ai.errors import AIError, BudgetDenied, BudgetExhausted
from agentic_portfolio.ai.gateway import AIGateway
from agentic_portfolio.ai.schemas import research_from_payload
from agentic_portfolio.ai.types import AIConfidence, DeepResearchResult, ModelRole, RecommendedAction
from agentic_portfolio.schemas import PortfolioContext

RESEARCH_INSTRUCTIONS = """You are the deep-research reasoner for an agentic portfolio.

Python gathered the facts in the context object. Interpret those facts.
Hard rules:
- Never invent missing observed facts.
- Never override security classification.
- Never alter NAV, cash, positions, or risk limits.
- Never place or cancel an order.
- recommended_action must be one of REJECT, WATCH, BUY_CANDIDATE, HOLD, REDUCE, EXIT.
- BUY_CANDIDATE means this name may be considered by portfolio decision + Risk Gate. It is not permission to trade.
- If evidence is thin, prefer WATCH or REJECT and LOW confidence.
"""


def _blocked(ctx: AIContext, reason: str) -> DeepResearchResult:
    return DeepResearchResult(
        ticker=ctx.ticker,
        thesis="",
        bull_case="",
        bear_case="",
        catalysts=[],
        risks=["ai_unavailable_or_budget"],
        valuation_observations="",
        technical_observations="",
        confidence=AIConfidence.LOW,
        recommended_action=RecommendedAction.REJECT,
        context_id=ctx.context_id,
        research_id=str(uuid4()),
        runtime_mode=ctx.runtime_mode,
        rejection_reason=reason,
        operational_failure=True,
    )


def research_candidate(
    gateway: AIGateway,
    ctx: AIContext,
    portfolio: PortfolioContext,
    *,
    persist=None,
    now: datetime | None = None,
    escalate: bool = False,
) -> DeepResearchResult:
    stamp = now or datetime.now(timezone.utc)
    user = overlay_broker_facts(ctx.to_prompt_dict(), portfolio)
    messages = [
        {"role": "system", "content": RESEARCH_INSTRUCTIONS},
        {"role": "user", "content": json.dumps(user, default=str)},
    ]
    role = ModelRole.ESCALATION if escalate else ModelRole.RESEARCH
    try:
        result = gateway.complete_structured(
            role=role,
            purpose="deep_research",
            schema_name="deep_research",
            messages=messages,
            ticker=ctx.ticker,
            critical=False,
        )
    except (BudgetDenied, BudgetExhausted, AIError) as exc:
        return _blocked(ctx, str(exc))
    row = research_from_payload(
        result.payload,
        provider=result.provider,
        model=result.model,
        cost=result.actual_cost,
        context_id=ctx.context_id,
        research_id=str(uuid4()),
        runtime_mode=ctx.runtime_mode,
    )
    if row.ticker != ctx.ticker:
        row.ticker = ctx.ticker
        row.rejection_reason = "ticker_mismatch_overlaid"
    if persist is not None and not row.operational_failure:
        persist.save_research(
            row.research_id,
            {**row.__dict__, "created_at": stamp.isoformat(), "context_id": ctx.context_id},
        )
    return row
