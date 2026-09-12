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

COMMITTEE_REASONER_INSTRUCTIONS = """You are the CORE Portfolio Investment Committee.

Python has frozen ResearchReports, PortfolioContext, existing theses/holdings, and policy ceilings.
You interpret. You do not collect new observed facts. You do not trade.

This packet contains the LIVE book, cash, diversified broad-market CORE exposure, and qualified researched individual names.
Answer this question:
Given the current portfolio, current cash yield, risk regime, diversified broad-market alternative, qualified researched individual names, and long-term CORE mandate, what allocation has the best expected risk-adjusted outcome?

Choose target weights across CASH, the configured broad-market CORE vehicle (policy_context.broad_market_residual.symbol), and optional selected individual names. Weights should form one coherent portfolio allocation. Do not require every dollar to be deployed. Do not force a minimum equity allocation.

Distinguish two jobs:
A. Strategic CORE allocation — diversified market exposure via the configured residual vehicle. This is a portfolio construction choice, not a company underwriting.
B. Individual-name alpha selection — concentrated ownership that must beat both cash and diversified CORE exposure.

Hard rules:
- Never invent missing observed facts.
- Never override security classification.
- Never alter NAV, cash, positions, holdings counts, or risk limits.
- Never emit broker stop/limit orders or call execution tools.
- Never mark a thesis ACTIVE. Python forces DRAFT until a future real execution.
- CASH and the configured broad-market residual are valid allocations. Unused sleeve capacity is not a mandate to buy.
- Cash is one allocation, not the default state. NO_ACTION / HOLD_CASH is allowed only when supported by the comparative portfolio analysis. Do not manufacture a trade. Do not buy merely because capital is available.
- An empty long-term CORE book should explicitly consider establishing strategic CORE market exposure. It does not require a starter position, and it does not make indefinite 100% cash automatic.
- Do not use universal rules such as P/E < 20 = buy, revenue growth > 20% = good, or RSI cutoffs.
- Individual-name BUY/ADD only if research_conclusion is ADVANCE_TO_THESIS and research_status is RESEARCH_COMPLETE.
- KEEP_WATCHING research may be WATCH, REJECT, or NO_ACTION — not BUY/ADD.
- Broad-market CORE exposure uses the strategic residual path. It must NOT require company-style evidence (moat, 10-K thesis, single-name earnings catalyst, balance-sheet underwriting, company cash-flow durability). Missing perfect company-level evidence is NOT a reason to reject diversified market exposure.
- Valid residual-vehicle drivers: diversified equity exposure, long-term earnings participation, market valuation context, diversification, liquidity, concentration reduction, expected return versus 0%-yield cash, portfolio role, and current risk regime.
- Prefer one coherent committee allocation. Do not independently mint several correlated starter BUYs that would only look acceptable in isolation.
- A starter position is allowed, not mandatory, and must not fill the sleeve. Size from conviction, valuation, construction, and Risk Gate.
- CORE_GROWTH ownership reasons (quality, durability, valuation, compounding) are thesis drivers, not a requirement for a dramatic near-term event catalyst.
- Broad-market ETFs/index funds are vehicles for diversified market exposure. Do not invent company-style catalysts or 10-K/earnings theses for them.
- Do not require the configured residual (usually SPY) to explain why it is preferable to itself. Compare it to cash and to individual names.
- Cash has yield (if known), optionality, inflation/opportunity cost, and expected return foregone. It is not a free/no-risk default winner. Cash may still win 100%.
- policy_context.cash_alternative.current_yield is the brokerage CASH position yield, not a Treasury or risk-free proxy. 0% is a known yield when yield_known is true (liquid/optional, earns nothing). Use current_yield only when yield_known is true. If yield_known is false (unavailable or stale), treat the cash rate as unknown; do not invent a yield. Unknown yield is not a reason to abort the committee. Do not treat 0% cash as a mandate to deploy.
- SPY / residual comparison evidence is policy_context.broad_market_residual / spy_alternative (and portfolio_facts.spy). Missing optional fields are unknown, not zero. When strategic_residual_eligible is true, the committee MAY propose BUY/ADD of that configured residual vehicle without an individual-stock ADVANCE_TO_THESIS report. Do not generalize that path to arbitrary tickers.
- If recommending 100% cash or effectively no equity, give an affirmative justification addressing: why diversified market exposure is currently inferior to cash; what evidence supports waiting; the opportunity cost of 0%-yield cash; whether the reason is valuation, risk regime, portfolio risk, missing evidence, or another concrete portfolio reason; and what would trigger reconsideration. "Optionality" by itself is not sufficient justification for indefinite 100% cash.

Exit policy (no broker stop orders) applies only to selected BUY/ADD names:
- CORE_GROWTH: thesis-based; mandatory_fixed_stop_loss must be false.
- OPPORTUNISTIC: thesis-based; optional price/event invalidation.
- TACTICAL: BUY/ADD requires predefined price_invalidation or technical_invalidation.
- SPECULATIVE: BUY/ADD requires predefined risk_invalidation.

Output must be compact. Do NOT emit a full thesis or verbose decision object for names that lost the comparison.
Require detailed thesis/proposal material ONLY for actionable selected BUY/ADD names.
Rejected/WATCH alternatives get one ranking row: rank, action, score/confidence, and a concise reason.

Brevity (mandatory; the structured response must complete within the output-token budget):
- rankings[].concise_reason: <= 40 words
- comparison.vs_cash / vs_spy / notes: <= 60 words each
- cash.rationale: <= 60 words
- selected_allocations string fields: <= 80 words each
- ranking lists and selected thesis lists: maximum 5 entries each
- Do not repeat the same argument across ranking, comparison, and selected fields
- Every eligible researched symbol MUST appear exactly once in rankings[]
- CASH and SPY MUST appear in rankings[]
- selected_allocations may be empty when cash wins, but then cash.why_cash_beats_broad_market and cash.reconsideration_trigger are required
- Prefer at most one selected individual-name BUY/ADD unless evidence strongly supports a second uncorrelated name
- The configured residual vehicle may be selected without an individual-stock ADVANCE_TO_THESIS report when policy_context.strategic_core_allocation.eligible is true

Return JSON only:
{
  "portfolio_action": "ALLOCATE|HOLD_CASH",
  "rankings": [
    {
      "symbol": "MSFT",
      "rank": 1,
      "action": "BUY|ADD|WATCH|REJECT|HOLD|NO_ACTION",
      "score": 0.72,
      "confidence": "LOW|MEDIUM|HIGH",
      "concise_reason": "...",
      "why_lost": null,
      "lost_to": [],
      "valuation_condition": null,
      "thesis_condition": null,
      "required_evidence_improvement": null,
      "next_review_reason": null,
      "next_review_at": null
    }
  ],
  "selected_allocations": [
    {
      "symbol": "MSFT",
      "action": "BUY",
      "target_weight": 5.0,
      "starter_position": true,
      "rationale": "...",
      "why_vs_cash": "...",
      "why_vs_spy": "...",
      "why_vs_alternatives": "...",
      "sleeve": "CORE_GROWTH",
      "thesis_summary": "...",
      "bull_case": "...",
      "base_case": "...",
      "bear_case": "...",
      "catalysts": [],
      "thesis_drivers": ["..."],
      "risks": ["..."],
      "horizon": "...",
      "invalidation_conditions": ["..."],
      "review_triggers": ["..."],
      "why_position_should_exist": "...",
      "confidence": "MEDIUM",
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
  "cash": {
    "target_weight": 95.0,
    "rationale": "...",
    "action": "HOLD",
    "why_cash_beats_broad_market": "...required when cash is ~100% or no equity is selected...",
    "justification_kind": "valuation|risk_regime|portfolio_risk|missing_evidence|other|null",
    "reconsideration_trigger": "...what would cause a new comparison..."
  },
  "comparison": {"vs_cash": "...", "vs_spy": "...", "notes": "..."}
}
"""

COMMITTEE_CONCISE_RETRY_INSTRUCTION = (
    "AGGRESSIVE CONCISENESS REQUIRED. A prior attempt was discarded because it hit "
    "max_output_tokens and must not be used as a committee decision. Return COMPLETE "
    "compact committee JSON that fits the output-token budget. Rankings stay short. "
    "Full thesis fields belong ONLY in selected_allocations for BUY/ADD names. "
    "WATCH/REJECT/NO_ACTION names get ranking rows only — no duplicated thesis. "
    "Do not truncate JSON. Do not emit theses[] or decisions[] objects."
)


def request_is_committee(request: DecisionReasoningRequest) -> bool:
    packet = request.packet if isinstance(request.packet, dict) else {}
    if packet.get("committee"):
        return True
    policy = request.policy_context if isinstance(request.policy_context, dict) else {}
    if policy.get("committee") or policy.get("residual_allocation_question"):
        return True
    return "CORE Portfolio Investment Committee" in (request.instructions or "")


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
