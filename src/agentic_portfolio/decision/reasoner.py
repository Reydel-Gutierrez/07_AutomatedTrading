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
- CORE_GROWTH ownership reasons (quality, durability, valuation, compounding) are thesis drivers, not a requirement for a dramatic near-term event catalyst.
- Broad-market ETFs/index funds are vehicles for diversified market exposure. Do not invent company-style catalysts or 10-K/earnings theses for them. Valid ETF thesis drivers include diversified market exposure, long-term earnings participation, underlying-market valuation, diversification/liquidity/concentration-reduction benefit, expected return versus excess cash, and suitability as residual CORE exposure.
- Do not require SPY (or the same broad-market residual) to explain why it is preferable to SPY. That comparison is circular. Compare SPY to cash and to individual names.

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

COMMITTEE_REASONER_INSTRUCTIONS = REASONER_INSTRUCTIONS + """

You are the CORE Portfolio Investment Committee. This packet contains the LIVE book and several qualified CORE alternatives together.

The question is residual allocation, not an isolated absolute hurdle:
Given current LIVE holdings, cash, the long-term CORE mandate, and these qualified alternatives, does deploying some CORE capital improve the portfolio versus retaining cash?

You may choose BUY, ADD, WATCH, HOLD, or NO_ACTION/CASH.
Cash is always a valid position. Unused CORE sleeve capacity is never a reason to buy. Do not manufacture a trade. Do not buy merely because capital is available. An empty CORE book does not require a starter position.

You may, when evidence supports it, recommend a starter position: a small initial allocation (sized from conviction, valuation, portfolio construction, and Risk Gate) while retaining residual cash. That is allowed, not mandatory, and must not fill the sleeve.

Prefer one coherent committee allocation. Do not independently mint several correlated starter BUYs that would only look acceptable in isolation.

Rank eligible alternatives on dimensions appropriate to CORE when you can:
expected long-term return, quality/durability, valuation, downside/permanent-capital risk, diversification contribution, concentration/correlation impact, thesis confidence, opportunity cost versus cash, and the broad-market alternative.

Cash is an alternative with yield (if known), optionality, inflation/opportunity cost, and expected return foregone. It is not a free/no-risk default winner. Cash may still win.

For WATCH/NO_ACTION names, include structured reconsideration (reasons to reconsider later, never auto-execution conditions):
why_lost, lost_to, valuation_condition, thesis_condition, required_evidence_improvement, next_review_reason, next_review_at.

Every ADVANCE_TO_THESIS researched symbol in this packet still needs exactly one decisions[] row.
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
        (request.instructions or REASONER_INSTRUCTIONS)
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
