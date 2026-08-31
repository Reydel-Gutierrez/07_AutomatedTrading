"""AI gateway, structured output, provider failures, and the $10 monthly cap."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from agentic_portfolio.ai.budget import BudgetManager
from agentic_portfolio.ai.config import load_ai_config
from agentic_portfolio.ai.errors import (
    BudgetDenied,
    BudgetExhausted,
    MalformedResponse,
    ProviderOutage,
    ProviderTimeout,
    SchemaViolation,
)
from agentic_portfolio.ai.gateway import AIGateway, build_gateway
from agentic_portfolio.ai.ledger import UsageLedger, month_key
from agentic_portfolio.ai.providers.anthropic import AnthropicProvider
from agentic_portfolio.ai.providers.openai import OpenAIProvider
from agentic_portfolio.ai.providers.scripted import ScriptedProvider
from agentic_portfolio.ai.safety import inspect_src_for_direct_provider_calls
from agentic_portfolio.ai.schemas import SCREENING_SCHEMA
from agentic_portfolio.ai.types import BudgetMode, ModelRole
from agentic_portfolio.runtime import RuntimeMode

NOW = datetime(2026, 8, 30, 20, 0, tzinfo=timezone.utc)

SCREEN = {
    "ticker": "QUAL",
    "score": 71.0,
    "classification": "QUALITY_GROWTH",
    "catalyst_summary": "Facts support a closer look.",
    "risk_flags": [],
    "worth_deep_research": True,
    "confidence": "MEDIUM",
}


def _gw(tmp_path, providers, *, spent=None):
    cfg = load_ai_config()
    ledger = UsageLedger(tmp_path, config=cfg)
    if spent is not None:
        data = ledger.load_month(now=NOW)
        data["spent"] = str(spent)
        ledger.save_month(data)
    budget = BudgetManager(ledger, cfg, now_fn=lambda: NOW)
    return AIGateway(budget=budget, providers=providers, config=cfg, runtime_mode=RuntimeMode.PAPER)


def _screen(gateway, ticker="QUAL"):
    return gateway.complete_structured(
        role=ModelRole.SCREENING,
        purpose="candidate_screening",
        schema_name="screening",
        schema=SCREENING_SCHEMA,
        messages=[{"role": "user", "content": f"screen {ticker}"}],
        ticker=ticker,
    )


def test_no_application_code_calls_providers_directly():
    assert inspect_src_for_direct_provider_calls() == []


def test_openai_outage_raises(tmp_path):
    gw = _gw(tmp_path, {"openai": ScriptedProvider({ "*": SCREEN }, fail="outage", name="openai")})
    with pytest.raises(ProviderOutage):
        _screen(gw)


def test_anthropic_outage_raises(tmp_path):
    adapter = AnthropicProvider(api_key=None)
    assert adapter.available() is False
    gw = _gw(tmp_path, {"openai": ScriptedProvider({ "*": SCREEN }, fail="unavailable", name="openai"), "anthropic": ScriptedProvider({ "*": SCREEN }, fail="outage", name="anthropic")})
    with pytest.raises(ProviderOutage):
        _screen(gw)


def test_provider_timeout(tmp_path):
    gw = _gw(tmp_path, {"openai": ScriptedProvider({ "*": SCREEN }, fail="timeout", name="openai")})
    with pytest.raises(ProviderTimeout):
        _screen(gw)


def test_malformed_response(tmp_path):
    gw = _gw(tmp_path, {"openai": ScriptedProvider({ "*": SCREEN }, fail="malformed", name="openai")})
    with pytest.raises(MalformedResponse):
        _screen(gw)


def test_schema_violation(tmp_path):
    bad = ScriptedProvider({"screening": {"ticker": "QUAL"}}, name="openai")
    gw = _gw(tmp_path, {"openai": bad})
    with pytest.raises(SchemaViolation):
        _screen(gw)


def test_duplicate_response_is_idempotent(tmp_path):
    provider = ScriptedProvider({"screening": SCREEN}, name="openai")
    gw = _gw(tmp_path, {"openai": provider})
    first = _screen(gw)
    second = _screen(gw)
    assert first.payload == second.payload
    assert len(provider.calls) == 1


def test_cost_estimate_rejection(tmp_path):
    gw = _gw(tmp_path, {"openai": ScriptedProvider({"screening": SCREEN}, name="openai")}, spent="7.00")
    with pytest.raises((BudgetDenied, BudgetExhausted)):
        gw.complete_structured(
            role=ModelRole.RESEARCH,
            purpose="deep_research",
            schema_name="screening",
            schema=SCREENING_SCHEMA,
            messages=[{"role": "user", "content": "x" * 50000}],
            ticker="QUAL",
            estimated_input_tokens=2_000_000,
            estimated_output_tokens=500_000,
            allow_fallback=False,
        )


def test_conservation_at_8_blocks_research_allows_screening(tmp_path):
    provider = ScriptedProvider({"screening": SCREEN}, name="openai")
    gw = _gw(tmp_path, {"openai": provider}, spent="8.00")
    assert gw.budget.status().mode is BudgetMode.CONSERVING
    ok = _screen(gw)
    assert ok.payload["ticker"] == "QUAL"
    with pytest.raises(BudgetDenied, match="CONSERVING"):
        gw.complete_structured(
            role=ModelRole.RESEARCH,
            purpose="deep_research",
            schema_name="screening",
            schema=SCREENING_SCHEMA,
            messages=[{"role": "user", "content": "research QUAL"}],
            ticker="QUAL",
            allow_fallback=False,
        )


def test_critical_at_9_50_only_reassessment(tmp_path):
    provider = ScriptedProvider({"screening": SCREEN}, name="openai")
    gw = _gw(tmp_path, {"openai": provider}, spent="9.50")
    assert gw.budget.status().mode is BudgetMode.CRITICAL
    with pytest.raises(BudgetDenied, match="CRITICAL"):
        _screen(gw)
    result = gw.complete_structured(
        role=ModelRole.SCREENING,
        purpose="portfolio_reassessment",
        schema_name="screening",
        schema=SCREENING_SCHEMA,
        messages=[{"role": "user", "content": "reassess QUAL"}],
        ticker="QUAL",
        critical=True,
    )
    assert result.payload["ticker"] == "QUAL"


def test_exact_10_hard_stop(tmp_path):
    gw = _gw(tmp_path, {"openai": ScriptedProvider({"screening": SCREEN}, name="openai")}, spent="10.00")
    assert gw.budget.status().mode is BudgetMode.EXHAUSTED
    with pytest.raises(BudgetExhausted):
        _screen(gw)


def test_restart_does_not_reset_monthly_budget(tmp_path):
    gw = _gw(tmp_path, {"openai": ScriptedProvider({"screening": SCREEN}, name="openai")}, spent="6.25")
    assert gw.budget.status().spent == Decimal("6.25")
    restarted = build_gateway(
        tmp_path,
        providers={"openai": ScriptedProvider({"screening": SCREEN}, name="openai")},
        now_fn=lambda: NOW,
    )
    assert restarted.budget.status().spent == Decimal("6.25")
    assert restarted.budget.status().remaining == Decimal("3.75")


def test_fallback_provider_uses_same_global_budget(tmp_path):
    openai = ScriptedProvider({"screening": SCREEN}, fail="outage", name="openai")
    anthropic = ScriptedProvider({"screening": SCREEN}, name="anthropic")
    gw = _gw(tmp_path, {"openai": openai, "anthropic": anthropic}, spent="1.00")
    result = _screen(gw)
    assert result.fallback_used is True
    assert result.provider == "anthropic"
    after = gw.budget.status()
    assert after.spent >= Decimal("1.00")
    assert after.spent <= Decimal("10")
    assert after.cap == Decimal("10")


def test_live_gateway_refuses_scripted_fallback(tmp_path):
    scripted = ScriptedProvider({"screening": SCREEN}, name="scripted")
    gw = build_gateway(
        tmp_path,
        providers={"openai": OpenAIProvider(api_key=None), "scripted": scripted},
        runtime_mode=RuntimeMode.LIVE,
        now_fn=lambda: NOW,
        config={**load_ai_config(), "roles": {**(load_ai_config().get("roles") or {}), "fallback": {"provider": "scripted", "model": "scripted"}}},
    )
    with pytest.raises(ProviderOutage, match="scripted provider is not allowed as a LIVE fallback|unavailable"):
        _screen(gw)
    assert scripted.calls == []
    assert gw.budget.status().calls_month == 0


def test_openai_key_only_from_openai_api_key():
    adapter = OpenAIProvider(environ={"OPENAI_API_KEY": "sk-from-env", "OPENAI_KEY": "nope"})
    assert adapter.available() is True
    other = OpenAIProvider(environ={"OPENAI_SECRET": "sk-other", "OPENAI_TOKEN": "sk-token"})
    assert other.available() is False
    empty = OpenAIProvider(environ={})
    assert empty.available() is False


def test_openai_transport_structured_output():
    seen = []

    def transport(url, body, timeout):
        seen.append((url, body))
        assert "/responses" in url
        assert "chat/completions" not in url
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
        assert body["text"]["format"]["name"] == "screening"
        assert "max_output_tokens" in body
        assert "max_tokens" not in body
        assert "response_format" not in body
        return {
            "id": "resp_test",
            "model": body["model"],
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": __import__("json").dumps(SCREEN)}]}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    from agentic_portfolio.ai.providers.base import ProviderRequest

    for model in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
        adapter = OpenAIProvider(api_key="sk-test", transport=transport)
        resp = adapter.complete(
            ProviderRequest(
                model=model,
                messages=[{"role": "user", "content": "x"}],
                schema_name="screening",
                schema=SCREENING_SCHEMA,
                reasoning_effort="low",
            )
        )
        assert resp.payload["ticker"] == "QUAL"
        assert resp.model == model
        assert resp.input_tokens == 10
        assert resp.output_tokens == 5
        assert "sk-test" not in __import__("json").dumps(resp.raw)
    assert all(body["model"] in {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"} for _, body in seen)
    assert all(body.get("reasoning") == {"effort": "low"} for _, body in seen)


def test_openai_http_error_redacts_secrets(monkeypatch):
    import io
    import urllib.error

    from agentic_portfolio.ai.providers.base import ProviderRequest

    def boom(req, timeout=None):
        fp = io.BytesIO(b'{"error":{"message":"bad key sk-live-secret-value-here"}}')
        raise urllib.error.HTTPError("https://api.openai.com/v1/responses", 401, "Unauthorized", hdrs={}, fp=fp)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    adapter = OpenAIProvider(api_key="sk-live-secret-value-here")
    with pytest.raises(ProviderOutage) as exc:
        adapter.complete(
            ProviderRequest(model="gpt-5.6-luna", messages=[{"role": "user", "content": "x"}], schema_name="screening", schema=SCREENING_SCHEMA)
        )
    assert "sk-live-secret-value-here" not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


def test_gpt56_pricing_table_matches_openai_docs():
    from agentic_portfolio.ai.pricing import estimate_cost

    cfg = load_ai_config()
    table = cfg["pricing_per_million"]
    assert table["gpt-5.6-luna"] == {"input": 0.20, "output": 1.20}
    assert table["gpt-5.6-terra"] == {"input": 2.00, "output": 12.00}
    assert table["gpt-5.6-sol"] == {"input": 4.00, "output": 20.00}
    assert cfg["roles"]["screening"]["model"] == "gpt-5.6-luna"
    assert cfg["roles"]["research"]["model"] == "gpt-5.6-terra"
    assert cfg["roles"]["escalation"]["model"] == "gpt-5.6-sol"
    assert cfg["budget"]["monthly_cap"] == 10.0
    assert cfg["budget"]["conservation_threshold"] == 8.0
    assert cfg["budget"]["critical_threshold"] == 9.5
    assert cfg["budget"]["hard_stop"] == 10.0
    assert estimate_cost(model="gpt-5.6-luna", input_tokens=1_000_000, output_tokens=1_000_000) == Decimal("1.40")
    assert estimate_cost(model="gpt-5.6-terra", input_tokens=1_000_000, output_tokens=1_000_000) == Decimal("14.00")
    assert estimate_cost(model="gpt-5.6-sol", input_tokens=1_000_000, output_tokens=1_000_000) == Decimal("24.00")
    assert estimate_cost(model="gpt-5.6-luna-2026-08-01", input_tokens=1_000_000, output_tokens=0) == Decimal("0.20")


def test_anthropic_transport_tool_schema():
    def transport(url, body, timeout):
        assert "messages" in url
        assert body["tool_choice"]["name"] == "screening"
        return {
            "model": "claude-sonnet-4-20250514",
            "content": [{"type": "tool_use", "name": "screening", "input": SCREEN}],
            "usage": {"input_tokens": 11, "output_tokens": 6},
        }

    adapter = AnthropicProvider(api_key="sk-ant", transport=transport)
    from agentic_portfolio.ai.providers.base import ProviderRequest

    resp = adapter.complete(
        ProviderRequest(model="claude-sonnet-4-20250514", messages=[{"role": "user", "content": "x"}], schema_name="screening", schema=SCREENING_SCHEMA)
    )
    assert resp.payload["score"] == 71.0
