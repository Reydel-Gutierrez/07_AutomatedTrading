"""AI Gateway adapters for the existing Research and Decision reasoner protocols.

The engines stay unchanged. These wrappers are the only production path that
may call a model, and they always go through AIGateway.
"""

from __future__ import annotations

from typing import Any

from agentic_portfolio.ai.errors import AIError, BudgetDenied, BudgetExhausted, MalformedResponse, SchemaViolation, is_incomplete_max_output_tokens
from agentic_portfolio.ai.gateway import AIGateway
from agentic_portfolio.ai.schemas import RESEARCH_REPORT_SCHEMA, THESIS_DECISION_SCHEMA
from agentic_portfolio.ai.types import GatewayResult, ModelRole
from agentic_portfolio.decision.reasoner import DecisionReasoner, build_reasoning_prompt as build_decision_prompt
from agentic_portfolio.decision.types import DecisionReasoningRequest
from agentic_portfolio.research.operational import payload_needs_schema_retry
from agentic_portfolio.research.reasoner import CONCISE_RETRY_INSTRUCTION, ResearchReasoner, build_reasoning_prompt
from agentic_portfolio.research.types import ResearchReasoningRequest
from agentic_portfolio.decision.validate import DecisionValidationError
from agentic_portfolio.research.validate import ResearchValidationError


class GatewayResearchReasoner:
    """ResearchReasoner backed by the production AI Gateway."""

    def __init__(self, gateway: AIGateway, *, role: ModelRole | str = ModelRole.RESEARCH) -> None:
        self.gateway = gateway
        self.role = role
        self.last_result = None
        self.truncation_retry_used = False

    def reason(self, request: ResearchReasoningRequest) -> dict[str, Any]:
        self.truncation_retry_used = False
        prompt = build_reasoning_prompt(request)
        ticker = str((request.candidate or {}).get("symbol") or "") or None
        system = request.instructions or "Return JSON only."
        try:
            result = self._complete(system, prompt, ticker)
        except MalformedResponse as exc:
            if not is_incomplete_max_output_tokens(exc):
                raise
            result = self._retry(system, prompt, ticker)
        except SchemaViolation:
            result = self._retry(system, prompt, ticker)
        payload = dict(result.payload)
        if payload_needs_schema_retry(payload):
            result = self._retry(system, prompt, ticker)
            payload = dict(result.payload)
        self.last_result = result
        payload.setdefault("provider", result.provider)
        payload.setdefault("model", result.model)
        payload.setdefault("ai_call_id", result.reservation_id)
        payload.setdefault("estimated_cost", float(result.estimated_cost))
        payload.setdefault("actual_cost", float(result.actual_cost))
        payload.setdefault("research_source", "scripted" if result.provider == "scripted" else "AI")
        return payload

    def _retry(self, system: str, prompt: str, ticker: str | None) -> GatewayResult:
        retry_system = system.rstrip() + "\n\n" + CONCISE_RETRY_INSTRUCTION
        self.truncation_retry_used = True
        return self._complete(retry_system, prompt, ticker)

    def _complete(self, system: str, prompt: str, ticker: str | None) -> GatewayResult:
        return self.gateway.complete_structured(
            role=self.role,
            purpose="deep_research",
            schema_name="research_report",
            schema=RESEARCH_REPORT_SCHEMA,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            ticker=ticker,
            critical=False,
        )


class GatewayDecisionReasoner:
    """DecisionReasoner backed by the production AI Gateway."""

    def __init__(self, gateway: AIGateway, *, role: ModelRole | str = ModelRole.RESEARCH) -> None:
        self.gateway = gateway
        self.role = role
        self.last_result = None

    def reason(self, request: DecisionReasoningRequest) -> dict[str, Any]:
        prompt = build_decision_prompt(request)
        ticker = None
        for item in request.reports or []:
            if isinstance(item, dict) and item.get("symbol"):
                ticker = str(item["symbol"])
                break
        try:
            result = self.gateway.complete_structured(
                role=self.role,
                purpose="portfolio_decision",
                schema_name="thesis_decision",
                schema=THESIS_DECISION_SCHEMA,
                messages=[
                    {"role": "system", "content": request.instructions or "Return JSON only."},
                    {"role": "user", "content": prompt},
                ],
                ticker=ticker,
                critical=False,
            )
        except (BudgetDenied, BudgetExhausted) as exc:
            raise DecisionValidationError(f"AI budget blocked decision: {exc}") from exc
        except AIError as exc:
            raise DecisionValidationError(f"AI decision call failed: {exc}") from exc
        self.last_result = result
        return dict(result.payload)


# Protocol satisfaction for type checkers.
def _research_protocol(reasoner: GatewayResearchReasoner) -> ResearchReasoner:
    return reasoner


def _decision_protocol(reasoner: GatewayDecisionReasoner) -> DecisionReasoner:
    return reasoner
