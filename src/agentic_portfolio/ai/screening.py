"""Cheap AI screening. Facts come from Python; the model only scores/classifies."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from agentic_portfolio.ai.context import AIContext
from agentic_portfolio.ai.errors import AIError, BudgetDenied, BudgetExhausted
from agentic_portfolio.ai.gateway import AIGateway
from agentic_portfolio.ai.schemas import screening_from_payload
from agentic_portfolio.ai.types import AIConfidence, ModelRole, ScreeningResult

SCREEN_INSTRUCTIONS = """You screen one ticker for whether it deserves expensive deep research.

Python already gathered the facts. Interpret only those facts.
Never invent prices, NAV, cash, or fundamentals that are not in the context.
Never recommend placing an order. This is a screen, not a trade.
classification is a short qualitative label (for example QUALITY_GROWTH, DISLOCATION, TACTICAL, SPECULATIVE, AVOID) — not a security-class override.
score is 0-100.
worth_deep_research should be true only if the facts justify spending more of a $10/month research budget.
"""


def _blocked(ctx: AIContext, reason: str, classification: str, flags: list[str]) -> ScreeningResult:
    return ScreeningResult(
        ticker=ctx.ticker,
        score=0.0,
        classification=classification,
        catalyst_summary="",
        risk_flags=flags,
        worth_deep_research=False,
        confidence=AIConfidence.LOW,
        context_id=ctx.context_id,
        screening_id=str(uuid4()),
        runtime_mode=ctx.runtime_mode,
        rejection_reason=reason,
    )


def screen_candidate(
    gateway: AIGateway,
    ctx: AIContext,
    *,
    persist=None,
    now: datetime | None = None,
) -> ScreeningResult:
    stamp = now or datetime.now(timezone.utc)
    messages = [
        {"role": "system", "content": SCREEN_INSTRUCTIONS},
        {"role": "user", "content": json.dumps(ctx.to_prompt_dict(), default=str)},
    ]
    try:
        result = gateway.complete_structured(
            role=ModelRole.SCREENING,
            purpose="candidate_screening",
            schema_name="screening",
            messages=messages,
            ticker=ctx.ticker,
        )
    except (BudgetDenied, BudgetExhausted) as exc:
        return _blocked(ctx, str(exc), "BUDGET_BLOCKED", ["ai_budget_blocked"])
    except AIError as exc:
        return _blocked(ctx, str(exc), "AI_UNAVAILABLE", ["ai_unavailable"])
    row = screening_from_payload(
        result.payload,
        provider=result.provider,
        model=result.model,
        cost=result.actual_cost,
        context_id=ctx.context_id,
        screening_id=str(uuid4()),
        runtime_mode=ctx.runtime_mode,
    )
    if row.ticker != ctx.ticker:
        row.ticker = ctx.ticker
        row.rejection_reason = "ticker_mismatch_overlaid"
    if persist is not None:
        persist.save_screening(
            row.screening_id,
            {**row.__dict__, "created_at": stamp.isoformat(), "context_id": ctx.context_id},
        )
    return row
