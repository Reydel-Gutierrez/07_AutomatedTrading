"""AI Gateway adapters for the existing Research and Decision reasoner protocols.

The engines stay unchanged. These wrappers are the only production path that
may call a model, and they always go through AIGateway.
"""

from __future__ import annotations

import logging
from typing import Any

from agentic_portfolio.ai.config import committee_output_token_limits
from agentic_portfolio.ai.errors import AIError, BudgetDenied, BudgetExhausted, MalformedResponse, SchemaViolation, is_incomplete_max_output_tokens
from agentic_portfolio.ai.gateway import AIGateway
from agentic_portfolio.ai.schemas import COMMITTEE_DECISION_SCHEMA, RESEARCH_REPORT_SCHEMA, THESIS_DECISION_SCHEMA
from agentic_portfolio.ai.types import GatewayResult, ModelRole
from agentic_portfolio.decision.reasoner import (
    COMMITTEE_CONCISE_RETRY_INSTRUCTION,
    DecisionReasoner,
    build_reasoning_prompt as build_decision_prompt,
    request_is_committee,
)
from agentic_portfolio.decision.types import DecisionReasoningRequest
from agentic_portfolio.research.operational import payload_needs_schema_retry
from agentic_portfolio.research.reasoner import CONCISE_RETRY_INSTRUCTION, ResearchReasoner, build_reasoning_prompt
from agentic_portfolio.research.types import ResearchReasoningRequest
from agentic_portfolio.decision.validate import DecisionValidationError
from agentic_portfolio.research.validate import ResearchValidationError

log = logging.getLogger(__name__)


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
        self.call_count = 0
        self.truncation_retry_used = False

    def reason(self, request: DecisionReasoningRequest) -> dict[str, Any]:
        prompt = build_decision_prompt(request)
        ticker = None
        for item in request.reports or []:
            if isinstance(item, dict) and item.get("symbol"):
                ticker = str(item["symbol"])
                break
        committee = request_is_committee(request)
        system = request.instructions or "Return JSON only."
        self.call_count = 0
        self.truncation_retry_used = False
        self.last_result = None
        first_max, retry_max = committee_output_token_limits(self.gateway.config) if committee else (None, None)
        try:
            result = self._complete(system, prompt, ticker, committee=committee, max_output_tokens=first_max)
        except MalformedResponse as exc:
            if not (committee and is_incomplete_max_output_tokens(exc)):
                raise DecisionValidationError(f"AI decision call failed: {exc}") from exc
            log.warning(
                "committee portfolio_decision truncated (max_output_tokens); retrying once with %s output tokens",
                retry_max,
            )
            self.truncation_retry_used = True
            retry_system = system.rstrip() + "\n\n" + COMMITTEE_CONCISE_RETRY_INSTRUCTION
            try:
                result = self._complete(
                    retry_system,
                    prompt,
                    ticker,
                    committee=committee,
                    max_output_tokens=retry_max,
                )
            except (BudgetDenied, BudgetExhausted) as budget_exc:
                raise DecisionValidationError(f"AI budget blocked decision: {budget_exc}") from budget_exc
            except MalformedResponse as retry_exc:
                if is_incomplete_max_output_tokens(retry_exc):
                    log.warning(
                        "committee portfolio_decision retry still incomplete (max_output_tokens); failing closed"
                    )
                raise DecisionValidationError(f"AI decision call failed: {retry_exc}") from retry_exc
            except AIError as retry_exc:
                raise DecisionValidationError(f"AI decision call failed: {retry_exc}") from retry_exc
        except (BudgetDenied, BudgetExhausted) as exc:
            raise DecisionValidationError(f"AI budget blocked decision: {exc}") from exc
        except AIError as exc:
            raise DecisionValidationError(f"AI decision call failed: {exc}") from exc
        self.last_result = result
        return dict(result.payload)

    def _complete(
        self,
        system: str,
        prompt: str,
        ticker: str | None,
        *,
        committee: bool,
        max_output_tokens: int | None,
    ) -> GatewayResult:
        self.call_count += 1
        kwargs: dict[str, Any] = {}
        if max_output_tokens is not None:
            kwargs["max_output_tokens"] = int(max_output_tokens)
        return self.gateway.complete_structured(
            role=self.role,
            purpose="portfolio_decision",
            schema_name="committee_decision" if committee else "thesis_decision",
            schema=COMMITTEE_DECISION_SCHEMA if committee else THESIS_DECISION_SCHEMA,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            ticker=ticker,
            critical=False,
            **kwargs,
        )


# Protocol satisfaction for type checkers.
def _research_protocol(reasoner: GatewayResearchReasoner) -> ResearchReasoner:
    return reasoner


def _decision_protocol(reasoner: GatewayDecisionReasoner) -> DecisionReasoner:
    return reasoner
