"""Human Approval Packet tests. Packaging only; no broker calls."""

from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pytest

from agentic_portfolio.approval.engine import (
    create_approval_packet,
    record_human_decision,
    refresh_approval,
    run_approval,
)
from agentic_portfolio.approval.report import render_packet
from agentic_portfolio.approval.safety import (
    APPROVAL_FORBIDDEN_TOOLS,
    ApprovalSafetyError,
    inspect_approval_module_for_forbidden_tools,
)
from agentic_portfolio.approval.store import ApprovalStore
from agentic_portfolio.approval.types import (
    ApprovalMarketView,
    ApprovalRequest,
    ApprovalStatus,
)
from agentic_portfolio.approval.validate import ApprovalValidationError
from agentic_portfolio.decision.types import NameDecision, PortfolioComparison
from agentic_portfolio.execution.types import (
    ExecutionStatus,
    LiquidityCheck,
    OrderPlan,
    OrderSide,
    OrderType,
    QuoteSnapshot,
    SlippageCheck,
    TimeInForce,
)
from agentic_portfolio.research.types import ResearchFreshness, ResearchReport, ResearchStatus, ScenarioCase
from agentic_portfolio.schemas import (
    ClassificationStatus,
    Decision,
    ExitPolicy,
    GateVerdict,
    Position,
    RiskGateResult,
    RiskState,
    SecurityClass,
    Sleeve,
    ThesisRecord,
    ThesisStatus,
)
from tests.conftest import act, ctx

TS = "2026-08-30T18:30:00+00:00"
NOW = datetime(2026, 8, 30, 18, 30, tzinfo=timezone.utc)


def _quote(symbol, price, *, stale=False, observed_at=TS, spread=0.001):
    half = price * spread / 2.0
    return QuoteSnapshot(
        symbol=symbol,
        last_price=price,
        bid=price - half,
        ask=price + half,
        spread_pct=spread,
        observed_at=observed_at,
        stale=stale,
        source="paper",
    )


def _held(symbol, pct, nav, sleeve, price):
    return Position(
        symbol=symbol,
        market_value=pct * nav,
        quantity=(pct * nav) / price,
        current_price=price,
        sleeve=sleeve,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        classification_status=ClassificationStatus.VALIDATED,
    )


def _plan(symbol, action, quantity, notional, price, *, status=ExecutionStatus.PAPER_ONLY, after_pct=None, blocked=None, decision_id="dec-1"):
    return OrderPlan(
        order_plan_id=str(uuid4()),
        symbol=symbol,
        action=action,
        quantity=quantity,
        notional=notional,
        estimated_price=price,
        estimated_position_quantity_after=None,
        estimated_position_notional_after=None,
        estimated_position_pct_after=after_pct,
        order_side=OrderSide.BUY if action in {Decision.BUY, Decision.ADD} else OrderSide.SELL_TO_CLOSE,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GFD,
        slippage_check=SlippageCheck(ok=True, estimated_slippage_pct=0.0005, max_slippage_pct=0.01, spread_pct=0.001, codes=[]),
        liquidity_check=LiquidityCheck(ok=True, codes=[]),
        source_decision_id=decision_id,
        thesis_id="t-1",
        risk_evaluation_id="risk-1",
        execution_status=status,
        live_execution_blocked=True,
        blocked_reasons=list(blocked or []),
        created_at=TS,
        stop_orders_created=0,
        broker_submitted=False,
        live_trade_actions_allowed=False,
        auto_execution=False,
    )


def _risk(*, verdict=GateVerdict.PASS, rec=True, reviews=None):
    return RiskGateResult(
        verdict=verdict,
        execution_permitted=False,
        recommendation_permitted=rec,
        reasons=[],
        required_reviews=list(reviews or []),
        applicable_position_ceiling_pct=15.0,
        snapshot_id="risk-1",
    )


def _thesis(**kwargs):
    return ThesisRecord(
        thesis_id=kwargs.get("thesis_id", "t-1"),
        symbol=kwargs.get("symbol", "NKE"),
        sleeve=kwargs.get("sleeve", Sleeve.OPPORTUNISTIC),
        created_at=TS,
        updated_at=kwargs.get("updated_at", TS),
        status=kwargs.get("status", ThesisStatus.WEAKENED),
        thesis_summary=kwargs.get("thesis_summary", "Post-selloff recovery."),
        bull_case=kwargs.get("bull_case", "Brand restabilizes."),
        base_case=kwargs.get("base_case", "Range-bound earnings."),
        bear_case=kwargs.get("bear_case", "Further deterioration."),
        risks=kwargs.get("risks", ["brand heat loss"]),
        invalidation_conditions=kwargs.get("invalidation_conditions", ["recovery thesis fails"]),
        exit_policy=ExitPolicy(thesis_based=True, event_invalidation="recovery thesis fails", broker_stop_orders_created=False),
        expected_horizon="12-24 months",
        research_id="r-1",
        desired_allocation_pct=2.0,
    )


def _report(**kwargs):
    return ResearchReport(
        research_id=kwargs.get("research_id", "r-1"),
        candidate_id="c-1",
        symbol=kwargs.get("symbol", "NKE"),
        started_at=TS,
        completed_at=TS,
        provisional_sleeve=Sleeve.OPPORTUNISTIC,
        executive_summary="Drawdown is not proof of dislocation.",
        bull_case=ScenarioCase(case="BULL_CASE", summary="Brand restabilizes."),
        base_case=ScenarioCase(case="BASE_CASE", summary="Range-bound."),
        bear_case=ScenarioCase(case="BEAR_CASE", summary="Further deterioration."),
        key_risks=["brand heat loss"],
        expected_horizon="12-24 months",
        research_status=kwargs.get("research_status", ResearchStatus.RESEARCH_COMPLETE),
        freshness=kwargs.get("freshness", ResearchFreshness.FRESH),
        stale_after=kwargs.get("stale_after"),
        evidence_refs=["fact:market_price"],
    )


def _req(plan, context, action=None, risk=None, quote=None, thesis=None, decision=None, report=None, comparison=None, monitoring=None):
    action = action or act(
        symbol=plan.symbol,
        sleeve=Sleeve.OPPORTUNISTIC,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        decision=plan.action,
        proposed_notional=plan.notional,
        expected_resulting_position_pct=plan.estimated_position_pct_after,
        current_price=plan.estimated_price,
        sector="CONSUMER_STAPLES",
        explicitly_risk_reducing=plan.action in {Decision.SELL, Decision.REDUCE},
        thesis_id="t-1",
    )
    return ApprovalRequest(
        plan=plan,
        action=action,
        risk=risk or _risk(),
        context=context,
        thesis=thesis if thesis is not None else _thesis(symbol=plan.symbol),
        decision=decision
        if decision is not None
        else NameDecision(
            symbol=plan.symbol,
            decision=plan.action,
            desired_allocation_pct=2.0 if plan.action == Decision.REDUCE else 0.0,
            rationale="Earnings plus news keep recovery vs deterioration open. Reduce.",
            why_preferable_to_cash=None,
            why_preferable_to_spy=None,
            thesis_id="t-1",
            research_id="r-1",
        ),
        comparison=comparison or PortfolioComparison(ranking=[plan.symbol, "CASH"], vs_cash="Moving toward cash.", vs_spy="Not a SPY swap."),
        report=report if report is not None else _report(symbol=plan.symbol),
        monitoring=monitoring or {"state": "THESIS_WEAKENED", "reassessment": {"rationale": "Reduce; do not add."}},
        quote=quote if quote is not None else _quote(plan.symbol, plan.estimated_price),
        monitoring_run_id="mon-1",
    )


def _run(items, context, *, persist=False, tmp_path=None, sources=None, account_rules=None):
    kwargs = dict(persist=persist, now=NOW, sources_observed=sources, account_rules=account_rules)
    if persist and tmp_path is not None:
        kwargs["store"] = ApprovalStore(tmp_path)
        kwargs["journal"] = tmp_path / "journal.jsonl"
    else:
        kwargs["journal"] = (tmp_path / "journal.jsonl") if tmp_path is not None else None
    return run_approval(items, context, **kwargs)


def test_hold_creates_no_packet():
    context = ctx(10_000, [_held("NVDA", 0.05, 10_000, Sleeve.CORE_GROWTH, 180)])
    plan = _plan("NVDA", Decision.HOLD, None, None, 180, after_pct=0.05)
    # HOLD is not a valid OrderPlan action in production; still skipped if presented.
    plan.action = Decision.HOLD
    out = _run([_req(plan, context)], context)
    assert out.packets == []
    assert out.skipped[0].reason == "NON_EXECUTABLE_ACTION"
    assert out.execution_attempted is False
    assert out.broker_orders_submitted == 0


def test_packet_fields_from_order_plan():
    nav = 10_000
    context = ctx(nav, [_held("NKE", 0.04, nav, Sleeve.OPPORTUNISTIC, 60)])
    plan = _plan("NKE", Decision.REDUCE, 200 / 60, 200.0, 60.0, after_pct=0.02)
    packet = create_approval_packet(_req(plan, context), now=NOW)
    assert packet.status == ApprovalStatus.PENDING_HUMAN_APPROVAL
    assert packet.symbol == "NKE"
    assert packet.action == Decision.REDUCE
    assert packet.current_allocation_pct == pytest.approx(4.0)
    assert packet.desired_allocation_pct == pytest.approx(2.0)
    assert packet.order_notional == pytest.approx(200.0)
    assert packet.order_quantity == pytest.approx(200 / 60)
    assert packet.current_price == pytest.approx(60.0)
    assert packet.sleeve == Sleeve.OPPORTUNISTIC
    assert "Post-selloff" in (packet.thesis_summary or "")
    assert packet.why_now
    assert packet.why_not_cash
    assert packet.why_not_spy
    assert packet.bull_case
    assert packet.base_case
    assert packet.bear_case
    assert packet.key_risks
    assert packet.invalidation_exit_policy
    assert "not a broker stop" in packet.invalidation_exit_policy
    assert packet.expected_horizon
    assert packet.portfolio_effect
    assert packet.sector_concentration_effect
    assert packet.risk_gate_verdict == "PASS"
    assert packet.order_plan_summary.order_plan_id == plan.order_plan_id
    assert packet.evidence_refs.thesis_id == "t-1"
    assert packet.evidence_refs.research_id == "r-1"
    assert packet.broker_submitted is False
    assert packet.approved_does_not_place_order is True
    md = render_packet(packet)
    assert "NKE" in md
    assert "PENDING_HUMAN_APPROVAL" in md
    assert "does not place" in md.lower() or "APPROVED does not place" in md


def test_blocked_and_fail_skipped():
    context = ctx(10_000)
    blocked = _plan("MSFT", Decision.BUY, 5, 500, 100, status=ExecutionStatus.BLOCKED_FROM_LIVE, after_pct=0.05, blocked=["MISSING_QUOTE"])
    fail_plan = _plan("AAPL", Decision.BUY, 5, 500, 100, after_pct=0.05)
    fail_req = _req(fail_plan, context, risk=_risk(verdict=GateVerdict.FAIL, rec=False), quote=_quote("AAPL", 100))
    out = _run([_req(blocked, context, quote=_quote("MSFT", 100)), fail_req], context)
    reasons = {s.symbol: s.reason for s in out.skipped}
    assert reasons["MSFT"] == "NOT_PAPER_ONLY"
    assert reasons["AAPL"] == "RISK_GATE_NOT_PERMITTED"
    assert out.packets == []


def test_enhanced_review_requirements_copied():
    context = ctx(10_000, [_held("NVDA", 0.12, 10_000, Sleeve.CORE_GROWTH, 180)])
    plan = _plan("NVDA", Decision.REDUCE, 1, 180, 180, after_pct=0.10)
    req = _req(
        plan,
        context,
        risk=_risk(reviews=["ENHANCED_CONCENTRATION_REVIEW"]),
        quote=_quote("NVDA", 180),
        thesis=_thesis(symbol="NVDA", sleeve=Sleeve.CORE_GROWTH, thesis_summary="Core compounder."),
        report=_report(symbol="NVDA"),
    )
    packet = create_approval_packet(req, now=NOW)
    assert packet.enhanced_review_requirements == ["ENHANCED_CONCENTRATION_REVIEW"]


def test_approved_does_not_place(tmp_path):
    context = ctx(10_000, [_held("NKE", 0.04, 10_000, Sleeve.OPPORTUNISTIC, 60)])
    plan = _plan("NKE", Decision.REDUCE, 200 / 60, 200.0, 60.0, after_pct=0.02)
    store = ApprovalStore(tmp_path)
    out = run_approval(
        [_req(plan, context)],
        context,
        persist=True,
        now=NOW,
        store=store,
        journal=tmp_path / "journal.jsonl",
    )
    packet = record_human_decision(
        out.packets[0],
        ApprovalStatus.APPROVED,
        note="ok to reduce on paper",
        now=NOW,
        store=store,
        persist=True,
        journal=tmp_path / "journal.jsonl",
    )
    assert packet.status == ApprovalStatus.APPROVED
    assert packet.broker_submitted is False
    assert packet.live_trade_actions_allowed is False
    assert packet.auto_execution is False
    assert packet.approved_does_not_place_order is True
    stored = store.get_packet(packet.approval_id)
    assert stored.status == ApprovalStatus.APPROVED
    assert stored.broker_submitted is False
    journal = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert "APPROVAL_APPROVED" in journal
    assert "review_equity_order" not in journal
    assert "place_equity_order" not in journal
    assert "cancel_equity_order" not in journal


def test_rejected_and_cannot_approve_after(tmp_path):
    context = ctx(10_000, [_held("NKE", 0.04, 10_000, Sleeve.OPPORTUNISTIC, 60)])
    plan = _plan("NKE", Decision.REDUCE, 200 / 60, 200.0, 60.0, after_pct=0.02)
    store = ApprovalStore(tmp_path)
    out = run_approval([_req(plan, context)], context, persist=True, now=NOW, store=store, journal=tmp_path / "j.jsonl")
    packet = record_human_decision(out.packets[0], ApprovalStatus.REJECTED, now=NOW, store=store, journal=tmp_path / "j.jsonl")
    assert packet.status == ApprovalStatus.REJECTED
    with pytest.raises(ApprovalValidationError):
        record_human_decision(packet, ApprovalStatus.APPROVED, now=NOW, persist=False, journal=None)


def test_expire_stale_quote():
    context = ctx(10_000, [_held("NKE", 0.04, 10_000, Sleeve.OPPORTUNISTIC, 60)])
    plan = _plan("NKE", Decision.REDUCE, 200 / 60, 200.0, 60.0, after_pct=0.02)
    packet = create_approval_packet(_req(plan, context), now=NOW)
    later = NOW + timedelta(seconds=301)
    stale = _quote("NKE", 60, observed_at=TS)
    updated = refresh_approval(packet, ApprovalMarketView(quote=stale), now=later, persist=False, journal=None)
    assert updated.status == ApprovalStatus.EXPIRED
    assert "STALE_QUOTE" in updated.expiry_reasons


def test_expire_stale_research_and_thesis():
    context = ctx(10_000, [_held("NKE", 0.04, 10_000, Sleeve.OPPORTUNISTIC, 60)])
    plan = _plan("NKE", Decision.REDUCE, 200 / 60, 200.0, 60.0, after_pct=0.02)
    packet = create_approval_packet(_req(plan, context), now=NOW)
    stale_research = _report(freshness=ResearchFreshness.STALE)
    updated = refresh_approval(
        packet,
        ApprovalMarketView(quote=_quote("NKE", 60), research=stale_research),
        now=NOW,
        persist=False,
        journal=None,
    )
    assert updated.status == ApprovalStatus.EXPIRED
    assert "STALE_RESEARCH" in updated.expiry_reasons

    packet2 = create_approval_packet(_req(plan, context), now=NOW)
    newer_thesis = _thesis(updated_at="2026-08-30T19:00:00+00:00")
    updated2 = refresh_approval(
        packet2,
        ApprovalMarketView(quote=_quote("NKE", 60), thesis=newer_thesis),
        now=NOW,
        persist=False,
        journal=None,
    )
    assert updated2.status == ApprovalStatus.EXPIRED
    assert "STALE_THESIS" in updated2.expiry_reasons


def test_expire_portfolio_and_risk_state():
    nav = 10_000
    context = ctx(nav, [_held("NKE", 0.04, nav, Sleeve.OPPORTUNISTIC, 60)])
    plan = _plan("NKE", Decision.REDUCE, 200 / 60, 200.0, 60.0, after_pct=0.02)
    packet = create_approval_packet(_req(plan, context), now=NOW)
    bigger = ctx(12_000, [_held("NKE", 0.04, 12_000, Sleeve.OPPORTUNISTIC, 60)])
    updated = refresh_approval(
        packet,
        ApprovalMarketView(quote=_quote("NKE", 60), context=bigger),
        now=NOW,
        persist=False,
        journal=None,
    )
    assert updated.status == ApprovalStatus.EXPIRED
    assert "PORTFOLIO_MATERIAL_CHANGE" in updated.expiry_reasons

    packet2 = create_approval_packet(_req(plan, context), now=NOW)
    halted = ctx(nav, [_held("NKE", 0.04, nav, Sleeve.OPPORTUNISTIC, 60)])
    halted.risk_state = RiskState.HALTED
    updated2 = refresh_approval(
        packet2,
        ApprovalMarketView(quote=_quote("NKE", 60), context=halted),
        now=NOW,
        persist=False,
        journal=None,
    )
    assert updated2.status == ApprovalStatus.EXPIRED
    assert "RISK_STATE_CHANGED" in updated2.expiry_reasons


def test_supersede_newer_decision(tmp_path):
    nav = 10_000
    context = ctx(nav, [_held("NKE", 0.04, nav, Sleeve.OPPORTUNISTIC, 60)])
    first = _plan("NKE", Decision.REDUCE, 200 / 60, 200.0, 60.0, after_pct=0.02, decision_id="dec-old")
    store = ApprovalStore(tmp_path)
    out = run_approval([_req(first, context)], context, persist=True, now=NOW, store=store, journal=tmp_path / "j.jsonl")
    second = _plan("NKE", Decision.SELL, 400 / 60, 400.0, 60.0, after_pct=0.0, decision_id="dec-new")
    out2 = run_approval([_req(second, context)], context, persist=True, now=NOW, store=store, journal=tmp_path / "j.jsonl")
    prior = store.get_packet(out.packets[0].approval_id)
    assert prior.status == ApprovalStatus.SUPERSEDED
    assert prior.superseded_by == out2.packets[0].approval_id
    assert out2.packets[0].status == ApprovalStatus.PENDING_HUMAN_APPROVAL
    journal = (tmp_path / "j.jsonl").read_text(encoding="utf-8")
    assert "APPROVAL_SUPERSEDED" in journal
    assert "APPROVAL_CREATED" in journal


def test_refresh_supersede_by_newer_decision_id():
    context = ctx(10_000, [_held("NKE", 0.04, 10_000, Sleeve.OPPORTUNISTIC, 60)])
    plan = _plan("NKE", Decision.REDUCE, 200 / 60, 200.0, 60.0, after_pct=0.02, decision_id="dec-old")
    packet = create_approval_packet(_req(plan, context), now=NOW)
    updated = refresh_approval(
        packet,
        ApprovalMarketView(quote=_quote("NKE", 60), newer_decision_id="dec-new"),
        now=NOW,
        persist=False,
        journal=None,
    )
    assert updated.status == ApprovalStatus.SUPERSEDED
    assert updated.superseded_by == "dec-new"


def test_persist_and_journal(tmp_path):
    context = ctx(10_000, [_held("NKE", 0.04, 10_000, Sleeve.OPPORTUNISTIC, 60)])
    plan = _plan("NKE", Decision.REDUCE, 200 / 60, 200.0, 60.0, after_pct=0.02)
    out = _run([_req(plan, context)], context, persist=True, tmp_path=tmp_path)
    stored = ApprovalStore(tmp_path).get(out.packets[0].approval_id)
    assert stored is not None
    assert stored["execution_attempted"] is False if "execution_attempted" in stored else stored["broker_submitted"] is False
    assert stored["status"] == "PENDING_HUMAN_APPROVAL"
    journal = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert "APPROVAL_CREATED" in journal
    assert "review_equity_order" not in journal
    assert "place_equity_order" not in journal
    assert "cancel_equity_order" not in journal


def test_no_execution_tools_reachable():
    hits = inspect_approval_module_for_forbidden_tools()
    assert hits == []
    for tool in ("review_equity_order", "place_equity_order", "cancel_equity_order"):
        assert tool in APPROVAL_FORBIDDEN_TOOLS
    with pytest.raises(ApprovalSafetyError):
        from agentic_portfolio.approval.safety import assert_no_forbidden_tools

        assert_no_forbidden_tools(["place_equity_order"])
    context = ctx(10_000, [_held("NKE", 0.04, 10_000, Sleeve.OPPORTUNISTIC, 60)])
    plan = _plan("NKE", Decision.REDUCE, 200 / 60, 200.0, 60.0, after_pct=0.02)
    with pytest.raises(ApprovalSafetyError):
        _run([_req(plan, context)], context, sources=["place_equity_order"])


def test_live_flags_remain_false():
    context = ctx(10_000, [_held("NKE", 0.04, 10_000, Sleeve.OPPORTUNISTIC, 60)])
    plan = _plan("NKE", Decision.REDUCE, 200 / 60, 200.0, 60.0, after_pct=0.02)
    rules = {"execution": {"live_trade_actions_allowed": True, "auto_execution": False}}
    with pytest.raises(ApprovalSafetyError):
        _run([_req(plan, context)], context, account_rules=rules)


def test_paper_monitor_mapping_hold_creates_no_packet():
    nav = 10_000
    positions = [
        _held("NVDA", 0.05, nav, Sleeve.CORE_GROWTH, 180),
        _held("NKE", 0.04, nav, Sleeve.OPPORTUNISTIC, 60),
        _held("ESTC", 0.02, nav, Sleeve.TACTICAL, 70),
        _held("IONQ", 0.01, nav, Sleeve.SPECULATIVE, 8),
    ]
    context = ctx(nav, positions)
    rows = [
        ("NVDA", Decision.HOLD, None, None, 180, None),
        ("NKE", Decision.REDUCE, 200 / 60, 200.0, 60.0, 0.02),
        ("ESTC", Decision.SELL, 200 / 70, 200.0, 70.0, 0.0),
        ("IONQ", Decision.SELL, 12.5, 100.0, 8.0, 0.0),
    ]
    items = []
    for symbol, decision, qty, notional, price, after in rows:
        plan = _plan(symbol, decision, qty, notional, price, after_pct=after)
        if decision == Decision.HOLD:
            plan.action = Decision.HOLD
        items.append(_req(plan, context, quote=_quote(symbol, price)))
    out = _run(items, context)
    skipped = {s.symbol: s.reason for s in out.skipped}
    pending = {p.symbol: p for p in out.pending}
    assert skipped["NVDA"] == "NON_EXECUTABLE_ACTION"
    assert "NVDA" not in pending
    assert pending["NKE"].action == Decision.REDUCE
    assert pending["ESTC"].action == Decision.SELL
    assert pending["IONQ"].action == Decision.SELL
    assert all(p.broker_submitted is False for p in out.packets)
    assert out.execution_attempted is False
    assert out.live_execution_attempted is False
