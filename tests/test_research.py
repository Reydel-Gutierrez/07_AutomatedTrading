"""Deep Research tests. No broker execution."""

from datetime import datetime, timedelta, timezone

import pytest

from agentic_portfolio.research.comparison import ScriptedComparisonReasoner, build_comparison
from agentic_portfolio.research.engine import compare_reports, request_refresh, run_research
from agentic_portfolio.research.freshness import evaluate_freshness, freshness_horizon
from agentic_portfolio.research.packet import ResearchPayload, build_packet
from agentic_portfolio.research.reasoner import ScriptedResearchReasoner
from agentic_portfolio.research.safety import (
    RESEARCH_FORBIDDEN_TOOLS,
    ResearchSafetyError,
    as_proposed_action,
    inspect_research_module_for_forbidden_tools,
    research_cannot_become_buy,
)
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.types import (
    EvidenceKind,
    ResearchConclusion,
    ResearchConfidence,
    ResearchFreshness,
    ResearchStatus,
    ResearchSubjectKind,
)
from agentic_portfolio.research.validate import ResearchValidationError, validate_reasoning
from agentic_portfolio.schemas import (
    Candidate,
    CandidateStatus,
    ClassificationResult,
    ClassificationStatus,
    Decision,
    SecurityClass,
    Sleeve,
)
from agentic_portfolio.sectors import CanonicalSector, SectorStatus
from tests.conftest import ctx

TS = "2026-08-30T16:00:00+00:00"
NOW = datetime(2026, 8, 30, 16, 0, tzinfo=timezone.utc)


def _wrap(symbol, **fields):
    return {"data": {"results": [{"symbol": symbol, **fields}]}}


def _cls(symbol="X", sector=CanonicalSector.INFORMATION_TECHNOLOGY, sc=SecurityClass.INDIVIDUAL_EQUITY):
    return ClassificationResult(
        security_class=sc,
        status=ClassificationStatus.VALIDATED,
        effective_class_for_ceiling=sc,
        confidence="high",
        symbol=symbol,
        sector=sector,
        sector_status=SectorStatus.MAPPED,
    )


def _candidate(
    symbol="QUAL",
    *,
    sleeve=Sleeve.CORE_GROWTH,
    score=72.0,
    sector="INFORMATION_TECHNOLOGY",
    price=100.0,
    cid=None,
):
    return Candidate(
        candidate_id=cid or f"cand-{symbol}",
        symbol=symbol,
        discovered_at=TS,
        discovery_source="test",
        provisional_sleeve=sleeve,
        primary_provisional_sleeve=sleeve,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        classification_status=ClassificationStatus.VALIDATED,
        current_price=price,
        sector=sector,
        discovery_score=score,
        status=CandidateStatus.PROMOTED_TO_RESEARCH,
        industry="Semiconductors",
    )


def _payload(symbol="QUAL", *, rich=True, news=None, extra=None):
    p = ResearchPayload(
        symbol=symbol,
        observed_at=TS,
        sources_attempted=["get_equity_quotes", "get_equity_fundamentals", "get_financials", "get_equity_news"],
        sources_observed=["get_equity_quotes", "get_equity_fundamentals", "get_financials", "get_equity_news"],
        sources_unavailable=[],
        tradability=_wrap(symbol, name=f"{symbol} Inc. Common Stock", state="active", tradeable=True),
        fundamentals=_wrap(
            symbol,
            description="Operating company.",
            sector="Electronic Technology",
            industry="Semiconductors",
            market_cap=8.0e11,
            pe_ratio=28.0,
            shares_outstanding=2.4e9,
            high_52_weeks=140.0,
            average_volume_2_weeks=2.0e7,
            free_cash_flow=2.0e10,
            total_debt=1.0e10,
            total_cash=3.0e10,
        ),
        quotes=_wrap(symbol, last_trade_price=120.0, previous_close=118.0, bid_price=119.9, ask_price=120.1, volume=1.5e7),
        financials={
            "data": {
                "results": [
                    {"symbol": symbol, "period": "2025-Q4", "revenue": 12e9, "net_income": 3e9, "net_margin": 0.25, "gross_profit": 7e9},
                    {"symbol": symbol, "period": "2025-Q3", "revenue": 11e9, "net_income": 2.6e9, "net_margin": 0.236, "gross_profit": 6.4e9},
                    {"symbol": symbol, "period": "2025-Q2", "revenue": 10.4e9, "net_income": 2.4e9, "net_margin": 0.23, "gross_profit": 6.0e9},
                ]
            }
        },
        historicals={
            "data": {
                "results": [{"close": 100 + i, "close_price": 100 + i} for i in range(60)]
            }
        },
        rsi={"data": {"value": 55.0}},
        sma_50={"data": {"value": 110.0}},
        sma_200={"data": {"value": 95.0}},
        earnings_results={
            "data": {
                "results": [
                    {"report_date": "2026-08-20", "actual_eps": 1.10, "estimated_eps": 1.00},
                    {"report_date": "2026-05-20", "actual_eps": 0.95, "estimated_eps": 0.94},
                ]
            }
        },
        news={
            "data": {
                "results": news
                or [
                    {"title": "Company reports product progress", "published_at": "2026-08-21", "source": "Wire"},
                    {"title": "Analyst notes industry demand", "published_at": "2026-08-22", "source": "Note"},
                ]
            }
        },
        sec_index={
            "data": {
                "results": [
                    {"form_type": "10-K", "filing_id": "f-10k", "filed_at": "2026-02-01"},
                    {"form_type": "10-Q", "filing_id": "f-10q", "filed_at": "2026-08-01"},
                ]
            }
        },
        classification=_cls(symbol),
    )
    if not rich:
        p.financials = None
        p.news = None
        p.sec_index = None
        p.historicals = None
        p.earnings_results = None
        p.sources_observed = ["get_equity_quotes"]
        p.sources_unavailable = ["get_financials", "get_equity_news", "get_sec_filing_index", "get_equity_historicals"]
        p.sources_attempted = ["get_equity_quotes", "get_financials"]
    if extra:
        for k, v in extra.items():
            setattr(p, k, v)
    return p


def _cases():
    return {
        "bull_case": {
            "case": "BULL_CASE",
            "summary": "Durable demand continues and margins hold.",
            "major_assumptions": ["demand persists"],
            "expected_business_outcome": "compounding earnings",
            "major_risk": "competition",
            "attractiveness_implication": "constructive if quality holds",
            "evidence_refs": ["fact:market_price"],
            "price_target": None,
        },
        "base_case": {
            "case": "BASE_CASE",
            "summary": "Growth decelerates but remains profitable.",
            "major_assumptions": ["normal cycle"],
            "expected_business_outcome": "moderate compounding",
            "major_risk": "multiple compression",
            "attractiveness_implication": "selective",
            "evidence_refs": ["derived:revenue_growth_qoq"],
        },
        "bear_case": {
            "case": "BEAR_CASE",
            "summary": "Demand rolls over and cash conversion weakens.",
            "major_assumptions": ["cycle turns"],
            "expected_business_outcome": "earnings decline",
            "major_risk": "permanent impairment of growth",
            "attractiveness_implication": "unattractive vs cash",
            "evidence_refs": ["fact:total_debt"],
        },
    }


def _ai(
    symbol="QUAL",
    *,
    conclusion="ADVANCE_TO_THESIS",
    confidence="MEDIUM",
    extra=None,
    conflicting=None,
    missing=None,
    dislocation=None,
    deterioration=None,
):
    payload = {
        "executive_summary": f"{symbol} research interpretation based on packet facts.",
        "business_summary": "Operating business described by observed profile.",
        "investment_question": "Is this attractive enough for a thesis?",
        "fundamental_analysis": "Quality and durability interpreted from revenue and income series.",
        "financial_analysis": "Margins and cash interpreted from derived metrics.",
        "valuation_analysis": "Valuation judged in context of growth and quality, not a PE cutoff.",
        "earnings_analysis": "Recent print versus estimate is a fact; quality of earnings is interpreted.",
        "competitive_analysis": "Position inferred from description, not invented share data.",
        "technical_context": "SMA alignment is supporting context only.",
        "market_context": "Portfolio remains cash-heavy; this is not an allocation decision.",
        "sector_context": "Sector overlap is a research warning, not a hard reject.",
        "news_analysis": "Two distinct headlines; not an article-count sentiment score.",
        "filing_analysis": "10-K and 10-Q are present as filing facts; no keyword score.",
        "catalyst_analysis": "Upcoming catalysts remain uncertain.",
        "risk_analysis": "Balance sheet and competition are the primary observed risks.",
        **_cases(),
        "key_catalysts": ["product cycle"],
        "key_risks": ["competition"],
        "invalidation_candidates": ["sustained negative revenue growth"],
        "expected_horizon": "multi-year",
        "missing_information": missing or [],
        "conflicting_evidence": conflicting or [],
        "evidence_refs": ["fact:market_price", "derived:revenue_growth_qoq"],
        "ai_interpretations": [
            {"name": "growth_durability", "value": "appears durable on available series", "evidence_refs": ["derived:revenue_growth_qoq"]}
        ],
        "confidence": confidence,
        "research_conclusion": conclusion,
        "recommended_next_step": conclusion,
        "earnings_effect_kind": "UNCERTAIN",
    }
    if dislocation:
        payload["temporary_dislocation_assessment"] = dislocation
    if deterioration:
        payload["fundamental_deterioration_assessment"] = deterioration
    if extra:
        payload.update(extra)
    return payload


def _run(symbol="QUAL", *, sleeve=Sleeve.CORE_GROWTH, score=72.0, payload=None, response=None, persist=False, store=None, journal=None, nav=10_000, **kwargs):
    cand = kwargs.pop("candidate", None) or _candidate(symbol, sleeve=sleeve, score=score)
    payload = payload or _payload(symbol)
    reasoner = ScriptedResearchReasoner({symbol: response or _ai(symbol)})
    return run_research(
        cand,
        payload,
        kwargs.pop("context", ctx(nav)),
        reasoner,
        persist=persist,
        now=NOW,
        store=store,
        journal=journal or (store.root.parent / "research.jsonl" if store else None),
        **kwargs,
    )


def test_evidence_packet_construction():
    cand = _candidate()
    packet = build_packet(_payload(), cand, ctx(10_000))
    assert packet.symbol == "QUAL"
    assert packet.facts
    assert packet.derived_metrics
    names = {e.name for e in packet.facts}
    assert "market_price" in names
    assert "revenue_periods" in names
    derived_names = {e.name for e in packet.derived_metrics}
    assert "revenue_growth_qoq" in derived_names
    assert all(e.kind == EvidenceKind.OBSERVED_FACT for e in packet.facts)
    assert all(e.kind == EvidenceKind.DETERMINISTIC_DERIVED_METRIC for e in packet.derived_metrics)
    assert packet.portfolio_facts is not None
    assert packet.portfolio_facts.current_nav == 10_000
    assert packet.risk_limits.limits


def test_facts_derived_ai_separation():
    out = _run()
    fact_ids = {e.evidence_id for e in out.packet.facts}
    report_fact_ids = {e.evidence_id for e in out.report.facts}
    assert fact_ids == report_fact_ids
    assert all(e.kind == EvidenceKind.OBSERVED_FACT for e in out.report.facts)
    assert all(e.kind == EvidenceKind.DETERMINISTIC_DERIVED_METRIC for e in out.report.derived_metrics)
    assert all(e.kind == EvidenceKind.AI_INTERPRETATION for e in out.report.ai_interpretations)
    assert out.report.ai_interpretations
    # AI must not have rewritten revenue into facts.
    rev = next(e for e in out.report.facts if e.name == "revenue_periods")
    pkt = next(e for e in out.packet.facts if e.name == "revenue_periods")
    assert rev.value == pkt.value


def test_malformed_ai_response_rejected(tmp_path):
    store = ResearchStore(tmp_path)
    journal = tmp_path / "j.jsonl"
    cand = _candidate()
    reasoner = ScriptedResearchReasoner({"QUAL": {"executive_summary": "nope"}})
    out = run_research(cand, _payload(), ctx(10_000), reasoner, persist=True, now=NOW, store=store, journal=journal)
    assert out.report.research_status == ResearchStatus.RESEARCH_INCONCLUSIVE
    assert out.report.validation_errors
    assert out.report.research_conclusion == ResearchConclusion.NEED_MORE_DATA
    assert out.proposed_actions_created == 0


def test_unsupported_ai_fact_detected():
    extra = {"facts": [{"name": "secret_revenue_from_nowhere", "value": 99e9}]}
    out = _run(response=_ai(extra=extra))
    assert any("invented_observed_fact" in c for c in out.report.unsupported_claims)


def test_core_research_report():
    out = _run("NVDA", sleeve=Sleeve.CORE_GROWTH, score=70)
    r = out.report
    assert r.provisional_sleeve == Sleeve.CORE_GROWTH
    assert r.research_status == ResearchStatus.RESEARCH_COMPLETE
    assert r.bull_case and r.base_case and r.bear_case
    assert r.fundamental_analysis
    assert r.valuation_analysis
    assert "SMA alignment is supporting context" in (r.technical_context or "")
    assert r.buy_actions_created == 0
    assert r.proposed_actions_created == 0


def test_opportunistic_dislocation_analysis():
    loc = {
        "verdict": "LIKELY_DISLOCATION",
        "reasoning": "Price drawdown with stable revenue series suggests dislocation more than collapse.",
        "evidence_refs": ["derived:drawdown_from_52w_high", "fact:revenue_periods"],
    }
    det = {
        "verdict": "INSUFFICIENT_EVIDENCE",
        "reasoning": "No structural earnings break is observed in the packet.",
        "evidence_refs": ["fact:net_income_periods"],
    }
    out = _run(
        "NKE",
        sleeve=Sleeve.OPPORTUNISTIC,
        score=61,
        response=_ai("NKE", conclusion="KEEP_WATCHING", dislocation=loc, deterioration=det),
    )
    assert out.report.temporary_dislocation_assessment
    assert out.report.temporary_dislocation_assessment.verdict.value == "LIKELY_DISLOCATION"
    assert out.report.research_conclusion == ResearchConclusion.KEEP_WATCHING


def test_opportunistic_deterioration_analysis():
    loc = {
        "verdict": "LIKELY_DETERIORATION",
        "reasoning": "Revenue and income series weaken alongside the drawdown.",
        "evidence_refs": ["derived:revenue_growth_span"],
    }
    det = {
        "verdict": "LIKELY_DETERIORATION",
        "reasoning": "Earning power appears impaired on available periods.",
        "evidence_refs": ["fact:net_income_periods"],
    }
    out = _run(
        "GAP",
        sleeve=Sleeve.OPPORTUNISTIC,
        score=58,
        response=_ai("GAP", conclusion="REJECT", dislocation=loc, deterioration=det),
    )
    assert out.report.fundamental_deterioration_assessment.verdict.value == "LIKELY_DETERIORATION"
    assert out.report.research_status == ResearchStatus.RESEARCH_REJECTED


def test_tactical_research():
    out = _run(
        "ESTC",
        sleeve=Sleeve.TACTICAL,
        score=66,
        response=_ai("ESTC", conclusion="KEEP_WATCHING", extra={"expected_horizon": "days_to_weeks", "technical_context": "Setup depends on trend and volume confirmation, not a long-term thesis."}),
    )
    assert out.report.provisional_sleeve == Sleeve.TACTICAL
    assert "Setup depends on trend" in (out.report.technical_context or "")
    assert out.packet.technical_weight == "primary_setup_context"


def test_speculative_survival_dilution_research():
    extra = {
        "risk_analysis": "Survival and dilution dominate. Financing/liquidity risk is material if cash burn continues.",
        "catalyst_analysis": "Asymmetry depends on a specific catalyst; failure may impair capital.",
    }
    out = _run("JOBY", sleeve=Sleeve.SPECULATIVE, score=62, response=_ai("JOBY", conclusion="REJECT", extra=extra))
    assert "dilution" in (out.report.risk_analysis or "").lower() or "Survival" in (out.report.risk_analysis or "")
    assert out.report.research_conclusion == ResearchConclusion.REJECT


def test_incomplete_evidence_need_more_data():
    payload = _payload("THIN", rich=False)
    cand = _candidate("THIN", score=80)
    packet = build_packet(payload, cand, ctx(10_000))
    assert packet.completeness == "INCOMPLETE"
    out = run_research(
        cand,
        payload,
        ctx(10_000),
        ScriptedResearchReasoner({"THIN": _ai("THIN", conclusion="NEED_MORE_DATA", confidence="LOW", extra={"bull_case": None, "base_case": None, "bear_case": None})}),
        persist=False,
        now=NOW,
        journal=None,
    )
    assert out.report.research_conclusion == ResearchConclusion.NEED_MORE_DATA
    assert out.report.research_status == ResearchStatus.RESEARCH_INCONCLUSIVE
    assert out.report.confidence == ResearchConfidence.LOW


def test_conflicting_evidence_lowers_confidence():
    out = _run(response=_ai(confidence="HIGH", conflicting=["margin expansion vs guidance caution"]))
    assert out.report.confidence == ResearchConfidence.MEDIUM


def test_research_can_reject_high_discovery_score():
    out = _run(score=91.0, response=_ai(conclusion="REJECT", extra={"bull_case": None, "base_case": None, "bear_case": None}))
    assert out.packet.discovery_score == 91.0
    assert out.report.research_conclusion == ResearchConclusion.REJECT
    assert out.report.buy_actions_created == 0


def test_research_can_advance_lower_discovery_score():
    out = _run(score=48.0, response=_ai(conclusion="ADVANCE_TO_THESIS"))
    assert out.packet.discovery_score == 48.0
    assert out.report.research_conclusion == ResearchConclusion.ADVANCE_TO_THESIS
    assert out.report.research_status == ResearchStatus.RESEARCH_COMPLETE


def test_comparison_between_same_sector_candidates():
    a = _run("AAPL", score=64).report
    b = _run("MSFT", score=67).report
    c = _run("NVDA", score=70).report
    payload = {
        "dimensions": [
            {"name": "business_quality", "ranking": ["MSFT", "NVDA", "AAPL"], "notes": "quality is interpreted", "uncertainty": "MEDIUM"},
            {"name": "valuation", "ranking": ["AAPL", "MSFT", "NVDA"], "notes": "no PE cutoff", "uncertainty": "HIGH"},
            {"name": "portfolio_overlap", "ranking": ["AAPL", "MSFT", "NVDA"], "notes": "same sector group", "uncertainty": "LOW"},
        ],
        "relative_conclusion": "Research can prefer among peers rather than first-three discovery order.",
    }
    cmp = build_comparison([a, b, c], reasoner=ScriptedComparisonReasoner(payload))
    assert set(cmp.symbols) == {"AAPL", "MSFT", "NVDA"}
    assert "first-three" in (cmp.relative_conclusion or "")
    dims = {d.name for d in cmp.dimensions}
    assert "business_quality" in dims
    assert "valuation" in dims


def test_research_persistence_and_history(tmp_path):
    store = ResearchStore(tmp_path)
    journal = tmp_path / "j.jsonl"
    first = _run(persist=True, store=store, journal=journal)
    second = _run(
        persist=True,
        store=store,
        journal=journal,
        response=_ai(conclusion="KEEP_WATCHING"),
        candidate=_candidate(cid="cand-QUAL-b"),
    )
    assert first.report.research_id != second.report.research_id
    loaded = store.get(first.report.research_id)
    assert loaded is not None
    assert loaded.executive_summary == first.report.executive_summary
    hist = store.by_symbol("QUAL")
    assert len(hist) == 2
    assert store.by_candidate("cand-QUAL")
    assert first.report.completed_at.startswith("2026-08-30")
    assert store.by_date("2026-08-30")
    with pytest.raises(FileExistsError):
        store.save(first.report)


def test_research_freshness_and_triggers():
    out = _run()
    later = NOW + timedelta(hours=400)
    fresh, triggers = evaluate_freshness(out.report, now=later)
    assert fresh == ResearchFreshness.RESEARCH_REFRESH_REQUIRED
    assert "ELAPSED_TIME" in triggers
    earn = request_refresh(out.report, earnings_event=True, now=NOW)
    assert earn.freshness == ResearchFreshness.RESEARCH_REFRESH_REQUIRED
    assert "EARNINGS_EVENT" in earn.refresh_triggers
    news = request_refresh(out.report, major_news=True, now=NOW)
    assert "MAJOR_NEWS" in news.refresh_triggers


def test_tactical_research_shorter_freshness():
    core = freshness_horizon(Sleeve.CORE_GROWTH)
    tact = freshness_horizon(Sleeve.TACTICAL)
    assert tact < core
    out = _run("ESTC", sleeve=Sleeve.TACTICAL, response=_ai("ESTC", conclusion="KEEP_WATCHING"))
    soon = NOW + timedelta(hours=9)
    fresh, triggers = evaluate_freshness(out.report, now=soon)
    assert fresh == ResearchFreshness.RESEARCH_REFRESH_REQUIRED
    core_out = _run("NVDA", sleeve=Sleeve.CORE_GROWTH, response=_ai("NVDA"))
    core_fresh, _ = evaluate_freshness(core_out.report, now=soon)
    assert core_fresh != ResearchFreshness.RESEARCH_REFRESH_REQUIRED


def test_existing_holding_research_refresh():
    cand = _candidate("NVDA")
    out = run_research(
        cand,
        _payload("NVDA"),
        ctx(10_000),
        ScriptedResearchReasoner({"NVDA": _ai("NVDA")}),
        subject_kind=ResearchSubjectKind.EXISTING_POSITION_REVIEW,
        existing_thesis_id="thesis-nvda",
        persist=False,
        now=NOW,
        journal=None,
    )
    assert out.report.subject_kind == ResearchSubjectKind.EXISTING_POSITION_REVIEW
    assert out.report.thesis_id == "thesis-nvda"
    refreshed = request_refresh(out.report, thesis_concern=True, now=NOW)
    assert refreshed.freshness == ResearchFreshness.RESEARCH_REFRESH_REQUIRED
    assert "THESIS_CONCERN" in refreshed.refresh_triggers


def test_risk_limits_cannot_be_changed_by_ai():
    extra = {"risk_limits": {"speculative_name_pct": 0.50}}
    out = _run(response=_ai(extra=extra))
    assert "attempted_risk_limit_change" in out.report.unsupported_claims
    assert out.report.risk_limits_unchanged is True
    assert out.packet.risk_limits.limits != {"speculative_name_pct": 0.50}


def test_ai_cannot_rewrite_portfolio_facts():
    extra = {"current_nav": 1.0, "positions": [{"symbol": "FAKE", "market_value": 999}]}
    out = _run(nav=25_000, response=_ai(extra=extra))
    assert "attempted_nav_rewrite" in out.report.unsupported_claims
    assert "attempted_position_rewrite" in out.report.unsupported_claims
    assert out.packet.portfolio_facts.current_nav == 25_000
    assert out.packet.portfolio_facts.positions == []


def test_no_execution_tools_reachable_from_research():
    hits = inspect_research_module_for_forbidden_tools()
    assert hits == []
    for tool in ("review_equity_order", "place_equity_order", "cancel_equity_order"):
        assert tool in RESEARCH_FORBIDDEN_TOOLS
    with pytest.raises(ResearchSafetyError):
        from agentic_portfolio.research.safety import assert_no_forbidden_tools

        assert_no_forbidden_tools(["place_equity_order"])


def test_candidate_does_not_become_proposed_action_from_research():
    out = _run()
    with pytest.raises(ResearchSafetyError):
        research_cannot_become_buy(out.report)
    with pytest.raises(ResearchSafetyError):
        as_proposed_action(out.report)
    assert out.report.proposed_actions_created == 0
    assert out.buy_actions_created == 0
    assert Decision.BUY.value == "BUY"


def test_research_not_dependent_on_fixed_account_nav():
    conclusions = []
    for nav in (1_000, 10_000, 100_000, 1_000_000):
        out = _run(nav=nav, response=_ai(conclusion="KEEP_WATCHING"))
        conclusions.append(out.report.research_conclusion)
        growth = next(e for e in out.packet.derived_metrics if e.name == "revenue_growth_qoq")
        if nav == 1_000:
            g0 = growth.value
        else:
            assert growth.value == g0
    assert len(set(conclusions)) == 1


def test_claimed_unavailable_source_is_unsupported():
    extra = {"claimed_sources": ["get_sec_filing_facts"]}
    payload = _payload()
    payload.sources_unavailable.append("get_sec_filing_facts")
    out = _run(payload=payload, response=_ai(extra=extra))
    assert any("claimed_unavailable_source" in c for c in out.report.unsupported_claims)


def test_validate_reasoning_raises_on_non_object():
    packet = build_packet(_payload(), _candidate(), ctx(10_000))
    with pytest.raises(ResearchValidationError):
        validate_reasoning("not-json", packet)


def test_compare_reports_helper():
    reports = [_run("AAPL").report, _run("MSFT").report]
    result = compare_reports(reports, persist=False)
    assert result.comparison is not None
    assert set(result.comparison.symbols) == {"AAPL", "MSFT"}
