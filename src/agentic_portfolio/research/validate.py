"""Validate AI research output. Reject malformed payloads. Flag unsupported claims."""

from __future__ import annotations

from typing import Any

from agentic_portfolio.research.types import (
    DislocationAssessment,
    DislocationVerdict,
    EarningsEffectKind,
    EvidenceItem,
    EvidenceKind,
    ResearchConclusion,
    ResearchConfidence,
    ResearchEvidencePacket,
    ResearchReport,
    ScenarioCase,
)

REQUIRED_KEYS = (
    "research_conclusion",
    "confidence",
    "executive_summary",
)

VALID_CONCLUSIONS = {c.value for c in ResearchConclusion}
VALID_CONFIDENCE = {c.value for c in ResearchConfidence}
VALID_DISLOCATION = {c.value for c in DislocationVerdict}
VALID_EARNINGS_EFFECT = {c.value for c in EarningsEffectKind}

ANALYSIS_FIELDS = (
    "executive_summary",
    "business_summary",
    "investment_question",
    "fundamental_analysis",
    "financial_analysis",
    "valuation_analysis",
    "earnings_analysis",
    "competitive_analysis",
    "technical_context",
    "market_context",
    "sector_context",
    "news_analysis",
    "filing_analysis",
    "catalyst_analysis",
    "risk_analysis",
    "expected_horizon",
    "recommended_next_step",
)

PROTECTED_FACT_KEYS = {
    "current_nav",
    "cash",
    "buying_power",
    "positions",
    "holdings_count",
    "high_water_mark",
    "risk_limits",
    "security_class",
    "classification_status",
}


class ResearchValidationError(ValueError):
    """Malformed AI output. The engine must not persist this as RESEARCH_COMPLETE."""


def validate_reasoning(
    payload: Any,
    packet: ResearchEvidencePacket,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Return (normalized_payload, unsupported_claims, validation_errors).

    Raises ResearchValidationError when the payload is not a usable dict/schema.
    """
    errors: list[str] = []
    unsupported: list[str] = []
    if not isinstance(payload, dict):
        raise ResearchValidationError("AI output is not an object")
    missing = [k for k in REQUIRED_KEYS if k not in payload or payload[k] in (None, "")]
    if missing:
        raise ResearchValidationError(f"malformed AI response missing keys: {missing}")

    conclusion = str(payload.get("research_conclusion") or "")
    if conclusion not in VALID_CONCLUSIONS:
        raise ResearchValidationError(f"invalid research_conclusion: {conclusion}")
    confidence = str(payload.get("confidence") or "").upper()
    if confidence not in VALID_CONFIDENCE:
        raise ResearchValidationError(f"invalid confidence: {payload.get('confidence')}")
    payload = dict(payload)
    payload["confidence"] = confidence

    known_ids = packet.all_evidence_ids()
    known_names = {e.name for e in packet.facts} | {e.name for e in packet.derived_metrics}
    observed_sources = set(packet.sources_observed)

    refs = payload.get("evidence_refs") or []
    if refs and not isinstance(refs, list):
        raise ResearchValidationError("evidence_refs must be a list")
    for ref in refs or []:
        if str(ref) not in known_ids and str(ref) not in known_names:
            unsupported.append(f"unknown_evidence_ref:{ref}")

    claimed_facts = payload.get("facts")
    if claimed_facts is not None:
        if not isinstance(claimed_facts, list):
            raise ResearchValidationError("facts must be a list if provided")
        packet_fact_map = {e.name: e.value for e in packet.facts}
        for item in claimed_facts:
            if not isinstance(item, dict):
                unsupported.append("invented_fact_non_object")
                continue
            name = item.get("name")
            value = item.get("value")
            if name not in packet_fact_map:
                unsupported.append(f"invented_observed_fact:{name}")
            elif _values_conflict(packet_fact_map[name], value):
                unsupported.append(f"rewritten_observed_fact:{name}")

    for key in PROTECTED_FACT_KEYS:
        if key in payload and payload[key] is not None:
            unsupported.append(f"attempted_override:{key}")

    if packet.portfolio_facts:
        nav_claim = payload.get("current_nav")
        if nav_claim is not None and _values_conflict(packet.portfolio_facts.current_nav, nav_claim):
            unsupported.append("attempted_nav_rewrite")
        pos_claim = payload.get("positions")
        if pos_claim is not None:
            unsupported.append("attempted_position_rewrite")

    if payload.get("risk_limits") is not None:
        unsupported.append("attempted_risk_limit_change")

    cls_claim = payload.get("security_class")
    frozen_cls = packet.classification.security_class
    if cls_claim is not None and frozen_cls and str(cls_claim) != str(frozen_cls):
        unsupported.append("attempted_classification_override")

    claimed_sources = payload.get("sources_observed") or payload.get("claimed_sources")
    if claimed_sources:
        for src in claimed_sources:
            if src not in observed_sources and src not in packet.sources_unavailable:
                unsupported.append(f"claimed_unobserved_source:{src}")
            elif src in packet.sources_unavailable:
                unsupported.append(f"claimed_unavailable_source:{src}")

    for src in payload.get("claimed_unavailable_as_observed") or []:
        unsupported.append(f"claimed_unavailable_source:{src}")

    for case_key in ("bull_case", "base_case", "bear_case"):
        case = payload.get(case_key)
        if case is None:
            continue
        if not isinstance(case, dict) or not case.get("summary"):
            raise ResearchValidationError(f"{case_key} malformed")
        for ref in case.get("evidence_refs") or []:
            if str(ref) not in known_ids and str(ref) not in known_names:
                unsupported.append(f"unknown_evidence_ref:{case_key}:{ref}")

    for assess_key in ("temporary_dislocation_assessment", "fundamental_deterioration_assessment"):
        assess = payload.get(assess_key)
        if assess is None:
            continue
        if not isinstance(assess, dict) or not assess.get("verdict"):
            raise ResearchValidationError(f"{assess_key} malformed")
        if str(assess["verdict"]) not in VALID_DISLOCATION:
            raise ResearchValidationError(f"invalid {assess_key} verdict")

    effect = payload.get("earnings_effect_kind")
    if effect not in (None, "") and str(effect) not in VALID_EARNINGS_EFFECT:
        raise ResearchValidationError(f"invalid earnings_effect_kind: {effect}")

    interps = payload.get("ai_interpretations") or []
    if interps and not isinstance(interps, list):
        raise ResearchValidationError("ai_interpretations must be a list")
    for item in interps or []:
        if not isinstance(item, dict) or "name" not in item:
            errors.append("malformed_ai_interpretation")
            continue
        for ref in item.get("evidence_refs") or []:
            if str(ref) not in known_ids and str(ref) not in known_names:
                unsupported.append(f"unknown_evidence_ref:interp:{ref}")

    material = conclusion in {ResearchConclusion.ADVANCE_TO_THESIS.value, ResearchConclusion.KEEP_WATCHING.value}
    if material:
        for case_key in ("bull_case", "base_case", "bear_case"):
            if not payload.get(case_key):
                errors.append(f"missing_{case_key}_for_material_report")
        if errors:
            raise ResearchValidationError(f"malformed material report: {errors}")

    return payload, unsupported, errors


def apply_validated_payload(
    report: ResearchReport,
    payload: dict[str, Any],
    packet: ResearchEvidencePacket,
    *,
    unsupported: list[str],
) -> ResearchReport:
    """Copy packet facts/derived; attach AI interpretation. Packet remains authoritative."""
    report.facts = list(packet.facts)
    report.derived_metrics = list(packet.derived_metrics)
    report.sources_observed = list(packet.sources_observed)
    report.sources_unavailable = list(packet.sources_unavailable)
    report.packet_id = packet.packet_id
    report.missing_information = list(payload.get("missing_information") or packet.missing_information)
    report.conflicting_evidence = list(payload.get("conflicting_evidence") or [])
    report.unsupported_claims = list(unsupported)
    report.investment_question = payload.get("investment_question") or packet.investment_question

    for field in ANALYSIS_FIELDS:
        if field == "investment_question":
            continue
        if payload.get(field) is not None:
            setattr(report, field, payload.get(field))

    report.bull_case = _case(payload.get("bull_case"), "BULL_CASE")
    report.base_case = _case(payload.get("base_case"), "BASE_CASE")
    report.bear_case = _case(payload.get("bear_case"), "BEAR_CASE")
    report.temporary_dislocation_assessment = _dislocation(payload.get("temporary_dislocation_assessment"))
    report.fundamental_deterioration_assessment = _dislocation(payload.get("fundamental_deterioration_assessment"))
    report.key_catalysts = list(payload.get("key_catalysts") or [])
    report.key_risks = list(payload.get("key_risks") or [])
    report.invalidation_candidates = list(payload.get("invalidation_candidates") or [])
    report.evidence_refs = [str(r) for r in (payload.get("evidence_refs") or [])]
    report.recommended_next_step = payload.get("recommended_next_step")
    report.earnings_effect_kind = payload.get("earnings_effect_kind")
    report.research_conclusion = ResearchConclusion(payload["research_conclusion"])
    report.confidence = _cap_confidence(
        ResearchConfidence(payload["confidence"]),
        packet,
        report.conflicting_evidence,
    )
    report.ai_interpretations = []
    for i, item in enumerate(payload.get("ai_interpretations") or []):
        if not isinstance(item, dict):
            continue
        report.ai_interpretations.append(
            EvidenceItem(
                evidence_id=f"ai:{item.get('name') or i}",
                kind=EvidenceKind.AI_INTERPRETATION,
                name=str(item.get("name") or f"interp_{i}"),
                value=item.get("value"),
                source="research_reasoner",
                data_type="interpretation",
                derived=False,
                evidence_refs=[str(r) for r in (item.get("evidence_refs") or [])],
            )
        )
    if not report.ai_interpretations and report.executive_summary:
        report.ai_interpretations.append(
            EvidenceItem(
                evidence_id="ai:executive_summary",
                kind=EvidenceKind.AI_INTERPRETATION,
                name="executive_summary",
                value=report.executive_summary,
                source="research_reasoner",
                data_type="interpretation",
                evidence_refs=list(report.evidence_refs),
            )
        )
    report.risk_limits_unchanged = True
    report.portfolio_facts_unchanged = "attempted_nav_rewrite" not in unsupported and "attempted_position_rewrite" not in unsupported
    report.classification_unchanged = "attempted_classification_override" not in unsupported
    report.proposed_actions_created = 0
    report.buy_actions_created = 0
    report.execution_attempted = False
    return report


def _cap_confidence(
    confidence: ResearchConfidence,
    packet: ResearchEvidencePacket,
    conflicting: list[str],
) -> ResearchConfidence:
    order = [ResearchConfidence.LOW, ResearchConfidence.MEDIUM, ResearchConfidence.HIGH]
    cap = ResearchConfidence.HIGH
    if conflicting:
        cap = ResearchConfidence.MEDIUM
    if packet.completeness == "INCOMPLETE":
        cap = ResearchConfidence.LOW
    return order[min(order.index(confidence), order.index(cap))]


def _case(raw: Any, default_name: str) -> ScenarioCase | None:
    if not raw or not isinstance(raw, dict):
        return None
    return ScenarioCase(
        case=str(raw.get("case") or default_name),
        summary=str(raw.get("summary") or ""),
        major_assumptions=list(raw.get("major_assumptions") or []),
        expected_business_outcome=raw.get("expected_business_outcome"),
        major_risk=raw.get("major_risk"),
        attractiveness_implication=raw.get("attractiveness_implication"),
        evidence_refs=[str(r) for r in (raw.get("evidence_refs") or [])],
        price_target=raw.get("price_target"),
        notes=list(raw.get("notes") or []),
    )


def _dislocation(raw: Any) -> DislocationAssessment | None:
    if not raw or not isinstance(raw, dict):
        return None
    return DislocationAssessment(
        verdict=DislocationVerdict(raw["verdict"]),
        reasoning=str(raw.get("reasoning") or ""),
        evidence_refs=[str(r) for r in (raw.get("evidence_refs") or [])],
    )


def _values_conflict(expected: Any, claimed: Any) -> bool:
    if claimed is None:
        return False
    if isinstance(expected, (int, float)) and isinstance(claimed, (int, float)):
        return abs(float(expected) - float(claimed)) > 1e-6
    return str(expected) != str(claimed)
