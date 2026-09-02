"""Provider-agnostic reasoner for thesis formation and portfolio decision."""

from __future__ import annotations

from dataclasses import is_dataclass
from enum import Enum
from typing import Any, Callable, Protocol

from agentic_portfolio.decision.types import DecisionReasoningRequest
from agentic_portfolio.schemas import to_dict

REASONER_INSTRUCTIONS = """You are the Investment Thesis + Portfolio Decision reasoner.

Python has frozen ResearchReports, PortfolioContext, existing theses/holdings, and policy ceilings.
You interpret. You do not collect new observed facts. You do not trade.

Hard rules:
- Never invent missing observed facts.
- Never override security classification.
- Never alter NAV, cash, positions, holdings counts, or risk limits.
- Never emit broker stop/limit orders or call execution tools.
- Never mark a thesis ACTIVE. Python forces DRAFT until a future real execution.
- CASH and SPY are valid alternatives. Unused sleeve capacity is not a mandate to buy.
- NO_ACTION is always valid.
- Do not use universal rules such as P/E < 20 = buy, revenue growth > 20% = good, or RSI cutoffs.
- Compare each candidate to cash, SPY, and the other researched names in this packet.
- Define why a position should exist and what would invalidate the thesis.
- Propose desired allocation as percent of current NAV (not dollars). Size from conviction; never size up only because unused ceiling remains.
- BUY/ADD only if research_conclusion is ADVANCE_TO_THESIS and research_status is RESEARCH_COMPLETE.
- KEEP_WATCHING research may be WATCH, REJECT, or NO_ACTION — not BUY/ADD.
- Every researched ADVANCE_TO_THESIS symbol MUST appear exactly once in decisions[]. CASH and SPY rows may coexist. Preferring cash is a named NO_ACTION or WATCH on that symbol. A CASH-only, SPY-only, or CASH+SPY payload that omits the researched ticker is malformed.

Exit policy (no broker stop orders):
- CORE_GROWTH: thesis-based; mandatory_fixed_stop_loss must be false.
- OPPORTUNISTIC: thesis-based; optional price/event invalidation.
- TACTICAL: BUY/ADD requires predefined price_invalidation or technical_invalidation.
- SPECULATIVE: BUY/ADD requires predefined risk_invalidation.

Return JSON only:
{
  "theses": [
    {
      "symbol": "NVDA",
      "research_id": "...",
      "sleeve": "CORE_GROWTH|OPPORTUNISTIC|TACTICAL|SPECULATIVE",
      "thesis_summary": "...",
      "bull_case": "...",
      "base_case": "...",
      "bear_case": "...",
      "catalysts": ["..."],
      "risks": ["..."],
      "horizon": "...",
      "invalidation_conditions": ["..."],
      "review_triggers": ["..."],
      "why_position_should_exist": "...",
      "confidence": "LOW|MEDIUM|HIGH",
      "exit_policy": {
        "thesis_based": true,
        "mandatory_fixed_stop_loss": false,
        "price_invalidation": null,
        "event_invalidation": null,
        "technical_invalidation": null,
        "risk_invalidation": null,
        "broker_stop_orders_created": false,
        "notes": null
      }
    }
  ],
  "comparison": {
    "ranking": ["NVDA", "CASH", "SPY", "..."],
    "vs_cash": "...",
    "vs_spy": "...",
    "notes": "..."
  },
  "decisions": [
    {
      "symbol": "NVDA",
      "decision": "BUY|ADD|HOLD|REDUCE|SELL|WATCH|REJECT|NO_ACTION",
      "desired_allocation_pct": 5.0,
      "rationale": "...",
      "why_preferable_to_cash": "...",
      "why_preferable_to_spy": "...",
      "why_preferable_to_alternatives": "..."
    },
    {
      "symbol": "CASH",
      "decision": "HOLD",
      "desired_allocation_pct": 95.0,
      "rationale": "..."
    }
  ]
}
"""


class DecisionReasoner(Protocol):
    def reason(self, request: DecisionReasoningRequest) -> dict[str, Any]: ...


class CallableDecisionReasoner:
    def __init__(self, complete: Callable[[str], dict[str, Any]]) -> None:
        self.complete = complete

    def reason(self, request: DecisionReasoningRequest) -> dict[str, Any]:
        payload = self.complete(build_reasoning_prompt(request))
        if not isinstance(payload, dict):
            raise TypeError("DecisionReasoner complete() must return a dict")
        return payload


class ScriptedDecisionReasoner:
    """Deterministic reasoner for tests and paper pilots."""

    def __init__(self, response: dict[str, Any] | Callable[[DecisionReasoningRequest], dict[str, Any]]) -> None:
        self._response = response

    def reason(self, request: DecisionReasoningRequest) -> dict[str, Any]:
        if callable(self._response):
            return self._response(request)
        return dict(self._response)


def build_reasoning_prompt(request: DecisionReasoningRequest) -> str:
    return (
        REASONER_INSTRUCTIONS
        + "\n\nPACKET:\n"
        + _dump(request.packet)
        + "\n\nRESEARCH BRIEFS:\n"
        + _dump(request.reports)
        + "\n\nPORTFOLIO CONTEXT (facts; do not rewrite):\n"
        + _dump(request.portfolio_context)
        + "\n\nEXISTING THESES:\n"
        + _dump(request.existing_theses)
        + "\n\nPOLICY CONTEXT:\n"
        + _dump(request.policy_context)
        + "\n\nALTERNATIVES:\n"
        + _dump(request.alternatives)
    )


def _dump(obj: Any) -> str:
    import json

    if is_dataclass(obj) and not isinstance(obj, type):
        obj = to_dict(obj)
    elif isinstance(obj, Enum):
        obj = obj.value
    return json.dumps(obj, default=str, indent=2)
