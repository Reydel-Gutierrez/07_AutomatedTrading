"""LIVE security identity, provenance, ETF vs equity facts, and fixture rejection."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from agentic_portfolio.adapters.discovery_source import SymbolPayloads, assemble_snapshot
from agentic_portfolio.ai.context import assemble_context
from agentic_portfolio.ai.identity import collect_candidate_facts, facts_from_payloads, validate_live_candidate
from agentic_portfolio.ai.pipeline import run_candidate_pipeline
from agentic_portfolio.ai.providers.scripted import ScriptedProvider
from agentic_portfolio.ai.safety import LIVE_ORDER_PLACEMENT
from agentic_portfolio.discovery.snapshot import SecuritySnapshot
from agentic_portfolio.runtime import RuntimeMode
from agentic_portfolio.schemas import (
    CandidateValidationStatus,
    ClassificationResult,
    ClassificationStatus,
    FreshnessStatus,
    LiquidityEvidence,
    SecurityClass,
)
from agentic_portfolio.sectors import CanonicalSector, SectorStatus
from tests.conftest import ctx
from tests.test_ai_gateway import NOW, SCREEN, _gw
from tests.test_discovery import _quality_core
from tests.test_live_mode import _accounts, _fetcher, _portfolio, _positions, _quotes

RESPONSES = {
    "screening": SCREEN,
    "deep_research": {
        "ticker": "QCOR",
        "thesis": "Quality compounder versus cash.",
        "bull_case": "Growth continues.",
        "bear_case": "Multiple compresses.",
        "catalysts": ["earnings"],
        "risks": ["valuation"],
        "valuation_observations": "PE in growth context.",
        "technical_observations": "Supporting only.",
        "confidence": "MEDIUM",
        "recommended_action": "BUY_CANDIDATE",
    },
    "portfolio_decision": {
        "ticker": "QCOR",
        "action": "BUY_CANDIDATE",
        "confidence": "MEDIUM",
        "rationale": "Tiny starter, Risk Gate still decides.",
        "suggested_allocation_pct": 3.0,
        "suggested_max_dollars": 15.0,
        "reassessment_conditions": ["earnings miss"],
        "risk_notes": ["small NAV"],
    },
}


def _wrap(symbol: str, **fields):
    return {"data": {"results": [{"symbol": symbol, **fields}]}}


def qual_etf_payloads(*, quote_as_of: str | None = None) -> dict:
    as_of = quote_as_of or "2026-08-28T19:59:58+00:00"
    return {
        "search": {
            "data": {
                "results": [
                    {
                        "instrument_id": "ecf1119a-d382-46bc-9d40-db9c442ed457",
                        "symbol": "QUAL",
                        "name": "iShares MSCI USA Quality Factor ETF",
                    },
                    {
                        "instrument_id": "5ea5e761-1747-4911-beec-5a24af338329",
                        "symbol": "QCOM",
                        "name": "QUALCOMM Incorporated Common Stock",
                        "simple_name": "Qualcomm",
                    },
                ]
            }
        },
        "tradability": _wrap(
            "QUAL",
            name="iShares MSCI USA Quality Factor ETF",
            state="active",
            tradeable=True,
        ),
        "fundamentals": _wrap(
            "QUAL",
            description="QUAL tracks an index of US large- and mid-cap stocks, selected and weighted by high ROE, stable earnings growth and low debt/equity, relative to peers in each sector.",
            sector="Miscellaneous",
            industry="Investment Trusts Or Mutual Funds",
            market_cap="47930803500.000000",
            pe_ratio="27.822700",
            pb_ratio="7.689672",
            average_volume="805182.947644",
            average_volume_2_weeks="805182.947644",
            high_52_weeks="227.320000",
            low_52_weeks="185.885000",
        ),
        "quotes": {
            "data": {
                "results": [
                    {
                        "quote": {
                            "symbol": "QUAL",
                            "last_trade_price": "223.610000",
                            "venue_last_trade_time": as_of,
                            "previous_close": "224.600000",
                            "bid_price": "223.500000",
                            "ask_price": "223.620000",
                            "has_traded": True,
                            "state": "active",
                        }
                    }
                ]
            }
        },
    }


def live_equity_payloads(symbol: str = "QCOR", *, quote_as_of: str | None = None) -> dict:
    as_of = quote_as_of or "2026-08-28T19:59:58+00:00"
    return {
        "search": {"data": {"results": [{"instrument_id": "inst-qcor", "symbol": symbol, "name": "Quality Core Inc. Common Stock"}]}},
        "tradability": _wrap(symbol, name="Quality Core Inc. Common Stock", state="active", tradeable=True),
        "fundamentals": _wrap(
            symbol,
            description="Quality Core Inc. designs software infrastructure.",
            sector="Electronic Technology",
            industry="Telecommunications Equipment",
            market_cap="110000000000.000000",
            pe_ratio="19.500000",
            average_volume="4200000.000000",
            average_volume_2_weeks="4200000.000000",
        ),
        "quotes": {
            "data": {
                "results": [
                    {
                        "quote": {
                            "symbol": symbol,
                            "last_trade_price": "91.500000",
                            "venue_last_trade_time": as_of,
                            "previous_close": "90.000000",
                            "bid_price": "91.480000",
                            "ask_price": "91.520000",
                            "has_traded": True,
                            "state": "active",
                        }
                    }
                ]
            }
        },
        "financials": {
            "data": {
                "results": [
                    {"symbol": symbol, "period": "2026-Q2", "revenue": 10e9, "net_income": 2e9, "net_margin": 20.0},
                    {"symbol": symbol, "period": "2026-Q1", "revenue": 9.5e9, "net_income": 1.8e9, "net_margin": 19.0},
                    {"symbol": symbol, "period": "2025-Q4", "revenue": 9.1e9, "net_income": 1.7e9, "net_margin": 18.7},
                    {"symbol": symbol, "period": "2025-Q3", "revenue": 8.8e9, "net_income": 1.6e9, "net_margin": 18.2},
                    {"symbol": symbol, "period": "2025-Q2", "revenue": 8.2e9, "net_income": 1.4e9, "net_margin": 17.0},
                ]
            }
        },
    }


def fake_qual_equity_snapshot() -> SecuritySnapshot:
    return SecuritySnapshot(
        symbol="QUAL",
        observed_at=NOW.isoformat(),
        sources=["fundamentals", "financials"],
        name="Quality Check Co",
        instrument_kind="equity",
        tradable=True,
        current_price=85.0,
        market_cap=8.0e10,
        pe_ratio=22.0,
        sector="INFORMATION_TECHNOLOGY",
        classification=ClassificationResult(
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            status=ClassificationStatus.VALIDATED,
            effective_class_for_ceiling=SecurityClass.INDIVIDUAL_EQUITY,
            confidence="high",
            symbol="QUAL",
            sector=CanonicalSector.INFORMATION_TECHNOLOGY,
            sector_status=SectorStatus.MAPPED,
        ),
        liquidity=LiquidityEvidence(median_daily_dollar_volume_20d=1e12, recent_dollar_volume=1e12),
        revenue_periods=[10e9, 9.5e9, 9.1e9, 8.8e9, 8.2e9],
        net_income_periods=[2e9, 1.8e9, 1.7e9, 1.6e9, 1.4e9],
        net_margin_periods=[0.20, 0.19, 0.187, 0.182, 0.17],
        data_origin="fixture",
    )


def _providers():
    return {
        "openai": ScriptedProvider(RESPONSES, name="openai"),
        "anthropic": ScriptedProvider(RESPONSES, name="anthropic"),
    }


def _refresh(tmp_path):
    from agentic_portfolio.live.engine import refresh_live_portfolio

    return refresh_live_portfolio(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        now=NOW,
        root=tmp_path,
        persist=True,
    )


def test_qual_resolves_as_etf_not_technology_company():
    snap, validation = facts_from_payloads("QUAL", qual_etf_payloads(), now=NOW)
    assert snap.name == "iShares MSCI USA Quality Factor ETF"
    assert snap.instrument_kind == "etf"
    assert snap.classification is not None
    assert snap.classification.security_class != SecurityClass.INDIVIDUAL_EQUITY
    assert "ETF" in snap.classification.security_class.value
    assert validation.identity is not None
    assert validation.identity.is_etf is True
    assert validation.identity.is_equity is False
    assert validation.identity.security_type.value == "etf"
    assert validation.eligible_for_ai is True
    assert validation.synthetic_data_detected is False
    assert validation.data_freshness["quote"] == "LAST_SESSION"
    facts = collect_candidate_facts(snap, now=NOW)
    assert facts.get("market_cap").unavailable is True
    assert facts.get("pe_ratio").unavailable is True
    assert facts.get("sector").unavailable is True
    assert facts.get("net_assets").unavailable is False
    assert facts.get("net_assets").value == 47930803500.0
    ai_ctx = assemble_context("QUAL", ctx(500), now_iso=NOW.isoformat(), runtime_mode=RuntimeMode.LIVE, instrument_facts=facts)
    blob = str(ai_ctx.to_prompt_dict())
    assert "Quality Check Co" not in blob
    assert "INFORMATION_TECHNOLOGY" not in blob
    assert "85.0" not in blob and "85.00" not in blob
    assert "80000000000" not in blob
    assert ai_ctx.fundamentals["market_cap"]["unavailable"] is True
    assert ai_ctx.fundamentals["pe_ratio"]["unavailable"] is True
    assert ai_ctx.fundamentals["net_assets"]["value"] == 47930803500.0


def test_qual_search_does_not_take_qualcomm_collision():
    payloads = qual_etf_payloads()
    snap = assemble_snapshot(
        SymbolPayloads(symbol="QUAL", search=payloads["search"], tradability=payloads["tradability"], fundamentals=payloads["fundamentals"], quotes=payloads["quotes"], observed_at=NOW.isoformat()),
        source_id="robinhood",
    )
    assert snap.broker_instrument_id == "ecf1119a-d382-46bc-9d40-db9c442ed457"
    assert "QUALCOMM" not in (snap.name or "")
    assert snap.name.startswith("iShares")


def test_live_rejects_fake_qual_price_market_cap_and_pe(tmp_path):
    refresh = _refresh(tmp_path)
    gw = _gw(tmp_path, _providers())
    gw.runtime_mode = RuntimeMode.LIVE.value
    fake = fake_qual_equity_snapshot()
    check = validate_live_candidate(fake, now=NOW, runtime_mode=RuntimeMode.LIVE)
    assert check.status is CandidateValidationStatus.SYNTHETIC_DATA_DETECTED
    assert check.eligible_for_ai is False
    result = run_candidate_pipeline(
        [fake],
        refresh.context,
        gw,
        runtime_mode=RuntimeMode.LIVE,
        root=tmp_path,
        now=NOW,
        snapshot=refresh.snapshot,
        snapshot_id=refresh.snapshot_id,
    )
    assert result.ai_calls == 0
    assert result.actual_cost == Decimal("0")
    assert gw.calls == []
    assert any(row.get("reason") == "SYNTHETIC_DATA_DETECTED" for row in result.rejected)
    promptish = str(result.validations)
    assert "Quality Check Co" in promptish or result.validations
    facts = collect_candidate_facts(fake, now=NOW)
    ctx = assemble_context("QUAL", refresh.context, now_iso=NOW.isoformat(), runtime_mode=RuntimeMode.LIVE, instrument_facts=facts)
    # Even if assembled for inspection, LIVE pipeline never sent this to a provider.
    assert fake.current_price == 85.0
    assert fake.market_cap == 8.0e10
    assert fake.pe_ratio == 22.0


def test_fixture_market_facts_rejected_in_live():
    snap = _quality_core()
    check = validate_live_candidate(snap, now=NOW, runtime_mode=RuntimeMode.LIVE)
    assert check.synthetic_data_detected is True
    assert check.eligible_for_ai is False
    assert check.status is CandidateValidationStatus.SYNTHETIC_DATA_DETECTED


def test_missing_facts_stay_null_unavailable():
    payloads = {
        "tradability": _wrap("QUAL", name="iShares MSCI USA Quality Factor ETF", state="active", tradeable=True),
        "fundamentals": _wrap(
            "QUAL",
            description="QUAL tracks an index of US large- and mid-cap stocks.",
            sector="Miscellaneous",
            industry="Investment Trusts Or Mutual Funds",
        ),
        "quotes": {
            "data": {
                "results": [
                    {
                        "quote": {
                            "symbol": "QUAL",
                            "last_trade_price": "223.610000",
                            "venue_last_trade_time": "2026-08-28T19:59:58+00:00",
                            "previous_close": "224.600000",
                            "bid_price": "223.50",
                            "ask_price": "223.62",
                        }
                    }
                ]
            }
        },
    }
    snap, _ = facts_from_payloads("QUAL", payloads, now=NOW)
    facts = collect_candidate_facts(snap, now=NOW)
    assert facts.get("expense_ratio").unavailable is True
    assert facts.get("expense_ratio").value is None
    assert facts.get("fund_nav").unavailable is True
    assert facts.get("holdings").unavailable is True
    assert facts.get("revenue_growth").unavailable is True
    assert facts.get("earnings_growth").unavailable is True
    assert facts.get("market_cap").unavailable is True
    ai_ctx = assemble_context("QUAL", ctx(500), now_iso=NOW.isoformat(), runtime_mode=RuntimeMode.LIVE, instrument_facts=facts)
    assert ai_ctx.fundamentals["expense_ratio"]["value"] is None
    assert ai_ctx.fundamentals["expense_ratio"]["unavailable"] is True


def test_stale_quotes_fail_closed():
    payloads = qual_etf_payloads(quote_as_of="2026-08-20T20:00:00+00:00")
    snap, validation = facts_from_payloads("QUAL", payloads, now=NOW)
    snap.quote_as_of = "2026-08-20T20:00:00+00:00"
    snap.fact_provenance["last_price"].as_of = "2026-08-20T20:00:00+00:00"
    snap.fact_provenance["last_price"].freshness = FreshnessStatus.UNKNOWN
    check = validate_live_candidate(snap, now=NOW, runtime_mode=RuntimeMode.LIVE)
    assert check.status is CandidateValidationStatus.STALE_QUOTE
    assert check.eligible_for_ai is False


def test_missing_quote_fail_closed():
    payloads = qual_etf_payloads()
    payloads.pop("quotes")
    snap, validation = facts_from_payloads("QUAL", payloads, now=NOW)
    assert validation.status in {CandidateValidationStatus.MISSING_QUOTE, CandidateValidationStatus.MISSING_LIQUIDITY} or not validation.eligible_for_ai
    assert validation.facts.get("last_price").unavailable is True
    assert validation.facts.get("last_price").value is None


def test_etf_context_uses_etf_fields_equity_uses_company_fields():
    etf, _ = facts_from_payloads("QUAL", qual_etf_payloads(), now=NOW)
    eq, _ = facts_from_payloads("QCOR", live_equity_payloads(), now=NOW)
    etf_facts = collect_candidate_facts(etf, now=NOW)
    eq_facts = collect_candidate_facts(eq, now=NOW)
    etf_ctx = assemble_context("QUAL", ctx(500), now_iso=NOW.isoformat(), runtime_mode=RuntimeMode.LIVE, instrument_facts=etf_facts)
    eq_ctx = assemble_context("QCOR", ctx(500), now_iso=NOW.isoformat(), runtime_mode=RuntimeMode.LIVE, instrument_facts=eq_facts)
    assert etf_ctx.fundamentals["net_assets"]["unavailable"] is False
    assert etf_ctx.fundamentals["market_cap"]["unavailable"] is True
    assert etf_ctx.identity["is_etf"] is True
    assert eq_ctx.fundamentals["market_cap"]["unavailable"] is False
    assert eq_ctx.fundamentals["pe_ratio"]["value"] == 19.5
    assert eq_ctx.identity["is_equity"] is True
    assert eq_ctx.identity["is_etf"] is False


def test_invalid_candidate_never_invokes_ai_and_costs_zero(tmp_path):
    refresh = _refresh(tmp_path)
    gw = _gw(tmp_path, _providers())
    gw.runtime_mode = RuntimeMode.LIVE.value
    result = run_candidate_pipeline(
        [fake_qual_equity_snapshot(), _quality_core()],
        refresh.context,
        gw,
        runtime_mode=RuntimeMode.LIVE,
        root=tmp_path,
        now=NOW,
        snapshot=refresh.snapshot,
        snapshot_id=refresh.snapshot_id,
    )
    assert result.ai_calls == 0
    assert result.estimated_cost == Decimal("0")
    assert result.actual_cost == Decimal("0")
    assert gw.calls == []
    assert result.screened == []
    assert LIVE_ORDER_PLACEMENT is False
    assert result.placement_attempted is False


def test_live_valid_equity_still_reaches_ai(tmp_path):
    refresh = _refresh(tmp_path)
    gw = _gw(tmp_path, _providers())
    gw.runtime_mode = RuntimeMode.LIVE.value
    snap, validation = facts_from_payloads("QCOR", live_equity_payloads(quote_as_of="2026-08-28T19:59:58+00:00"), now=NOW)
    assert validation.eligible_for_ai is True
    result = run_candidate_pipeline(
        [snap],
        refresh.context,
        gw,
        runtime_mode=RuntimeMode.LIVE,
        root=tmp_path,
        now=NOW,
        snapshot=refresh.snapshot,
        snapshot_id=refresh.snapshot_id,
    )
    assert result.ai_calls > 0
    assert result.placement_attempted is False
    assert any(s.ticker == "QCOR" for s in result.screened)


def test_paper_fixtures_still_allowed_for_discovery():
    check = validate_live_candidate(_quality_core(), now=NOW, runtime_mode=RuntimeMode.PAPER)
    assert check.status is CandidateValidationStatus.VALID
    assert check.eligible_for_ai is True


def test_live_placement_remains_impossible():
    from agentic_portfolio.policy import load_account_rules

    rules = load_account_rules()["execution"]
    assert rules["auto_execution"] is False
    assert rules["live_trade_actions_allowed"] is False
    assert LIVE_ORDER_PLACEMENT is False


def test_weekend_friday_quote_is_last_session_not_stale():
    sunday = datetime(2026, 8, 30, 20, 0, tzinfo=timezone.utc)
    payloads = qual_etf_payloads(quote_as_of="2026-08-28T19:59:58+00:00")
    snap, validation = facts_from_payloads("QUAL", payloads, now=sunday)
    assert snap.instrument_kind == "etf"
    assert validation.data_freshness["quote"] == "LAST_SESSION"
    assert validation.status is CandidateValidationStatus.VALID
    assert validation.eligible_for_ai is True
    assert validation.synthetic_data_detected is False
    assert validation.facts.get("average_volume").freshness.value == "FRESH"


def test_open_session_quote_ttl_is_not_weakened():
    friday_open = datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc)
    payloads = qual_etf_payloads(quote_as_of="2026-08-28T15:00:00+00:00")
    _, validation = facts_from_payloads("QUAL", payloads, now=friday_open)
    assert validation.data_freshness["quote"] == "STALE"
    assert validation.status is CandidateValidationStatus.STALE_QUOTE
    assert validation.eligible_for_ai is False


def test_old_quote_still_rejected_when_market_closed():
    payloads = qual_etf_payloads(quote_as_of="2026-08-20T20:00:00+00:00")
    _, validation = facts_from_payloads("QUAL", payloads, now=NOW)
    assert validation.status is CandidateValidationStatus.STALE_QUOTE
    assert validation.eligible_for_ai is False


def test_diagnostic_fetches_its_own_live_facts(capsys):
    from agentic_portfolio.adapters.robinhood_read import MappingReadOnlyFetcher, fetch_instrument_payloads
    from scripts.check_live_candidate_facts import main

    fetcher = MappingReadOnlyFetcher.from_payloads("QUAL", qual_etf_payloads())
    payloads, calls = fetch_instrument_payloads("QUAL", fetcher)
    assert calls == ["get_equity_tradability", "get_equity_fundamentals", "search", "get_equity_quotes"]
    assert "place_equity_order" not in calls
    assert "review_equity_order" not in calls
    assert "cancel_equity_order" not in calls
    assert payloads["search"]["data"]["results"][0]["symbol"] == "QUAL"
    code = main(["QUAL"], fetcher=MappingReadOnlyFetcher.from_payloads("QUAL", qual_etf_payloads()), now=NOW)
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ticker"] == "QUAL"
    assert report["security_name"]["value"] == "iShares MSCI USA Quality Factor ETF"
    assert report["security_type"]["value"] == "etf"
    assert report["broker_instrument_id"]["value"] == "ecf1119a-d382-46bc-9d40-db9c442ed457"
    assert report["quote_source"] == "get_equity_quotes.last_trade_price"
    assert report["latest_completed_session"] == "2026-08-28"
    assert report["freshness"]["quote"] == "LAST_SESSION"
    assert report["company_market_cap"]["unavailable"] is True
    assert report["company_pe_ratio"]["unavailable"] is True
    assert report["net_assets"]["unavailable"] is False
    assert report["synthetic_data_detected"] is False
    assert report["eligible_for_ai"] is True
    assert report["ai_provider_called"] is False
    assert report["ai_cost"] == 0
    assert report["LIVE_ORDER_PLACEMENT"] is False
    assert "get_equity_quotes" in report["read_only_source_calls"]
    assert "mcp_payloads_not_supplied" not in report["rejection_reasons"]
    assert not (set(report["read_only_source_calls"]) & {"place_equity_order", "review_equity_order", "cancel_equity_order"})


def test_diagnostic_fail_closed_when_mcp_unreachable(capsys):
    from agentic_portfolio.adapters.portfolio_facts import LiveDataUnavailable
    from agentic_portfolio.adapters.robinhood_read import MappingReadOnlyFetcher
    from scripts.check_live_candidate_facts import main

    fetcher = MappingReadOnlyFetcher.from_payloads(
        "QUAL",
        {},
        error=LiveDataUnavailable("readonly_mcp_unreachable: connection refused"),
    )
    code = main(["QUAL"], fetcher=fetcher, now=NOW)
    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["eligible_for_ai"] is False
    assert report["ai_provider_called"] is False
    assert report["ai_cost"] == 0
    blob = " ".join(report["rejection_reasons"])
    assert "mcp_payloads_not_supplied" not in blob
    assert "readonly_mcp_unreachable" in blob
    assert "connection refused" in blob


def test_diagnostic_skips_ai_and_broker_writes():
    import ast
    import inspect

    from agentic_portfolio.ai import identity as identity_mod
    from scripts import check_live_candidate_facts

    src = inspect.getsource(check_live_candidate_facts)
    tree = ast.parse(src)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "observe_live_candidate" in src
    assert "facts_from_payloads" in src
    assert "def validate_live_candidate" not in src
    assert "build_gateway" not in imported
    assert "OpenAIProvider" not in imported
    assert "run_candidate_pipeline" not in imported
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    assert not called & {"place_equity_order", "review_equity_order", "cancel_equity_order", "build_gateway"}
    obs = inspect.getsource(identity_mod.observe_live_candidate)
    assert "facts_from_payloads" in obs
    assert "config=config" in obs
    facts = inspect.getsource(identity_mod.facts_from_payloads)
    assert "validate_live_candidate" in facts
    assert "config=config" in facts


def test_check_live_candidate_facts_script_costs_zero_and_skips_ai(tmp_path, capsys):
    from agentic_portfolio.adapters.robinhood_read import MappingReadOnlyFetcher
    from scripts.check_live_candidate_facts import main

    code = main(["QUAL"], fetcher=MappingReadOnlyFetcher.from_payloads("QUAL", qual_etf_payloads()), now=NOW)
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ticker"] == "QUAL"
    assert report["ai_provider_called"] is False
    assert report["ai_cost"] == 0
    assert report["LIVE_ORDER_PLACEMENT"] is False
