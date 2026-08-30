"""Paper fill + blotter reconciliation tests. No broker calls."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agentic_portfolio.decision.reasoner import ScriptedDecisionReasoner
from agentic_portfolio.execution.types import (
    ExecutionStatus,
    LiquidityCheck,
    OrderPlan,
    OrderSide,
    OrderType,
    QuoteSnapshot,
    SkippedAction,
    SlippageCheck,
    TimeInForce,
)
from agentic_portfolio.monitoring.engine import run_position_monitor
from agentic_portfolio.monitoring.reasoner import ScriptedMonitoringReasoner
from agentic_portfolio.monitoring.types import PositionObservation
from agentic_portfolio.paper_fill.engine import run_paper_fill
from agentic_portfolio.paper_fill.safety import (
    PAPER_FILL_FORBIDDEN_TOOLS,
    PaperFillSafetyError,
    inspect_paper_fill_module_for_forbidden_tools,
    live_state_paths,
)
from agentic_portfolio.paper_fill.store import PaperFillStore
from agentic_portfolio.paper_fill.types import FillStatus, order_plan_from_dict
from agentic_portfolio.schemas import (
    ClassificationStatus,
    Decision,
    Position,
    SecurityClass,
    Sleeve,
    SleeveAssignmentStatus,
    ThesisStatus,
)
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry
from tests.conftest import ctx

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


def _held(symbol, pct, nav, sleeve, price, *, thesis_id=None, average_cost=None):
    qty = (pct * nav) / price
    cost = average_cost if average_cost is not None else price
    return Position(
        symbol=symbol,
        market_value=pct * nav,
        quantity=qty,
        average_cost=cost,
        current_price=price,
        sleeve=sleeve,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        classification_status=ClassificationStatus.VALIDATED,
        unrealized_pnl=(price - cost) * qty,
        thesis_id=thesis_id,
    )


def _plan(
    symbol,
    action,
    quantity,
    notional,
    price,
    *,
    order_plan_id=None,
    thesis_id=None,
    status=ExecutionStatus.PAPER_ONLY,
    after_qty=None,
    after_mv=None,
    after_pct=None,
    source_decision_id="test-decision",
):
    return OrderPlan(
        order_plan_id=order_plan_id or str(uuid4()),
        symbol=symbol,
        action=action,
        quantity=quantity,
        notional=notional,
        estimated_price=price,
        estimated_position_quantity_after=after_qty,
        estimated_position_notional_after=after_mv,
        estimated_position_pct_after=after_pct,
        order_side=OrderSide.BUY if action in {Decision.BUY, Decision.ADD} else OrderSide.SELL_TO_CLOSE,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GFD,
        slippage_check=SlippageCheck(
            ok=True, estimated_slippage_pct=0.0005, max_slippage_pct=0.01, spread_pct=0.001, codes=[]
        ),
        liquidity_check=LiquidityCheck(ok=True, codes=[]),
        source_decision_id=source_decision_id,
        thesis_id=thesis_id,
        risk_evaluation_id="risk-1",
        execution_status=status,
        live_execution_blocked=True,
        blocked_reasons=[],
        created_at=TS,
        stop_orders_created=0,
        broker_submitted=False,
        live_trade_actions_allowed=False,
        auto_execution=False,
    )


def _fill(plans, context, quotes, *, persist=False, tmp_path=None, **kwargs):
    if persist and tmp_path is not None:
        kwargs.setdefault("store", PaperFillStore(tmp_path))
        kwargs.setdefault("journal", tmp_path / "journal.jsonl")
    return run_paper_fill(
        plans,
        context,
        quotes,
        persist=persist,
        now=NOW,
        journal=kwargs.pop("journal", (tmp_path / "journal.jsonl") if tmp_path is not None else None),
        **kwargs,
    )


def test_buy_opens_position_and_may_activate_paper_thesis(tmp_path):
    nav = 10_000
    theses = ThesisRegistry(tmp_path / "theses.json")
    sleeves = SleeveRegistry(tmp_path / "sleeves.json")
    thesis = theses.create(symbol="MSFT", sleeve=Sleeve.CORE_GROWTH, status=ThesisStatus.DRAFT, decision=Decision.BUY)
    sleeves.assign(symbol="MSFT", sleeve=Sleeve.CORE_GROWTH, thesis_id=thesis.thesis_id, status=SleeveAssignmentStatus.PROPOSED)
    plan = _plan("MSFT", Decision.BUY, 5.0, 500.0, 100.0, thesis_id=thesis.thesis_id, after_qty=5.0, after_mv=500.0, after_pct=0.05)
    out = _fill([plan], ctx(nav), {"MSFT": _quote("MSFT", 100)}, theses=theses, sleeves=sleeves)
    fill = out.filled[0]
    assert fill.status == FillStatus.FILLED
    assert fill.symbol == "MSFT"
    assert fill.side == OrderSide.BUY
    assert fill.quantity == pytest.approx(5.0)
    assert fill.fill_price == pytest.approx(100.0)
    assert fill.filled_notional == pytest.approx(500.0)
    assert fill.thesis_id == thesis.thesis_id
    assert fill.source_decision_id == "test-decision"
    book = out.context_after
    assert book.cash == pytest.approx(9_500)
    pos = next(p for p in book.positions if p.symbol == "MSFT")
    assert pos.quantity == pytest.approx(5.0)
    assert pos.average_cost == pytest.approx(100.0)
    assert pos.market_value == pytest.approx(500.0)
    assert pos.market_value / book.current_nav == pytest.approx(0.05)
    assert pos.sleeve == Sleeve.CORE_GROWTH
    assert book.sleeve_allocation_pct[Sleeve.CORE_GROWTH.value] == pytest.approx(0.05)
    assert book.current_nav == pytest.approx(book.cash + sum(p.market_value for p in book.positions))
    assert theses.get(thesis.thesis_id).status == ThesisStatus.ACTIVE
    assert sleeves.get("MSFT").status == SleeveAssignmentStatus.ACTIVE
    assert out.reconciliation.ok is True
    assert out.execution_attempted is False
    assert out.broker_orders_submitted == 0
    assert out.broker_stop_orders_created == 0


def test_add_weighted_average_cost():
    nav = 10_000
    held = [_held("AMD", 0.05, nav, Sleeve.CORE_GROWTH, 100, thesis_id="t-amd")]
    plan = _plan("AMD", Decision.ADD, 2.0, 240.0, 120.0, thesis_id="t-amd", after_qty=7.0, after_mv=840.0, after_pct=0.084)
    out = _fill([plan], ctx(nav, held), {"AMD": _quote("AMD", 120)})
    pos = next(p for p in out.context_after.positions if p.symbol == "AMD")
    assert pos.quantity == pytest.approx(7.0)
    assert pos.average_cost == pytest.approx((5 * 100 + 2 * 120) / 7)
    assert pos.thesis_id == "t-amd"
    assert pos.sleeve == Sleeve.CORE_GROWTH
    assert out.blotter[0].realized_pnl == pytest.approx(0.0)
    assert out.reconciliation.ok is True


def test_reduce_and_sell_realized_pnl_and_close():
    nav = 10_000
    held = [_held("NVDA", 0.05, nav, Sleeve.CORE_GROWTH, 100, thesis_id="t-nvda", average_cost=80)]
    reduce_plan = _plan("NVDA", Decision.REDUCE, 2.0, 200.0, 100.0, thesis_id="t-nvda", after_qty=3.0, after_mv=300.0, after_pct=0.03)
    out = _fill([reduce_plan], ctx(nav, held, cash=9_500), {"NVDA": _quote("NVDA", 100)})
    pos = next(p for p in out.context_after.positions if p.symbol == "NVDA")
    assert pos.quantity == pytest.approx(3.0)
    assert pos.average_cost == pytest.approx(80.0)
    assert out.blotter[0].realized_pnl == pytest.approx((100 - 80) * 2)
    assert out.context_after.cash == pytest.approx(9_700)
    assert out.context_after.realized_pnl == pytest.approx(40.0)
    assert pos.unrealized_pnl == pytest.approx((100 - 80) * 3)

    sell = _plan("NVDA", Decision.SELL, 3.0, 300.0, 100.0, thesis_id="t-nvda", after_qty=0.0, after_mv=0.0, after_pct=0.0)
    sold = _fill([sell], out.context_after, {"NVDA": _quote("NVDA", 100)}, lots=out.lots)
    assert sold.filled[0].status == FillStatus.FILLED
    assert all(p.symbol != "NVDA" for p in sold.context_after.positions)
    assert sold.blotter[0].position_closed is True
    assert sold.blotter[0].realized_pnl == pytest.approx((100 - 80) * 3)
    assert sold.context_after.cash == pytest.approx(10_000)
    assert sold.context_after.holdings_count == 0
    assert sold.reconciliation.ok is True
    assert sold.reconciliation.checks["position_closed_at_zero"] is True


def test_hold_and_blocked_create_no_fill():
    context = ctx(10_000)
    skipped = [SkippedAction(symbol="NVDA", action=Decision.HOLD, reason="NON_EXECUTABLE_ACTION")]
    out = run_paper_fill([], context, {}, skipped=skipped, persist=False, now=NOW, journal=None)
    assert out.fills == []
    assert out.blotter == []
    assert out.skipped[0].symbol == "NVDA"
    assert out.context_after.cash == context.cash

    blocked = _plan("MSFT", Decision.BUY, 5.0, 500.0, 100.0, status=ExecutionStatus.BLOCKED_FROM_LIVE)
    blocked.blocked_reasons = ["RISK_GATE_NOT_PERMITTED"]
    out2 = _fill([blocked], context, {"MSFT": _quote("MSFT", 100)})
    assert out2.filled == []
    assert out2.skipped[0].reason == "BLOCKED_FROM_LIVE"
    assert out2.context_after.positions == []


def test_duplicate_fill_rejected(tmp_path):
    plan = _plan("MSFT", Decision.BUY, 5.0, 500.0, 100.0)
    store = PaperFillStore(tmp_path)
    first = run_paper_fill(
        [plan],
        ctx(10_000),
        {"MSFT": _quote("MSFT", 100)},
        persist=True,
        now=NOW,
        store=store,
        journal=tmp_path / "journal.jsonl",
    )
    assert first.filled
    second = run_paper_fill(
        [plan],
        first.context_after,
        {"MSFT": _quote("MSFT", 100)},
        persist=True,
        now=NOW,
        store=store,
        journal=tmp_path / "journal2.jsonl",
        lots=first.lots,
    )
    assert second.filled == []
    assert second.rejected[0].status == FillStatus.REJECTED
    assert "DUPLICATE_FILL" in second.rejected[0].reject_reasons
    assert second.context_after.cash == pytest.approx(first.context_after.cash)


def test_sell_cannot_create_negative_position():
    nav = 10_000
    held = [_held("NVDA", 0.02, nav, Sleeve.CORE_GROWTH, 100)]
    plan = _plan("NVDA", Decision.SELL, 5.0, 500.0, 100.0, after_qty=0.0, after_pct=0.0)
    out = _fill([plan], ctx(nav, held), {"NVDA": _quote("NVDA", 100)})
    assert out.filled == []
    assert "SELL_CREATES_NEGATIVE_POSITION" in out.rejected[0].reject_reasons
    assert next(p for p in out.context_after.positions if p.symbol == "NVDA").quantity == pytest.approx(2.0)


def test_reconciliation_fail_closed_does_not_apply_inconsistent_fill():
    nav = 10_000
    plan = _plan("MSFT", Decision.BUY, 5.0, 500.0, 100.0)
    plan.order_side = OrderSide.SELL_TO_CLOSE
    out = _fill([plan], ctx(nav), {"MSFT": _quote("MSFT", 100)})
    assert out.filled == []
    assert "SIDE_ACTION_MISMATCH" in out.rejected[0].reject_reasons
    assert out.context_after.cash == pytest.approx(10_000)
    assert out.context_after.positions == []


def test_no_execution_tools_reachable():
    hits = inspect_paper_fill_module_for_forbidden_tools()
    assert hits == []
    for tool in ("review_equity_order", "place_equity_order", "cancel_equity_order"):
        assert tool in PAPER_FILL_FORBIDDEN_TOOLS
    with pytest.raises(PaperFillSafetyError):
        from agentic_portfolio.paper_fill.safety import assert_no_forbidden_tools

        assert_no_forbidden_tools(["place_equity_order"])
    plan = _plan("MSFT", Decision.BUY, 5.0, 500.0, 100.0)
    with pytest.raises(PaperFillSafetyError):
        _fill([plan], ctx(10_000), {"MSFT": _quote("MSFT", 100)}, sources_observed=["place_equity_order"])


def test_persist_blotter_and_paper_book(tmp_path):
    plan = _plan("MSFT", Decision.BUY, 5.0, 500.0, 100.0, thesis_id="t-msft")
    out = _fill([plan], ctx(10_000), {"MSFT": _quote("MSFT", 100)}, persist=True, tmp_path=tmp_path)
    store = PaperFillStore(tmp_path)
    stored = store.get(out.run_id)
    assert stored is not None
    assert stored["execution_attempted"] is False
    assert stored["broker_stop_orders_created"] == 0
    assert stored["paper_environment"] is True
    assert stored["fills"][0]["status"] == "FILLED"
    assert stored["blotter"][0]["symbol"] == "MSFT"
    book = store.current_book()
    assert book["live_book_untouched"] is True
    assert book["context"]["cash"] == pytest.approx(9_500)
    journal = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert "PAPER_FILL_CREATED" in journal
    assert "PAPER_BOOK_UPDATED" in journal
    assert "review_equity_order" not in journal
    assert "place_equity_order" not in journal
    assert "cancel_equity_order" not in journal


def test_live_thesis_and_sleeve_registries_untouched(tmp_path):
    live = live_state_paths()
    before = {path: path.read_text(encoding="utf-8") if path.exists() else None for path in live}
    theses = ThesisRegistry(tmp_path / "paper_theses.json")
    sleeves = SleeveRegistry(tmp_path / "paper_sleeves.json")
    thesis = theses.create(symbol="MSFT", sleeve=Sleeve.CORE_GROWTH, status=ThesisStatus.DRAFT)
    sleeves.assign(symbol="MSFT", sleeve=Sleeve.CORE_GROWTH, thesis_id=thesis.thesis_id)
    plan = _plan("MSFT", Decision.BUY, 5.0, 500.0, 100.0, thesis_id=thesis.thesis_id)
    _fill(
        [plan],
        ctx(10_000),
        {"MSFT": _quote("MSFT", 100)},
        persist=True,
        tmp_path=tmp_path,
        theses=theses,
        sleeves=sleeves,
    )
    after = {path: path.read_text(encoding="utf-8") if path.exists() else None for path in live}
    assert after == before
    assert theses.get(thesis.thesis_id).status == ThesisStatus.ACTIVE


def test_paper_monitor_mapping_and_nav(tmp_path):
    nav = 10_000
    positions = [
        _held("NVDA", 0.05, nav, Sleeve.CORE_GROWTH, 180, thesis_id="t-nvda"),
        _held("NKE", 0.04, nav, Sleeve.OPPORTUNISTIC, 60, thesis_id="t-nke"),
        _held("ESTC", 0.02, nav, Sleeve.TACTICAL, 70, thesis_id="t-estc"),
        _held("IONQ", 0.01, nav, Sleeve.SPECULATIVE, 8, thesis_id="t-ionq"),
    ]
    context = ctx(nav, positions)
    plans = [
        _plan("NKE", Decision.REDUCE, 200.0 / 60.0, 200.0, 60.0, thesis_id="t-nke", after_qty=200.0 / 60.0, after_mv=200.0, after_pct=0.02),
        _plan("ESTC", Decision.SELL, 200.0 / 70.0, 200.0, 70.0, thesis_id="t-estc", after_qty=0.0, after_mv=0.0, after_pct=0.0),
        _plan("IONQ", Decision.SELL, 12.5, 100.0, 8.0, thesis_id="t-ionq", after_qty=0.0, after_mv=0.0, after_pct=0.0),
    ]
    quotes = {p.symbol: _quote(p.symbol, p.estimated_price) for p in plans}
    quotes["NVDA"] = _quote("NVDA", 180)
    skipped = [SkippedAction(symbol="NVDA", action=Decision.HOLD, reason="NON_EXECUTABLE_ACTION")]
    theses = ThesisRegistry(tmp_path / "theses.json")
    sleeves = SleeveRegistry(tmp_path / "sleeves.json")
    for symbol, sleeve, tid in (
        ("NVDA", Sleeve.CORE_GROWTH, "t-nvda"),
        ("NKE", Sleeve.OPPORTUNISTIC, "t-nke"),
        ("ESTC", Sleeve.TACTICAL, "t-estc"),
        ("IONQ", Sleeve.SPECULATIVE, "t-ionq"),
    ):
        theses.create(symbol=symbol, sleeve=sleeve, status=ThesisStatus.ACTIVE, thesis_id=tid)
        sleeves.assign(symbol=symbol, sleeve=sleeve, thesis_id=tid, status=SleeveAssignmentStatus.ACTIVE)
    out = run_paper_fill(
        plans,
        context,
        quotes,
        skipped=skipped,
        persist=True,
        now=NOW,
        store=PaperFillStore(tmp_path),
        journal=tmp_path / "journal.jsonl",
        theses=theses,
        sleeves=sleeves,
    )
    filled = {f.symbol: f for f in out.filled}
    assert "NVDA" not in filled
    assert any(s.symbol == "NVDA" and s.reason == "NON_EXECUTABLE_ACTION" for s in out.skipped)
    assert filled["NKE"].status == FillStatus.FILLED
    assert filled["ESTC"].status == FillStatus.FILLED
    assert filled["IONQ"].status == FillStatus.FILLED
    book = out.context_after
    symbols = {p.symbol for p in book.positions}
    assert symbols == {"NVDA", "NKE"}
    nke = next(p for p in book.positions if p.symbol == "NKE")
    assert nke.market_value == pytest.approx(200.0)
    assert nke.quantity == pytest.approx(200.0 / 60.0)
    assert book.cash == pytest.approx(9_300)
    assert book.current_nav == pytest.approx(10_000)
    assert book.sleeve_allocation_pct[Sleeve.CORE_GROWTH.value] == pytest.approx(0.05)
    assert book.sleeve_allocation_pct[Sleeve.OPPORTUNISTIC.value] == pytest.approx(0.02)
    assert book.sleeve_allocation_pct[Sleeve.TACTICAL.value] == pytest.approx(0.0)
    assert theses.get("t-estc").status == ThesisStatus.CLOSED
    assert theses.get("t-ionq").status == ThesisStatus.CLOSED
    assert sleeves.get("ESTC").status == SleeveAssignmentStatus.CLOSED
    assert sleeves.get("NKE").status == SleeveAssignmentStatus.REDUCING
    assert out.reconciliation.ok is True
    assert all(out.reconciliation.checks.values())
    assert order_plan_from_dict(
        {
            "order_plan_id": plans[0].order_plan_id,
            "symbol": "NKE",
            "action": "REDUCE",
            "quantity": plans[0].quantity,
            "notional": 200.0,
            "estimated_price": 60.0,
            "order_side": "sell_to_close",
            "order_type": "market",
            "time_in_force": "gfd",
            "slippage_check": {"ok": True, "codes": []},
            "liquidity_check": {"ok": True, "codes": []},
            "execution_status": "PAPER_ONLY",
            "live_execution_blocked": True,
        }
    ).action == Decision.REDUCE


def test_monitoring_rerun_sees_updated_paper_book():
    nav = 10_000
    positions = [
        _held("NVDA", 0.05, nav, Sleeve.CORE_GROWTH, 180, thesis_id="t-nvda"),
        _held("NKE", 0.04, nav, Sleeve.OPPORTUNISTIC, 60, thesis_id="t-nke"),
        _held("ESTC", 0.02, nav, Sleeve.TACTICAL, 70, thesis_id="t-estc"),
        _held("IONQ", 0.01, nav, Sleeve.SPECULATIVE, 8, thesis_id="t-ionq"),
    ]
    plans = [
        _plan("NKE", Decision.REDUCE, 200.0 / 60.0, 200.0, 60.0, thesis_id="t-nke"),
        _plan("ESTC", Decision.SELL, 200.0 / 70.0, 200.0, 70.0, thesis_id="t-estc"),
        _plan("IONQ", Decision.SELL, 12.5, 100.0, 8.0, thesis_id="t-ionq"),
    ]
    quotes = {p.symbol: _quote(p.symbol, p.estimated_price) for p in plans}
    filled = _fill(plans, ctx(nav, positions), quotes)
    observations = [
        PositionObservation(symbol="NVDA", current_price=180, reference_price=210, price_move_pct=-0.15),
        PositionObservation(symbol="NKE", current_price=60, earnings_event=True, major_news=True),
    ]

    def _monitor(request):
        symbol = request.facts["symbol"]
        return {
            "NVDA": {
                "symbol": "NVDA",
                "thesis_status": "UNCHANGED",
                "monitoring_state": "RESEARCH_REFRESH_REQUIRED",
                "recommended_action": "HOLD",
                "desired_allocation_pct": 5.0,
                "rationale": "Hold remaining core.",
                "broker_stop_orders_created": False,
            },
            "NKE": {
                "symbol": "NKE",
                "thesis_status": "WEAKENED",
                "monitoring_state": "THESIS_WEAKENED",
                "recommended_action": "HOLD",
                "desired_allocation_pct": 2.0,
                "rationale": "Already reduced to 2% NAV.",
                "broker_stop_orders_created": False,
            },
        }[symbol]

    def _decision(request):
        symbol = request.reports[0]["symbol"]
        action, alloc = {"NVDA": ("HOLD", 5.0), "NKE": ("HOLD", 2.0)}[symbol]
        return {
            "comparison": {"ranking": [symbol, "CASH"], "vs_cash": "paper", "vs_spy": "paper"},
            "decisions": [
                {"symbol": symbol, "decision": action, "desired_allocation_pct": alloc, "rationale": "post-fill"},
                {"symbol": "CASH", "decision": "HOLD", "desired_allocation_pct": 100.0 - alloc, "rationale": "cash"},
            ],
        }

    monitored = run_position_monitor(
        filled.context_after,
        observations,
        reasoner=ScriptedMonitoringReasoner(_monitor),
        decision_reasoner=ScriptedDecisionReasoner(_decision),
        persist=False,
        now=NOW,
        journal=None,
    )
    symbols = [p.symbol for p in monitored.positions]
    assert symbols == ["NVDA", "NKE"]
    assert "ESTC" not in symbols
    assert "IONQ" not in symbols
    by_sym = {p.symbol: p for p in monitored.positions}
    assert by_sym["NVDA"].recommended_action == Decision.HOLD
    assert by_sym["NKE"].recommended_action in {Decision.HOLD, Decision.NO_ACTION, Decision.REDUCE}
    assert monitored.execution_attempted is False
    assert monitored.broker_stop_orders_created == 0
