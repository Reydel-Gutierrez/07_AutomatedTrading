"""Provider-agnostic reasoner for thesis reassessment on monitored positions."""

from __future__ import annotations

from dataclasses import is_dataclass
from enum import Enum
from typing import Any, Callable, Protocol

from agentic_portfolio.monitoring.types import MonitoringReasoningRequest
from agentic_portfolio.schemas import to_dict

REASONER_INSTRUCTIONS = """You are the Position Monitoring + Thesis Reassessment reasoner.

Python has frozen monitoring facts, triggers, the existing thesis, research brief, and portfolio/risk state.
You interpret new evidence. You do not collect facts. You do not trade. You do not create broker stop orders.

Hard rules:
- Never invent missing observed facts.
- Never override security classification.
- Never alter NAV, cash, positions, holdings counts, or risk limits.
- Never emit broker stop/limit orders or call execution tools.
- An exit condition is not a broker stop order.
- Price movement alone must not invalidate CORE_GROWTH. Core uses thesis/fundamental invalidation.
- OPPORTUNISTIC: judge recovery thesis vs structural deterioration (LIKELY_DISLOCATION vs LIKELY_DETERIORATION).
- TACTICAL: evaluate the predefined price/technical invalidation if Python detected it.
- SPECULATIVE: evaluate the predefined risk/catalyst invalidation if Python detected it.
- NO_ACTION is always valid. HOLD is valid when the thesis still stands.
- Recommended action must be HOLD, ADD, REDUCE, SELL, or NO_ACTION.
- ADD still requires later Portfolio Decision + Risk Gate. Do not treat monitoring as permission to trade.

Return JSON only:
{
  "symbol": "NVDA",
  "thesis_status": "UNCHANGED|STRENGTHENED|WEAKENED|INVALIDATED",
  "monitoring_state": "HEALTHY|REVIEW_REQUIRED|RESEARCH_REFRESH_REQUIRED|THESIS_WEAKENED|THESIS_INVALIDATED|EXIT_CONDITION_TRIGGERED",
  "recommended_action": "HOLD|ADD|REDUCE|SELL|NO_ACTION",
  "desired_allocation_pct": 5.0,
  "rationale": "...",
  "opportunistic_verdict": "LIKELY_DISLOCATION|MIXED|LIKELY_DETERIORATION|INSUFFICIENT_EVIDENCE"|null,
  "tactical_invalidation_detected": false,
  "speculative_invalidation_detected": false,
  "exit_condition_triggered": false,
  "research_refresh_needed": false,
  "broker_stop_orders_created": false
}
"""


class MonitoringReasoner(Protocol):
    def reason(self, request: MonitoringReasoningRequest) -> dict[str, Any]: ...


class CallableMonitoringReasoner:
    def __init__(self, complete: Callable[[str], dict[str, Any]]) -> None:
        self.complete = complete

    def reason(self, request: MonitoringReasoningRequest) -> dict[str, Any]:
        payload = self.complete(build_reasoning_prompt(request))
        if not isinstance(payload, dict):
            raise TypeError("MonitoringReasoner complete() must return a dict")
        return payload


class ScriptedMonitoringReasoner:
    """Deterministic reasoner for tests and paper pilots."""

    def __init__(self, response: dict[str, Any] | Callable[[MonitoringReasoningRequest], dict[str, Any]]) -> None:
        self._response = response

    def reason(self, request: MonitoringReasoningRequest) -> dict[str, Any]:
        if callable(self._response):
            return self._response(request)
        return dict(self._response)


def build_reasoning_prompt(request: MonitoringReasoningRequest) -> str:
    return (
        REASONER_INSTRUCTIONS
        + "\n\nPACKET:\n"
        + _dump(request.packet)
        + "\n\nFACTS:\n"
        + _dump(request.facts)
        + "\n\nTRIGGERS:\n"
        + _dump(request.triggers)
        + "\n\nTHESIS:\n"
        + _dump(request.thesis)
        + "\n\nRESEARCH BRIEF:\n"
        + _dump(request.research_brief)
        + "\n\nPORTFOLIO CONTEXT (facts; do not rewrite):\n"
        + _dump(request.portfolio_context)
        + "\n\nPOLICY CONTEXT:\n"
        + _dump(request.policy_context)
    )


def _dump(obj: Any) -> str:
    import json

    if is_dataclass(obj) and not isinstance(obj, type):
        obj = to_dict(obj)
    elif isinstance(obj, Enum):
        obj = obj.value
    return json.dumps(obj, default=str, indent=2)
