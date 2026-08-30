"""Robinhood review-only tests. Preflight via review_equity_order; never places."""

from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pytest

from agentic_portfolio.approval.engine import create_approval_packet, record_human_decision
from agentic_portfolio.approval.types import ApprovalRequest, ApprovalStatus
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
from agentic_portfolio.review.engine import run_review
from agentic_portfolio.review.report import render_result
from agentic_portfolio.review.safety import (
    REVIEW_ALLOWED_TOOLS,
    REVIEW_FORBIDDEN_TOOLS,
    ReviewSafetyError,
    inspect_review_module_for_forbidden_tools,
)
from agentic_portfolio.review.store import ReviewStore
from agentic_portfolio.review.types import ReviewRequest, ReviewStatus, StaticReviewClient
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
        sector="CONSUMER_STAPLES",
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


def _req(plan, context, action=None, risk=None, quote=None, thesis=None, decision=None, report=None):
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
        investment_thesis_review_complete=True,
        risk_review_complete=True,
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
            rationale="Reduce.",
            thesis_id="t-1",
            research_id="r-1",
        ),
        comparison=PortfolioComparison(ranking=[plan.symbol, "CASH"], vs_cash="Moving toward cash.", vs_spy="Not a SPY swap."),
        report=report if report is not None else _report(symbol=plan.symbol),
        monitoring={"state": "THESIS_WEAKENED"},
        quote=quote if quote is not None else _quote(plan.symbol, plan.estimated_price),
        monitoring_run_id="mon-1",
    )


def _approved(plan=None, context=None, **kwargs):
    context = context or ctx(10_000, [_held("NKE", 0.04, 10_000, Sleeve.OPPORTUNISTIC, 60)])
    plan = plan or _plan("NKE", Decision.REDUCE, 200 / 60, 200.0, 60.0, after_pct=0.02)
    packet = create_approval_packet(_req(plan, context, **kwargs), now=NOW)
    packet = record_human_decision(packet, ApprovalStatus.APPROVED, now=NOW, persist=False, journal=None)
    quote = kwargs.get("quote") or _quote(plan.symbol, plan.estimated_price)
    action = kwargs.get("action") or _req(plan, context, **kwargs).action
    return ReviewRequest(
        packet=packet,
        plan=plan,
        action=action,
        context=context,
        quote=quote,
        thesis=kwargs.get("thesis") or _thesis(symbol=plan.symbol),
        research=kwargs.get("report") or _report(symbol=plan.symbol),
    )


def _ok_client(**extra):
    body = {
        "order": {"symbol": "NKE", "side": "sell", "type": "market", "quantity": "3.333333"},
        "estimated_proceeds": 200.0,
        "warnings": [],
        "errors": [],
    }
    body.update(extra)
    return StaticReviewClient(response=body)


def _run(req, client=None, *, persist=False, tmp_path=None, sources=None, account_rules=None):
    kwargs = dict(persist=persist, now=NOW, sources_observed=sources, account_rules=account_rules)
    if persist and tmp_path is not None:
        kwargs["store"] = ReviewStore(tmp_path)
        kwargs["journal"] = tmp_path / "journal.jsonl"
    else:
        kwargs["journal"] = (tmp_path / "journal.jsonl") if tmp_path is not None else None
    return run_review(req, client or _ok_client(), **kwargs)


def test_approved_review_accepted_does_not_place(tmp_path):
    req = _approved()
    client = _ok_client()
    out = _run(req, client, persist=True, tmp_path=tmp_path)
    assert out.status == ReviewStatus.REVIEW_ACCEPTED
    assert out.order_placed is False
    assert out.broker_submitted is False
    assert out.execution_attempted is False
    assert out.review_accepted_does_not_execute is True
    assert out.live_trade_actions_allowed is False
    assert out.auto_execution is False
    assert len(client.calls) == 1
    assert client.calls[0]["symbol"] == "NKE"
    assert client.calls[0]["side"] == "sell"
    assert client.calls[0]["type"] == "market"
    assert "quantity" in client.calls[0]
    stored = ReviewStore(tmp_path).get_result(out.review_id)
    assert stored.status == ReviewStatus.REVIEW_ACCEPTED
    assert stored.order_placed is False
    journal = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert "REVIEW_RECORDED" in journal
    assert "place_equity_order" not in journal
    assert "cancel_equity_order" not in journal
    text = render_result(out)
    assert "does not place" in text.lower() or "does not execute" in text.lower()


def test_warnings_are_review_ready_not_execution():
    req = _approved()
    client = _ok_client(warnings=["outside regular hours queues to next open"])
    out = _run(req, client)
    assert out.status == ReviewStatus.REVIEW_READY
    assert out.order_placed is False
    assert out.warnings


def test_pending_rejected_expired_superseded_fail_closed():
    req = _approved()
    req.packet.status = ApprovalStatus.PENDING_HUMAN_APPROVAL
    client = _ok_client()
    out = _run(req, client)
    assert out.status == ReviewStatus.REVIEW_FAILED
    assert "APPROVAL_NOT_APPROVED" in out.fail_reasons
    assert client.calls == []

    req = _approved()
    req.packet.status = ApprovalStatus.REJECTED
    out = _run(req, client)
    assert out.status == ReviewStatus.REVIEW_FAILED
    assert "APPROVAL_REJECTED" in out.fail_reasons
    assert client.calls == []

    req = _approved()
    req.packet.status = ApprovalStatus.EXPIRED
    out = _run(req, client)
    assert out.status == ReviewStatus.REVIEW_EXPIRED
    assert "APPROVAL_EXPIRED" in out.fail_reasons
    assert client.calls == []

    req = _approved()
    req.packet.status = ApprovalStatus.SUPERSEDED
    out = _run(req, client)
    assert out.status == ReviewStatus.REVIEW_EXPIRED
    assert "APPROVAL_SUPERSEDED" in out.fail_reasons
    assert client.calls == []


def test_stale_quote_and_material_quote_change_fail_closed():
    req = _approved()
    client = _ok_client()
    later = NOW + timedelta(seconds=301)
    out = run_review(req, client, persist=False, now=later, journal=None)
    assert out.status == ReviewStatus.REVIEW_EXPIRED
    assert "STALE_QUOTE" in out.fail_reasons
    assert client.calls == []

    req = _approved()
    req.quote = _quote("NKE", 80.0)
    out = _run(req, client)
    assert out.status == ReviewStatus.REVIEW_FAILED
    assert "QUOTE_MATERIAL_CHANGE" in out.fail_reasons
    assert client.calls == []


def test_stale_research_thesis_portfolio_risk_fail_closed():
    client = _ok_client()
    req = _approved()
    req.research = _report(freshness=ResearchFreshness.STALE)
    out = _run(req, client)
    assert out.status == ReviewStatus.REVIEW_EXPIRED
    assert "STALE_RESEARCH" in out.fail_reasons
    assert client.calls == []

    req = _approved()
    req.thesis = _thesis(updated_at="2026-08-30T19:00:00+00:00")
    out = _run(req, client)
    assert out.status == ReviewStatus.REVIEW_EXPIRED
    assert "STALE_THESIS" in out.fail_reasons
    assert client.calls == []

    bigger = ctx(12_000, [_held("NKE", 0.04, 12_000, Sleeve.OPPORTUNISTIC, 60)])
    req = _approved()
    req.context = bigger
    out = _run(req, client)
    assert out.status == ReviewStatus.REVIEW_FAILED
    assert "PORTFOLIO_MATERIAL_CHANGE" in out.fail_reasons
    assert client.calls == []

    halted = ctx(10_000, [_held("NKE", 0.04, 10_000, Sleeve.OPPORTUNISTIC, 60)])
    halted.risk_state = RiskState.HALTED
    req = _approved()
    req.context = halted
    out = _run(req, client)
    assert out.status == ReviewStatus.REVIEW_FAILED
    assert "RISK_STATE_CHANGED" in out.fail_reasons
    assert client.calls == []


def test_risk_gate_recheck_blocks_buy():
    nav = 10_000
    context = ctx(nav, [])
    plan = _plan("NKE", Decision.BUY, 2000 / 60, 2000.0, 60.0, after_pct=0.20)
    action = act(
        symbol="NKE",
        sleeve=Sleeve.OPPORTUNISTIC,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        decision=Decision.BUY,
        proposed_notional=2000.0,
        expected_resulting_position_pct=0.20,
        current_price=60.0,
        sector="CONSUMER_STAPLES",
        thesis_id="t-1",
        investment_thesis_review_complete=True,
        risk_review_complete=True,
    )
    packet = create_approval_packet(_req(plan, context, action=action), now=NOW)
    packet = record_human_decision(packet, ApprovalStatus.APPROVED, now=NOW, persist=False, journal=None)
    req = ReviewRequest(
        packet=packet,
        plan=plan,
        action=action,
        context=context,
        quote=_quote("NKE", 60),
        thesis=_thesis(),
        research=_report(),
    )
    client = StaticReviewClient(response={"order": {"symbol": "NKE", "side": "buy", "type": "market", "quantity": "33.333333"}})
    out = _run(req, client)
    assert out.status == ReviewStatus.REVIEW_FAILED
    assert "RISK_GATE_NOT_PERMITTED" in out.fail_reasons
    assert client.calls == []


def test_robinhood_reject_and_call_failure():
    req = _approved()
    client = _ok_client(errors=["insufficient shares"])
    out = _run(req, client)
    assert out.status == ReviewStatus.REVIEW_REJECTED
    assert out.order_placed is False
    assert "insufficient shares" in out.errors
    assert len(client.calls) == 1

    req = _approved()
    boom = StaticReviewClient(error=RuntimeError("mcp down"))
    out = _run(req, boom)
    assert out.status == ReviewStatus.REVIEW_FAILED
    assert "REVIEW_CALL_FAILED" in out.fail_reasons
    assert out.order_placed is False


def test_review_differs_from_order_plan():
    req = _approved()
    client = StaticReviewClient(
        response={
            "order": {"symbol": "AAPL", "side": "sell", "type": "market", "quantity": "3.333333"},
            "estimated_proceeds": 200.0,
        }
    )
    out = _run(req, client)
    assert out.status == ReviewStatus.REVIEW_FAILED
    assert "REVIEW_DIFFERS_FROM_ORDER_PLAN" in out.fail_reasons
    assert out.order_placed is False


def test_no_place_or_cancel_reachable():
    hits = inspect_review_module_for_forbidden_tools()
    assert hits == []
    assert "review_equity_order" in REVIEW_ALLOWED_TOOLS
    assert "review_equity_order" not in REVIEW_FORBIDDEN_TOOLS
    for tool in ("place_equity_order", "cancel_equity_order"):
        assert tool in REVIEW_FORBIDDEN_TOOLS
    with pytest.raises(ReviewSafetyError):
        from agentic_portfolio.review.safety import assert_no_forbidden_tools

        assert_no_forbidden_tools(["place_equity_order"])
    req = _approved()
    with pytest.raises(ReviewSafetyError):
        _run(req, sources=["cancel_equity_order"])


def test_live_flags_remain_false():
    req = _approved()
    rules = {"account": {"account_number": "549688554"}, "execution": {"live_trade_actions_allowed": True, "auto_execution": False}}
    with pytest.raises(ReviewSafetyError):
        _run(req, account_rules=rules)
