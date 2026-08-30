from agentic_portfolio.adapters.robinhood_read import RobinhoodSecurityBundle
from agentic_portfolio.correlation import CorrelationObservation, CorrelationStatus, observe_correlation
from agentic_portfolio.paper_workflow import run_paper_research_workflow
from agentic_portfolio.risk_gate import evaluate
from agentic_portfolio.schemas import Decision, GateVerdict, SecurityClass, Sleeve
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry
from tests.conftest import act, ctx


def _wrap(symbol, **fields):
    return {"data": {"results": [{"symbol": symbol, **fields}]}}


def test_paper_research_workflow_does_not_execute(tmp_path):
    bundle = RobinhoodSecurityBundle(
        symbol="SPY",
        tradability=_wrap("SPY", name="State Street SPDR S&P 500 ETF Trust"),
        fundamentals=_wrap(
            "SPY",
            description="SPY tracks a market cap-weighted index of US large- and mid-cap stocks selected by the S&P Committee.",
            sector="Miscellaneous",
            industry="Investment Trusts Or Mutual Funds",
        ),
    )
    nav = 10_000
    c = ctx(nav)
    out = run_paper_research_workflow(
        symbol="SPY",
        bundle=bundle,
        context=c,
        sleeves=SleeveRegistry(tmp_path / "sleeves.json"),
        theses=ThesisRegistry(tmp_path / "theses.json"),
        proposed_sleeve=Sleeve.CORE_GROWTH,
        decision=Decision.BUY,
        proposed_notional=0.05 * nav,
        action_kwargs={"expected_resulting_position_pct": 0.05, "current_price": 500.0},
        cache_path=tmp_path / "class.json",
        journal_path=tmp_path / "journal.jsonl",
        thesis_fields={"thesis_summary": "core beta", "bull_case": "beta", "bear_case": "drawdown"},
    )
    assert out.execution_attempted is False
    assert out.risk.execution_permitted is False
    assert out.classification.security_class.value == "BROAD_MARKET_INDEX_ETF"
    assert out.thesis is not None
    assert out.sleeve == Sleeve.CORE_GROWTH
    assert "review_equity_order" not in (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert "place_equity_order" not in (tmp_path / "journal.jsonl").read_text(encoding="utf-8")


def test_correlation_is_informational_not_a_hard_reject():
    nav = 10_000
    obs = observe_correlation(
        pairwise=[],
        sleeve_level={"CORE_GROWTH": 0.9},
        sector_overlap={"INFORMATION_TECHNOLOGY": 0.4},
    )
    assert obs.status in {CorrelationStatus.PARTIAL, CorrelationStatus.INSUFFICIENT_DATA, CorrelationStatus.AVAILABLE}
    assert obs.future_hard_limit is None
    assert obs.reject_on_limit is False
    r = evaluate(
        ctx(nav),
        act(
            symbol="AAPL",
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            proposed_notional=0.05 * nav,
            expected_resulting_position_pct=0.05,
            correlation_with_book=0.95,
            correlation=CorrelationObservation(status=CorrelationStatus.AVAILABLE, future_hard_limit=None, reject_on_limit=False),
        ),
    )
    assert r.verdict == GateVerdict.PASS
    assert "OVERLAP_OBSERVED" in {x.code for x in r.reasons}
    assert r.verdict != GateVerdict.FAIL
