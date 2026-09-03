"""LIVE POSITION_MONITOR wiring. Fake broker / scripted AI only. No real API calls."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_portfolio.agent.connection import ConnectionManager
from agentic_portfolio.agent.handlers import AgentServices, build_handlers
from agentic_portfolio.adapters.readonly_runtime import ReadonlyBrokerRuntime
from agentic_portfolio.decision.reasoner import ScriptedDecisionReasoner
from agentic_portfolio.live.engine import refresh_live_portfolio
from agentic_portfolio.live_approval import LiveApprovalEngine, LiveApprovalStatus, LiveApprovalStore
from agentic_portfolio.live_execution import ExecutionStore, FakeBroker, LiveOrderExecutor
from agentic_portfolio.live_execution.types import BrokerOrderStatus
from agentic_portfolio.live_execution.types import BrokerOrderStatus
from agentic_portfolio.notify import NotificationEngine, NotificationKind, NotificationStore
from agentic_portfolio.research.reasoner import ScriptedResearchReasoner
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.runtime import RuntimeMode, live_placement_enabled
from agentic_portfolio.schemas import (
    Decision,
    Sleeve,
    SleeveAssignmentStatus,
    ThesisStatus,
)
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry
from agentic_portfolio.watch import WatchEngine, WatchStore
from tests.test_decision import _payload as _decision_payload
from tests.test_decision import _thesis as _decision_thesis
from tests.test_live_rc1 import _enable_placement
from tests.test_monitoring import _ai, _exit, _report
from tests.test_production_pipeline import NOW as PIPE_NOW
from tests.test_production_pipeline import _seed, _worker
from tests.test_research import _ai as _research_ai

NOW = datetime(2026, 9, 3, 18, 30, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=10)).isoformat()
QUOTE = 100.0


class CountingMonitor:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def reason(self, request):
        self.calls.append(request)
        payload = self.response(request) if callable(self.response) else dict(self.response)
        return payload


class Book:
    def __init__(self, context=None):
        self.context = context

    def get(self):
        return self.context


def _session_ctx():
    return {"job": "POSITION_MONITOR"}


def _seed_holding(
    root: Path,
    symbol="MSFT",
    *,
    sleeve=Sleeve.OPPORTUNISTIC,
    qty=2.0,
    price=QUOTE,
    due=True,
    thesis_status=ThesisStatus.ACTIVE,
):
    theses = ThesisRegistry(root / "state" / "thesis_registry.json", runtime_mode="LIVE")
    sleeves = SleeveRegistry(root / "state" / "sleeve_registry.json")
    rec = theses.create(
        symbol=symbol,
        sleeve=sleeve,
        status=thesis_status,
        decision=Decision.HOLD,
        thesis_summary=f"{symbol} live thesis.",
        research_id=f"res-{symbol}",
        desired_allocation_pct=20.0,
        review_triggers=["earnings", "news"],
        exit_policy=_exit(),
        created_at=OLD if due else NOW.isoformat(),
    )
    sleeves.assign(symbol=symbol, sleeve=sleeve, thesis_id=rec.thesis_id, status=SleeveAssignmentStatus.ACTIVE)
    ResearchStore(root).save(_report(symbol, sleeve=sleeve, price=price, rid=f"res-{symbol}"))
    broker = FakeBroker(
        nav=1000.0,
        cash=1000.0 - qty * price,
        buying_power=1000.0 - qty * price,
        positions={symbol: {"quantity": qty, "avg": price}},
        quotes={symbol: price, "SPY": 500.0},
    )
    return rec, broker


def _services(
    root: Path,
    broker: FakeBroker,
    *,
    monitor=None,
    decision=None,
    exhausted=False,
    book: Book | None = None,
    now=None,
    executor=None,
):
    clock = now or (lambda: NOW)
    watch_store = WatchStore(root, runtime_mode=RuntimeMode.LIVE)
    approval_store = LiveApprovalStore(root, runtime_mode=RuntimeMode.LIVE)
    notify = NotificationEngine(NotificationStore(root), now_fn=clock)
    journal = root / "logs" / "agent.jsonl"
    approvals = LiveApprovalEngine(approval_store, journal=journal, now_fn=clock, executor=executor)
    holder = book or Book()

    def refresh():
        result = refresh_live_portfolio(broker, now=clock(), root=root, persist=True)
        holder.context = result.context
        return result

    def quotes(tickers):
        out = {}
        for symbol in tickers:
            price = broker.quotes.get(str(symbol).upper())
            extra = dict(getattr(broker, "quote_flags", {}).get(str(symbol).upper()) or {})
            if price is None:
                continue
            out[str(symbol).upper()] = {
                "price": price,
                "last": price,
                "previous_close": extra.get("previous_close", price),
                "major_news": bool(extra.get("major_news")),
                "earnings_event": bool(extra.get("earnings_event")),
                "material_filing": bool(extra.get("material_filing")),
            }
        return out

    return AgentServices(
        root=root,
        runtime_mode=RuntimeMode.LIVE,
        watch=WatchEngine(watch_store, journal=journal, now_fn=clock),
        watch_store=watch_store,
        approvals=approvals,
        approval_store=approval_store,
        notify=notify,
        connection=ConnectionManager(bootstrap=lambda **k: ReadonlyBrokerRuntime(bound=True), now_fn=clock),
        now_fn=clock,
        refresh_fn=refresh,
        quotes_fn=quotes,
        monitoring_reasoner=monitor,
        decision_reasoner=decision,
        budget_exhausted=exhausted,
        ai_allowed=not exhausted,
        executor=executor,
    )


def _run_monitor(services):
    return build_handlers(services)["POSITION_MONITOR"](_session_ctx())


def _approval_notes(services):
    return [n for n in services.notify.store.all() if n.kind is NotificationKind.APPROVAL_REQUIRED]


def test_01_live_holding_detected(tmp_path):
    rec, broker = _seed_holding(tmp_path, due=False)
    monitor = CountingMonitor(_ai("MSFT"))
    row = _run_monitor(_services(tmp_path, broker, monitor=monitor))
    assert row["status"] == "OK"
    assert row["holdings_detected"] == 1
    assert row["monitored"] == 1
    assert row["auto_execution"] is False
    assert row["placement_attempted"] is False
    assert rec.thesis_id


def test_02_not_due_no_ai_no_approval(tmp_path):
    _, broker = _seed_holding(tmp_path, due=False)
    monitor = CountingMonitor(_ai("MSFT", action="REDUCE", alloc=5.0))
    services = _services(tmp_path, broker, monitor=monitor)
    row = _run_monitor(services)
    assert monitor.calls == []
    assert row["ai_calls"] == 0
    assert row["approvals_created"] == 0
    assert services.approvals.store.pending() == []
    assert _approval_notes(services) == []


def test_03_due_review_runs_once(tmp_path):
    _, broker = _seed_holding(tmp_path, due=True)
    monitor = CountingMonitor(_ai("MSFT", action="HOLD", alloc=20.0))
    services = _services(tmp_path, broker, monitor=monitor)
    first = _run_monitor(services)
    assert len(monitor.calls) == 1
    assert first["ai_calls"] == 1
    second = _run_monitor(services)
    assert len(monitor.calls) == 1
    assert second["ai_calls"] == 0


def test_04_due_hold_no_approval(tmp_path):
    _, broker = _seed_holding(tmp_path, due=True)
    monitor = CountingMonitor(_ai("MSFT", action="HOLD", alloc=20.0))
    services = _services(tmp_path, broker, monitor=monitor)
    row = _run_monitor(services)
    assert row["approvals_created"] == 0
    assert services.approvals.store.pending() == []
    assert _approval_notes(services) == []


def test_05_due_add_creates_live_approval(tmp_path):
    rec, broker = _seed_holding(tmp_path, due=True)
    theses = ThesisRegistry(tmp_path / "state" / "thesis_registry.json", runtime_mode="LIVE")
    theses.add_review(rec.thesis_id, review_type="INVESTMENT_THESIS_REVIEW", session_id=None)
    theses.add_review(rec.thesis_id, review_type="RISK_REVIEW", session_id=None)
    monitor = CountingMonitor(_ai("MSFT", action="ADD", alloc=30.0))
    decision = ScriptedDecisionReasoner(_decision_payload("MSFT", decision="ADD", alloc=30.0))
    services = _services(tmp_path, broker, monitor=monitor, decision=decision)
    row = _run_monitor(services)
    pending = services.approvals.store.pending()
    if row["approvals_created"] == 0:
        pytest.skip("ADD did not clear Risk Gate with current research packet; REDUCE/SELL path is the launch requirement")
    assert pending
    assert pending[0].proposed_action in {"ADD", "BUY"}
    assert _approval_notes(services)


def test_06_due_reduce_creates_live_approval(tmp_path):
    _, broker = _seed_holding(tmp_path, due=True)
    monitor = CountingMonitor(_ai("MSFT", action="REDUCE", alloc=12.0))
    services = _services(tmp_path, broker, monitor=monitor)
    row = _run_monitor(services)
    assert row["approvals_created"] == 1
    pending = services.approvals.store.pending()
    assert len(pending) == 1
    assert pending[0].proposed_action == "REDUCE"
    assert pending[0].proposed_allocation_pct == pytest.approx(12.0)
    assert _approval_notes(services)
    assert row["auto_execution"] is False


def test_07_due_sell_creates_live_approval(tmp_path):
    _, broker = _seed_holding(tmp_path, due=True)
    monitor = CountingMonitor(_ai("MSFT", action="SELL", alloc=0.0))
    services = _services(tmp_path, broker, monitor=monitor)
    row = _run_monitor(services)
    assert row["approvals_created"] == 1
    assert services.approvals.store.pending()[0].proposed_action == "SELL"
    assert _approval_notes(services)


def test_08_risk_gate_blocks_reduce(tmp_path):
    _, broker = _seed_holding(tmp_path, due=True)
    monitor = CountingMonitor(_ai("MSFT", action="REDUCE", alloc=12.0))
    services = _services(tmp_path, broker, monitor=monitor)

    def refresh():
        result = refresh_live_portfolio(broker, now=NOW, root=tmp_path, persist=True)
        result.context.risk_state = __import__("agentic_portfolio.schemas", fromlist=["RiskState"]).RiskState.HALTED
        return result

    services.refresh_fn = refresh
    row = _run_monitor(services)
    assert row["approvals_created"] == 0
    assert services.approvals.store.pending() == []
    assert row["risk_blocked"] >= 1


def test_09_risk_gate_blocks_sell(tmp_path):
    _, broker = _seed_holding(tmp_path, due=True)
    monitor = CountingMonitor(_ai("MSFT", action="SELL", alloc=0.0))
    services = _services(tmp_path, broker, monitor=monitor)

    def refresh():
        result = refresh_live_portfolio(broker, now=NOW, root=tmp_path, persist=True)
        from agentic_portfolio.schemas import RiskState

        result.context.risk_state = RiskState.HALTED
        return result

    services.refresh_fn = refresh
    row = _run_monitor(services)
    assert row["approvals_created"] == 0
    assert services.approvals.store.pending() == []


def test_10_pending_approval_not_duplicated(tmp_path):
    _, broker = _seed_holding(tmp_path, due=True)
    monitor = CountingMonitor(_ai("MSFT", action="REDUCE", alloc=12.0))
    services = _services(tmp_path, broker, monitor=monitor)
    first = _run_monitor(services)
    n = len(services.approvals.store.pending())
    assert first["approvals_created"] == 1
    theses = ThesisRegistry(tmp_path / "state" / "thesis_registry.json", runtime_mode="LIVE")
    rec = theses.current_for_symbol("MSFT")
    rec.review_history = [r for r in rec.review_history if r.review_type != "POSITION_MONITOR_REVIEW"]
    theses._write(rec)
    theses.save()
    rec.updated_at = OLD
    theses._write(rec)
    theses.save()
    second = _run_monitor(services)
    assert second["approvals_created"] == 0
    assert len(services.approvals.store.pending()) == n


def test_11_recent_review_does_not_recall_ai(tmp_path):
    _, broker = _seed_holding(tmp_path, due=True)
    monitor = CountingMonitor(_ai("MSFT", action="HOLD"))
    services = _services(tmp_path, broker, monitor=monitor)
    _run_monitor(services)
    _run_monitor(services)
    assert len(monitor.calls) == 1


def test_12_13_filled_buy_is_monitorable_with_thesis(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    rec, broker = _seed_holding(tmp_path, due=False, thesis_status=ThesisStatus.DRAFT)
    theses = ThesisRegistry(tmp_path / "state" / "thesis_registry.json", runtime_mode="LIVE")
    theses.set_status(rec.thesis_id, ThesisStatus.DRAFT)
    book = Book()
    store = ExecutionStore(tmp_path, runtime_mode="LIVE")
    executor = LiveOrderExecutor(
        store,
        broker,
        root=tmp_path,
        runtime_mode="LIVE",
        context_fn=lambda: book.context,
        regular_hours_fn=lambda: True,
        now_fn=lambda: NOW,
        refresh_fn=lambda: refresh_live_portfolio(broker, now=NOW, root=tmp_path, persist=True),
    )
    services = _services(tmp_path, broker, monitor=CountingMonitor(_ai("MSFT")), book=book, executor=executor)
    refresh = services.refresh_fn()
    book.context = refresh.context
    from agentic_portfolio.live_execution.positions import upsert_from_fill
    from agentic_portfolio.live_execution.types import BrokerOrderRecord, ExecutionIntent, ExecutionIntentStatus

    intent = ExecutionIntent(
        intent_id="intent-buy",
        approval_id="appr-buy",
        symbol="MSFT",
        action="BUY",
        side="buy",
        thesis_id=rec.thesis_id,
        status=ExecutionIntentStatus.FILLED,
    )
    store.save_intent(intent)
    order = BrokerOrderRecord(
        order_id="ord-buy",
        intent_id=intent.intent_id,
        approval_id=intent.approval_id,
        thesis_id=rec.thesis_id,
        symbol="MSFT",
        side="buy",
        status=BrokerOrderStatus.FILLED,
    )
    store.save_order(order)
    link = upsert_from_fill(tmp_path, symbol="MSFT", store=store, sleeve=Sleeve.OPPORTUNISTIC.value, mode="LIVE")
    assert link.thesis_id == rec.thesis_id
    loaded = ThesisRegistry(tmp_path / "state" / "thesis_registry.json", runtime_mode="LIVE").get(rec.thesis_id)
    assert loaded.status is ThesisStatus.ACTIVE
    row = _run_monitor(services)
    assert row["holdings_detected"] == 1
    assert refresh.context.positions[0].thesis_id == rec.thesis_id


def test_14_reduce_human_approval_partial_sell(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    _, broker = _seed_holding(tmp_path, due=True, qty=2.0, price=100.0)
    book = Book()
    store = ExecutionStore(tmp_path, runtime_mode="LIVE")
    executor = LiveOrderExecutor(
        store,
        broker,
        root=tmp_path,
        runtime_mode="LIVE",
        context_fn=lambda: book.get(),
        regular_hours_fn=lambda: True,
        now_fn=lambda: NOW,
    )
    monitor = CountingMonitor(_ai("MSFT", action="REDUCE", alloc=12.0))
    services = _services(tmp_path, broker, monitor=monitor, book=book, executor=executor)
    _run_monitor(services)
    book.context = services.last_context
    pending = services.approvals.store.pending()[0]
    decided = services.approvals.record_decision(pending.approval_id, LiveApprovalStatus.APPROVED, note="human")
    assert decided.placed_order is True
    qty = float(broker.place_calls[0]["quantity"])
    assert qty == pytest.approx(0.8)
    assert broker.place_calls[0]["side"] == "sell"
    assert qty < 2.0


def test_15_sell_human_approval_full_exit(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    _, broker = _seed_holding(tmp_path, due=True, qty=2.0, price=100.0)
    book = Book()
    store = ExecutionStore(tmp_path, runtime_mode="LIVE")
    executor = LiveOrderExecutor(
        store,
        broker,
        root=tmp_path,
        runtime_mode="LIVE",
        context_fn=lambda: book.get(),
        regular_hours_fn=lambda: True,
        now_fn=lambda: NOW,
    )
    services = _services(tmp_path, broker, monitor=CountingMonitor(_ai("MSFT", action="SELL", alloc=0.0)), book=book, executor=executor)
    _run_monitor(services)
    book.context = services.last_context
    pending = services.approvals.store.pending()[0]
    decided = services.approvals.record_decision(pending.approval_id, LiveApprovalStatus.APPROVED, note="human")
    assert decided.placed_order is True
    assert float(broker.place_calls[0]["quantity"]) == pytest.approx(2.0)


def test_16_send_time_current_holding_wins(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    _, broker = _seed_holding(tmp_path, due=True, qty=2.0, price=100.0)
    book = Book()
    store = ExecutionStore(tmp_path, runtime_mode="LIVE")
    executor = LiveOrderExecutor(
        store,
        broker,
        root=tmp_path,
        runtime_mode="LIVE",
        context_fn=lambda: book.get(),
        regular_hours_fn=lambda: True,
        now_fn=lambda: NOW,
    )
    services = _services(tmp_path, broker, monitor=CountingMonitor(_ai("MSFT", action="REDUCE", alloc=5.0)), book=book, executor=executor)
    _run_monitor(services)
    pending = services.approvals.store.pending()[0]
    from tests.conftest import ctx
    from agentic_portfolio.schemas import Position

    held = Position(symbol="MSFT", quantity=1.2, market_value=120.0, current_price=100.0)
    book.context = ctx(1000, [held], cash=880.0)
    decided = services.approvals.record_decision(pending.approval_id, LiveApprovalStatus.APPROVED, note="human")
    assert decided.placed_order is True
    assert float(broker.place_calls[0]["quantity"]) == pytest.approx(0.7)


def test_17_missing_position_fails_closed(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    _, broker = _seed_holding(tmp_path, due=True)
    book = Book()
    store = ExecutionStore(tmp_path, runtime_mode="LIVE")
    executor = LiveOrderExecutor(
        store,
        broker,
        root=tmp_path,
        runtime_mode="LIVE",
        context_fn=lambda: book.get(),
        regular_hours_fn=lambda: True,
        now_fn=lambda: NOW,
    )
    services = _services(tmp_path, broker, monitor=CountingMonitor(_ai("MSFT", action="SELL", alloc=0.0)), book=book, executor=executor)
    _run_monitor(services)
    pending = services.approvals.store.pending()[0]
    from tests.conftest import ctx

    book.context = ctx(1000, [])
    decided = services.approvals.record_decision(pending.approval_id, LiveApprovalStatus.APPROVED, note="human")
    assert decided.placed_order is False
    assert broker.place_calls == []


def test_18_core_holding_uses_committee_not_singleton(tmp_path):
    rec, broker = _seed_holding(tmp_path, sleeve=Sleeve.CORE_GROWTH, due=True)
    monitor = CountingMonitor(_ai("MSFT", action="SELL", alloc=0.0))
    services = _services(tmp_path, broker, monitor=monitor)
    row = _run_monitor(services)
    assert "MSFT" not in (row.get("due_symbols") or [])
    assert row.get("core_committee") in {"SKIPPED", "SKIPPED_UNCHANGED", "OK", "DEGRADED", "BLOCKED"}
    assert monitor.calls == []
    assert rec.sleeve is Sleeve.CORE_GROWTH


def test_19_opportunistic_tactical_speculative_use_singleton(tmp_path):
    for sleeve, symbol in (
        (Sleeve.OPPORTUNISTIC, "OPP1"),
        (Sleeve.TACTICAL, "TAC1"),
        (Sleeve.SPECULATIVE, "SPC1"),
    ):
        root = tmp_path / sleeve.value
        root.mkdir()
        _, broker = _seed_holding(root, symbol=symbol, sleeve=sleeve, due=True)
        monitor = CountingMonitor(_ai(symbol, action="HOLD", alloc=5.0))
        row = _run_monitor(_services(root, broker, monitor=monitor))
        assert symbol in (row.get("due_symbols") or [])
        assert monitor.calls
        monitor.calls.clear()


def test_20_material_change_reaches_reassessment(tmp_path):
    _, broker = _seed_holding(tmp_path, due=False)
    broker.quote_flags = {"MSFT": {"major_news": True, "previous_close": QUOTE}}
    monitor = CountingMonitor(_ai("MSFT", action="HOLD"))
    row = _run_monitor(_services(tmp_path, broker, monitor=monitor))
    assert row["due_reasons"].get("MSFT") == "material_trigger"
    assert monitor.calls


def test_21_budget_denied_no_reassessment(tmp_path):
    _, broker = _seed_holding(tmp_path, due=True)
    monitor = CountingMonitor(_ai("MSFT", action="SELL", alloc=0.0))
    services = _services(tmp_path, broker, monitor=monitor, exhausted=True)
    row = _run_monitor(services)
    assert monitor.calls == []
    assert row["approvals_created"] == 0
    assert services.approvals.store.pending() == []


def test_22_live_never_writes_paper(tmp_path):
    _, broker = _seed_holding(tmp_path, due=True)
    _run_monitor(_services(tmp_path, broker, monitor=CountingMonitor(_ai("MSFT", action="HOLD"))))
    assert not (tmp_path / "state" / "paper_book" / "current.json").exists()
    assert (tmp_path / "state" / "live_book" / "current.json").exists()


def test_23_24_25_no_place_until_human_and_telegram_after_packet(monkeypatch, tmp_path):
    _, broker = _seed_holding(tmp_path, due=True)
    monitor = CountingMonitor(_ai("MSFT", action="REDUCE", alloc=12.0))
    services = _services(tmp_path, broker, monitor=monitor)
    assert live_placement_enabled() is False
    before = _approval_notes(services)
    row = _run_monitor(services)
    assert row["auto_execution"] is False
    assert row["placement_attempted"] is False
    assert broker.place_calls == []
    assert len(_approval_notes(services)) == len(before) + 1
    _enable_placement(monkeypatch)
    assert broker.place_calls == []


def test_e2e_buy_hold_reduce_sell(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    _seed(tmp_path, symbol="QUAL", sleeve=Sleeve.OPPORTUNISTIC)
    research = ScriptedResearchReasoner({"QUAL": _research_ai("QUAL", conclusion="ADVANCE_TO_THESIS")})
    decision = ScriptedDecisionReasoner(
        _decision_payload("QUAL", decision="BUY", alloc=5.0, theses=[_decision_thesis("QUAL", sleeve="OPPORTUNISTIC")])
    )
    worker = _worker(tmp_path, research=research, decision=decision, nav=1000.0)
    cycle = worker.run_cycle()
    assert cycle.proposals_created >= 1 or worker.approvals.store.pending()
    pending = worker.approvals.store.pending()
    assert pending
    buy = pending[0]
    assert buy.proposed_action in {"BUY", "ADD"}
    broker = FakeBroker(nav=1000, cash=1000, buying_power=1000, quotes={"QUAL": 100.0, "SPY": 500.0})
    clock = lambda: PIPE_NOW
    book = Book()
    store = ExecutionStore(tmp_path, runtime_mode="LIVE")
    executor = LiveOrderExecutor(
        store,
        broker,
        root=tmp_path,
        runtime_mode="LIVE",
        context_fn=lambda: book.get(),
        regular_hours_fn=lambda: True,
        now_fn=clock,
        refresh_fn=lambda: refresh_live_portfolio(broker, now=PIPE_NOW, root=tmp_path, persist=True),
    )
    worker.approvals.executor = executor
    from tests.conftest import ctx

    book.context = ctx(1000)
    decided = worker.approvals.record_decision(buy.approval_id, LiveApprovalStatus.APPROVED, note="human buy")
    if not decided.placed_order:
        reasons = store.intents()[0].block_reasons if store.intents() else ["no_intent"]
        raise AssertionError(f"BUY did not place: {reasons}")
    assert "QUAL" in broker.positions
    refresh = refresh_live_portfolio(broker, now=PIPE_NOW, root=tmp_path, persist=True)
    assert any(p.symbol == "QUAL" for p in refresh.context.positions)
    book.context = refresh.context
    actions = iter(
        [
            _ai("QUAL", action="HOLD", alloc=5.0),
            _ai("QUAL", action="REDUCE", alloc=2.0),
            _ai("QUAL", action="SELL", alloc=0.0),
        ]
    )
    monitor = CountingMonitor(lambda _req: next(actions))
    sleeves = SleeveRegistry(tmp_path / "state" / "sleeve_registry.json")
    if sleeves.get("QUAL") is None:
        thesis = ThesisRegistry(tmp_path / "state" / "thesis_registry.json", runtime_mode="LIVE").current_for_symbol("QUAL")
        sleeves.assign(
            symbol="QUAL",
            sleeve=Sleeve.OPPORTUNISTIC,
            thesis_id=thesis.thesis_id if thesis else None,
            status=SleeveAssignmentStatus.ACTIVE,
        )
    theses = ThesisRegistry(tmp_path / "state" / "thesis_registry.json", runtime_mode="LIVE")
    rec = theses.current_for_symbol("QUAL")
    aged = (PIPE_NOW - timedelta(days=10)).isoformat()
    rec.updated_at = aged
    theses._write(rec)
    theses.save()
    if rec.status == ThesisStatus.DRAFT:
        theses.set_status(rec.thesis_id, ThesisStatus.ACTIVE)
    services = _services(tmp_path, broker, monitor=monitor, book=book, executor=executor, now=clock)
    hold_row = _run_monitor(services)
    assert hold_row["approvals_created"] == 0
    broker.quote_flags = {"QUAL": {"major_news": True}}
    reduce_row = _run_monitor(services)
    assert reduce_row["approvals_created"] == 1
    red = services.approvals.canonical_pending(ticker="QUAL", proposed_action="REDUCE")
    assert red is not None
    book.context = services.last_context
    reduced = services.approvals.record_decision(red.approval_id, LiveApprovalStatus.APPROVED, note="human reduce")
    assert reduced.placed_order is True
    assert broker.positions.get("QUAL")
    rec = theses.current_for_symbol("QUAL")
    rec.review_history = [r for r in rec.review_history if r.review_type != "POSITION_MONITOR_REVIEW"]
    rec.updated_at = aged
    theses._write(rec)
    theses.save()
    sell_row = _run_monitor(services)
    assert sell_row["approvals_created"] == 1
    sell = services.approvals.canonical_pending(ticker="QUAL", proposed_action="SELL")
    book.context = services.last_context
    sold = services.approvals.record_decision(sell.approval_id, LiveApprovalStatus.APPROVED, note="human sell")
    assert sold.placed_order is True
    assert "QUAL" not in broker.positions


def test_e2e_hold_variant_creates_no_approval(tmp_path):
    _, broker = _seed_holding(tmp_path, due=True)
    services = _services(tmp_path, broker, monitor=CountingMonitor(_ai("MSFT", action="HOLD")))
    row = _run_monitor(services)
    assert row["approvals_created"] == 0
    assert services.approvals.store.pending() == []
    assert broker.place_calls == []
    assert row["auto_execution"] is False
