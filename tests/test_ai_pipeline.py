"""Proposal-only LIVE AI pipeline, isolation, and scheduler safety."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from agentic_portfolio.ai.config import load_ai_config
from agentic_portfolio.ai.errors import DuplicateJobError, MissingBrokerFacts, PlacementForbidden, StaleSnapshotError
from agentic_portfolio.ai.pipeline import run_candidate_pipeline
from agentic_portfolio.ai.proposals import attempt_placement
from agentic_portfolio.ai.providers.scripted import ScriptedProvider
from agentic_portfolio.ai.safety import inspect_ai_module_for_forbidden_calls
from agentic_portfolio.ai.scheduler import JobLock, Scheduler
from agentic_portfolio.ai.store import AIArtifactStore
from agentic_portfolio.ai.identity import facts_from_payloads
from agentic_portfolio.ai.types import AIConfidence, LiveProposal, ProposalStatus, RecommendedAction
from agentic_portfolio.live.engine import refresh_live_portfolio
from agentic_portfolio.runtime import RuntimeMode
from agentic_portfolio.schemas import Decision
from tests.test_ai_gateway import NOW, SCREEN, _gw
from tests.test_live_identity import live_equity_payloads
from tests.test_live_mode import _accounts, _fetcher, _portfolio, _positions, _quotes

RESEARCH = {
    "ticker": "QUAL",
    "thesis": "Quality compounder versus cash.",
    "bull_case": "Growth continues.",
    "bear_case": "Multiple compresses.",
    "catalysts": ["earnings"],
    "risks": ["valuation"],
    "valuation_observations": "PE in growth context.",
    "technical_observations": "Supporting only.",
    "confidence": "MEDIUM",
    "recommended_action": "BUY_CANDIDATE",
}
DECISION = {
    "ticker": "QUAL",
    "action": "BUY_CANDIDATE",
    "confidence": "MEDIUM",
    "rationale": "Tiny starter, Risk Gate still decides.",
    "suggested_allocation_pct": 3.0,
    "suggested_max_dollars": 15.0,
    "reassessment_conditions": ["earnings miss"],
    "risk_notes": ["small NAV"],
}
RESPONSES = {"screening": SCREEN, "deep_research": RESEARCH, "portfolio_decision": DECISION}


def _providers():
    return {
        "openai": ScriptedProvider(RESPONSES, name="openai"),
        "anthropic": ScriptedProvider(RESPONSES, name="anthropic"),
    }


def _live_equity():
    snap, _ = facts_from_payloads("QCOR", live_equity_payloads(quote_as_of="2026-08-28T19:59:58+00:00"), now=NOW)
    return snap


def _live_snapshot(tmp_path: Path, now: datetime = NOW):
    return refresh_live_portfolio(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        now=now,
        root=tmp_path,
        persist=True,
    )


def test_ai_module_never_calls_place_or_cancel():
    assert inspect_ai_module_for_forbidden_calls() == []


def test_live_pipeline_cannot_call_placement(tmp_path):
    refresh = _live_snapshot(tmp_path)
    gw = _gw(tmp_path, _providers())
    gw.runtime_mode = RuntimeMode.LIVE.value
    result = run_candidate_pipeline(
        [_live_equity()],
        refresh.context,
        gw,
        runtime_mode=RuntimeMode.LIVE,
        root=tmp_path,
        now=NOW,
        snapshot=refresh.snapshot,
        snapshot_id=refresh.snapshot_id,
    )
    assert result.placement_attempted is False
    assert result.paper_contamination is False
    assert result.ai_calls > 0
    fake = LiveProposal(
        proposal_id="x",
        ticker="QUAL",
        action=RecommendedAction.BUY_CANDIDATE,
        decision=Decision.BUY,
        status=ProposalStatus.PROPOSED,
        confidence=AIConfidence.MEDIUM,
        rationale="",
        suggested_allocation_pct=3,
        suggested_max_dollars=15,
        capped_max_dollars=15,
        reassessment_conditions=[],
        risk_notes=[],
        risk_verdict=None,
        risk_reasons=[],
        runtime_mode="LIVE",
        source_of_truth="robinhood_agentic_account",
        paper_environment=False,
        live_order_placement=False,
        placement_attempted=False,
        context_id=None,
        screening_id=None,
        research_id=None,
        decision_id=None,
        snapshot_id=None,
        provider=None,
        model=None,
        cost=Decimal("0"),
        created_at=NOW.isoformat(),
    )
    with pytest.raises(PlacementForbidden):
        attempt_placement(fake, root=tmp_path)


def test_paper_ai_artifacts_cannot_contaminate_live(tmp_path):
    paper = AIArtifactStore(tmp_path, runtime_mode=RuntimeMode.PAPER)
    live = AIArtifactStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    paper.save_screening(
        "paper-screen",
        {"ticker": "NVDA", "score": 99, "runtime_mode": "PAPER", "paper_environment": True, "created_at": NOW.isoformat()},
    )
    live.save_screening(
        "live-screen",
        {"ticker": "QUAL", "score": 70, "created_at": NOW.isoformat()},
    )
    live_tickers = {row["ticker"] for row in live.screenings()}
    paper_tickers = {row["ticker"] for row in paper.screenings()}
    assert "NVDA" not in live_tickers
    assert "QUAL" in live_tickers
    assert "QUAL" not in paper_tickers


def test_stale_live_snapshot_prevents_decision(tmp_path):
    refresh = _live_snapshot(tmp_path)
    gw = _gw(tmp_path, _providers())
    stale = dict(refresh.snapshot)
    stale["created_at"] = "2026-08-30T10:00:00+00:00"
    cfg = load_ai_config()
    cfg["pipeline"] = dict(cfg["pipeline"])
    cfg["pipeline"]["stale_snapshot_seconds"] = 60
    cfg["pipeline"]["stale_snapshot_seconds_market_hours"] = 60
    with pytest.raises(StaleSnapshotError):
        run_candidate_pipeline(
            [_live_equity()],
            refresh.context,
            gw,
            runtime_mode=RuntimeMode.LIVE,
            root=tmp_path,
            now=NOW,
            snapshot=stale,
            config=cfg,
        )


def test_missing_broker_facts_fail_closed(tmp_path):
    refresh = _live_snapshot(tmp_path)
    broken = replace(refresh.context, current_nav=0.0)
    gw = _gw(tmp_path, _providers())
    with pytest.raises(MissingBrokerFacts):
        run_candidate_pipeline(
            [_live_equity()],
            broken,
            gw,
            runtime_mode=RuntimeMode.LIVE,
            root=tmp_path,
            now=NOW,
            snapshot=refresh.snapshot,
        )


def test_duplicate_scheduled_job_does_not_duplicate_research(tmp_path):
    refresh = _live_snapshot(tmp_path)
    gw = _gw(tmp_path, _providers())
    sched = Scheduler(
        tmp_path,
        gateway=gw,
        runtime_mode=RuntimeMode.LIVE,
        snapshots_fn=lambda: [_live_equity()],
        refresh_fn=lambda: refresh,
        now_fn=lambda: NOW,
    )
    first = sched.run_job("PREMARKET", now=NOW, snapshots=[_live_equity()])
    assert first["status"] == "OK"
    lock = JobLock(tmp_path / "state" / "scheduler" / "locks" / "PREMARKET.lock")
    assert lock.acquire(job="PREMARKET", now=NOW) is True
    try:
        second = sched.run_job("PREMARKET", now=NOW, snapshots=[_live_equity()])
        assert second["status"] == "SKIPPED_ALREADY_RUNNING"
        with pytest.raises(DuplicateJobError):
            sched.run_job("PREMARKET", now=NOW, snapshots=[_live_equity()], force=True)
    finally:
        lock.release()
    assert second["placement_attempted"] is False
    assert first.get("placement_attempted") is False


def test_run_live_ai_check_proposal_only(tmp_path):
    from scripts.run_live_ai_check import run_live_ai_check

    report = run_live_ai_check(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        root=tmp_path,
        persist=True,
        now=NOW,
        use_real_ai=False,
        snapshots=[_live_equity()],
    )
    assert report["ok"] is True
    assert report["placement_attempted"] is False
    assert report["paper_contamination"] is False
    assert report["LIVE_ORDER_PLACEMENT"] is False
    assert report["nav"] == 500.0
    assert report["NAV"] == 500.0
    assert report["cash"] == 500.0
    assert report["buying_power"] == 500.0
    assert report["runtime"] == "LIVE"
    assert report["real_provider"] is False
    assert report["scripted_provider"] is True
    assert report["provider"] == "scripted"
    assert report["provider_call_attempted"] is True
    assert report["explicit_ticker_status"] == "NOT_REQUESTED"
    assert report["candidate"] == "QCOR"
    assert report["screening_result"]
    assert report["research_result"]
    assert report["portfolio_decision"]
    assert report["risk_gate_result"]
    assert report["proposal_result"]
    assert "budget_before" in report
    assert "budget_after" in report
    assert "budget_remaining" in report
    assert "actual_input_tokens" in report
    assert "actual_output_tokens" in report
    blob = __import__("json").dumps(report)
    assert "sk-" not in blob


def test_real_provider_refuses_without_openai_key(tmp_path):
    from scripts.run_live_ai_check import run_live_ai_check

    report = run_live_ai_check(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        root=tmp_path,
        persist=True,
        now=NOW,
        use_real_ai=True,
        environ={},
    )
    assert report["ok"] is False
    assert any("OPENAI_API_KEY" in str(x) for x in report["fail_reasons"])
    assert report["placement_attempted"] is False
    assert report["provider_call_attempted"] is False
    assert __import__("json").dumps(report).count("sk-") == 0


def test_real_provider_fails_if_scripted_substituted(tmp_path):
    from scripts.run_live_ai_check import run_live_ai_check

    report = run_live_ai_check(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        root=tmp_path,
        persist=True,
        now=NOW,
        use_real_ai=True,
        providers={"openai": ScriptedProvider(RESPONSES, name="openai")},
        environ={"OPENAI_API_KEY": "sk-live-secret-MUST-NOT-LEAK"},
        snapshots=[_live_equity()],
    )
    assert report["ok"] is False
    assert "scripted_provider_used" in report["fail_reasons"]
    assert report["scripted_provider"] is True
    assert report["real_provider"] is False
    assert report["placement_attempted"] is False
    blob = __import__("json").dumps(report)
    assert "sk-live-secret-MUST-NOT-LEAK" not in blob


def test_real_provider_mock_openai_responses_and_budget(tmp_path):
    import json

    from agentic_portfolio.ai.providers.openai import OpenAIProvider
    from scripts.run_live_ai_check import run_live_ai_check

    def transport(url, body, timeout):
        assert "/responses" in url
        assert "chat/completions" not in url
        name = body["text"]["format"]["name"]
        model = body["model"]
        payload = RESPONSES[name]
        return {
            "id": "resp_mock",
            "model": model,
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(payload)}]}],
            "usage": {"input_tokens": 20, "output_tokens": 10},
        }

    adapter = OpenAIProvider(api_key="sk-test", transport=transport)
    report = run_live_ai_check(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        root=tmp_path,
        persist=True,
        now=NOW,
        use_real_ai=True,
        providers={"openai": adapter},
        environ={"OPENAI_API_KEY": "sk-live-secret-MUST-NOT-LEAK"},
        snapshots=[_live_equity()],
    )
    assert report["ok"] is True
    assert report["real_provider"] is True
    assert report["scripted_provider"] is False
    assert report["provider"] == "openai"
    assert report["provider_call_attempted"] is True
    assert report["placement_attempted"] is False
    assert report["paper_contamination"] is False
    assert report["LIVE_ORDER_PLACEMENT"] is False
    assert report["actual_input_tokens"] > 0
    assert report["actual_output_tokens"] > 0
    assert report["actual_cost"] > 0
    assert report["budget_after"]["spent"] >= report["budget_before"]["spent"]
    assert report["budget_remaining"] == report["budget_after"]["remaining"]
    assert "gpt-5.6-luna" in report["models"] or report["models_configured"]["screening"] == "gpt-5.6-luna"
    blob = json.dumps(report)
    assert "sk-live-secret-MUST-NOT-LEAK" not in blob
    assert "sk-test" not in blob


def test_live_ai_cli_does_not_silently_script_when_key_present():
    from scripts.run_live_ai_check import resolve_live_ai_provider_mode

    with pytest.raises(ValueError, match="silently substitute"):
        resolve_live_ai_provider_mode(use_real_ai=False, scripted=False, environ={"OPENAI_API_KEY": "sk-present"})
    assert resolve_live_ai_provider_mode(use_real_ai=True, scripted=False, environ={"OPENAI_API_KEY": "sk-present"}) is True
    assert resolve_live_ai_provider_mode(use_real_ai=False, scripted=True, environ={"OPENAI_API_KEY": "sk-present"}) is False
    assert resolve_live_ai_provider_mode(use_real_ai=False, scripted=False, environ={}) is False


def test_production_execution_flags_unchanged():
    from agentic_portfolio.ai.config import load_ai_config
    from agentic_portfolio.ai.safety import LIVE_ORDER_PLACEMENT, inspect_ai_module_for_forbidden_calls
    from agentic_portfolio.policy import load_account_rules

    rules = load_account_rules()
    exe = rules["execution"]
    assert exe["auto_execution"] is False
    assert exe["live_trade_actions_allowed"] is False
    assert LIVE_ORDER_PLACEMENT is False
    cfg = load_ai_config()
    assert cfg["invariants"]["LIVE_ORDER_PLACEMENT"] is False
    assert inspect_ai_module_for_forbidden_calls() == []


def test_use_real_ai_without_candidate_does_not_report_provider_call_failed(tmp_path):
    import json

    from agentic_portfolio.ai.providers.openai import OpenAIProvider
    from scripts.run_live_ai_check import run_live_ai_check

    def transport(url, body, timeout):
        raise AssertionError("OpenAI must not be called when no candidate exists")

    report = run_live_ai_check(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        root=tmp_path,
        persist=True,
        now=NOW,
        use_real_ai=True,
        providers={"openai": OpenAIProvider(api_key="sk-test", transport=transport)},
        environ={"OPENAI_API_KEY": "sk-live-secret-MUST-NOT-LEAK"},
        snapshots=[],
    )
    assert report["provider_call_attempted"] is False
    assert "provider_call_failed" not in report["fail_reasons"]
    assert report["candidate"] is None
    assert report["placement_attempted"] is False
    assert report["LIVE_ORDER_PLACEMENT"] is False
    assert json.dumps(report).count("sk-") == 0


def test_ticker_qual_uses_observe_live_candidate_not_hardcoded_facts(tmp_path):
    import inspect
    import json
    from pathlib import Path

    from agentic_portfolio.adapters.robinhood_read import MappingReadOnlyFetcher
    from agentic_portfolio.ai.providers.openai import OpenAIProvider
    from scripts.run_live_ai_check import run_live_ai_check
    from tests.test_live_identity import qual_etf_payloads

    src = Path("scripts/run_live_ai_check.py").read_text(encoding="utf-8")
    assert "observe_live_candidate" in src
    assert "bootstrap_readonly_broker_runtime" in src
    assert "--ticker" in src
    assert "Diagnostic/testing only" in src
    assert "Quality Check Co" not in src
    assert "80000000000" not in src
    from scripts import run_live_ai_check as live_check_mod

    assert "observe_live_candidate" in inspect.getsource(live_check_mod._observe_diagnostic_ticker)

    openai_calls: list[dict] = []

    def transport(url, body, timeout):
        openai_calls.append(body)
        name = body["text"]["format"]["name"]
        model = body["model"]
        payload = RESPONSES[name]
        return {
            "id": "resp_mock",
            "model": model,
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(payload)}]}],
            "usage": {"input_tokens": 20, "output_tokens": 10},
        }

    instrument = MappingReadOnlyFetcher.from_payloads("QUAL", qual_etf_payloads())
    report = run_live_ai_check(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        root=tmp_path,
        persist=True,
        now=NOW,
        use_real_ai=True,
        providers={"openai": OpenAIProvider(api_key="sk-test", transport=transport)},
        environ={"OPENAI_API_KEY": "sk-live-secret-MUST-NOT-LEAK"},
        ticker="QUAL",
        instrument_fetcher=instrument,
    )
    assert instrument.calls == [
        "get_equity_tradability",
        "get_equity_fundamentals",
        "search",
        "get_equity_quotes",
    ]
    assert "place_equity_order" not in instrument.calls
    assert report["ticker"] == "QUAL"
    assert report["explicit_ticker_status"] == "VALID"
    assert report["placement_attempted"] is False
    assert report["LIVE_ORDER_PLACEMENT"] is False
    obs = report["candidate_discovery"]["observe_validation"]
    assert obs["ticker"] == "QUAL"
    assert obs["eligible_for_ai"] is True
    assert obs["synthetic_data_detected"] is False
    explicit = report["explicit_ticker_validation"]
    assert explicit["status"] == "VALID"
    assert explicit["eligible_for_ai"] is True
    assert explicit["rejection_reasons"] == []
    assert explicit["synthetic_data_detected"] is False
    assert explicit["security_name"]
    assert explicit["security_type"]
    assert explicit["quote_value"] is not None
    assert explicit["quote_freshness"]
    assert explicit["liquidity_value"] is not None
    assert explicit["liquidity_freshness"]
    assert "Quality Check Co" not in json.dumps(report)
    assert openai_calls, "validated QUAL must reach the provider adapter"
    assert report["provider_call_attempted"] is True
    assert report["candidate"] == "QUAL"
    assert report["screening_result"]
    assert report["screening_result"][0]["ticker"] == "QUAL"
    assert "provider_call_failed" not in report["fail_reasons"]
    assert "CANDIDATE_INVALID" not in report["fail_reasons"]
    assert report["placement_attempted"] is False
    assert "place_equity_order" in (report.get("mcp_not_called") or [])
    assert "place_equity_order" not in instrument.calls


def test_ineligible_ticker_does_not_call_openai_or_report_provider_call_failed(tmp_path):
    import json

    from agentic_portfolio.adapters.portfolio_facts import LiveDataUnavailable
    from agentic_portfolio.adapters.robinhood_read import MappingReadOnlyFetcher
    from agentic_portfolio.ai.providers.openai import OpenAIProvider
    from scripts.run_live_ai_check import run_live_ai_check

    def transport(url, body, timeout):
        raise AssertionError("OpenAI must not be called for an ineligible ticker")

    instrument = MappingReadOnlyFetcher.from_payloads(
        "QUAL",
        {},
        error=LiveDataUnavailable("readonly_mcp_unreachable: connection refused"),
    )
    report = run_live_ai_check(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        root=tmp_path,
        persist=True,
        now=NOW,
        use_real_ai=True,
        providers={"openai": OpenAIProvider(api_key="sk-test", transport=transport)},
        environ={"OPENAI_API_KEY": "sk-live-secret-MUST-NOT-LEAK"},
        ticker="QUAL",
        instrument_fetcher=instrument,
    )
    assert report["ticker"] == "QUAL"
    assert report["explicit_ticker_status"] == "INVALID"
    assert report["provider_call_attempted"] is False
    assert report["candidate"] is None
    assert "CANDIDATE_INVALID" in report["fail_reasons"]
    assert "provider_call_failed" not in report["fail_reasons"]
    assert report["candidate_discovery"]["ticker_eligible_for_ai"] is False
    assert report["placement_attempted"] is False
    reasons = " ".join(report["candidate_discovery"]["observe_validation"]["rejection_reasons"])
    assert "readonly_mcp_unreachable" in reasons
    assert json.dumps(report).count("sk-") == 0


def test_ineligible_ticker_does_not_require_openai_key(tmp_path):
    from agentic_portfolio.adapters.robinhood_read import MappingReadOnlyFetcher
    from scripts.run_live_ai_check import run_live_ai_check

    report = run_live_ai_check(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        root=tmp_path,
        persist=True,
        now=NOW,
        use_real_ai=True,
        environ={},
        ticker="QUAL",
        instrument_fetcher=MappingReadOnlyFetcher.from_payloads("QUAL", {}),
    )
    assert report["provider_call_attempted"] is False
    assert report["explicit_ticker_status"] == "INVALID"
    assert "CANDIDATE_INVALID" in report["fail_reasons"]
    assert "provider_call_failed" not in report["fail_reasons"]
    assert not any("OPENAI_API_KEY" in str(x) for x in report["fail_reasons"])
    assert report["placement_attempted"] is False


def test_eligible_ticker_calls_openai_only_after_validation(tmp_path):
    import json

    from agentic_portfolio.adapters.robinhood_read import MappingReadOnlyFetcher
    from agentic_portfolio.ai.providers.openai import OpenAIProvider
    from scripts.run_live_ai_check import run_live_ai_check

    openai_calls: list[dict] = []

    def transport(url, body, timeout):
        openai_calls.append(body)
        name = body["text"]["format"]["name"]
        model = body["model"]
        payload = dict(RESPONSES[name])
        payload["ticker"] = "QCOR"
        return {
            "id": "resp_mock",
            "model": model,
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(payload)}]}],
            "usage": {"input_tokens": 20, "output_tokens": 10},
        }

    instrument = MappingReadOnlyFetcher.from_payloads(
        "QCOR",
        live_equity_payloads(quote_as_of="2026-08-28T19:59:58+00:00"),
    )
    report = run_live_ai_check(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        root=tmp_path,
        persist=True,
        now=NOW,
        use_real_ai=True,
        providers={"openai": OpenAIProvider(api_key="sk-test", transport=transport)},
        environ={"OPENAI_API_KEY": "sk-live-secret-MUST-NOT-LEAK"},
        ticker="QCOR",
        instrument_fetcher=instrument,
    )
    assert report["ticker"] == "QCOR"
    assert report["explicit_ticker_status"] == "VALID"
    assert report["candidate"] == "QCOR"
    assert report["candidate_discovery"]["observe_validation"]["eligible_for_ai"] is True
    assert report["placement_attempted"] is False
    assert report["LIVE_ORDER_PLACEMENT"] is False
    assert openai_calls
    assert report["provider_call_attempted"] is True
    assert report["real_provider"] is True
    assert "provider_call_failed" not in report["fail_reasons"]
    assert "CANDIDATE_INVALID" not in report["fail_reasons"]


def test_valid_explicit_ticker_does_not_run_ordinary_discovery(tmp_path, monkeypatch):
    import json

    from agentic_portfolio.adapters.robinhood_read import MappingReadOnlyFetcher
    from agentic_portfolio.ai.providers.openai import OpenAIProvider
    from scripts.run_live_ai_check import run_live_ai_check
    from tests.test_live_identity import qual_etf_payloads

    def boom(*args, **kwargs):
        raise AssertionError("ordinary universe discovery must not run for --ticker")

    monkeypatch.setattr("agentic_portfolio.ai.pipeline.run_discovery", boom)

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

    instrument = MappingReadOnlyFetcher.from_payloads("QUAL", qual_etf_payloads())
    report = run_live_ai_check(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        root=tmp_path,
        persist=True,
        now=NOW,
        use_real_ai=True,
        providers={"openai": OpenAIProvider(api_key="sk-test", transport=transport)},
        environ={"OPENAI_API_KEY": "sk-live-secret-MUST-NOT-LEAK"},
        ticker="QUAL",
        instrument_fetcher=instrument,
        snapshots=[_live_equity()],
    )
    assert report["explicit_ticker_status"] == "VALID"
    assert report["candidate"] == "QUAL"
    assert report["candidate"] != "QCOR"
    assert openai_calls
    assert report["provider_call_attempted"] is True
    assert report["screening_result"]
    assert report["placement_attempted"] is False
    assert report["LIVE_ORDER_PLACEMENT"] is False
    assert "place_equity_order" not in instrument.calls
    assert "place_equity_order" not in (report.get("mcp_tools_used") or [])


def test_invalid_explicit_ticker_never_reaches_provider(tmp_path, monkeypatch):
    import json

    from agentic_portfolio.adapters.robinhood_read import MappingReadOnlyFetcher
    from agentic_portfolio.ai.providers.openai import OpenAIProvider
    from scripts.run_live_ai_check import run_live_ai_check

    def boom(*args, **kwargs):
        raise AssertionError("ordinary universe discovery must not run for --ticker")

    monkeypatch.setattr("agentic_portfolio.ai.pipeline.run_discovery", boom)

    def transport(url, body, timeout):
        raise AssertionError("OpenAI must not be called for an invalid explicit ticker")

    report = run_live_ai_check(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        root=tmp_path,
        persist=True,
        now=NOW,
        use_real_ai=True,
        providers={"openai": OpenAIProvider(api_key="sk-test", transport=transport)},
        environ={"OPENAI_API_KEY": "sk-live-secret-MUST-NOT-LEAK"},
        ticker="ZZZZ",
        instrument_fetcher=MappingReadOnlyFetcher.from_payloads("ZZZZ", {}),
        snapshots=[_live_equity()],
    )
    assert report["explicit_ticker_status"] == "INVALID"
    assert report["candidate"] is None
    assert report["provider_call_attempted"] is False
    assert report["screening_result"] == []
    assert "CANDIDATE_INVALID" in report["fail_reasons"]
    assert report["placement_attempted"] is False
    assert json.dumps(report).count("sk-") == 0


def test_check_live_candidate_facts_and_explicit_ticker_agree_on_same_payloads(tmp_path, capsys):
    import inspect
    import json

    from agentic_portfolio.adapters.robinhood_read import MappingReadOnlyFetcher
    from agentic_portfolio.ai import identity as identity_mod
    from scripts import run_live_ai_check as live_mod
    from scripts.check_live_candidate_facts import main as check_main
    from scripts.run_live_ai_check import run_live_ai_check
    from tests.test_live_identity import qual_etf_payloads

    check_src = inspect.getsource(check_main)
    observe_src = inspect.getsource(live_mod._observe_diagnostic_ticker)
    obs_src = inspect.getsource(identity_mod.observe_live_candidate)
    facts_src = inspect.getsource(identity_mod.facts_from_payloads)
    assert "observe_live_candidate" in check_src
    assert "config=cfg" in check_src
    assert "observe_live_candidate" in observe_src
    assert "config=config" in observe_src
    assert "facts_from_payloads" in obs_src
    assert "config=config" in obs_src
    assert "validate_live_candidate" in facts_src
    assert "config=config" in facts_src

    payloads = qual_etf_payloads()
    check_code = check_main(
        ["QUAL"],
        fetcher=MappingReadOnlyFetcher.from_payloads("QUAL", payloads),
        now=NOW,
    )
    assert check_code == 0
    check_report = json.loads(capsys.readouterr().out)
    report = run_live_ai_check(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        root=tmp_path,
        persist=True,
        now=NOW,
        use_real_ai=False,
        environ={},
        ticker="QUAL",
        instrument_fetcher=MappingReadOnlyFetcher.from_payloads("QUAL", payloads),
    )
    explicit = report["explicit_ticker_validation"]
    assert explicit is not None
    assert check_report["status"] == explicit["status"] == "VALID"
    assert check_report["eligible_for_ai"] is True
    assert explicit["eligible_for_ai"] is True
    assert check_report["rejection_reasons"] == explicit["rejection_reasons"] == []
    assert check_report["synthetic_data_detected"] is False
    assert explicit["synthetic_data_detected"] is False
    name = check_report["security_name"]
    assert (name.get("value") if isinstance(name, dict) else name) == explicit["security_name"]
    stype = check_report["security_type"]
    assert (stype.get("value") if isinstance(stype, dict) else stype) == explicit["security_type"]
    quote = check_report["quote"] if isinstance(check_report.get("quote"), dict) else {}
    assert quote.get("value") == explicit["quote_value"]
    assert check_report["freshness"]["quote"] == explicit["quote_freshness"]
    liquidity = check_report["liquidity"] if isinstance(check_report.get("liquidity"), dict) else {}
    assert liquidity.get("value") == explicit["liquidity_value"]
    assert check_report["freshness"]["liquidity"] == explicit["liquidity_freshness"]
    assert report["explicit_ticker_status"] == "VALID"
    assert report["placement_attempted"] is False
    assert report["LIVE_ORDER_PLACEMENT"] is False


def test_check_live_candidate_facts_and_explicit_ticker_agree_when_invalid(tmp_path, capsys):
    import json

    from agentic_portfolio.adapters.robinhood_read import MappingReadOnlyFetcher
    from scripts.check_live_candidate_facts import main as check_main
    from scripts.run_live_ai_check import run_live_ai_check

    check_main(["QUAL"], fetcher=MappingReadOnlyFetcher.from_payloads("QUAL", {}), now=NOW)
    check_report = json.loads(capsys.readouterr().out)
    report = run_live_ai_check(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        root=tmp_path,
        persist=True,
        now=NOW,
        use_real_ai=False,
        environ={},
        ticker="QUAL",
        instrument_fetcher=MappingReadOnlyFetcher.from_payloads("QUAL", {}),
    )
    explicit = report["explicit_ticker_validation"]
    assert check_report["eligible_for_ai"] is False
    assert explicit["eligible_for_ai"] is False
    assert check_report["status"] == explicit["status"]
    assert check_report["rejection_reasons"] == explicit["rejection_reasons"]
    assert check_report["synthetic_data_detected"] == explicit["synthetic_data_detected"]
    assert report["explicit_ticker_status"] == "INVALID"
    assert "CANDIDATE_INVALID" in report["fail_reasons"]
    assert report["placement_attempted"] is False
    assert report["provider_call_attempted"] is False


def test_system_continues_when_ai_blocked(tmp_path):
    refresh = _live_snapshot(tmp_path)
    gw = _gw(tmp_path, _providers(), spent="10.00")
    result = run_candidate_pipeline(
        [_live_equity()],
        refresh.context,
        gw,
        runtime_mode=RuntimeMode.LIVE,
        root=tmp_path,
        now=NOW,
        snapshot=refresh.snapshot,
        snapshot_id=refresh.snapshot_id,
    )
    assert result.placement_attempted is False
    assert result.ai_blocked is True
    assert result.nav == 500.0
