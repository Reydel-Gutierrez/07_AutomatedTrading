"""JSON Schema for structured AI outputs. Trading code never parses prose."""

from __future__ import annotations

from typing import Any

from agentic_portfolio.ai.errors import SchemaViolation
from agentic_portfolio.ai.types import (
    AIConfidence,
    DeepResearchResult,
    parse_confidence,
    parse_recommended_action,
    PortfolioDecisionResult,
    RecommendedAction,
    ScreeningResult,
)

_CONF = ["LOW", "MEDIUM", "HIGH"]
_ACTIONS = [a.value for a in RecommendedAction]


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


SCREENING_SCHEMA = _object(
    {
        "ticker": {"type": "string"},
        "score": {"type": "number"},
        "classification": {"type": "string"},
        "catalyst_summary": {"type": "string"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "worth_deep_research": {"type": "boolean"},
        "confidence": {"type": "string", "enum": _CONF},
    },
    ["ticker", "score", "classification", "catalyst_summary", "risk_flags", "worth_deep_research", "confidence"],
)

DEEP_RESEARCH_SCHEMA = _object(
    {
        "ticker": {"type": "string"},
        "thesis": {"type": "string"},
        "bull_case": {"type": "string"},
        "bear_case": {"type": "string"},
        "catalysts": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "valuation_observations": {"type": "string"},
        "technical_observations": {"type": "string"},
        "confidence": {"type": "string", "enum": _CONF},
        "recommended_action": {"type": "string", "enum": _ACTIONS},
    },
    [
        "ticker",
        "thesis",
        "bull_case",
        "bear_case",
        "catalysts",
        "risks",
        "valuation_observations",
        "technical_observations",
        "confidence",
        "recommended_action",
    ],
)

PORTFOLIO_DECISION_SCHEMA = _object(
    {
        "ticker": {"type": "string"},
        "action": {"type": "string", "enum": _ACTIONS},
        "confidence": {"type": "string", "enum": _CONF},
        "rationale": {"type": "string"},
        "suggested_allocation_pct": {"type": ["number", "null"]},
        "suggested_max_dollars": {"type": ["number", "null"]},
        "reassessment_conditions": {"type": "array", "items": {"type": "string"}},
        "risk_notes": {"type": "array", "items": {"type": "string"}},
    },
    [
        "ticker",
        "action",
        "confidence",
        "rationale",
        "suggested_allocation_pct",
        "suggested_max_dollars",
        "reassessment_conditions",
        "risk_notes",
    ],
)


def _str_array(*, max_items: int | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
    if max_items is not None:
        spec["maxItems"] = max_items
    return spec


_CASE = _object(
    {
        "case": {"type": "string"},
        "summary": {"type": "string"},
        "major_assumptions": _str_array(max_items=5),
        "expected_business_outcome": {"type": ["string", "null"]},
        "major_risk": {"type": ["string", "null"]},
        "attractiveness_implication": {"type": ["string", "null"]},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "price_target": {"type": ["number", "null", "string"]},
        "notes": _str_array(max_items=5),
    },
    ["summary"],
)

_DISLOCATION = _object(
    {
        "verdict": {"type": "string"},
        "reasoning": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
    ["verdict", "reasoning"],
)

_INTERP = _object(
    {
        "name": {"type": "string"},
        "value": {"type": ["string", "number", "null"]},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
    ["name"],
)

RESEARCH_REPORT_SCHEMA = _object(
    {
        "executive_summary": {"type": "string"},
        "business_summary": {"type": "string"},
        "investment_question": {"type": "string"},
        "fundamental_analysis": {"type": "string"},
        "financial_analysis": {"type": "string"},
        "valuation_analysis": {"type": "string"},
        "earnings_analysis": {"type": "string"},
        "competitive_analysis": {"type": "string"},
        "technical_context": {"type": "string"},
        "market_context": {"type": "string"},
        "sector_context": {"type": "string"},
        "news_analysis": {"type": "string"},
        "filing_analysis": {"type": "string"},
        "catalyst_analysis": {"type": "string"},
        "risk_analysis": {"type": "string"},
        "bull_case": _CASE,
        "base_case": _CASE,
        "bear_case": _CASE,
        "temporary_dislocation_assessment": _DISLOCATION,
        "fundamental_deterioration_assessment": _DISLOCATION,
        "key_catalysts": _str_array(max_items=5),
        "key_risks": _str_array(max_items=5),
        "invalidation_candidates": _str_array(max_items=5),
        "expected_horizon": {"type": "string"},
        "missing_information": _str_array(max_items=5),
        "conflicting_evidence": _str_array(max_items=5),
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "ai_interpretations": {"type": "array", "items": _INTERP, "maxItems": 5},
        "confidence": {"type": "string", "enum": _CONF},
        "research_conclusion": {
            "type": "string",
            "enum": ["ADVANCE_TO_THESIS", "KEEP_WATCHING", "REJECT", "NEED_MORE_DATA"],
        },
        "recommended_next_step": {"type": "string"},
        "earnings_effect_kind": {"type": ["string", "null"]},
    },
    ["research_conclusion", "confidence", "executive_summary"],
)

_EXIT = _object(
    {
        "thesis_based": {"type": "boolean"},
        "mandatory_fixed_stop_loss": {"type": "boolean"},
        "price_invalidation": {"type": ["string", "null"]},
        "event_invalidation": {"type": ["string", "null"]},
        "technical_invalidation": {"type": ["string", "null"]},
        "risk_invalidation": {"type": ["string", "null"]},
        "broker_stop_orders_created": {"type": "boolean"},
        "notes": {"type": ["string", "null"]},
    },
    ["thesis_based", "mandatory_fixed_stop_loss", "broker_stop_orders_created"],
)

_THESIS_ITEM = _object(
    {
        "symbol": {"type": "string"},
        "research_id": {"type": ["string", "null"]},
        "sleeve": {"type": "string"},
        "thesis_summary": {"type": "string"},
        "bull_case": {"type": "string"},
        "base_case": {"type": "string"},
        "bear_case": {"type": "string"},
        "catalysts": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "horizon": {"type": "string"},
        "invalidation_conditions": {"type": "array", "items": {"type": "string"}},
        "review_triggers": {"type": "array", "items": {"type": "string"}},
        "why_position_should_exist": {"type": "string"},
        "confidence": {"type": "string", "enum": _CONF},
        "exit_policy": _EXIT,
        "status": {"type": ["string", "null"]},
    },
    ["symbol", "thesis_summary", "sleeve"],
)

_DECISION_ITEM = _object(
    {
        "symbol": {"type": "string"},
        "decision": {"type": "string"},
        "desired_allocation_pct": {"type": ["number", "null"]},
        "rationale": {"type": "string"},
        "why_preferable_to_cash": {"type": "string"},
        "why_preferable_to_spy": {"type": "string"},
        "why_preferable_to_alternatives": {"type": "string"},
    },
    ["symbol", "decision", "rationale"],
)

THESIS_DECISION_SCHEMA = _object(
    {
        "theses": {"type": "array", "items": _THESIS_ITEM},
        "comparison": _object(
            {
                "ranking": {"type": "array", "items": {"type": "string"}},
                "vs_cash": {"type": "string"},
                "vs_spy": {"type": "string"},
                "notes": {"type": "string"},
            },
            ["vs_cash", "vs_spy"],
        ),
        "decisions": {"type": "array", "items": _DECISION_ITEM},
    },
    ["decisions", "comparison"],
)

SCHEMAS = {
    "screening": SCREENING_SCHEMA,
    "deep_research": DEEP_RESEARCH_SCHEMA,
    "portfolio_decision": PORTFOLIO_DECISION_SCHEMA,
    "research_report": RESEARCH_REPORT_SCHEMA,
    "thesis_decision": THESIS_DECISION_SCHEMA,
}


def to_openai_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a permissive internal schema to OpenAI Structured Outputs strict mode.

    Internal canonical schemas may keep optional properties out of `required`.
    OpenAI strict objects must list every property in `required` and set
    `additionalProperties=false`. Previously-optional fields become nullable.
    """
    return _strict_node(schema)


def _strict_node(spec: dict[str, Any]) -> dict[str, Any]:
    out = dict(spec)
    raw_type = out.get("type")
    types = raw_type if isinstance(raw_type, list) else ([raw_type] if raw_type else [])
    if "object" in types or (not types and "properties" in out):
        props_in = dict(out.get("properties") or {})
        originally_required = set(out.get("required") or [])
        props: dict[str, Any] = {}
        for key, child in props_in.items():
            node = _strict_node(child) if isinstance(child, dict) else child
            if key not in originally_required and isinstance(node, dict):
                node = _make_nullable(node)
            props[key] = node
        out["type"] = "object"
        out["properties"] = props
        out["required"] = list(props.keys())
        out["additionalProperties"] = False
    if "array" in types and isinstance(out.get("items"), dict):
        out["items"] = _strict_node(out["items"])
    return out


def _make_nullable(spec: dict[str, Any]) -> dict[str, Any]:
    child = dict(spec)
    raw_type = child.get("type")
    if raw_type is None:
        return child
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    if "null" not in types:
        child["type"] = [*types, "null"]
    return child


def validate_against_schema(payload: Any, schema: dict[str, Any], *, name: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SchemaViolation(f"{name}: expected object, got {type(payload).__name__}")
    required = list(schema.get("required") or [])
    properties = dict(schema.get("properties") or {})
    missing = [key for key in required if key not in payload]
    if missing:
        raise SchemaViolation(f"{name}: missing fields {missing}")
    if schema.get("additionalProperties") is False:
        extra = [key for key in payload if key not in properties]
        if extra:
            raise SchemaViolation(f"{name}: unexpected fields {extra}")
    out: dict[str, Any] = {}
    for key, spec in properties.items():
        if key not in payload:
            continue
        out[key] = _check_value(payload[key], spec, path=f"{name}.{key}")
    return out


def _check_value(value: Any, spec: dict[str, Any], *, path: str) -> Any:
    allowed = spec.get("type")
    types = allowed if isinstance(allowed, list) else [allowed]
    if value is None:
        if "null" in types:
            return None
        raise SchemaViolation(f"{path}: null not allowed")
    if "string" in types and isinstance(value, str):
        enum = spec.get("enum")
        if enum and value not in enum:
            raise SchemaViolation(f"{path}: {value!r} not in {enum}")
        return value
    if "number" in types and isinstance(value, bool):
        raise SchemaViolation(f"{path}: boolean is not a number")
    if "number" in types and isinstance(value, (int, float)):
        return float(value)
    if "boolean" in types and isinstance(value, bool):
        return value
    if "object" in types and isinstance(value, dict):
        props = dict(spec.get("properties") or {})
        if not props:
            return dict(value)
        required = list(spec.get("required") or [])
        missing = [key for key in required if key not in value]
        if missing:
            raise SchemaViolation(f"{path}: missing fields {missing}")
        if spec.get("additionalProperties") is False:
            extra = [key for key in value if key not in props]
            if extra:
                raise SchemaViolation(f"{path}: unexpected fields {extra}")
        out: dict[str, Any] = {}
        for key, child in props.items():
            if key not in value:
                continue
            out[key] = _check_value(value[key], child, path=f"{path}.{key}")
        return out
    if "array" in types and isinstance(value, list):
        max_items = spec.get("maxItems")
        if max_items is not None and len(value) > int(max_items):
            raise SchemaViolation(f"{path}: more than {max_items} items")
        item_spec = spec.get("items") or {"type": "string"}
        return [_check_value(item, item_spec, path=f"{path}[]") for item in value]
    raise SchemaViolation(f"{path}: expected {allowed}, got {type(value).__name__}")


def screening_from_payload(payload: dict[str, Any], **meta: Any) -> ScreeningResult:
    data = validate_against_schema(payload, SCREENING_SCHEMA, name="screening")
    return ScreeningResult(
        ticker=str(data["ticker"]).upper(),
        score=float(data["score"]),
        classification=str(data["classification"]),
        catalyst_summary=str(data["catalyst_summary"]),
        risk_flags=list(data["risk_flags"]),
        worth_deep_research=bool(data["worth_deep_research"]),
        confidence=parse_confidence(data["confidence"]),
        **meta,
    )


def research_from_payload(payload: dict[str, Any], **meta: Any) -> DeepResearchResult:
    data = validate_against_schema(payload, DEEP_RESEARCH_SCHEMA, name="deep_research")
    return DeepResearchResult(
        ticker=str(data["ticker"]).upper(),
        thesis=str(data["thesis"]),
        bull_case=str(data["bull_case"]),
        bear_case=str(data["bear_case"]),
        catalysts=list(data["catalysts"]),
        risks=list(data["risks"]),
        valuation_observations=str(data["valuation_observations"]),
        technical_observations=str(data["technical_observations"]),
        confidence=parse_confidence(data["confidence"]),
        recommended_action=parse_recommended_action(data["recommended_action"]),
        **meta,
    )


def decision_from_payload(payload: dict[str, Any], **meta: Any) -> PortfolioDecisionResult:
    data = validate_against_schema(payload, PORTFOLIO_DECISION_SCHEMA, name="portfolio_decision")
    return PortfolioDecisionResult(
        ticker=str(data["ticker"]).upper(),
        action=parse_recommended_action(data["action"]),
        confidence=parse_confidence(data["confidence"]),
        rationale=str(data["rationale"]),
        suggested_allocation_pct=None if data["suggested_allocation_pct"] is None else float(data["suggested_allocation_pct"]),
        suggested_max_dollars=None if data["suggested_max_dollars"] is None else float(data["suggested_max_dollars"]),
        reassessment_conditions=list(data["reassessment_conditions"]),
        risk_notes=list(data["risk_notes"]),
        **meta,
    )


def confidence_at_least(value: AIConfidence | str, minimum: AIConfidence | str) -> bool:
    have = parse_confidence(value)
    need = parse_confidence(minimum)
    rank = {AIConfidence.LOW: 0, AIConfidence.MEDIUM: 1, AIConfidence.HIGH: 2}
    return rank[have] >= rank[need]
