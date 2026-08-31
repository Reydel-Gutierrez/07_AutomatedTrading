"""Execution Controller tests. Paper OrderPlan only; no broker calls."""

from datetime import datetime, timezone, timedelta

import pytest

from agentic_portfolio.execution.engine import plan_order, run_execution
from agentic_portfolio.execution.safety import (
    EXECUTION_FORBIDDEN_TOOLS,
    ExecutionSafetyError,
    inspect_execution_module_for_forbidden_tools,
)
from agentic_portfolio.execution.store import OrderPlanStore
from agentic_portfolio.execution.types import (
    ExecutionStatus,
    OrderSide,
    OrderType,
    QuoteSnapshot,
    TimeInForce,
    TradabilitySnapshot,
)
from agentic_portfolio.risk_gate import evaluate
from agentic_portfolio.schemas import (
    ClassificationStatus,
    Decision,
    GateVerdict,
    OpenOrder,
    Position,
    RiskGateResult,
    SecurityClass,
    Sleeve,
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


def _tradable(symbol, ok=True):
    return TradabilitySnapshot(symbol=symbol, tradable=ok, state="active", observed_at=TS, source="paper")


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


def _run(action, context, quote, trad, *, persist=False, tmp_path=None, open_orders=None, account_rules=None, sources=None):
    risk = evaluate(context, action)
    kwargs = dict(
        open_orders=open_orders,
        persist=persist,
        now=NOW,
        journal=(tmp_path / "journal.jsonl") if tmp_path is not None else None,
        account_rules=account_rules,
        sources_observed=sources,
        source_decision_id="test-decision",
    )
    if persist and tmp_path is not None:
        kwargs["store"] = OrderPlanStore(tmp_path)
    return run_execution(
        [(action, risk)],
        context,
        {action.symbol: quote},
        {action.symbol: trad},
        **kwargs,
    )


def test_hold_watch_reject_no_action_create_no_plan():
    context = ctx(10_000)
    quote = _quote("NVDA", 100)
    trad = _tradable("NVDA")
    for decision in (Decision.HOLD, Decision.WATCH, Decision.REJECT, Decision.NO_ACTION):
        action = act(
            symbol="NVDA",
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            decision=decision,
            proposed_notional=None,
            expected_resulting_position_pct=0.0,
        )
        out = _run(action, context, quote, trad)
        assert out.plans == []
        assert len(out.skipped) == 1
        assert out.skipped[0].reason == "NON_EXECUTABLE_ACTION"
        assert out.execution_attempted is False
        assert out.broker_orders_submitted == 0
        assert out.broker_stop_orders_created == 0


def test_buy_reduce_sell_paper_plans():
    nav = 10_000
    buy = act(
        symbol="MSFT",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        decision=Decision.BUY,
        proposed_notional=500.0,
        expected_resulting_position_pct=0.05,
        sector="INFORMATION_TECHNOLOGY",
        thesis_id="thesis-msft",
    )
    buy_out = _run(buy, ctx(nav), _quote("MSFT", 100), _tradable("MSFT"))
    plan = buy_out.paper_plans[0]
    assert plan.action == Decision.BUY
    assert plan.order_side == OrderSide.BUY
    assert plan.order_type == OrderType.MARKET
    assert plan.time_in_force == TimeInForce.GFD
    assert plan.quantity == pytest.approx(5.0)
    assert plan.notional == pytest.approx(500.0)
    assert plan.estimated_price == pytest.approx(100.0)
    assert plan.estimated_position_pct_after == pytest.approx(0.05)
    assert plan.execution_status == ExecutionStatus.PAPER_ONLY
    assert plan.live_execution_blocked is True
    assert plan.stop_orders_created == 0
    assert plan.broker_submitted is False
    assert plan.thesis_id == "thesis-msft"
    assert plan.source_decision_id == "test-decision"
    assert plan.risk_evaluation_id
    assert plan.slippage_check.ok is True
    assert plan.liquidity_check.ok is True

    held = [_held("NVDA", 0.05, nav, Sleeve.CORE_GROWTH, 100)]
    add_ctx = ctx(nav, held)
    reduce_action = act(
        symbol="NVDA",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        decision=Decision.REDUCE,
        proposed_notional=200.0,
        expected_resulting_position_pct=0.03,
        sector="INFORMATION_TECHNOLOGY",
        explicitly_risk_reducing=True,
    )
    red = _run(reduce_action, add_ctx, _quote("NVDA", 100), _tradable("NVDA"))
    assert red.paper_plans[0].action == Decision.REDUCE
    assert red.paper_plans[0].order_side == OrderSide.SELL_TO_CLOSE
    assert red.paper_plans[0].quantity == pytest.approx(2.0)
    assert red.paper_plans[0].estimated_position_pct_after == pytest.approx(0.03)

    sell = act(
        symbol="NVDA",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        decision=Decision.SELL,
        proposed_notional=500.0,
        expected_resulting_position_pct=0.0,
        sector="INFORMATION_TECHNOLOGY",
        explicitly_risk_reducing=True,
    )
    sold = _run(sell, add_ctx, _quote("NVDA", 100), _tradable("NVDA"))
    assert sold.paper_plans[0].action == Decision.SELL
    assert sold.paper_plans[0].order_side == OrderSide.SELL_TO_CLOSE
    assert sold.paper_plans[0].estimated_position_quantity_after == pytest.approx(0.0)


def test_add_paper_plan():
    nav = 10_000
    action = act(
        symbol="AMD",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        decision=Decision.ADD,
        proposed_notional=200.0,
        expected_resulting_position_pct=0.07,
        sector="INFORMATION_TECHNOLOGY",
        investment_thesis_review_complete=True,
        risk_review_complete=True,
        thesis_id="thesis-amd",
    )
    context = ctx(nav, [_held("AMD", 0.05, nav, Sleeve.CORE_GROWTH, 100)])
    risk = RiskGateResult(
        verdict=GateVerdict.PASS,
        execution_permitted=False,
        recommendation_permitted=True,
        reasons=[],
        required_reviews=[],
        applicable_position_ceiling_pct=20.0,
        snapshot_id="snap-add",
    )
    out = run_execution(
        [(action, risk)],
        context,
        {"AMD": _quote("AMD", 100)},
        {"AMD": _tradable("AMD")},
        persist=False,
        now=NOW,
        journal=None,
        source_decision_id="test-add",
    )
    plan = out.paper_plans[0]
    assert plan.action == Decision.ADD
    assert plan.order_side == OrderSide.BUY
    assert plan.quantity == pytest.approx(2.0)
    assert plan.estimated_position_pct_after == pytest.approx(0.07)
    assert plan.execution_status == ExecutionStatus.PAPER_ONLY


def test_risk_gate_must_permit():
    nav = 10_000
    action = act(
        symbol="NVDA",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        proposed_notional=3000.0,
        expected_resulting_position_pct=0.30,
        sector="INFORMATION_TECHNOLOGY",
    )
    out = _run(action, ctx(nav), _quote("NVDA", 100), _tradable("NVDA"))
    assert out.paper_plans == []
    assert out.blocked_plans[0].execution_status == ExecutionStatus.BLOCKED_FROM_LIVE
    assert "RISK_GATE_NOT_PERMITTED" in out.blocked_plans[0].blocked_reasons


def test_live_flags_must_remain_false():
    action = act(
        symbol="MSFT",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        proposed_notional=500.0,
        expected_resulting_position_pct=0.05,
        sector="INFORMATION_TECHNOLOGY",
    )
    rules = {
        "execution": {"live_trade_actions_allowed": True, "auto_execution": False},
        "account": {"account_number": "549688554"},
    }
    out = _run(action, ctx(10_000), _quote("MSFT", 100), _tradable("MSFT"), account_rules=rules)
    assert out.blocked_plans[0].execution_status == ExecutionStatus.BLOCKED_FROM_LIVE
    assert "LIVE_TRADE_ACTIONS_MUST_REMAIN_FALSE" in out.blocked_plans[0].blocked_reasons
    assert out.execution_attempted is False

    rules2 = {
        "execution": {"live_trade_actions_allowed": False, "auto_execution": True},
        "account": {"account_number": "549688554"},
    }
    out2 = _run(action, ctx(10_000), _quote("MSFT", 100), _tradable("MSFT"), account_rules=rules2)
    assert "AUTO_EXECUTION_MUST_REMAIN_FALSE" in out2.blocked_plans[0].blocked_reasons


def test_duplicate_conflicting_order_blocks():
    action = act(
        symbol="MSFT",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        proposed_notional=500.0,
        expected_resulting_position_pct=0.05,
        sector="INFORMATION_TECHNOLOGY",
    )
    orders = [OpenOrder(order_id="open-1", symbol="MSFT", side="buy", state="queued", notional=100.0)]
    out = _run(action, ctx(10_000), _quote("MSFT", 100), _tradable("MSFT"), open_orders=orders)
    assert "DUPLICATE_CONFLICTING_ORDER" in out.blocked_plans[0].blocked_reasons


def test_missing_notional_and_sell_exceeds_held():
    missing = act(
        symbol="MSFT",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        proposed_notional=None,
        expected_resulting_position_pct=0.05,
        sector="INFORMATION_TECHNOLOGY",
    )
    out = _run(missing, ctx(10_000), _quote("MSFT", 100), _tradable("MSFT"))
    assert "MISSING_NOTIONAL" in out.blocked_plans[0].blocked_reasons

    nav = 10_000
    context = ctx(nav, [_held("NVDA", 0.02, nav, Sleeve.CORE_GROWTH, 100)])
    sell = act(
        symbol="NVDA",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        decision=Decision.SELL,
        proposed_notional=500.0,
        expected_resulting_position_pct=0.0,
        sector="INFORMATION_TECHNOLOGY",
        explicitly_risk_reducing=True,
    )
    sold = _run(sell, context, _quote("NVDA", 100), _tradable("NVDA"))
    assert "SELL_EXCEEDS_HELD_QUANTITY" in sold.blocked_plans[0].blocked_reasons


def test_buy_cannot_exceed_cash_or_buying_power():
    action = act(
        symbol="MSFT",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        proposed_notional=400.0,
        expected_resulting_position_pct=0.04,
        sector="INFORMATION_TECHNOLOGY",
    )
    context = ctx(10_000, cash=100.0, buying_power=100.0)
    out = _run(action, context, _quote("MSFT", 100), _tradable("MSFT"))
    assert "BUY_EXCEEDS_AVAILABLE_CASH" in out.blocked_plans[0].blocked_reasons


def test_symbol_must_be_tradable_and_stale_quote_blocks():
    action = act(
        symbol="MSFT",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        proposed_notional=500.0,
        expected_resulting_position_pct=0.05,
        sector="INFORMATION_TECHNOLOGY",
    )
    untradable = _run(action, ctx(10_000), _quote("MSFT", 100), _tradable("MSFT", ok=False))
    assert "SYMBOL_NOT_TRADABLE" in untradable.blocked_plans[0].blocked_reasons

    stale = _run(action, ctx(10_000), _quote("MSFT", 100, stale=True), _tradable("MSFT"))
    assert "STALE_QUOTE" in stale.blocked_plans[0].blocked_reasons

    old = (NOW - timedelta(hours=2)).isoformat()
    aged = _run(action, ctx(10_000), _quote("MSFT", 100, observed_at=old), _tradable("MSFT"))
    assert "STALE_QUOTE" in aged.blocked_plans[0].blocked_reasons


def test_malformed_plan_fails_closed_and_never_invents_stops():
    action = act(
        symbol="MSFT",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        proposed_notional=500.0,
        expected_resulting_position_pct=0.05,
        sector="INFORMATION_TECHNOLOGY",
    )
    context = ctx(10_000)
    risk = evaluate(context, action)
    plan = plan_order(
        action,
        risk,
        context,
        quote=_quote("MSFT", 100),
        tradability=_tradable("MSFT"),
        now=NOW,
    )
    assert plan.stop_orders_created == 0
    assert plan.execution_status == ExecutionStatus.PAPER_ONLY
    plan.stop_orders_created = 1
    from agentic_portfolio.execution.validate import plan_consistency_codes

    codes = plan_consistency_codes(plan, action, context, abs_tol=0.01, rel_tol=1e-6, pct_tol=0.001)
    assert "STOP_ORDER_NOT_ALLOWED" in codes


def test_no_execution_tools_reachable():
    hits = inspect_execution_module_for_forbidden_tools()
    assert hits == []
    for tool in ("review_equity_order", "place_equity_order", "cancel_equity_order"):
        assert tool in EXECUTION_FORBIDDEN_TOOLS
    with pytest.raises(ExecutionSafetyError):
        from agentic_portfolio.execution.safety import assert_no_forbidden_tools

        assert_no_forbidden_tools(["place_equity_order"])
    action = act(
        symbol="MSFT",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        proposed_notional=500.0,
        expected_resulting_position_pct=0.05,
        sector="INFORMATION_TECHNOLOGY",
    )
    with pytest.raises(ExecutionSafetyError):
        _run(action, ctx(10_000), _quote("MSFT", 100), _tradable("MSFT"), sources=["place_equity_order"])


def test_persist_and_journal(tmp_path):
    action = act(
        symbol="MSFT",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        proposed_notional=500.0,
        expected_resulting_position_pct=0.05,
        sector="INFORMATION_TECHNOLOGY",
        thesis_id="t-msft",
    )
    out = _run(action, ctx(10_000), _quote("MSFT", 100), _tradable("MSFT"), persist=True, tmp_path=tmp_path)
    stored = OrderPlanStore(tmp_path).get(out.run_id)
    assert stored is not None
    assert stored["execution_attempted"] is False
    assert stored["broker_stop_orders_created"] == 0
    journal = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert "ORDER_PLAN_CREATED" in journal
    assert "review_equity_order" not in journal
    assert "place_equity_order" not in journal
    assert "cancel_equity_order" not in journal


def test_paper_monitor_mapping_hold_creates_no_order(tmp_path):
    nav = 10_000
    positions = [
        _held("NVDA", 0.05, nav, Sleeve.CORE_GROWTH, 180),
        _held("NKE", 0.04, nav, Sleeve.OPPORTUNISTIC, 60),
        _held("ESTC", 0.02, nav, Sleeve.TACTICAL, 70),
        _held("IONQ", 0.01, nav, Sleeve.SPECULATIVE, 8),
    ]
    context = ctx(nav, positions)
    rows = [
        ("NVDA", Decision.HOLD, None, 0.05, 180, Sleeve.CORE_GROWTH),
        ("NKE", Decision.REDUCE, 200.0, 0.02, 60, Sleeve.OPPORTUNISTIC),
        ("ESTC", Decision.SELL, 200.0, 0.0, 70, Sleeve.TACTICAL),
        ("IONQ", Decision.SELL, 100.0, 0.0, 8, Sleeve.SPECULATIVE),
    ]
    items = []
    quotes = {}
    trad = {}
    for symbol, decision, notional, resulting, price, sleeve in rows:
        action = act(
            symbol=symbol,
            sleeve=sleeve,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            decision=decision,
            proposed_notional=notional,
            expected_resulting_position_pct=resulting,
            current_price=price,
            explicitly_risk_reducing=decision in {Decision.SELL, Decision.REDUCE},
        )
        items.append((action, evaluate(context, action)))
        quotes[symbol] = _quote(symbol, price)
        trad[symbol] = _tradable(symbol)
    out = run_execution(
        items,
        context,
        quotes,
        trad,
        persist=True,
        now=NOW,
        store=OrderPlanStore(tmp_path),
        journal=tmp_path / "journal.jsonl",
        source_decision_id="paper-monitor",
    )
    skipped = {s.symbol: s.reason for s in out.skipped}
    paper = {p.symbol: p for p in out.paper_plans}
    assert skipped["NVDA"] == "NON_EXECUTABLE_ACTION"
    assert "NVDA" not in paper
    assert paper["NKE"].action == Decision.REDUCE
    assert paper["NKE"].notional == pytest.approx(200.0)
    assert paper["NKE"].execution_status == ExecutionStatus.PAPER_ONLY
    assert paper["ESTC"].action == Decision.SELL
    assert paper["ESTC"].notional == pytest.approx(200.0)
    assert paper["IONQ"].action == Decision.SELL
    assert paper["IONQ"].notional == pytest.approx(100.0)
    assert all(p.live_execution_blocked for p in out.plans)
    assert all(p.stop_orders_created == 0 for p in out.plans)
    assert out.execution_attempted is False
    assert out.broker_orders_submitted == 0


def test_fail_closed_constructed_risk_result():
    action = act(
        symbol="MSFT",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        proposed_notional=500.0,
        expected_resulting_position_pct=0.05,
        sector="INFORMATION_TECHNOLOGY",
    )
    context = ctx(10_000)
    risk = RiskGateResult(
        verdict=GateVerdict.FAIL,
        execution_permitted=False,
        recommendation_permitted=False,
        reasons=[],
        required_reviews=[],
        applicable_position_ceiling_pct=20.0,
        snapshot_id="snap-fail",
    )
    out = run_execution(
        [(action, risk)],
        context,
        {"MSFT": _quote("MSFT", 100)},
        {"MSFT": _tradable("MSFT")},
        persist=False,
        now=NOW,
        journal=None,
    )
    assert out.blocked_plans[0].risk_evaluation_id == "snap-fail"
    assert "RISK_GATE_NOT_PERMITTED" in out.blocked_plans[0].blocked_reasons


def test_off_hours_spread_does_not_satisfy_execution_liquidity():
    action = act(
        symbol="QUAL",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        proposed_notional=500.0,
        expected_resulting_position_pct=0.05,
        sector="INFORMATION_TECHNOLOGY",
    )
    sunday = datetime(2026, 8, 31, 1, 6, tzinfo=timezone.utc)
    quote = QuoteSnapshot(
        symbol="QUAL",
        last_price=223.61,
        bid=222.24,
        ask=226.57,
        spread_pct=0.019295470243532828,
        observed_at=sunday.isoformat(),
        bid_as_of="2026-08-31T00:03:23.00981Z",
        ask_as_of="2026-08-31T00:03:23.00981Z",
        source="mcp",
    )
    # Width is under max_spread_pct (0.02); session must still fail.
    assert quote.spread_pct < 0.02
    out = run_execution(
        [(action, evaluate(ctx(10_000), action))],
        ctx(10_000),
        {"QUAL": quote},
        {"QUAL": _tradable("QUAL")},
        persist=False,
        now=sunday,
        journal=None,
    )
    plan = out.blocked_plans[0]
    assert plan.liquidity_check.ok is False
    assert "SPREAD_NOT_REGULAR_SESSION" in plan.liquidity_check.codes
    assert "LIQUIDITY_CHECK_FAILED" in plan.blocked_reasons
    assert "SLIPPAGE_CHECK_FAILED" in plan.blocked_reasons
    assert plan.slippage_check.ok is False


def test_open_market_execution_requires_fresh_regular_session_bid_ask():
    action = act(
        symbol="MSFT",
        sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        proposed_notional=500.0,
        expected_resulting_position_pct=0.05,
        sector="INFORMATION_TECHNOLOGY",
    )
    friday_open = datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc)
    rth = QuoteSnapshot(
        symbol="MSFT",
        last_price=100.0,
        bid=99.95,
        ask=100.05,
        spread_pct=0.001,
        observed_at=friday_open.isoformat(),
        bid_as_of="2026-08-28T17:59:00+00:00",
        ask_as_of="2026-08-28T17:59:00+00:00",
        source="mcp",
    )
    ok = run_execution(
        [(action, evaluate(ctx(10_000), action))],
        ctx(10_000),
        {"MSFT": rth},
        {"MSFT": _tradable("MSFT")},
        persist=False,
        now=friday_open,
        journal=None,
    )
    assert ok.paper_plans[0].liquidity_check.ok is True

    premarket = QuoteSnapshot(
        symbol="MSFT",
        last_price=100.0,
        bid=99.95,
        ask=100.05,
        spread_pct=0.001,
        observed_at=friday_open.isoformat(),
        bid_as_of="2026-08-28T12:00:00+00:00",
        ask_as_of="2026-08-28T12:00:00+00:00",
        source="mcp",
    )
    blocked = run_execution(
        [(action, evaluate(ctx(10_000), action))],
        ctx(10_000),
        {"MSFT": premarket},
        {"MSFT": _tradable("MSFT")},
        persist=False,
        now=friday_open,
        journal=None,
    )
    assert blocked.blocked_plans[0].liquidity_check.ok is False
    assert "SPREAD_NOT_REGULAR_SESSION" in blocked.blocked_plans[0].liquidity_check.codes
