from agentic_portfolio.reconciliation import reconcile
from agentic_portfolio.risk_gate import evaluate
from agentic_portfolio.schemas import (
    Decision,
    GateVerdict,
    SecurityClass,
    Sleeve,
    SleeveAssignmentStatus,
    ThesisStatus,
)
from agentic_portfolio.sleeve_registry import SleeveReclassificationRequired, SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry
from tests.conftest import act, ctx, pos


def test_sleeve_persisted_and_loaded(tmp_path):
    path = tmp_path / "sleeves.json"
    reg = SleeveRegistry(path)
    rec = reg.assign(symbol="AAPL", sleeve=Sleeve.TACTICAL, status=SleeveAssignmentStatus.ACTIVE, thesis_id="t1")
    loaded = SleeveRegistry(path)
    got = loaded.get("AAPL")
    assert got.sleeve == Sleeve.TACTICAL
    assert got.thesis_id == "t1"
    assert got.assigned_at == rec.assigned_at


def test_thesis_persisted_and_loaded(tmp_path):
    path = tmp_path / "theses.json"
    reg = ThesisRegistry(path)
    rec = reg.create(
        symbol="AAPL",
        sleeve=Sleeve.CORE_GROWTH,
        status=ThesisStatus.ACTIVE,
        thesis_summary="compound",
        bull_case="bull",
        bear_case="bear",
    )
    loaded = ThesisRegistry(path)
    got = loaded.get(rec.thesis_id)
    assert got.symbol == "AAPL"
    assert got.status == ThesisStatus.ACTIVE
    assert got.thesis_summary == "compound"


def test_tactical_cannot_silently_become_core(tmp_path):
    reg = SleeveRegistry(tmp_path / "sleeves.json")
    reg.assign(symbol="NVDA", sleeve=Sleeve.TACTICAL, status=SleeveAssignmentStatus.ACTIVE)
    try:
        reg.assign(symbol="NVDA", sleeve=Sleeve.CORE_GROWTH, status=SleeveAssignmentStatus.ACTIVE)
        raised = False
    except SleeveReclassificationRequired:
        raised = True
    assert raised is True
    assert reg.get("NVDA").sleeve == Sleeve.TACTICAL


def test_explicit_reclassification_creates_journal_event(tmp_path):
    journal = tmp_path / "sleeve_reclassification.jsonl"
    reg = SleeveRegistry(tmp_path / "sleeves.json", journal_path=journal)
    reg.assign(symbol="NVDA", sleeve=Sleeve.TACTICAL, status=SleeveAssignmentStatus.ACTIVE)
    event = reg.propose_reclassification(
        symbol="NVDA",
        new_sleeve=Sleeve.CORE_GROWTH,
        reason="multi-year compounding thesis now primary",
        new_thesis_id="t-core",
        review="SLEEVE_RECLASSIFICATION_REVIEW documented",
        approved=True,
    )
    assert event.old_sleeve == Sleeve.TACTICAL
    assert event.new_sleeve == Sleeve.CORE_GROWTH
    assert event.review_flag == "SLEEVE_RECLASSIFICATION_REVIEW"
    assert event.approved is True
    assert "SLEEVE_RECLASSIFICATION_REVIEW" in journal.read_text(encoding="utf-8")
    assert reg.get("NVDA").sleeve == Sleeve.CORE_GROWTH


def test_price_does_not_auto_change_thesis_status(tmp_path):
    reg = ThesisRegistry(tmp_path / "theses.json")
    rec = reg.create(symbol="AAPL", sleeve=Sleeve.CORE_GROWTH, status=ThesisStatus.ACTIVE)
    rec2 = reg.record_price_observation(rec.thesis_id, 400.0)
    assert rec2.status == ThesisStatus.ACTIVE
    assert rec2.last_price_observed == 400.0


def test_add_without_active_thesis_fails_structural(tmp_path):
    nav = 10_000
    c = ctx(nav, [pos("MSFT", 0.08, nav, Sleeve.CORE_GROWTH, SecurityClass.INDIVIDUAL_EQUITY, "Information Technology")])
    theses = ThesisRegistry(tmp_path / "theses.json")
    sleeves = SleeveRegistry(tmp_path / "sleeves.json")
    sleeves.assign(symbol="MSFT", sleeve=Sleeve.CORE_GROWTH, status=SleeveAssignmentStatus.ACTIVE)
    r = evaluate(
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
        ),
        sleeves=sleeves,
        theses=theses,
    )
    assert r.verdict == GateVerdict.FAIL
    assert "ADD_NO_ACTIVE_THESIS" in {x.code for x in r.reasons}


def test_add_with_stale_review_fails(tmp_path):
    nav = 10_000
    c = ctx(nav, [pos("MSFT", 0.08, nav, Sleeve.CORE_GROWTH, SecurityClass.INDIVIDUAL_EQUITY, "Information Technology")])
    theses = ThesisRegistry(tmp_path / "theses.json")
    sleeves = SleeveRegistry(tmp_path / "sleeves.json")
    sleeves.assign(symbol="MSFT", sleeve=Sleeve.CORE_GROWTH, status=SleeveAssignmentStatus.ACTIVE)
    thesis = theses.create(symbol="MSFT", sleeve=Sleeve.CORE_GROWTH, status=ThesisStatus.ACTIVE)
    theses.add_review(thesis.thesis_id, review_type="INVESTMENT_THESIS_REVIEW", reviewed_at="2020-01-01T00:00:00+00:00", session_id="old")
    theses.add_review(thesis.thesis_id, review_type="RISK_REVIEW", reviewed_at="2020-01-01T00:00:00+00:00", session_id="old")
    r = evaluate(
        c,
        act(
            symbol="MSFT",
            decision=Decision.ADD,
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            proposed_notional=0.02 * nav,
            expected_resulting_position_pct=0.10,
            thesis_id=thesis.thesis_id,
            investment_thesis_review_complete=True,
            risk_review_complete=True,
        ),
        sleeves=sleeves,
        theses=theses,
    )
    assert r.verdict == GateVerdict.FAIL
    codes = {x.code for x in r.reasons}
    assert "ADD_STALE_THESIS_REVIEW" in codes or "ADD_STALE_RISK_REVIEW" in codes


def test_unregistered_robinhood_position_blocks_add(tmp_path):
    nav = 10_000
    c = ctx(nav, [pos("MSFT", 0.08, nav, Sleeve.CORE_GROWTH, SecurityClass.INDIVIDUAL_EQUITY, "Information Technology")])
    sleeves = SleeveRegistry(tmp_path / "sleeves.json")
    theses = ThesisRegistry(tmp_path / "theses.json")
    r = evaluate(
        c,
        act(
            symbol="MSFT",
            decision=Decision.ADD,
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            proposed_notional=0.01 * nav,
            expected_resulting_position_pct=0.09,
            investment_thesis_review_complete=True,
            risk_review_complete=True,
        ),
        sleeves=sleeves,
        theses=theses,
    )
    assert r.verdict == GateVerdict.FAIL
    assert "UNREGISTERED_POSITION" in {x.code for x in r.reasons}
    report = reconcile(
        robinhood_positions=c.positions,
        sleeves=sleeves,
        theses=theses,
    )
    assert "UNREGISTERED_POSITION" in {f.code for f in report.findings}
    sell = evaluate(
        c,
        act(
            symbol="MSFT",
            decision=Decision.SELL,
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            sector="Information Technology",
            proposed_notional=0.08 * nav,
            expected_resulting_position_pct=0.0,
        ),
        sleeves=sleeves,
        theses=theses,
    )
    assert sell.verdict != GateVerdict.FAIL or "UNREGISTERED_POSITION" not in {x.code for x in sell.reasons}


def test_local_active_missing_robinhood_finding(tmp_path):
    sleeves = SleeveRegistry(tmp_path / "sleeves.json")
    theses = ThesisRegistry(tmp_path / "theses.json")
    sleeves.assign(symbol="MSFT", sleeve=Sleeve.CORE_GROWTH, status=SleeveAssignmentStatus.ACTIVE)
    theses.create(symbol="MSFT", sleeve=Sleeve.CORE_GROWTH, status=ThesisStatus.ACTIVE)
    report = reconcile(robinhood_positions=[], sleeves=sleeves, theses=theses)
    codes = {f.code for f in report.findings}
    assert "LOCAL_ACTIVE_NOT_HELD" in codes
    assert "ACTIVE_THESIS_NO_LIVE_POSITION" in codes


def test_closed_thesis_live_position_finding(tmp_path):
    nav = 10_000
    positions = [pos("MSFT", 0.08, nav, Sleeve.CORE_GROWTH, SecurityClass.INDIVIDUAL_EQUITY, "Information Technology")]
    sleeves = SleeveRegistry(tmp_path / "sleeves.json")
    theses = ThesisRegistry(tmp_path / "theses.json")
    sleeves.assign(symbol="MSFT", sleeve=Sleeve.CORE_GROWTH, status=SleeveAssignmentStatus.ACTIVE)
    theses.create(symbol="MSFT", sleeve=Sleeve.CORE_GROWTH, status=ThesisStatus.CLOSED)
    report = reconcile(robinhood_positions=positions, sleeves=sleeves, theses=theses)
    codes = {f.code for f in report.findings}
    assert "CLOSED_THESIS_LIVE_POSITION" in codes
    assert "MISSING_THESIS" in codes
