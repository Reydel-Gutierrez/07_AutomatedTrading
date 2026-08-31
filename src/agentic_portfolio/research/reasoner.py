"""Provider-agnostic AI reasoning interface for Deep Research.

Python prepares ResearchEvidencePacket. A ResearchReasoner returns structured
interpretation. Cursor is not required; any programmatic model can implement
this protocol.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Callable, Protocol

from agentic_portfolio.research.types import ResearchReasoningRequest
from agentic_portfolio.schemas import to_dict

REASONER_INSTRUCTIONS = """You are the Deep Research reasoner for an agentic portfolio system.

Python has already collected OBSERVED_FACT and DETERMINISTIC_DERIVED_METRIC items.
You interpret. You do not collect new observed facts.

Hard rules:
- Never invent missing observed facts.
- Never override security classification.
- Never alter NAV, cash, positions, or holdings counts.
- Never modify risk limits or concentration ceilings.
- Never claim a source was observed unless it is listed in sources_observed.
- Never emit a BUY/ADD decision or a ProposedAction.
- Cite evidence_refs using packet evidence_id values only.
- NEED_MORE_DATA is only for missing CORE observed facts: market price, security identity, and either financial statements or usable fundamental/quality facts (description, market cap, valuation multiples). For ETFs, mandate/description plus price is enough core evidence.
- Unavailable optional sources (news, SEC excerpts, technicals, earnings calendar, historicals) must not by themselves force NEED_MORE_DATA. Record them in missing_information.
- If core facts exist, you MUST choose ADVANCE_TO_THESIS, KEEP_WATCHING, or REJECT. Residual uncertainty is KEEP_WATCHING or lower confidence, not NEED_MORE_DATA.
- If evidence conflicts, list conflicting_evidence and do not claim HIGH confidence.
- A high Discovery score does not require ADVANCE_TO_THESIS.
- Do not use universal rules such as P/E < 20 = buy or revenue growth > 20% = good.
- Interpret valuation, growth, and quality in company/sector/sleeve context.
- Technical evidence is supporting context except TACTICAL, where it is setup context.
- For OPPORTUNISTIC, explicitly assess TEMPORARY_PRICE_DISLOCATION vs FUNDAMENTAL_BUSINESS_DETERIORATION.
- Distinguish ONE_TIME_EFFECT vs STRUCTURAL_CHANGE in earnings when evidence permits; otherwise UNCERTAIN.
- Bull/base/bear cases must be evidence-based. Do not invent precise price targets.
- Separate repeated reporting of one news event from independent developments.
- Filing analysis must reason over provided excerpts/facts, not keyword counts.

Return JSON only with these keys:
executive_summary, business_summary, investment_question,
fundamental_analysis, financial_analysis, valuation_analysis, earnings_analysis,
competitive_analysis, technical_context, market_context, sector_context,
news_analysis, filing_analysis, catalyst_analysis, risk_analysis,
bull_case, base_case, bear_case,
temporary_dislocation_assessment, fundamental_deterioration_assessment,
key_catalysts, key_risks, invalidation_candidates, expected_horizon,
missing_information, conflicting_evidence, evidence_refs, ai_interpretations,
confidence (LOW|MEDIUM|HIGH), research_conclusion
(ADVANCE_TO_THESIS|KEEP_WATCHING|REJECT|NEED_MORE_DATA),
recommended_next_step, earnings_effect_kind (ONE_TIME_EFFECT|STRUCTURAL_CHANGE|UNCERTAIN|null).

bull_case/base_case/bear_case objects: case, summary, major_assumptions,
expected_business_outcome, major_risk, attractiveness_implication, evidence_refs,
price_target (null unless evidence supports precision).

dislocation assessments: verdict (LIKELY_DISLOCATION|MIXED|LIKELY_DETERIORATION|INSUFFICIENT_EVIDENCE),
reasoning, evidence_refs.

ai_interpretations: list of {name, value, evidence_refs}.
"""


class ResearchReasoner(Protocol):
    """Swap model/provider without rewriting Research."""

    def reason(self, request: ResearchReasoningRequest) -> dict[str, Any]: ...


class CallableResearchReasoner:
    """Wrap any `complete(prompt: str) -> dict` callable (OpenAI, local model, etc.)."""

    def __init__(self, complete: Callable[[str], dict[str, Any]]) -> None:
        self.complete = complete

    def reason(self, request: ResearchReasoningRequest) -> dict[str, Any]:
        prompt = build_reasoning_prompt(request)
        payload = self.complete(prompt)
        if not isinstance(payload, dict):
            raise TypeError("ResearchReasoner complete() must return a dict")
        return payload


class ScriptedResearchReasoner:
    """Deterministic reasoner for tests and recorded pilots."""

    def __init__(self, responses: dict[str, dict[str, Any]] | Callable[[ResearchReasoningRequest], dict[str, Any]]) -> None:
        self._responses = responses

    def reason(self, request: ResearchReasoningRequest) -> dict[str, Any]:
        if callable(self._responses):
            return self._responses(request)
        symbol = str((request.candidate or {}).get("symbol") or "").upper()
        if symbol not in self._responses:
            raise KeyError(f"No scripted research response for {symbol}")
        return dict(self._responses[symbol])


def build_reasoning_prompt(request: ResearchReasoningRequest) -> str:
    """Serializable prompt for any programmatic model."""
    return (
        REASONER_INSTRUCTIONS
        + "\n\nCANDIDATE:\n"
        + _dump(request.candidate)
        + "\n\nEVIDENCE PACKET (facts + derived metrics only):\n"
        + _dump(request.packet)
        + "\n\nPORTFOLIO CONTEXT (facts; do not rewrite):\n"
        + _dump(request.portfolio_context)
        + "\n\nPOLICY CONTEXT:\n"
        + _dump(request.policy_context)
        + "\n\nSLEEVE QUESTIONS:\n"
        + _dump(request.sleeve_questions)
        + "\n\nCOMPARISON PEERS:\n"
        + _dump(request.comparison_peers)
        + "\n\nADDITIONAL INSTRUCTIONS:\n"
        + (request.instructions or "")
    )


def packet_for_reasoner(packet_dict: dict[str, Any]) -> dict[str, Any]:
    """Strip anything the model must not treat as writable book state."""
    out = dict(packet_dict)
    # Keep frozen copies as read-only facts; reasoner must not echo mutations.
    return out


def _dump(obj: Any) -> str:
    import json

    if is_dataclass(obj) and not isinstance(obj, type):
        obj = to_dict(obj)
    elif isinstance(obj, Enum):
        obj = obj.value
    return json.dumps(obj, default=str, indent=2)
