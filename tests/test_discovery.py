"""Deterministic Candidate Discovery tests. No broker execution."""

from datetime import datetime, timedelta, timezone

import pytest

from agentic_portfolio.discovery.engine import (
    expire_candidates,
    inspect_discovery_module_for_forbidden_tools,
    run_discovery,
)
from agentic_portfolio.discovery.safety import (
    DISCOVERY_FORBIDDEN_TOOLS,
    DiscoverySafetyError,
    as_proposed_action,
    candidate_cannot_become_buy,
)
from agentic_portfolio.discovery.scoring import score_signals
from agentic_portfolio.discovery.signals import contribution, make_signal
from agentic_portfolio.discovery.snapshot import SecuritySnapshot
from agentic_portfolio.discovery.store import CandidateStore, DiscoveryRunStore, ResearchQueue
from agentic_portfolio.schemas import (
    CandidateStatus,
    ClassificationResult,
    ClassificationStatus,
    Decision,
    DiscoveryPriority,
    LiquidityEvidence,
    MarketRegime,
    MarketRegimeStatus,
    RiskState,
    SecurityClass,
    SignalDirection,
    SignalType,
    Sleeve,
)
from agentic_portfolio.sectors import CanonicalSector, SectorStatus
from tests.conftest import ctx, pos

TS = "2026-08-29T15:00:00+00:00"
NOW = datetime(2026, 8, 29, 15, 0, tzinfo=timezone.utc)


def _liq(dollar=5.0e8, spread=0.0005):
    return LiquidityEvidence(recent_dollar_volume=dollar, bid_ask_spread_pct=spread, status="PARTIAL")


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


def _snap(**kwargs) -> SecuritySnapshot:
    kwargs.setdefault("observed_at", TS)
    kwargs.setdefault("tradable", True)
    kwargs.setdefault("instrument_kind", "equity")
    kwargs.setdefault("name", "Test Co")
    kwargs.setdefault("liquidity", _liq())
    kwargs.setdefault("sources", ["test"])
    kwargs.setdefault("data_origin", "test")
    return SecuritySnapshot(**kwargs)


def _run(snaps, context, *, persist=False, **kwargs):
    return run_discovery(
        snaps,
        context,
        regime=kwargs.pop("regime", MarketRegime.unknown(observed_at=TS)),
        persist=persist,
        promote_shortlist=kwargs.pop("promote_shortlist", False),
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


def _quality_core(symbol="QUAL"):
    return _snap(
        symbol=symbol,
        sources=["fundamentals", "financials", "test"],
        data_origin="test",
        current_price=85.0,
        market_cap=8.0e10,
        pe_ratio=22.0,
        sector="INFORMATION_TECHNOLOGY",
        classification=_cls(symbol),
        revenue_periods=[10e9, 9.5e9, 9.1e9, 8.8e9, 8.2e9],
        net_income_periods=[2e9, 1.8e9, 1.7e9, 1.6e9, 1.4e9],
        net_margin_periods=[0.20, 0.19, 0.187, 0.182, 0.17],
    )


def _weak_core(symbol="WEAK"):
    return _snap(
        symbol=symbol,
        current_price=40.0,
        market_cap=5.0e10,
        pe_ratio=8.0,
        sector="INFORMATION_TECHNOLOGY",
        classification=_cls(symbol),
        revenue_periods=[5e9, 6e9, 7e9, 8e9, 9e9],
        net_income_periods=[-2e8, -1e8, 1e8, 3e8, 4e8],
        net_margin_periods=[-0.04, -0.02, 0.01, 0.04, 0.05],
    )


def _famous_only(symbol="FAME"):
    return _snap(
        symbol=symbol,
        name="Globally Famous Inc",
        current_price=180.0,
        market_cap=2.0e12,
        sector="INFORMATION_TECHNOLOGY",
        classification=_cls(symbol),
    )


def _opp_quality_selloff(symbol="DISP"):
    return _snap(
        symbol=symbol,
        current_price=60.0,
        market_cap=4.0e10,
        pe_ratio=14.0,
        sector="HEALTH_CARE",
        classification=_cls(symbol, CanonicalSector.HEALTH_CARE),
        return_21d=-0.22,
        return_5d=0.03,
        drawdown_from_52w_high=0.35,
        high_52_week=92.0,
        revenue_periods=[7.7e9, 7.75e9, 7.72e9, 7.7e9, 7.68e9],
        net_income_periods=[1.1e9, 1.12e9, 1.08e9, 1.09e9, 1.05e9],
        net_margin_periods=[0.143, 0.144, 0.140, 0.141, 0.137],
        earnings_surprise_last=-0.08,
    )


def _opp_collapsing(symbol="CRASH"):
    s = _opp_quality_selloff(symbol)
    s.revenue_periods = [4e9, 5e9, 6.5e9, 8e9, 9e9]
    s.net_income_periods = [-8e8, -2e8, 4e8, 9e8, 1.2e9]
    s.net_margin_periods = [-0.20, -0.04, 0.06, 0.11, 0.13]
    s.pe_ratio = 4.0
    return s


def _tactical(symbol="TSET"):
    return _snap(
        symbol=symbol,
        current_price=100.0,
        sma_50=95.0,
        sma_200=90.0,
        rsi=62.0,
        return_21d=0.09,
        volume_vs_avg=1.8,
        volume=8e6,
        average_volume=4.4e6,
        sector="INDUSTRIALS",
        classification=_cls(symbol, CanonicalSector.INDUSTRIALS),
        sources=["technicals", "historicals"],
    )


def _spec_catalyst(symbol="ASYM", price=12.0, dollar=2.0e7):
    return _snap(
        symbol=symbol,
        current_price=price,
        market_cap=8.0e8,
        sector="HEALTH_CARE",
        classification=_cls(symbol, CanonicalSector.HEALTH_CARE),
        description="Clinical-stage biotech with a phase 3 pipeline readout.",
        earnings_upcoming_days=10,
        news_headlines=["Phase 3 trial meets primary endpoint"],
        liquidity=_liq(dollar, 0.01),
        revenue_periods=[4e7, 3.2e7, 2.8e7, 2.1e7, 1.5e7],
        net_income_periods=[-2e7, -1.8e7, -1.5e7, -1.2e7, -1.0e7],
        net_margin_periods=[-0.5, -0.56, -0.54, -0.57, -0.67],
        sources=["news", "earnings", "fundamentals"],
    )


def test_core_candidate_from_strong_quality_signals():
    out = _run([_quality_core()], ctx(10_000))
    live = [c for c in out.candidates if c.symbol == "QUAL"]
    assert live, out.rejected
    c = live[0]
    assert c.provisional_sleeve == Sleeve.CORE_GROWTH
    assert c.discovery_score >= 55
    assert c.status in {CandidateStatus.SHORTLISTED, CandidateStatus.DISCOVERED, CandidateStatus.PROMOTED_TO_RESEARCH}
    assert any(s.signal_type == SignalType.FUNDAMENTAL and s.name == "revenue_growth" for s in c.signals)


def test_weak_core_rejected():
    out = _run([_weak_core()], ctx(10_000))
    rejected = [c for c in out.rejected if c.symbol == "WEAK"]
    assert rejected
    assert rejected[0].status == CandidateStatus.REJECTED
    assert rejected[0].rejection_reason


def test_famous_company_not_automatic_core():
    out = _run([_famous_only()], ctx(10_000))
    rejected = [c for c in out.rejected if c.symbol == "FAME"]
    assert rejected
    assert rejected[0].status == CandidateStatus.REJECTED


def test_post_selloff_quality_becomes_opportunistic():
    out = _run([_opp_quality_selloff()], ctx(10_000))
    c = next(x for x in out.candidates if x.symbol == "DISP")
    assert c.provisional_sleeve == Sleeve.OPPORTUNISTIC
    assert "TEMPORARY_PRICE_DISLOCATION_CANDIDATE" in c.event_flags or any(
        "TEMPORARY_PRICE_DISLOCATION" in q for q in c.research_questions
    )
    assert c.status != CandidateStatus.REJECTED


def test_collapsing_fundamentals_penalize_opportunistic():
    good = _run([_opp_quality_selloff()], ctx(10_000))
    bad = _run([_opp_collapsing()], ctx(10_000))
    g = next(x for x in good.candidates if x.symbol == "DISP")
    b = next(x for x in bad.candidates + bad.rejected if x.symbol == "CRASH")
    assert b.discovery_score < g.discovery_score
    assert b.status == CandidateStatus.REJECTED or b.discovery_score < 40


def test_valid_tactical_momentum_volume_setup():
    out = _run([_tactical()], ctx(10_000))
    c = next(x for x in out.candidates if x.symbol == "TSET")
    assert c.provisional_sleeve == Sleeve.TACTICAL
    assert any(s.signal_type == SignalType.VOLUME for s in c.signals)
    assert any(s.signal_type == SignalType.MOMENTUM or s.name in {"trend", "sma_alignment"} for s in c.signals)


def test_tactical_expires_faster_than_core(tmp_path):
    store = CandidateStore(tmp_path / "c.json")
    _run(
        [_quality_core("COREX"), _tactical("TACTX")],
        ctx(10_000),
        persist=True,
        candidate_store=store,
        queue_store=ResearchQueue(tmp_path / "q.json"),
        run_store=DiscoveryRunStore(tmp_path / "r.json"),
    )
    later = NOW + timedelta(hours=9)
    expired = expire_candidates(store, now=later)
    symbols = {c.symbol for c in expired}
    assert "TACTX" in symbols
    assert "COREX" not in symbols
    core = store.active_for_symbol("COREX")
    tact = next(c for c in store.all() if c.symbol == "TACTX")
    assert core is not None and core.status != CandidateStatus.EXPIRED
    assert tact.status == CandidateStatus.EXPIRED


def test_speculative_with_catalyst_may_qualify():
    out = _run([_spec_catalyst()], ctx(10_000))
    found = [c for c in out.candidates if c.symbol == "ASYM"]
    assert found, [(c.symbol, c.rejection_reason) for c in out.rejected]
    c = found[0]
    assert c.provisional_sleeve == Sleeve.SPECULATIVE
    assert "sleeve_cap_3pct_nav_per_name_5pct_total" in c.known_risks


def test_low_share_price_alone_gives_no_positive_score():
    cheap = _snap(symbol="PENY", current_price=0.80, market_cap=4e8, sector="MATERIALS")
    out = _run([cheap], ctx(10_000))
    rejected = [c for c in out.rejected if c.symbol == "PENY"]
    assert rejected
    sig = make_signal(SignalType.VALUATION, "share_price", value=0.80, direction=SignalDirection.POSITIVE, strength=1.0)
    assert contribution(sig) == 0.0
    ra = _run([_spec_catalyst("P80", price=0.80)], ctx(10_000))
    rb = _run([_spec_catalyst("P80B", price=80.0)], ctx(10_000))
    ca = next(x for x in ra.candidates + ra.rejected if x.symbol == "P80")
    cb = next(x for x in rb.candidates + rb.rejected if x.symbol == "P80B")
    assert abs(ca.discovery_score - cb.discovery_score) < 1.0


def test_severe_speculative_liquidity_rejects():
    snap = _spec_catalyst("ILLQ", dollar=20_000)
    snap.liquidity = _liq(20_000, 0.12)
    out = _run([snap], ctx(10_000))
    hit = next(x for x in out.candidates + out.rejected if x.symbol == "ILLQ")
    assert hit.status == CandidateStatus.REJECTED
    assert hit.rejection_reason in {"unusable_liquidity", "extreme_spread", "severe_speculative_liquidity"}


def test_portfolio_sector_overlap_lowers_priority():
    snap = _quality_core("NVDAX")
    empty = _run([snap], ctx(10_000))
    heavy = ctx(
        10_000,
        [pos("OTHER", 0.35, 10_000, Sleeve.CORE_GROWTH, SecurityClass.INDIVIDUAL_EQUITY, sector="Information Technology")],
    )
    overlapped = _run([snap], heavy)
    c0 = next(x for x in empty.candidates if x.symbol == "NVDAX")
    c1 = next(x for x in overlapped.candidates if x.symbol == "NVDAX")
    assert c0.discovery_score == c1.discovery_score
    order = [DiscoveryPriority.LOW, DiscoveryPriority.MEDIUM, DiscoveryPriority.HIGH, DiscoveryPriority.URGENT_RESEARCH]
    assert order.index(c1.priority) <= order.index(c0.priority)
    assert c1.overlap_penalty > 0 or any("overlap" in k for k in c1.known_risks)


def test_risk_reduction_suppresses_tactical_and_spec():
    c = ctx(10_000)
    c.risk_state = RiskState.RISK_REDUCTION
    out = _run([_tactical(), _spec_catalyst(), _quality_core()], c)
    tact = next(x for x in out.candidates + out.rejected if x.symbol == "TSET")
    spec = next(x for x in out.candidates + out.rejected if x.symbol == "ASYM")
    core = next(x for x in out.candidates if x.symbol == "QUAL")
    if tact.status != CandidateStatus.REJECTED:
        assert tact.priority == DiscoveryPriority.LOW
        assert tact.status == CandidateStatus.DISCOVERED
    if spec.status != CandidateStatus.REJECTED:
        assert spec.priority == DiscoveryPriority.LOW
    assert core.status in {CandidateStatus.SHORTLISTED, CandidateStatus.DISCOVERED, CandidateStatus.PROMOTED_TO_RESEARCH}


def test_halted_marks_action_blocked_but_continues_discovery():
    c = ctx(10_000)
    c.risk_state = RiskState.HALTED
    out = _run([_quality_core()], c)
    assert out.candidates
    hit = out.candidates[0]
    assert hit.action_blocked_reason == "ACTION_CURRENTLY_BLOCKED_BY_RISK_STATE"
    assert hit.status != CandidateStatus.REJECTED


def test_unknown_market_regime_does_not_fabricate_signals():
    regime = MarketRegime.unknown(observed_at=TS)
    assert regime.status == MarketRegimeStatus.UNKNOWN
    assert regime.trend is None
    assert regime.spy_trend is None
    out = _run([_quality_core()], ctx(10_000), regime=regime)
    c = out.candidates[0]
    assert not any(s.signal_type == SignalType.MARKET_REGIME for s in c.signals)


def test_duplicate_discovery_sources_merge_into_one_candidate():
    a = _quality_core("DUP")
    a.sources = ["scanner"]
    b = _quality_core("DUP")
    b.sources = ["earnings"]
    b.pe_ratio = None
    out = _run([a, b], ctx(10_000))
    live = [c for c in out.candidates if c.symbol == "DUP"]
    assert len(live) == 1
    assert set(live[0].discovery_sources) >= {"scanner", "earnings"}


def test_candidate_and_queue_and_run_persist(tmp_path):
    cstore = CandidateStore(tmp_path / "c.json")
    qstore = ResearchQueue(tmp_path / "q.json")
    rstore = DiscoveryRunStore(tmp_path / "r.json")
    out = run_discovery(
        [_quality_core()],
        ctx(10_000),
        regime=MarketRegime.unknown(observed_at=TS),
        persist=True,
        promote_shortlist=True,
        now=NOW,
        candidate_store=cstore,
        queue_store=qstore,
        run_store=rstore,
    )
    assert cstore.get(out.candidates[0].candidate_id) is not None
    reloaded_c = CandidateStore(tmp_path / "c.json").get(out.candidates[0].candidate_id)
    assert reloaded_c is not None
    assert reloaded_c.symbol == "QUAL"
    run = DiscoveryRunStore(tmp_path / "r.json").get(out.run.run_id)
    assert run is not None
    assert run.theses_created == 0
    assert run.buy_actions_created == 0
    assert run.execution_attempted is False
    if out.candidates[0].status == CandidateStatus.PROMOTED_TO_RESEARCH:
        assert out.queue
        q = ResearchQueue(tmp_path / "q.json").get(out.queue[0].queue_id)
        assert q is not None
        assert q.symbol == "QUAL"


def test_repeat_discovery_does_not_duplicate_candidates_or_queue(tmp_path):
    from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT

    cstore = CandidateStore(tmp_path / "c.json")
    qstore = ResearchQueue(tmp_path / "q.json")
    rstore = DiscoveryRunStore(tmp_path / "r.json")
    kwargs = dict(
        snapshots=[_quality_core()],
        context=ctx(10_000),
        regime=MarketRegime.unknown(observed_at=TS),
        persist=True,
        promote_shortlist=True,
        now=NOW,
        candidate_store=cstore,
        queue_store=qstore,
        run_store=rstore,
    )
    first = run_discovery(**kwargs)
    origin = (first.candidates or first.rejected)[0]
    second = run_discovery(**kwargs)
    live = [c for c in cstore.all() if c.symbol == "QUAL" and c.status != CandidateStatus.REJECTED]
    assert len({c.candidate_id for c in live}) == 1
    assert live[0].candidate_id == origin.candidate_id
    queued = [e for e in qstore.all() if e.symbol == "QUAL"]
    assert len(queued) <= 1
    assert second.queue == []
    assert LIVE_ORDER_PLACEMENT is False


def test_empty_universe_is_no_high_quality_candidates():
    out = _run([], ctx(10_000))
    assert out.conclusion == "NO_HIGH_QUALITY_CANDIDATES"
    assert out.candidates == []


def test_no_execution_tool_reachable_from_discovery():
    hits = inspect_discovery_module_for_forbidden_tools()
    assert hits == []
    out = _run([_quality_core()], ctx(10_000), sources_queried=["search", "get_equity_fundamentals"])
    assert out.execution_attempted is False
    assert out.buy_actions_created == 0
    assert out.theses_created == 0
    with pytest.raises(DiscoverySafetyError):
        run_discovery(
            [_quality_core()],
            ctx(10_000),
            persist=False,
            promote_shortlist=False,
            now=NOW,
            sources_queried=["place_equity_order"],
        )


def test_candidate_cannot_directly_become_buy_action():
    out = _run([_quality_core()], ctx(10_000))
    c = out.candidates[0]
    with pytest.raises(DiscoverySafetyError):
        candidate_cannot_become_buy(c)
    with pytest.raises(DiscoverySafetyError):
        as_proposed_action(c)
    assert out.run.buy_actions_created == 0
    assert Decision.BUY.value == "BUY"


def test_scaling_nav_does_not_materially_alter_discovery_score():
    snap = _quality_core("SCALE")
    scores = []
    for nav in (1_000, 10_000, 100_000, 1_000_000):
        out = _run([snap], ctx(nav))
        c = next(x for x in out.candidates if x.symbol == "SCALE")
        scores.append(c.discovery_score)
    assert max(scores) - min(scores) < 1e-6


def test_sleeve_scores_are_not_universal():
    sigs = [
        make_signal(SignalType.QUALITY, "competitive_position", direction=SignalDirection.POSITIVE, strength=0.8),
        make_signal(SignalType.FUNDAMENTAL, "profitability", direction=SignalDirection.POSITIVE, strength=0.8),
        make_signal(SignalType.FUNDAMENTAL, "revenue_growth", direction=SignalDirection.POSITIVE, strength=0.7),
        make_signal(SignalType.MOMENTUM, "medium_term", direction=SignalDirection.POSITIVE, strength=0.9),
        make_signal(SignalType.VOLUME, "expansion", direction=SignalDirection.POSITIVE, strength=0.9),
        make_signal(SignalType.PRICE_ACTION, "selloff", direction=SignalDirection.POSITIVE, strength=0.9),
    ]
    core, _ = score_signals(sigs, Sleeve.CORE_GROWTH)
    tact, _ = score_signals(sigs, Sleeve.TACTICAL)
    opp, _ = score_signals(sigs, Sleeve.OPPORTUNISTIC)
    assert len({round(core, 2), round(tact, 2), round(opp, 2)}) > 1


def test_forbidden_tool_set_covers_execution():
    for tool in (
        "review_equity_order",
        "place_equity_order",
        "cancel_equity_order",
        "place_option_order",
        "place_crypto_order",
    ):
        assert tool in DISCOVERY_FORBIDDEN_TOOLS


def _broad_etf(symbol="SPYX"):
    return _snap(
        symbol=symbol,
        instrument_kind="etf",
        name="Test S&P 500 ETF",
        description="Tracks a market cap-weighted index of US large- and mid-cap stocks selected by the S&P Committee.",
        current_price=500.0,
        market_cap=4.0e11,
        sector="Miscellaneous",
        industry="Investment Trusts Or Mutual Funds",
        classification=ClassificationResult(
            security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
            status=ClassificationStatus.VALIDATED,
            effective_class_for_ceiling=SecurityClass.BROAD_MARKET_INDEX_ETF,
            confidence="high",
            symbol=symbol,
        ),
        sources=["search"],
    )


def test_broad_etf_without_financials_can_surface_as_core():
    out = _run([_broad_etf()], ctx(10_000))
    c = next(x for x in out.candidates + out.rejected if x.symbol == "SPYX")
    assert c.provisional_sleeve == Sleeve.CORE_GROWTH
    assert c.status != CandidateStatus.REJECTED
    assert any(s.name == "diversified_fund" for s in c.signals)


def test_total_market_etf_space_is_not_spac_speculative_theme():
    snap = _snap(
        symbol="VTIX",
        instrument_kind="etf",
        name="Total Stock Market ETF",
        description="The fund seeks to track a market-cap-weighted portfolio that provides total market exposure to the US equity space.",
        current_price=380.0,
        market_cap=6.0e11,
        classification=ClassificationResult(
            security_class=SecurityClass.OTHER_DIVERSIFIED_ETF,
            status=ClassificationStatus.VALIDATED,
            effective_class_for_ceiling=SecurityClass.OTHER_DIVERSIFIED_ETF,
            confidence="high",
            symbol="VTIX",
        ),
        sources=["search"],
        news_headlines=[],
    )
    out = _run([snap], ctx(10_000))
    hit = next(x for x in out.candidates + out.rejected if x.symbol == "VTIX")
    assert hit.provisional_sleeve != Sleeve.SPECULATIVE
    assert "SPECULATIVE_ASYMMETRY_DISCOVERY" not in (hit.channels or [])


def test_no_max_3_same_sector_sleeve_rejection():
    """Five research-worthy Tech Core names must all remain persisted.

    Overlap may lower queue priority and assign a comparison group.
    It must not REJECT the fourth/fifth solely because three appeared first.
    """
    snaps = [_quality_core(sym) for sym in ("NVDAX", "AVGX", "MSFTX", "AAPLX", "PLTRX")]
    solo = _run([_quality_core("AAPLX")], ctx(10_000), promote_shortlist=True)
    out = _run(snaps, ctx(10_000), promote_shortlist=True)
    by_sym = {c.symbol: c for c in out.candidates}
    for sym in ("NVDAX", "AVGX", "MSFTX", "AAPLX", "PLTRX"):
        assert sym in by_sym, (sym, [(c.symbol, c.rejection_reason) for c in out.rejected])
        c = by_sym[sym]
        assert c.status != CandidateStatus.REJECTED
        assert c.rejection_reason is None
        assert c.comparison_group_id
        assert any("OVERLAP_PRIORITY_PENALTY" in w for w in c.overlap_warnings)
        assert c.status in {
            CandidateStatus.SHORTLISTED,
            CandidateStatus.PROMOTED_TO_RESEARCH,
            CandidateStatus.DISCOVERED,
        }
    solo_aapl = next(c for c in solo.candidates if c.symbol == "AAPLX")
    assert by_sym["AAPLX"].discovery_score == solo_aapl.discovery_score
    assert len({c.comparison_group_id for c in by_sym.values()}) == 1
    deferred = [c for c in by_sym.values() if c.deferred_due_to_overlap]
    assert deferred, "lower-ranked peers should be deferred, not dropped"
    assert all("DEFERRED_DUE_TO_RESEARCH_QUEUE_OVERLAP" in c.reasons for c in deferred)
    queued = {q.symbol: q for q in out.queue}
    for c in deferred:
        assert c.symbol in queued, "deferred names stay on the research queue"
        assert queued[c.symbol].deferred_due_to_research_queue_overlap is True
        assert queued[c.symbol].comparison_group_id == c.comparison_group_id
    assert not any(c.rejection_reason == "excessive_duplication_sector_sleeve" for c in out.rejected)
