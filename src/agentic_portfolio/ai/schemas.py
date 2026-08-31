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

SCHEMAS = {
    "screening": SCREENING_SCHEMA,
    "deep_research": DEEP_RESEARCH_SCHEMA,
    "portfolio_decision": PORTFOLIO_DECISION_SCHEMA,
}


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
    if "array" in types and isinstance(value, list):
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
