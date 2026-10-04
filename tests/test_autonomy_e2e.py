"""End-to-end tests for the unified autonomous portfolio pipeline.

discover/queue → research (AI, scripted) → decision (AI, scripted) → Risk Gate
→ ProposalIntake → OrderAuthority (human | autonomous) → OrderExecutor
→ send-time Risk Gate re-run → broker (FakeBroker for LIVE, PaperBroker for PAPER)
→ reconcile / account refresh.

No network. No real broker. No real AI. LIVE tests use the in-memory FakeBroker.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_portfolio.agent.handlers import AgentServices, build_handlers
from agentic_portfolio.agent.pipeline import ResearchQueueWorker
from agentic_portfolio.autonomy import (
    AutonomyLimits,
    ExecutionMode,
    ExecutionPolicy,
    KillSwitch,
    OrderAuthority,
    ProposalIntake,
    load_execution_policy,
)
from agentic_portfolio.autonomy.paper_broker import PaperBroker
from agentic_portfolio.live.engine import refresh_live_portfolio
from agentic_portfolio.live_approval import LiveApprovalEngine, LiveApprovalStatus, LiveApprovalStore
from agentic_portfolio.live_execution import ExecutionStore, FakeBroker, LiveOrderExecutor
from agentic_portfolio.live_execution.audit import read_audit
from agentic_portfolio.notify import NotificationEngine, NotificationKind, NotificationStore
from agentic_portfolio.risk_gate import evaluate
from agentic_portfolio.runtime import RuntimeMode
from agentic_portfolio.schemas import (
    CandidateStatus,
    ClassificationStatus,
    Decision,
    DiscoveryPriority,
    LiquidityInputs,
    ProposedAction,
    ResearchQueueEntry,
    ResearchQueueStatus,
    SecurityClass,
    Sleeve,
)
from agentic_portfolio.watch import WatchEngine, WatchStatus, WatchStore
from tests.conftest import ctx, pos
from tests.test_production_pipeline import _buy_reasoners, _seed
from tests.test_research import _candidate, _payload

# Monday 2026-08-31 10:30 ET: regular session, outside the open/close buffers.
OPEN = datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc)
NIGHT = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)  # Sun 21:00 ET, before Monday's session
QUOTE = 120.0
ROOT_SRC = Path(__file__).resolve().parents[1] / "src" / "agentic_portfolio"


# ----------------------------------------------------------------------------- helpers
def _mode(monkeypatch, mode: str, *, placement: bool = True) -> None:
    monkeypatch.setenv("AGENTIC_EXECUTION_MODE", mode)
    monkeypatch.setenv("AGENTIC_LIVE_ORDER_PLACEMENT", "true" if placement else "false")


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class Book:
    def __init__(self, context) -> None:
        self.context = context

    def __call__(self):
        return self.context


def _is_regular(clock: Clock):
    from agentic_portfolio.calendar import is_regular_hours

    return lambda: is_regular_hours(clock())


def _live_stack(root: Path, broker, *, clock: Clock, book: Book, authority: OrderAuthority | None = None):
    notify = NotificationEngine(NotificationStore(root), now_fn=clock)
    store = ExecutionStore(root, runtime_mode=RuntimeMode.LIVE)
    authority = authority or OrderAuthority(root, execution_store=store, notify=notify, now_fn=clock)
    executor = LiveOrderExecutor(
        store,
        broker,
        root=root,
        runtime_mode=RuntimeMode.LIVE,
        context_fn=book,
        regular_hours_fn=_is_regular(clock),
        notify=notify,
        now_fn=clock,
        authority=authority,
    )
    approvals = LiveApprovalEngine(
        LiveApprovalStore(root, runtime_mode=RuntimeMode.LIVE),
        journal=root / "logs" / "approval.jsonl",
        now_fn=clock,
        executor=executor,
    )
    return store, executor, approvals, notify, authority


def _worker(root: Path, approvals, notify, *, clock: Clock, book: Book, mode=RuntimeMode.LIVE, research=None, decision=None):
    watch_store = WatchStore(root, runtime_mode=mode)
    if research is None or decision is None:
        research, decision = _buy_reasoners()
    return ResearchQueueWorker(
        root,
        runtime_mode=mode,
        research_reasoner=research,
        decision_reasoner=decision,
        payload_fn=lambda candidate: _payload(candidate.symbol),
        context_fn=book,
        watch=WatchEngine(watch_store, journal=root / "logs" / "agent.jsonl", now_fn=clock),
        approvals=approvals,
        notify=notify,
        now_fn=clock,
    )


def _qual_action(nav: float, pct: float = 0.05, *, decision=Decision.BUY, sleeve=Sleeve.CORE_GROWTH, cls=SecurityClass.OTHER_DIVERSIFIED_ETF, sector=None):
    return ProposedAction(
        symbol="QUAL",
        decision=decision,
        security_class=cls,
        classification_status=ClassificationStatus.VALIDATED,
        sleeve=sleeve,
        current_price=QUOTE,
        proposed_notional=pct * nav,
        sector=sector,
        liquidity=LiquidityInputs(median_daily_dollar_volume_20d=1e10),
    )


def _submit(intake: ProposalIntake, context, action: ProposedAction, *, origin="test"):
    risk = evaluate(context, action)
    return intake.submit(
        ticker=action.symbol,
        action=action.decision.value,
        risk=risk,
        proposed_action=action,
        dollars=action.proposed_notional,
        pct=None,
        context=context,
        reason="test proposal",
        quote=action.current_price,
        origin=origin,
    ), risk


def _intake(approvals, authority, root, notify):
    return ProposalIntake(approvals, authority, root=root, notify=notify)


# ------------------------------------------------------------------- policy + switch
def test_execution_policy_single_source_and_fail_safe(monkeypatch):
    monkeypatch.delenv("AGENTIC_EXECUTION_MODE", raising=False)
    committed = load_execution_policy()
    assert committed.mode is ExecutionMode.HUMAN_APPROVAL  # committed default never trades alone
    assert load_execution_policy(environ={"AGENTIC_EXECUTION_MODE": "autonomous"}).mode is ExecutionMode.AUTONOMOUS
    assert load_execution_policy(environ={"AGENTIC_EXECUTION_MODE": "observe"}).mode is ExecutionMode.OBSERVE
    typo = load_execution_policy(environ={"AGENTIC_EXECUTION_MODE": "autonomus"})
    assert typo.mode is ExecutionMode.HUMAN_APPROVAL
    assert typo.mode_source == "fail_safe_invalid_value"
    # Limits can only restrict: negative values fall back to defaults.
    weird = load_execution_policy(environ={}, config={"mode": "AUTONOMOUS", "autonomous": {"max_orders_per_day": -5}})
    assert weird.limits.max_orders_per_day == AutonomyLimits().max_orders_per_day


def test_kill_switch_file_env_and_fail_closed(tmp_path):
    ks = KillSwitch(tmp_path, environ={})
    assert ks.engaged() is False
    ks.engage("operator test", by="reydel")
    assert ks.engaged() is True
    with pytest.raises(ValueError):
        ks.release(by="")
    ks.release(by="reydel", note="ok")
    assert ks.engaged() is False
    # Corrupt state fails closed.
    ks.path.write_text("{not json", encoding="utf-8")
    assert ks.engaged() is True
    # Environment switch cannot be released from the file/dashboard.
    env_ks = KillSwitch(tmp_path / "env", environ={"AGENTIC_KILL_SWITCH": "1"})
    assert env_ks.engaged() is True
    with pytest.raises(PermissionError):
        env_ks.release(by="reydel")


# ----------------------------------------------------- full pipeline, LIVE + FakeBroker
def test_autonomous_live_pipeline_research_to_broker_fill(monkeypatch, tmp_path):
    _mode(monkeypatch, "AUTONOMOUS")
    clock = Clock(OPEN)
    book = Book(ctx(500.0))
    broker = FakeBroker(quotes={"QUAL": QUOTE, "SPY": 500.0})
    store, _executor, approvals, notify, _ = _live_stack(tmp_path, broker, clock=clock, book=book)
    _seed(tmp_path)

    result = _worker(tmp_path, approvals, notify, clock=clock, book=book).run_cycle()

    assert result.proposals_created == 1
    assert len(broker.place_calls) == 1, "autonomous policy must place exactly one order"
    item = approvals.store.all()[0]
    assert item.decided_by.startswith("autonomous_policy")
    assert item.status is LiveApprovalStatus.EXECUTED
    assert item.proposed_action_snapshot["symbol"] == "QUAL"
    assert item.send_time_risk["verdict"] == "PASS"
    assert item.authorization["outcome"] == "AUTO_APPROVE"
    kinds = {row["type"] for row in read_audit(tmp_path)}
    assert {"ORDER_REVIEW_ACCEPTED", "ORDER_SUBMITTED", "ORDER_FILLED"} <= kinds
    assert any(n.kind is NotificationKind.ORDER_AUTHORIZED for n in notify.store.all())
    assert not any(n.kind is NotificationKind.APPROVAL_REQUIRED for n in notify.store.all())
    # Idempotent: a second cycle does not trade again.
    _worker(tmp_path, approvals, notify, clock=clock, book=book).run_cycle()
    assert len(broker.place_calls) == 1


def test_human_approval_mode_escalates_and_never_places(monkeypatch, tmp_path):
    _mode(monkeypatch, "HUMAN_APPROVAL")
    clock = Clock(OPEN)
    book = Book(ctx(500.0))
    broker = FakeBroker(quotes={"QUAL": QUOTE})
    _, _, approvals, notify, _ = _live_stack(tmp_path, broker, clock=clock, book=book)
    _seed(tmp_path)
    _worker(tmp_path, approvals, notify, clock=clock, book=book).run_cycle()
    pending = approvals.store.pending()
    assert len(pending) == 1
    assert pending[0].escalation_reasons == ["human_approval_mode"]
    assert broker.place_calls == []
    # The human APPROVE still flows through the same send-time checks and places once.
    decided = approvals.record_decision(pending[0].approval_id, LiveApprovalStatus.APPROVED, decided_by="reydel")
    assert decided.placed_order is True
    assert len(broker.place_calls) == 1


def test_observe_mode_creates_no_order(monkeypatch, tmp_path):
    _mode(monkeypatch, "OBSERVE")
    clock = Clock(OPEN)
    book = Book(ctx(500.0))
    broker = FakeBroker(quotes={"QUAL": QUOTE})
    _, _, approvals, notify, _ = _live_stack(tmp_path, broker, clock=clock, book=book)
    _seed(tmp_path)
    worker = _worker(tmp_path, approvals, notify, clock=clock, book=book)
    worker.run_cycle()
    assert approvals.store.all() == []
    assert broker.place_calls == []
    rows = [json.loads(line) for line in (tmp_path / "logs" / "order_intake.jsonl").read_text().splitlines()]
    assert rows and rows[-1]["type"] == "PROPOSAL_OBSERVED"
    watch = worker.watch.store.by_ticker("QUAL")
    assert watch is not None and watch.status is WatchStatus.WATCH


def test_placement_switch_off_means_no_broker_write_even_when_autonomous(monkeypatch, tmp_path):
    _mode(monkeypatch, "AUTONOMOUS", placement=False)
    clock = Clock(OPEN)
    book = Book(ctx(500.0))
    broker = FakeBroker(quotes={"QUAL": QUOTE})
    _, _, approvals, notify, _ = _live_stack(tmp_path, broker, clock=clock, book=book)
    _seed(tmp_path)
    _worker(tmp_path, approvals, notify, clock=clock, book=book).run_cycle()
    assert broker.place_calls == []
    assert approvals.store.all()[0].status is LiveApprovalStatus.APPROVED_EXECUTION_DISABLED


# ------------------------------------------------------------------ escalation rules
def test_enhanced_review_is_escalated_then_human_approval_executes(monkeypatch, tmp_path):
    _mode(monkeypatch, "AUTONOMOUS")
    clock = Clock(OPEN)
    context = ctx(1000.0)
    book = Book(context)
    broker = FakeBroker(nav=1000, cash=1000, buying_power=1000, quotes={"MSFT": 100.0})
    _, _, approvals, notify, authority = _live_stack(tmp_path, broker, clock=clock, book=book)
    intake = _intake(approvals, authority, tmp_path, notify)
    action = ProposedAction(
        symbol="MSFT",
        decision=Decision.BUY,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        classification_status=ClassificationStatus.VALIDATED,
        sleeve=Sleeve.CORE_GROWTH,
        current_price=100.0,
        proposed_notional=120.0,  # 12% NAV > 10% → enhanced concentration review
        sector="INFORMATION_TECHNOLOGY",
        liquidity=LiquidityInputs(median_daily_dollar_volume_20d=1e10),
    )
    out, risk = _submit(intake, context, action)
    assert risk.verdict.value == "REQUIRES_ENHANCED_REVIEW"
    assert out.status == "ESCALATED"
    assert any(r.startswith("risk_verdict_requires_human") for r in out.reasons)
    assert broker.place_calls == []
    decided = approvals.record_decision(out.approval.approval_id, LiveApprovalStatus.APPROVED, decided_by="reydel")
    assert decided.placed_order is True  # the human APPROVE is the enhanced review


def test_daily_order_limit_escalates_further_orders(monkeypatch, tmp_path):
    _mode(monkeypatch, "AUTONOMOUS")
    clock = Clock(OPEN)
    context = ctx(10_000.0)
    book = Book(context)
    broker = FakeBroker(nav=10_000, cash=10_000, buying_power=10_000, quotes={"QUAL": QUOTE, "SPY": 500.0})
    policy = ExecutionPolicy(mode=ExecutionMode.AUTONOMOUS, mode_source="test", limits=AutonomyLimits(max_orders_per_day=1))
    notify = NotificationEngine(NotificationStore(tmp_path), now_fn=clock)
    store = ExecutionStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    authority = OrderAuthority(tmp_path, policy=policy, execution_store=store, notify=notify, now_fn=clock)
    _, _, approvals, notify, _ = _live_stack(tmp_path, broker, clock=clock, book=book, authority=authority)
    intake = _intake(approvals, authority, tmp_path, notify)
    first, _ = _submit(intake, context, _qual_action(10_000.0, 0.03))
    assert first.status == "AUTHORIZED" and len(broker.place_calls) == 1
    spy = ProposedAction(
        symbol="SPY",
        decision=Decision.BUY,
        security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
        classification_status=ClassificationStatus.VALIDATED,
        sleeve=Sleeve.CORE_GROWTH,
        current_price=500.0,
        proposed_notional=500.0,
        liquidity=LiquidityInputs(median_daily_dollar_volume_20d=2e10),
    )
    second, _ = _submit(intake, context, spy)
    assert second.status == "ESCALATED"
    assert "daily_order_limit:1" in second.reasons
    assert len(broker.place_calls) == 1


# -------------------------------------------------------------- send-time protection
def test_send_time_risk_gate_rerun_blocks_when_daily_halt_hits(monkeypatch, tmp_path):
    _mode(monkeypatch, "AUTONOMOUS")
    clock = Clock(NIGHT)  # authorize off-hours → deferred to the next send window
    context = ctx(500.0)
    book = Book(context)
    broker = FakeBroker(quotes={"QUAL": QUOTE})
    _, executor, approvals, notify, authority = _live_stack(tmp_path, broker, clock=clock, book=book)
    out, _ = _submit(_intake(approvals, authority, tmp_path, notify), context, _qual_action(500.0))
    assert out.status == "AUTHORIZED"
    assert getattr(out.outcome, "deferred", False) is True
    assert approvals.store.get(out.approval.approval_id).status is LiveApprovalStatus.APPROVED
    # Next morning the book is down >2% from start-of-day: Risk Gate must veto at send time.
    clock.at = OPEN
    book.context = ctx(480.0, start_of_day_nav=500.0)
    assert book.context.daily_risk_halt is True
    outcome = executor.execute_approved(approvals.store.get(out.approval.approval_id))
    assert outcome.placed is False
    assert outcome.reasons[0].startswith("SEND_TIME_RISK_")
    assert "DAILY_RISK_HALT" in outcome.reasons
    assert broker.place_calls == []


def test_market_closed_defers_then_drain_executes_inside_window(monkeypatch, tmp_path):
    _mode(monkeypatch, "AUTONOMOUS")
    clock = Clock(NIGHT)
    context = ctx(500.0)
    book = Book(context)
    broker = FakeBroker(quotes={"QUAL": QUOTE})
    _, executor, approvals, notify, authority = _live_stack(tmp_path, broker, clock=clock, book=book)
    out, _ = _submit(_intake(approvals, authority, tmp_path, notify), context, _qual_action(500.0))
    assert out.status == "AUTHORIZED" and broker.place_calls == []
    services = AgentServices(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        watch=WatchEngine(WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE), now_fn=clock),
        watch_store=WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE),
        approvals=approvals,
        approval_store=approvals.store,
        notify=notify,
        connection=None,
        now_fn=clock,
        executor=executor,
    )
    from agentic_portfolio.agent.session import classify_market_phase

    # 09:31 ET: inside the market but inside the 5-minute opening buffer → still deferred.
    clock.at = datetime(2026, 8, 31, 13, 31, tzinfo=timezone.utc)
    row = build_handlers(services)["APPROVED_EXECUTION_DRAIN"]({"session": classify_market_phase(clock.at), "job": "APPROVED_EXECUTION_DRAIN"})
    assert row["placed"] == 0 and row["deferred"] == 1
    clock.at = OPEN
    row = build_handlers(services)["APPROVED_EXECUTION_DRAIN"]({"session": classify_market_phase(clock.at), "job": "APPROVED_EXECUTION_DRAIN"})
    assert row["placed"] == 1
    assert len(broker.place_calls) == 1


def test_kill_switch_stops_autonomy_and_human_orders(monkeypatch, tmp_path):
    _mode(monkeypatch, "AUTONOMOUS")
    clock = Clock(OPEN)
    context = ctx(500.0)
    book = Book(context)
    broker = FakeBroker(quotes={"QUAL": QUOTE})
    _, _, approvals, notify, authority = _live_stack(tmp_path, broker, clock=clock, book=book)
    authority.kill_switch.engage("operator drill", by="reydel")
    out, _ = _submit(_intake(approvals, authority, tmp_path, notify), context, _qual_action(500.0))
    assert out.status == "ESCALATED" and "kill_switch_engaged" in out.reasons
    decided = approvals.record_decision(out.approval.approval_id, LiveApprovalStatus.APPROVED, decided_by="reydel")
    assert decided.placed_order is False
    assert broker.place_calls == []
    assert any(n.kind is NotificationKind.KILL_SWITCH for n in notify.store.all())


def test_ambiguous_submission_trips_kill_switch(monkeypatch, tmp_path):
    _mode(monkeypatch, "AUTONOMOUS")
    clock = Clock(OPEN)
    context = ctx(500.0)
    broker = FakeBroker(quotes={"QUAL": QUOTE}, place_timeout=True, ambiguous_submit=True, next_status="queued")
    _, _, approvals, notify, authority = _live_stack(tmp_path, broker, clock=clock, book=Book(context))
    out, _ = _submit(_intake(approvals, authority, tmp_path, notify), context, _qual_action(500.0))
    assert out.status == "AUTHORIZED"
    assert authority.kill_switch.engaged() is True
    assert "ambiguous" in (authority.kill_switch.status().reason or "")
    # Nothing else may be sent until a human reconciles and releases the switch.
    spy = ProposedAction(
        symbol="SPY",
        decision=Decision.BUY,
        security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
        classification_status=ClassificationStatus.VALIDATED,
        sleeve=Sleeve.CORE_GROWTH,
        current_price=500.0,
        proposed_notional=25.0,
        liquidity=LiquidityInputs(median_daily_dollar_volume_20d=2e10),
    )
    second, _ = _submit(_intake(approvals, authority, tmp_path, notify), context, spy)
    assert second.status == "ESCALATED" and "kill_switch_engaged" in second.reasons
    assert len(broker.place_calls) == 1


def test_repeated_broker_failures_trip_kill_switch(monkeypatch, tmp_path):
    _mode(monkeypatch, "AUTONOMOUS")
    clock = Clock(OPEN)
    context = ctx(10_000.0)
    broker = FakeBroker(nav=10_000, cash=10_000, buying_power=10_000, quotes={"QUAL": QUOTE, "SPY": 500.0, "VTI": 250.0, "IWM": 200.0}, review_ok=False)
    _, _, approvals, notify, authority = _live_stack(tmp_path, broker, clock=clock, book=Book(context))
    intake = _intake(approvals, authority, tmp_path, notify)
    for symbol, price in (("SPY", 500.0), ("VTI", 250.0), ("IWM", 200.0)):
        action = ProposedAction(
            symbol=symbol,
            decision=Decision.BUY,
            security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
            classification_status=ClassificationStatus.VALIDATED,
            sleeve=Sleeve.CORE_GROWTH,
            current_price=price,
            proposed_notional=200.0,
            liquidity=LiquidityInputs(median_daily_dollar_volume_20d=2e10),
        )
        _submit(intake, context, action)
    assert authority.failure_streak() >= 3
    assert authority.kill_switch.engaged() is True
    assert broker.place_calls == []


def test_wrong_account_context_trips_kill_switch(monkeypatch, tmp_path):
    _mode(monkeypatch, "AUTONOMOUS")
    clock = Clock(OPEN)
    good = ctx(500.0)
    broker = FakeBroker(quotes={"QUAL": QUOTE})
    book = Book(good)
    _, _, approvals, notify, authority = _live_stack(tmp_path, broker, clock=clock, book=book)
    from dataclasses import replace

    book.context = replace(good, account_number="000000000")
    out, _ = _submit(_intake(approvals, authority, tmp_path, notify), good, _qual_action(500.0))
    assert broker.place_calls == []
    assert authority.kill_switch.engaged() is True


# ------------------------------------------------------------------- PAPER == LIVE path
def _paper_quotes(prices: dict[str, float]):
    def source(symbols):
        rows = []
        for s in symbols:
            price = prices.get(str(s).upper())
            if price is None:
                continue
            rows.append({"quote": {"symbol": str(s).upper(), "last_trade_price": str(price), "previous_close": str(price), "bid_price": str(price - 0.05), "ask_price": str(price + 0.05)}})
        return {"data": {"results": rows}}

    return source


def _seed_paper(root: Path):
    from agentic_portfolio.discovery.store import CandidateStore, ResearchQueue
    from agentic_portfolio.runtime import discovery_state_dir

    cand = _candidate("QUAL", sleeve=Sleeve.CORE_GROWTH, score=72.0)
    cand.status = CandidateStatus.PROMOTED_TO_RESEARCH
    cand.priority = DiscoveryPriority.HIGH
    d = discovery_state_dir(root, mode=RuntimeMode.PAPER)
    d.mkdir(parents=True, exist_ok=True)
    CandidateStore(d / "candidates.json", runtime_mode="PAPER").upsert(cand)
    ResearchQueue(d / "research_queue.json", runtime_mode="PAPER").enqueue(
        ResearchQueueEntry(
            queue_id=f"q-{cand.candidate_id}",
            candidate_id=cand.candidate_id,
            symbol="QUAL",
            provisional_sleeve=Sleeve.CORE_GROWTH,
            discovery_score=72.0,
            priority=DiscoveryPriority.HIGH,
            why_research_warranted="Promoted by discovery.",
            required_research_areas=["business_quality"],
            enqueued_at=OPEN.isoformat(),
            freshness_deadline=(OPEN + timedelta(hours=72)).isoformat(),
            status=ResearchQueueStatus.QUEUED,
        )
    )


def test_paper_mode_runs_identical_pipeline_against_paper_broker(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_EXECUTION_MODE", "AUTONOMOUS")
    monkeypatch.delenv("AGENTIC_LIVE_ORDER_PLACEMENT", raising=False)  # LIVE switch is irrelevant to PAPER
    clock = Clock(OPEN)
    from agentic_portfolio.policy import load_account_rules

    account = str(load_account_rules()["account"]["account_number"])
    paper = PaperBroker(tmp_path, account_number=account, quote_source=_paper_quotes({"QUAL": QUOTE, "SPY": 500.0}), starting_cash=500.0, now_fn=clock)
    holder = Book(None)

    def refresh():
        result = refresh_live_portfolio(paper, now=clock(), root=tmp_path, runtime_mode=RuntimeMode.PAPER)
        holder.context = result.context
        return result

    refresh()
    assert holder.context.current_nav == pytest.approx(500.0)
    notify = NotificationEngine(NotificationStore(tmp_path), now_fn=clock)
    store = ExecutionStore(tmp_path, runtime_mode=RuntimeMode.PAPER)
    executor = LiveOrderExecutor(
        store,
        paper,
        root=tmp_path,
        runtime_mode=RuntimeMode.PAPER,
        context_fn=holder,
        regular_hours_fn=_is_regular(clock),
        notify=notify,
        now_fn=clock,
        refresh_fn=refresh,
    )
    approvals = LiveApprovalEngine(LiveApprovalStore(tmp_path, runtime_mode=RuntimeMode.PAPER), now_fn=clock, executor=executor)
    _seed_paper(tmp_path)
    worker = _worker(tmp_path, approvals, notify, clock=clock, book=holder, mode=RuntimeMode.PAPER)
    result = worker.run_cycle()
    assert result.proposals_created == 1
    snapshot = json.loads(paper.path.read_text())
    assert "QUAL" in snapshot["positions"]
    assert snapshot["cash"] == pytest.approx(475.0, abs=0.01)
    refresh()
    held = {p.symbol: p for p in holder.context.positions}
    assert "QUAL" in held and held["QUAL"].quantity > 0
    # Exit through the same intake/authority/executor path.
    authority = executor.authority
    exit_action = _qual_action(holder.context.current_nav, 0.0, decision=Decision.SELL)
    exit_action.proposed_notional = held["QUAL"].market_value
    out, _ = _submit(_intake(approvals, authority, tmp_path, notify), holder.context, exit_action)
    assert out.status == "AUTHORIZED"
    assert "QUAL" not in json.loads(paper.path.read_text())["positions"]
    # The real Robinhood account state was never touched.
    assert not (tmp_path / "state" / "live_execution").exists()
    assert json.loads((tmp_path / "state" / "live_book" / "current.json").read_text())["runtime_mode"] == "PAPER"


def test_paper_broker_refused_in_live_and_real_broker_refused_in_paper(monkeypatch, tmp_path):
    _mode(monkeypatch, "AUTONOMOUS")
    clock = Clock(OPEN)
    context = ctx(500.0)
    paper = PaperBroker(tmp_path / "p", account_number=context.account_number, quote_source=_paper_quotes({"QUAL": QUOTE}), now_fn=clock)
    _, _, approvals, notify, authority = _live_stack(tmp_path, paper, clock=clock, book=Book(context))
    out, _ = _submit(_intake(approvals, authority, tmp_path, notify), context, _qual_action(500.0))
    assert out.outcome.placed is False
    assert "paper_broker_bound_in_live_runtime" in out.outcome.reasons
    monkeypatch.setenv("AGENTIC_LIVE_ORDER_PLACEMENT", "false")
    fake = FakeBroker(quotes={"QUAL": QUOTE})
    store = ExecutionStore(tmp_path / "x", runtime_mode=RuntimeMode.PAPER)
    executor = LiveOrderExecutor(store, fake, root=tmp_path / "x", runtime_mode=RuntimeMode.PAPER, context_fn=lambda: context, regular_hours_fn=lambda: True, now_fn=clock)
    approvals2 = LiveApprovalEngine(LiveApprovalStore(tmp_path / "x", runtime_mode=RuntimeMode.PAPER), now_fn=clock, executor=executor)
    out2, _ = _submit(_intake(approvals2, executor.authority, tmp_path / "x", None), context, _qual_action(500.0))
    assert fake.place_calls == []


# ------------------------------------------------------------- watch path uses the gate
def test_watch_conditions_use_real_risk_gate_and_autonomy(monkeypatch, tmp_path):
    from agentic_portfolio.agent.session import classify_market_phase
    from tests.autonomy_helpers import seed_research

    _mode(monkeypatch, "AUTONOMOUS")
    clock = Clock(OPEN)
    context = ctx(500.0)
    broker = FakeBroker(quotes={"HD": 180.0, "BAD": 50.0})
    _, executor, approvals, notify, _ = _live_stack(tmp_path, broker, clock=clock, book=Book(context))
    seed_research(tmp_path, "HD", 180.0, sector="CONSUMER_DISCRETIONARY")
    seed_research(tmp_path, "BAD", 50.0, sector="UNKNOWN")  # individual equity w/o sector → Risk Gate FAIL
    watch_store = WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    watch = WatchEngine(watch_store, now_fn=clock)
    for ticker, price in (("HD", 180.0), ("BAD", 50.0)):
        watch.upsert_from_candidate(
            ticker=ticker,
            thesis="enter at the open",
            last_price=price,
            off_hours=True,
            session_id="2026-08-28",
            next_session_id="2026-08-31",
            status=WatchStatus.WAITING_FOR_OPEN,
            proposed_notional=25.0,
            desired_allocation_pct=5.0,
        )

    def quotes(tickers):
        return {t: {"price": broker.quotes[t], "spread_bps": 5.0, "dollar_volume": 5e8, "adverse_catalyst": False} for t in tickers if t in broker.quotes}

    services = AgentServices(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        watch=watch,
        watch_store=watch_store,
        approvals=approvals,
        approval_store=approvals.store,
        notify=notify,
        connection=None,
        now_fn=clock,
        quotes_fn=quotes,
        last_context=context,
        executor=executor,
    )
    row = build_handlers(services)["WATCH_CONDITION_MONITOR"]({"session": classify_market_phase(OPEN), "job": "WATCH_CONDITION_MONITOR"})
    assert row["approvals_created"] == 1
    assert [c["symbol"] for c in broker.place_calls] == ["HD"]
    assert watch_store.by_ticker("BAD").status is not WatchStatus.APPROVAL_REQUIRED
    assert all(a.ticker != "BAD" for a in approvals.store.all())


# ------------------------------------------------------------ AI never reaches a broker
def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_ai_layer_has_no_broker_or_authorization_access():
    """AI-facing packages produce decisions only: no broker client, no authority, no approve call."""
    forbidden_modules = ("agentic_portfolio.live_execution", "agentic_portfolio.autonomy")
    for package in ("ai", "research", "decision"):
        for path in (ROOT_SRC / package).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "place_equity_order(" not in text, path
            assert "approvals.authorize(" not in text and "record_decision(" not in text, path
            bad = [m for m in _imports(path) if m.startswith(forbidden_modules)]
            assert bad == [], f"{path} imports {bad}"


def test_only_executor_calls_place_and_ai_budget_unchanged():
    from agentic_portfolio.ai.config import load_ai_config
    from agentic_portfolio.live_execution import placement_call_sites

    assert placement_call_sites() == []
    assert float(load_ai_config()["budget"]["monthly_cap"]) == 10.0
    assert float(load_ai_config()["budget"]["hard_stop"]) == 10.0


# ----------------------------------------------------------- runtime wiring (PAPER)
class _QuoteOnlyFetcher:
    """Read-only market data, like the observation MCP. No account or order methods."""

    def __init__(self, prices):
        self.source = _paper_quotes(prices)

    def get_equity_quotes(self, symbols):
        return self.source(list(symbols) if not isinstance(symbols, str) else [symbols])


def test_agent_runtime_paper_mode_wires_paper_broker_and_kill_switch(monkeypatch, tmp_path):
    from agentic_portfolio.adapters.readonly_runtime import ReadonlyBrokerRuntime
    from agentic_portfolio.agent.connection import ConnectionManager
    from agentic_portfolio.agent.heartbeat import load_health
    from agentic_portfolio.agent.runtime import AgentRuntime

    monkeypatch.setenv("AGENTIC_EXECUTION_MODE", "AUTONOMOUS")
    monkeypatch.setenv("AGENTIC_PAPER_STARTING_CASH", "2500")
    clock = Clock(OPEN)
    fetcher = _QuoteOnlyFetcher({"SPY": 500.0, "QUAL": QUOTE})
    conn = ConnectionManager(bootstrap=lambda **k: ReadonlyBrokerRuntime(bound=True, fetcher=fetcher), root=tmp_path, now_fn=clock)
    runtime = AgentRuntime(tmp_path, runtime_mode=RuntimeMode.PAPER, connection=conn, now_fn=clock, sleep_fn=lambda _s: None, max_cycles=1, ai_allowed=False)
    assert getattr(runtime.services.executor.broker, "is_paper", False) is True
    runtime.run()
    health = load_health(tmp_path)
    assert health["execution"]["policy"]["mode"] == "AUTONOMOUS"
    assert health["kill_switch_engaged"] is False
    assert runtime.services.last_context is not None
    assert runtime.services.last_context.current_nav == pytest.approx(2500.0)
    assert (tmp_path / "state" / "paper_broker" / "account.json").exists()
    # Engaging the kill switch makes the drain job skip without consuming approvals.
    runtime.kill_switch.engage("drill", by="test")
    from agentic_portfolio.agent.session import classify_market_phase

    row = runtime.orchestrator.handlers["APPROVED_EXECUTION_DRAIN"]({"session": classify_market_phase(OPEN), "job": "APPROVED_EXECUTION_DRAIN"})
    assert row["status"] == "SKIPPED" and row["skipped"] == "kill_switch_engaged"


def test_state_root_isolates_paper_from_live(monkeypatch, tmp_path):
    from agentic_portfolio.autonomy.policy import resolve_state_root

    assert resolve_state_root(tmp_path, environ={}, runtime_mode="LIVE") == tmp_path
    assert resolve_state_root(tmp_path, environ={}, runtime_mode="PAPER") == tmp_path / "var" / "paper"
    assert resolve_state_root(tmp_path, environ={"AGENTIC_STATE_ROOT": str(tmp_path / "x")}, runtime_mode="LIVE") == tmp_path / "x"


def test_autonomous_authorization_is_valid_for_first_eligible_session_only(monkeypatch, tmp_path):
    _mode(monkeypatch, "AUTONOMOUS")
    friday_evening = datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc)  # Fri 17:00 ET
    clock = Clock(friday_evening)
    context = ctx(500.0)
    broker = FakeBroker(quotes={"QUAL": QUOTE})
    _, executor, approvals, notify, authority = _live_stack(tmp_path, broker, clock=clock, book=Book(context))
    out, _ = _submit(_intake(approvals, authority, tmp_path, notify), context, _qual_action(500.0))
    assert out.status == "AUTHORIZED" and broker.place_calls == []
    item = approvals.store.get(out.approval.approval_id)
    assert item.expires_at.startswith("2026-08-31T20:00")  # Monday 16:00 ET close
    # Monday morning, inside the send window, facts unchanged: the Risk Gate re-runs and it sends.
    clock.at = OPEN
    outcome = executor.execute_approved(item)
    assert outcome.placed is True
    assert len(broker.place_calls) == 1
    # A Tuesday send of a Monday-morning authorization is stale.
    assert authority._authorization_stale(OPEN, OPEN + timedelta(days=1)) is True
    assert authority._authorization_stale(OPEN, OPEN + timedelta(hours=3)) is False


def test_enhanced_concentration_review_is_never_auto_authorized(monkeypatch, tmp_path):
    """>10% NAV in one security needs ENHANCED_CONCENTRATION_REVIEW; autonomy escalates it."""
    _mode(monkeypatch, "AUTONOMOUS")
    clock = Clock(OPEN)
    context = ctx(10_000.0)
    broker = FakeBroker(nav=10_000, cash=10_000, buying_power=10_000, quotes={"SPY": 500.0})
    _, _, approvals, notify, authority = _live_stack(tmp_path, broker, clock=clock, book=Book(context))
    spy = ProposedAction(
        symbol="SPY",
        decision=Decision.BUY,
        security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
        classification_status=ClassificationStatus.VALIDATED,
        sleeve=Sleeve.CORE_GROWTH,
        current_price=500.0,
        proposed_notional=2_000.0,
        liquidity=LiquidityInputs(median_daily_dollar_volume_20d=2e10),
    )
    out, risk = _submit(_intake(approvals, authority, tmp_path, notify), context, spy)
    assert risk.verdict.value == "REQUIRES_ENHANCED_REVIEW"
    assert out.status == "ESCALATED" and broker.place_calls == []


def test_queued_authorizations_count_against_daily_limits(monkeypatch, tmp_path):
    """Overnight authorizations must not each see an empty day and overshoot the limits at the open."""
    _mode(monkeypatch, "AUTONOMOUS")
    clock = Clock(NIGHT)
    context = ctx(10_000.0)
    broker = FakeBroker(nav=10_000, cash=10_000, buying_power=10_000, quotes={"SPY": 500.0, "VTI": 250.0})
    policy = ExecutionPolicy(mode=ExecutionMode.AUTONOMOUS, mode_source="test", limits=AutonomyLimits(max_buy_notional_pct_nav_per_day=15.0))
    store = ExecutionStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    notify = NotificationEngine(NotificationStore(tmp_path), now_fn=clock)
    authority = OrderAuthority(tmp_path, policy=policy, execution_store=store, notify=notify, now_fn=clock)
    _, _, approvals, notify, _ = _live_stack(tmp_path, broker, clock=clock, book=Book(context), authority=authority)
    intake = _intake(approvals, authority, tmp_path, notify)

    def etf(symbol, price, notional):
        return ProposedAction(
            symbol=symbol,
            decision=Decision.BUY,
            security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
            classification_status=ClassificationStatus.VALIDATED,
            sleeve=Sleeve.CORE_GROWTH,
            current_price=price,
            proposed_notional=notional,
            liquidity=LiquidityInputs(median_daily_dollar_volume_20d=2e10),
        )

    first, _ = _submit(intake, context, etf("SPY", 500.0, 1_000.0))
    assert first.status == "AUTHORIZED"  # 10% NAV, queued for the open
    second, _ = _submit(intake, context, etf("VTI", 250.0, 900.0))
    assert second.status == "ESCALATED"  # 10% queued + 9% > 15% daily buy limit
    assert any(r.startswith("daily_buy_limit") for r in second.reasons)
