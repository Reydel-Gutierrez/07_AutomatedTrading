"""Deterministic policy tests. Same % → same verdict at any NAV."""

import pytest

from agentic_portfolio.journal import append_risk_decision
from agentic_portfolio.risk_gate import evaluate
from agentic_portfolio.schemas import (
    ClassificationStatus,
    Decision,
    GateVerdict,
    LiquidityInputs,
    SecurityClass,
    Sleeve,
)
from tests.conftest import act, ctx, pos

NAVS = (1_000, 10_000, 100_000, 1_000_000)


def _codes(result) -> set[str]:
    return {r.code for r in result.reasons}


@pytest.mark.parametrize("nav", NAVS)
def test_scale_invariance_12pct_core_equity(nav):
    c = ctx(nav)
    r = evaluate(
        c,
        act(
            symbol="AAPL",
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            proposed_notional=0.12 * nav,
            expected_resulting_position_pct=0.12,
            enhanced_concentration_review_complete=True,
        ),
    )
    assert r.verdict == GateVerdict.PASS
    assert r.execution_permitted is False


@pytest.mark.parametrize("nav", NAVS)
def test_39_broad_pass_41_fail(nav):
    c = ctx(nav)
    base = dict(
        symbol="SPY",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
        classification_status=ClassificationStatus.VALIDATED,
        enhanced_concentration_review_complete=True,
        high_concentration_review_complete=True,
    )
    ok = evaluate(c, act(**base, proposed_notional=0.39 * nav, expected_resulting_position_pct=0.39))
    bad = evaluate(c, act(**base, proposed_notional=0.41 * nav, expected_resulting_position_pct=0.41))
    assert ok.verdict == GateVerdict.PASS
    assert bad.verdict == GateVerdict.FAIL
    assert "POSITION_CEILING" in _codes(bad)


@pytest.mark.parametrize("nav", NAVS)
def test_other_etf_24_vs_26(nav):
    c = ctx(nav)
    base = dict(
        symbol="XLK",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.OTHER_DIVERSIFIED_ETF,
        enhanced_concentration_review_complete=True,
        high_concentration_review_complete=True,
        sector_concentration_review_complete=True,
    )
    ok = evaluate(c, act(**base, proposed_notional=0.24 * nav, expected_resulting_position_pct=0.24))
    bad = evaluate(c, act(**base, proposed_notional=0.26 * nav, expected_resulting_position_pct=0.26))
    assert ok.verdict == GateVerdict.PASS
    assert bad.verdict == GateVerdict.FAIL


@pytest.mark.parametrize("nav", NAVS)
def test_core_equity_19_review_21_fail(nav):
    c = ctx(nav)
    common = dict(
        symbol="MSFT",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        sector="Information Technology",
        enhanced_concentration_review_complete=True,
        high_concentration_review_complete=True,
        sector_concentration_review_complete=True,
    )
    ok = evaluate(c, act(**common, proposed_notional=0.19 * nav, expected_resulting_position_pct=0.19))
    bad = evaluate(c, act(**common, proposed_notional=0.21 * nav, expected_resulting_position_pct=0.21))
    assert ok.verdict == GateVerdict.PASS
    assert bad.verdict == GateVerdict.FAIL


@pytest.mark.parametrize("nav", NAVS)
def test_opportunistic_16_tactical_11_spec_3_1(nav):
    c = ctx(nav)
    opp = evaluate(
        c,
        act(
            symbol="X",
            sleeve=Sleeve.OPPORTUNISTIC,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Energy",
            proposed_notional=0.16 * nav,
            expected_resulting_position_pct=0.16,
            enhanced_concentration_review_complete=True,
            high_concentration_review_complete=True,
        ),
    )
    tac = evaluate(
        c,
        act(
            symbol="Y",
            sleeve=Sleeve.TACTICAL,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Energy",
            proposed_notional=0.11 * nav,
            expected_resulting_position_pct=0.11,
            enhanced_concentration_review_complete=True,
        ),
    )
    spec = evaluate(
        c,
        act(
            symbol="Z",
            sleeve=Sleeve.SPECULATIVE,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Health Care",
            proposed_notional=0.031 * nav,
            expected_resulting_position_pct=0.031,
            speculative_liquidity_review_complete=True,
        ),
    )
    assert opp.verdict == GateVerdict.FAIL
    assert tac.verdict == GateVerdict.FAIL
    assert spec.verdict == GateVerdict.FAIL


@pytest.mark.parametrize("nav", NAVS)
def test_speculative_sleeve_5_1_fail(nav):
    c = ctx(
        nav,
        [pos("S1", 0.03, nav, Sleeve.SPECULATIVE, SecurityClass.INDIVIDUAL_EQUITY, "Health Care")],
    )
    r = evaluate(
        c,
        act(
            symbol="S2",
            sleeve=Sleeve.SPECULATIVE,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Health Care",
            proposed_notional=0.021 * nav,
            expected_resulting_position_pct=0.021,
            expected_resulting_sleeve_pct=0.051,
            speculative_liquidity_review_complete=True,
        ),
    )
    assert r.verdict == GateVerdict.FAIL
    assert "SPECULATIVE_SLEEVE_CEILING" in _codes(r)


@pytest.mark.parametrize("nav", NAVS)
def test_over_10_without_enhanced_review(nav):
    c = ctx(nav)
    r = evaluate(
        c,
        act(
            symbol="AAPL",
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            proposed_notional=0.12 * nav,
            expected_resulting_position_pct=0.12,
            enhanced_concentration_review_complete=False,
        ),
    )
    assert r.verdict == GateVerdict.REQUIRES_ENHANCED_REVIEW


@pytest.mark.parametrize("nav", NAVS)
def test_over_15_equity_without_high_review(nav):
    c = ctx(nav)
    r = evaluate(
        c,
        act(
            symbol="AAPL",
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            proposed_notional=0.16 * nav,
            expected_resulting_position_pct=0.16,
            enhanced_concentration_review_complete=True,
            high_concentration_review_complete=False,
            sector_concentration_review_complete=True,
        ),
    )
    assert r.verdict == GateVerdict.REQUIRES_ENHANCED_REVIEW
    assert "HIGH_CONCENTRATION_REVIEW_REQUIRED" in _codes(r)


@pytest.mark.parametrize("nav", NAVS)
def test_sector_32_review_46_fail(nav):
    c = ctx(nav)
    review = evaluate(
        c,
        act(
            symbol="AAPL",
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            proposed_notional=0.12 * nav,
            expected_resulting_position_pct=0.12,
            expected_resulting_sector_pct=0.32,
            enhanced_concentration_review_complete=True,
            sector_concentration_review_complete=False,
        ),
    )
    hard = evaluate(
        c,
        act(
            symbol="AAPL",
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            proposed_notional=0.12 * nav,
            expected_resulting_position_pct=0.12,
            expected_resulting_sector_pct=0.46,
            enhanced_concentration_review_complete=True,
            sector_concentration_review_complete=True,
        ),
    )
    assert review.verdict == GateVerdict.REQUIRES_ENHANCED_REVIEW
    assert hard.verdict == GateVerdict.FAIL
    assert "SECTOR_HARD_CEILING" in _codes(hard)


@pytest.mark.parametrize("nav", NAVS)
def test_passive_drift_hold_not_forced_sale(nav):
    c = ctx(
        nav,
        [pos("MSFT", 0.21, nav, Sleeve.CORE_GROWTH, SecurityClass.INDIVIDUAL_EQUITY, "Information Technology")],
    )
    hold = evaluate(
        c,
        act(
            symbol="MSFT",
            decision=Decision.HOLD,
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            expected_resulting_position_pct=0.21,
            proposed_notional=0.0,
        ),
    )
    add = evaluate(
        c,
        act(
            symbol="MSFT",
            decision=Decision.ADD,
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            proposed_notional=0.01 * nav,
            expected_resulting_position_pct=0.22,
            investment_thesis_review_complete=True,
            risk_review_complete=True,
            enhanced_concentration_review_complete=True,
            high_concentration_review_complete=True,
        ),
    )
    assert hold.verdict == GateVerdict.PASS
    assert any(x.code == "PASSIVE_CONCENTRATION_DRIFT" for x in hold.reasons)
    assert add.verdict == GateVerdict.FAIL


@pytest.mark.parametrize("nav", NAVS)
def test_unknown_etf_classification_fail_closed(nav):
    c = ctx(nav)
    r = evaluate(
        c,
        act(
            symbol="SPY",
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
            classification_status=ClassificationStatus.INSUFFICIENT_EVIDENCE,
            proposed_notional=0.10 * nav,
            expected_resulting_position_pct=0.10,
        ),
    )
    assert r.verdict == GateVerdict.FAIL
    assert "CLASSIFICATION_INSUFFICIENT_EVIDENCE" in _codes(r) or "BROAD_MARKET_NOT_VALIDATED" in _codes(r)


@pytest.mark.parametrize("nav", NAVS)
def test_daily_halt_blocks_buy(nav):
    c = ctx(nav, start_of_day_nav=nav / 0.97)  # ~ -3% vs SOD
    assert c.daily_risk_halt
    r = evaluate(
        c,
        act(
            symbol="SPY",
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
            proposed_notional=0.05 * nav,
            expected_resulting_position_pct=0.05,
        ),
    )
    assert r.verdict == GateVerdict.FAIL
    assert "DAILY_RISK_HALT" in _codes(r)


@pytest.mark.parametrize("nav", NAVS)
def test_hwm_minus_10_blocks_spec_tactical(nav):
    c = ctx(nav, prior_hwm=nav / 0.90, prior_nav=nav / 0.90)
    assert c.risk_state.value == "RISK_REDUCTION"
    spec = evaluate(
        c,
        act(
            symbol="SPEC",
            sleeve=Sleeve.SPECULATIVE,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Health Care",
            proposed_notional=0.01 * nav,
            expected_resulting_position_pct=0.01,
            speculative_liquidity_review_complete=True,
        ),
    )
    assert spec.verdict == GateVerdict.FAIL
    assert "RISK_REDUCTION_BLOCK" in _codes(spec)


@pytest.mark.parametrize("nav", NAVS)
def test_defensive_core_must_be_broad_etf(nav):
    c = ctx(nav, prior_hwm=nav / 0.85, prior_nav=nav / 0.85)
    assert c.risk_state.value == "DEFENSIVE"
    eq = evaluate(
        c,
        act(
            symbol="AAPL",
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            proposed_notional=0.05 * nav,
            expected_resulting_position_pct=0.05,
        ),
    )
    etf = evaluate(
        c,
        act(
            symbol="SPY",
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
            proposed_notional=0.05 * nav,
            expected_resulting_position_pct=0.05,
        ),
    )
    assert eq.verdict == GateVerdict.FAIL
    assert etf.verdict == GateVerdict.PASS


@pytest.mark.parametrize("nav", NAVS)
def test_halted_sell_recommend_not_execute(nav):
    c = ctx(
        nav,
        [pos("MSFT", 0.10, nav, Sleeve.CORE_GROWTH, SecurityClass.INDIVIDUAL_EQUITY, "Information Technology")],
        prior_hwm=nav / 0.80,
        prior_nav=nav / 0.80,
    )
    assert c.risk_state.value == "HALTED"
    r = evaluate(
        c,
        act(
            symbol="MSFT",
            decision=Decision.SELL,
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            proposed_notional=0.10 * nav,
            expected_resulting_position_pct=0.0,
            human_authorized_halted_execution=False,
        ),
    )
    assert r.verdict == GateVerdict.HALTED
    assert r.recommendation_permitted is True
    assert r.execution_permitted is False
    buy = evaluate(
        c,
        act(
            symbol="SPY",
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
            proposed_notional=0.05 * nav,
            expected_resulting_position_pct=0.05,
        ),
    )
    assert buy.verdict == GateVerdict.FAIL


@pytest.mark.parametrize("nav", NAVS)
def test_add_without_thesis_and_add_only_for_lower_price(nav):
    c = ctx(
        nav,
        [pos("MSFT", 0.08, nav, Sleeve.CORE_GROWTH, SecurityClass.INDIVIDUAL_EQUITY, "Information Technology")],
    )
    missing = evaluate(
        c,
        act(
            symbol="MSFT",
            decision=Decision.ADD,
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            proposed_notional=0.02 * nav,
            expected_resulting_position_pct=0.10,
            investment_thesis_review_complete=False,
            risk_review_complete=False,
        ),
    )
    cost = evaluate(
        c,
        act(
            symbol="MSFT",
            decision=Decision.ADD,
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            proposed_notional=0.02 * nav,
            expected_resulting_position_pct=0.10,
            investment_thesis_review_complete=True,
            risk_review_complete=True,
            add_justified_only_by_lower_price=True,
        ),
    )
    assert missing.verdict == GateVerdict.FAIL
    assert cost.verdict == GateVerdict.FAIL
    assert "ADD_REVIEW_REQUIRED" in _codes(missing)
    assert "ADD_ONLY_TO_LOWER_COST" in _codes(cost)


@pytest.mark.parametrize("nav", NAVS)
def test_insufficient_liquidity_fail_closed(nav):
    c = ctx(nav)
    r = evaluate(
        c,
        act(
            symbol="AAPL",
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            proposed_notional=0.05 * nav,
            expected_resulting_position_pct=0.05,
            liquidity=LiquidityInputs(median_daily_dollar_volume_20d=None),
        ),
    )
    assert r.verdict == GateVerdict.FAIL
    assert "LIQUIDITY_INSUFFICIENT_EVIDENCE" in _codes(r)


def test_journal_record_shape(tmp_path):
    nav = 10_000
    c = ctx(nav)
    r = evaluate(
        c,
        act(
            symbol="SPY",
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
            proposed_notional=0.05 * nav,
            expected_resulting_position_pct=0.05,
        ),
    )
    p = tmp_path / "risk.jsonl"
    append_risk_decision(r, p)
    assert p.read_text(encoding="utf-8").strip()
    assert r.journal_record["verdict"] == r.verdict.value
    assert "nav" in r.journal_record
