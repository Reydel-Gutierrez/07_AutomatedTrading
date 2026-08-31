"""Shared read-only Robinhood MCP bootstrap for CLI and the production app."""

from __future__ import annotations

import inspect
import json

import pytest

from agentic_portfolio.adapters.portfolio_facts import LiveDataUnavailable
from agentic_portfolio.adapters.readonly_runtime import (
    READONLY_MODE,
    SHARED_PRODUCTION_TRANSPORT,
    GuardedReadonlyTransport,
    StreamableHttpMcpTransport,
    bind_readonly_broker_transport,
    bootstrap_readonly_broker_runtime,
    is_forbidden_observation_tool,
    unwrap_mcp_tool_result,
)
from agentic_portfolio.adapters.robinhood_read import MappingReadOnlyFetcher
from agentic_portfolio.ai.providers.openai import OpenAIProvider
from agentic_portfolio.dashboard.app import create_app
from agentic_portfolio.runtime import bootstrap_readonly_broker_runtime as runtime_bootstrap
from scripts.check_live_candidate_facts import main as check_main
from scripts.run_live_ai_check import run_live_ai_check
from tests.test_ai_gateway import NOW
from tests.test_ai_pipeline import RESPONSES
from tests.test_live_identity import qual_etf_payloads
from tests.test_live_mode import _accounts, _fetcher, _portfolio, _positions, _quotes

WRITE_TOOLS = (
    "place_equity_order",
    "cancel_equity_order",
    "review_equity_order",
    "preview_crypto_order",
    "place_option_order",
)


def _qual_transport():
    payloads = qual_etf_payloads()

    def transport(tool, **kwargs):
        mapping = {
            "get_equity_tradability": payloads["tradability"],
            "get_equity_fundamentals": payloads["fundamentals"],
            "search": payloads["search"],
            "get_equity_quotes": payloads["quotes"],
        }
        return mapping[tool]

    return transport


def test_cli_bootstrap_binds_mock_authorized_readonly_transport(capsys):
    runtime = bind_readonly_broker_transport(_qual_transport())
    assert runtime.bound is True
    assert runtime.mode == READONLY_MODE
    assert runtime.source == SHARED_PRODUCTION_TRANSPORT
    assert runtime.initialization_error is None
    code = check_main(["QUAL"], now=NOW)
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["readonly_broker_transport"] == {
        "bound": True,
        "mode": "READ_ONLY",
        "source": SHARED_PRODUCTION_TRANSPORT,
        "initialization_error": None,
    }
    assert report["eligible_for_ai"] is True
    assert report["ai_provider_called"] is False
    assert report["ai_cost"] == 0
    assert report["status"] == "VALID"


def test_app_and_cli_use_same_bootstrap_path(tmp_path):
    bound = bind_readonly_broker_transport(_qual_transport())
    app = create_app(tmp_path)
    assert runtime_bootstrap is bootstrap_readonly_broker_runtime
    assert create_app.__globals__["bootstrap_readonly_broker_runtime"] is runtime_bootstrap
    from scripts import check_live_candidate_facts, run_live_ai_check, run_scheduler

    assert check_live_candidate_facts.bootstrap_readonly_broker_runtime is runtime_bootstrap
    assert run_live_ai_check.bootstrap_readonly_broker_runtime is runtime_bootstrap
    assert run_scheduler.bootstrap_readonly_broker_runtime is runtime_bootstrap
    assert "bootstrap_readonly_broker_runtime" in inspect.getsource(create_app)
    stored = app.config["READONLY_BROKER_RUNTIME"]
    assert stored.bound is True
    assert stored.source == SHARED_PRODUCTION_TRANSPORT
    assert stored.as_report() == bound.as_report()


def test_failure_to_bind_fails_closed(capsys):
    runtime = bootstrap_readonly_broker_runtime()
    assert runtime.bound is False
    assert runtime.mode == READONLY_MODE
    assert runtime.initialization_error
    assert "AGENTIC_READONLY_MCP_TOKEN" in runtime.initialization_error
    code = check_main(["QUAL"], now=NOW)
    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["readonly_broker_transport"]["bound"] is False
    assert report["readonly_broker_transport"]["initialization_error"]
    assert report["eligible_for_ai"] is False
    assert report["ai_provider_called"] is False
    assert report["rejection_reasons"][0] == runtime.initialization_error
    name = report["security_name"]
    assert name is None or name.get("unavailable") or name.get("value") is None


def test_http_initialize_failure_fails_closed(monkeypatch):
    monkeypatch.setenv("AGENTIC_READONLY_MCP_TOKEN", "test-token-not-a-secret-for-unit")
    monkeypatch.setenv("AGENTIC_READONLY_MCP_URL", "https://example.invalid/mcp")

    def fail_post(self, body):
        raise LiveDataUnavailable("readonly_mcp_unreachable: MCP HTTP 401 for initialize")

    monkeypatch.setattr(
        "agentic_portfolio.adapters.readonly_runtime.StreamableHttpMcpTransport._default_post",
        fail_post,
    )
    runtime = bootstrap_readonly_broker_runtime(force=True)
    assert runtime.bound is False
    assert "401" in (runtime.initialization_error or "")
    assert runtime.fetcher is None


def test_no_write_order_capability_is_exposed():
    runtime = bind_readonly_broker_transport(_qual_transport())
    fetcher = runtime.fetcher
    assert fetcher is not None
    for tool in WRITE_TOOLS:
        assert not hasattr(fetcher, tool)
        assert not hasattr(runtime, tool)
        with pytest.raises(LiveDataUnavailable, match="refused forbidden MCP tool"):
            runtime.invoke(tool)
        assert is_forbidden_observation_tool(tool)
    with pytest.raises(LiveDataUnavailable, match="refused forbidden MCP tool"):
        runtime.invoke("initiate_withdrawals")
    guarded = GuardedReadonlyTransport(_qual_transport())
    with pytest.raises(LiveDataUnavailable, match="refused forbidden MCP tool"):
        guarded("place_equity_order", symbol="QUAL")


def test_qual_fetched_without_injected_payloads_once_bound(capsys):
    bind_readonly_broker_transport(_qual_transport())
    code = check_main(["QUAL"], now=NOW)
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ticker"] == "QUAL"
    assert report["security_name"]["value"] == "iShares MSCI USA Quality Factor ETF"
    assert report["security_type"]["value"] == "etf"
    quote = report["quote"] if isinstance(report.get("quote"), dict) else {}
    assert float(quote.get("value")) == pytest.approx(223.61)
    assert report["freshness"]["quote"] == "LAST_SESSION"
    assert report["eligible_for_ai"] is True
    assert report["synthetic_data_detected"] is False
    assert report["ai_provider_called"] is False
    assert report["readonly_broker_transport"]["bound"] is True
    assert "payloads_file" not in report["read_only_source_calls"]


def test_zero_dollar_diagnostic_does_not_call_ai(capsys):
    bind_readonly_broker_transport(MappingReadOnlyFetcher.from_payloads("QUAL", qual_etf_payloads()))
    code = check_main(["QUAL"], now=NOW)
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ai_provider_called"] is False
    assert report["ai_cost"] == 0
    src = inspect.getsource(check_main)
    assert "build_gateway" not in src
    assert "OpenAIProvider" not in src


def test_live_ai_check_reaches_provider_only_after_verified_live_facts(tmp_path):
    openai_calls: list[dict] = []

    def transport(url, body, timeout):
        openai_calls.append(body)
        name = body["text"]["format"]["name"]
        model = body["model"]
        payload = dict(RESPONSES[name])
        payload["ticker"] = "QUAL"
        return {
            "id": "resp_mock",
            "model": model,
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(payload)}]}],
            "usage": {"input_tokens": 20, "output_tokens": 10},
        }

    bind_readonly_broker_transport(_qual_transport())
    report = run_live_ai_check(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        root=tmp_path,
        persist=True,
        now=NOW,
        use_real_ai=True,
        providers={"openai": OpenAIProvider(api_key="sk-test", transport=transport)},
        environ={"OPENAI_API_KEY": "sk-live-secret-MUST-NOT-LEAK"},
        ticker="QUAL",
    )
    assert report["readonly_broker_transport"]["bound"] is True
    assert report["explicit_ticker_status"] == "VALID"
    assert report["explicit_ticker_validation"]["eligible_for_ai"] is True
    assert openai_calls
    assert report["provider_call_attempted"] is True
    assert report["placement_attempted"] is False
    assert "place_equity_order" not in (report.get("mcp_tools_used") or [])
    assert json.dumps(report).count("sk-") == 0


def test_unbound_live_ai_check_does_not_call_provider(tmp_path):
    def transport(url, body, timeout):
        raise AssertionError("OpenAI must not be called when the readonly transport is unbound")

    report = run_live_ai_check(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        root=tmp_path,
        persist=True,
        now=NOW,
        use_real_ai=True,
        providers={"openai": OpenAIProvider(api_key="sk-test", transport=transport)},
        environ={"OPENAI_API_KEY": "sk-live-secret-MUST-NOT-LEAK"},
        ticker="QUAL",
    )
    assert report["readonly_broker_transport"]["bound"] is False
    assert report["readonly_broker_transport"]["initialization_error"]
    assert report["provider_call_attempted"] is False
    assert report["explicit_ticker_status"] == "INVALID"
    assert report["placement_attempted"] is False


def test_no_place_review_cancel_methods_available():
    runtime = bind_readonly_broker_transport(_qual_transport())
    for name in ("place_equity_order", "review_equity_order", "cancel_equity_order"):
        assert not callable(getattr(runtime.fetcher, name, None))
        assert getattr(runtime.fetcher, name, None) is None
    payload = runtime.invoke("get_equity_tradability", symbols=["QUAL"])
    assert payload["data"]["results"][0]["symbol"] == "QUAL"


def test_unwrap_mcp_tool_result_reads_structured_and_text():
    structured = unwrap_mcp_tool_result({"result": {"structuredContent": {"data": {"ok": True}}}})
    assert structured == {"data": {"ok": True}}
    text = unwrap_mcp_tool_result({"result": {"content": [{"type": "text", "text": json.dumps({"data": {"results": []}})}]}})
    assert text == {"data": {"results": []}}


def test_http_transport_does_not_store_write_methods():
    client = StreamableHttpMcpTransport("https://example.invalid/mcp", "token")
    for name in WRITE_TOOLS:
        assert not hasattr(client, name) or not callable(getattr(client, name, None))
        assert name not in dir(client) or not callable(getattr(client, name, None))
    assert callable(client)
