"""OpenAI strict-schema optional-null normalization.

OpenAI Structured Outputs requires every property and makes previously-optional
fields nullable. Canonical validation must see those nulls omitted, not rejected.
"""

from __future__ import annotations

import copy

import pytest

from agentic_portfolio.ai.config import load_ai_config
from agentic_portfolio.ai.errors import SchemaViolation
from agentic_portfolio.ai.providers.scripted import ScriptedProvider
from agentic_portfolio.ai.reasoners import GatewayDecisionReasoner
from agentic_portfolio.ai.schemas import (
    THESIS_DECISION_SCHEMA,
    _object,
    normalize_openai_strict_payload,
    to_openai_strict_schema,
    validate_against_schema,
)
from agentic_portfolio.decision.engine import run_portfolio_decision
from agentic_portfolio.decision.reasoner import REASONER_INSTRUCTIONS
from agentic_portfolio.decision.types import DecisionReasoningRequest
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT
from agentic_portfolio.schemas import Decision
from tests.conftest import ctx
from tests.test_ai_gateway import _gw
from tests.test_decision import _cash_spy_only_payload, _payload, _report, _run


OPTIONAL_STRING_SCHEMA = _object(
    {"name": {"type": "string"}, "note": {"type": "string"}},
    ["name"],
)
REQUIRED_STRING_SCHEMA = _object({"name": {"type": "string"}}, ["name"])
NULLABLE_OPTIONAL_SCHEMA = _object(
    {"name": {"type": "string"}, "note": {"type": ["string", "null"]}},
    ["name"],
)


def _strict_null_decision_payload(symbol="QUAL", decision="WATCH", alloc=0, extra=None):
    payload = _with_openai_strict_optional_nulls(_payload(symbol, decision=decision, alloc=alloc, extra=extra))
    return payload


def _with_openai_strict_optional_nulls(payload: dict) -> dict:
    """Fill optional decision strings with null the way OpenAI strict mode does."""
    payload = copy.deepcopy(payload)
    for item in payload.get("decisions") or []:
        item["why_preferable_to_cash"] = None
        item["why_preferable_to_spy"] = None
        item["why_preferable_to_alternatives"] = None
        item.setdefault("desired_allocation_pct", None)
    comparison = payload.setdefault("comparison", {})
    if isinstance(comparison, dict):
        comparison["notes"] = None
    return payload


def _decision_request(symbol="QUAL") -> DecisionReasoningRequest:
    return DecisionReasoningRequest(
        packet={},
        reports=[{"symbol": symbol}],
        portfolio_context={},
        existing_theses=[],
        policy_context={},
        alternatives=["CASH", "SPY"],
        instructions=REASONER_INSTRUCTIONS,
    )


def test_optional_canonical_string_null_is_omitted_then_validates():
    raw = {"name": "x", "note": None}
    assert "null" in to_openai_strict_schema(OPTIONAL_STRING_SCHEMA)["properties"]["note"]["type"]
    with pytest.raises(SchemaViolation, match="note: null not allowed"):
        validate_against_schema(raw, OPTIONAL_STRING_SCHEMA, name="t")
    out = normalize_openai_strict_payload(raw, OPTIONAL_STRING_SCHEMA)
    assert out == {"name": "x"}
    assert "note" not in out
    assert validate_against_schema(out, OPTIONAL_STRING_SCHEMA, name="t") == {"name": "x"}


def test_required_canonical_string_null_still_fails():
    raw = {"name": None}
    out = normalize_openai_strict_payload(raw, REQUIRED_STRING_SCHEMA)
    assert out == {"name": None}
    with pytest.raises(SchemaViolation, match="name: null not allowed"):
        validate_against_schema(out, REQUIRED_STRING_SCHEMA, name="t")
    with pytest.raises(SchemaViolation, match="name: null not allowed"):
        validate_against_schema(raw, REQUIRED_STRING_SCHEMA, name="t")


def test_explicitly_nullable_optional_null_is_preserved():
    raw = {"name": "x", "note": None}
    out = normalize_openai_strict_payload(raw, NULLABLE_OPTIONAL_SCHEMA)
    assert out == {"name": "x", "note": None}
    assert validate_against_schema(out, NULLABLE_OPTIONAL_SCHEMA, name="t") == {"name": "x", "note": None}


def test_nested_optional_fields_inside_decisions_normalize():
    raw = _payload("QUAL", decision="WATCH", alloc=0)
    for item in raw["decisions"]:
        item["why_preferable_to_cash"] = None
        item["why_preferable_to_spy"] = None
        item["why_preferable_to_alternatives"] = None
    with pytest.raises(SchemaViolation, match="why_preferable_to_cash: null not allowed"):
        validate_against_schema(raw, THESIS_DECISION_SCHEMA, name="thesis_decision")
    raw["comparison"]["notes"] = None
    out = normalize_openai_strict_payload(raw, THESIS_DECISION_SCHEMA)
    named = next(item for item in out["decisions"] if item["symbol"] == "QUAL")
    cash = next(item for item in out["decisions"] if item["symbol"] == "CASH")
    for key in ("why_preferable_to_cash", "why_preferable_to_spy", "why_preferable_to_alternatives"):
        assert key not in named
        assert key not in cash
    assert named["desired_allocation_pct"] == 0
    assert cash["desired_allocation_pct"] == 100.0
    assert "notes" not in (out.get("comparison") or {})
    validated = validate_against_schema(out, THESIS_DECISION_SCHEMA, name="thesis_decision")
    v_named = next(item for item in validated["decisions"] if item["symbol"] == "QUAL")
    for key in ("why_preferable_to_cash", "why_preferable_to_spy", "why_preferable_to_alternatives"):
        assert key not in v_named


def test_watch_and_no_action_openai_null_comparisons_pass_schema():
    for decision in ("WATCH", "NO_ACTION"):
        raw = _strict_null_decision_payload("QUAL", decision=decision, alloc=0)
        for item in raw["decisions"]:
            assert item["why_preferable_to_cash"] is None
            assert item["why_preferable_to_spy"] is None
            assert item["why_preferable_to_alternatives"] is None
        with pytest.raises(SchemaViolation, match="null not allowed"):
            validate_against_schema(raw, THESIS_DECISION_SCHEMA, name="thesis_decision")
        out = normalize_openai_strict_payload(raw, THESIS_DECISION_SCHEMA)
        validate_against_schema(out, THESIS_DECISION_SCHEMA, name="thesis_decision")


def test_buy_add_semantic_rules_still_apply_after_optional_nulls():
    missing_thesis = _with_openai_strict_optional_nulls(_payload(decision="BUY", alloc=5.0, theses=[]))
    out = _run(payload=missing_thesis)
    assert "buy_add_requires_thesis" in out.validation_errors[0]

    missing_alloc = _with_openai_strict_optional_nulls(_payload(decision="BUY", alloc=5.0))
    missing_alloc["decisions"][0]["desired_allocation_pct"] = None
    out = _run(payload=missing_alloc)
    assert "buy_add_requires_allocation" in out.validation_errors[0]

    missing_compare = _with_openai_strict_optional_nulls(_payload(decision="ADD", alloc=5.0))
    missing_compare["comparison"]["vs_cash"] = ""
    missing_compare["comparison"]["vs_spy"] = ""
    blob = _run(payload=missing_compare).validation_errors[0]
    assert "missing_vs_cash" in blob
    assert "missing_vs_spy" in blob

    keep_watching = _with_openai_strict_optional_nulls(_payload(decision="BUY", alloc=5.0))
    from agentic_portfolio.research.types import ResearchConclusion

    out = _run(reports=[_report(conclusion=ResearchConclusion.KEEP_WATCHING)], payload=keep_watching)
    assert "buy_add_requires_advance_to_thesis" in out.validation_errors[0]


def test_missing_researched_symbol_still_fails_no_named_decision():
    payload = _with_openai_strict_optional_nulls(_cash_spy_only_payload("QUAL", "CASH"))
    out = _run(payload=payload)
    blob = " ".join(out.validation_errors)
    assert "no_named_decision:QUAL" in blob
    assert out.gated_actions == []
    assert out.theses == []


def test_cash_spy_only_still_fails_with_openai_strict_nulls():
    for rows in (("CASH",), ("SPY",), ("CASH", "SPY")):
        payload = _with_openai_strict_optional_nulls(_cash_spy_only_payload("QUAL", *rows))
        out = _run(payload=payload)
        blob = " ".join(out.validation_errors)
        assert "no_named_decision:QUAL" in blob
        assert "cash_spy_only_payload" in blob
        assert out.gated_actions == []


def test_gateway_decision_reasoner_accepts_openai_strict_optional_nulls(tmp_path):
    raw = _strict_null_decision_payload("QUAL", decision="WATCH", alloc=0)
    provider = ScriptedProvider({"thesis_decision": raw}, name="openai")
    gw = _gw(tmp_path, {"openai": provider})
    reasoner = GatewayDecisionReasoner(gw)
    payload = reasoner.reason(_decision_request("QUAL"))
    named = next(item for item in payload["decisions"] if item["symbol"] == "QUAL")
    for key in ("why_preferable_to_cash", "why_preferable_to_spy", "why_preferable_to_alternatives"):
        assert key not in named
    assert named["decision"] == "WATCH"
    assert reasoner.last_result is not None
    assert reasoner.last_result.provider == "openai"


def test_gateway_watch_and_no_action_with_strict_nulls_reach_decision_engine(tmp_path):
    cfg = load_ai_config()
    assert cfg["budget"]["monthly_cap"] == 10.0
    assert cfg["budget"]["hard_stop"] == 10.0
    assert LIVE_ORDER_PLACEMENT is False
    for decision in ("WATCH", "NO_ACTION"):
        raw = _strict_null_decision_payload("QUAL", decision=decision, alloc=0)
        provider = ScriptedProvider({"thesis_decision": raw}, name="openai")
        gw = _gw(tmp_path, {"openai": provider})
        out = run_portfolio_decision(
            [_report()],
            ctx(500),
            GatewayDecisionReasoner(gw),
            persist=False,
            journal=None,
        )
        assert out.validation_errors == []
        assert out.decisions[0].symbol == "QUAL"
        assert out.decisions[0].decision == Decision(decision)
        assert out.decisions[0].why_preferable_to_cash is None
        assert out.execution_attempted is False
        assert out.gated_actions[0].risk.execution_permitted is False


def test_gateway_required_null_still_fails_schema(tmp_path):
    raw = _strict_null_decision_payload("QUAL", decision="WATCH", alloc=0)
    raw["decisions"][0]["rationale"] = None
    provider = ScriptedProvider({"thesis_decision": raw}, name="openai")
    gw = _gw(tmp_path, {"openai": provider})
    reasoner = GatewayDecisionReasoner(gw)
    from agentic_portfolio.decision.validate import DecisionValidationError

    with pytest.raises(DecisionValidationError, match="null not allowed"):
        reasoner.reason(_decision_request("QUAL"))


def test_gateway_preserves_explicit_nullable_optional_null(tmp_path):
    raw = _strict_null_decision_payload("QUAL", decision="WATCH", alloc=0)
    raw["decisions"][0]["desired_allocation_pct"] = None
    provider = ScriptedProvider({"thesis_decision": raw}, name="openai")
    gw = _gw(tmp_path, {"openai": provider})
    payload = GatewayDecisionReasoner(gw).reason(_decision_request("QUAL"))
    named = next(item for item in payload["decisions"] if item["symbol"] == "QUAL")
    assert named.get("desired_allocation_pct") is None
    assert "why_preferable_to_cash" not in named
