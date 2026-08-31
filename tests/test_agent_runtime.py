"""24/7 agent runtime, watchlist, live approvals, and session-aware jobs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_portfolio.adapters.readonly_runtime import ReadonlyBrokerRuntime
from agentic_portfolio.agent.connection import ConnectionManager
from agentic_portfolio.agent.handlers import AgentServices, build_handlers
from agentic_portfolio.agent.heartbeat import load_health
from agentic_portfolio.agent.jobs import catalog
from agentic_portfolio.agent.orchestrator import JobOrchestrator
from agentic_portfolio.agent.runtime import AgentRuntime
from agentic_portfolio.agent.safety import inspect_agent_packages_for_forbidden_calls
from agentic_portfolio.agent.session import MarketPhase, classify_market_phase
from agentic_portfolio.calendar import EASTERN
from agentic_portfolio.live_approval import (
    APPROVED_HOLD,
    LiveApprovalEngine,
    LiveApprovalStatus,
    LiveApprovalStore,
)
from agentic_portfolio.notify import NotificationEngine, NotificationKind, NotificationStore
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, RuntimeMode
from agentic_portfolio.watch import ReassessTrigger, WatchEngine, WatchStatus, WatchStore

FRIDAY_OPEN = datetime(2026, 8, 28, 10, 0, tzinfo=EASTERN)
FRIDAY_PRE = datetime(2026, 8, 28, 7, 0, tzinfo=EASTERN)
FRIDAY_POST = datetime(2026, 8, 28, 16, 30, tzinfo=EASTERN)
FRIDAY_NIGHT = datetime(2026, 8, 28, 21, 0, tzinfo=EASTERN)
SATURDAY = datetime(2026, 8, 29, 14, 0, tzinfo=EASTERN)
LABOR_DAY = datetime(2026, 9, 7, 12, 0, tzinfo=EASTERN)


def _services(tmp_path: Path, *, now, quotes=None, candidates=None, exhausted=False, bootstrap=None):
    watch_store = WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    approval_store = LiveApprovalStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    notify = NotificationEngine(NotificationStore(tmp_path), now_fn=now)
    conn = ConnectionManager(
        bootstrap=bootstrap or (lambda **kwargs: ReadonlyBrokerRuntime(bound=True)),
        notify=notify,
        root=tmp_path,
        now_fn=now,
    )
    journal = tmp_path / "logs" / "agent.jsonl"
    return AgentServices(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        watch=WatchEngine(watch_store, journal=journal, now_fn=now),
        watch_store=watch_store,
        approvals=LiveApprovalEngine(approval_store, journal=journal, now_fn=now),
        approval_store=approval_store,
        notify=notify,
        connection=conn,
        now_fn=now,
        quotes_fn=quotes,
        candidates_fn=candidates,
        budget_exhausted=exhausted,
        ai_allowed=not exhausted,
    )


def test_session_phases():
    assert classify_market_phase(FRIDAY_OPEN).phase is MarketPhase.MARKET_OPEN
    assert classify_market_phase(FRIDAY_OPEN).executable_liquidity is True
    assert classify_market_phase(FRIDAY_PRE).phase is MarketPhase.PREMARKET
    assert classify_market_phase(FRIDAY_POST).phase is MarketPhase.AFTER_CLOSE
    assert classify_market_phase(FRIDAY_NIGHT).phase is MarketPhase.OVERNIGHT
    assert classify_market_phase(SATURDAY).phase is MarketPhase.WEEKEND
    assert classify_market_phase(LABOR_DAY).phase is MarketPhase.HOLIDAY
    assert classify_market_phase(SATURDAY).executable_liquidity is False


def test_runtime_stays_alive_across_job_failures(tmp_path):
    now = lambda: SATURDAY
    services = _services(tmp_path, now=now)
    handlers = build_handlers(services)
    handlers["HEARTBEAT"] = lambda ctx: (_ for _ in ()).throw(RuntimeError("boom"))
    runtime = AgentRuntime(
        tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        now_fn=now,
        sleep_fn=lambda _s: None,
        max_cycles=3,
        services=services,
        handlers=handlers,
        connection=services.connection,
        ai_allowed=False,
    )
    runtime.run()
    assert runtime.cycles == 3
    assert runtime.running is False
    health = load_health(tmp_path)
    assert health["agent"] == "OFFLINE"
    boom = runtime.orchestrator.state["jobs"]["HEARTBEAT"]
    assert boom["status"] == "ERROR"
    assert boom["placement_attempted"] is False


def test_off_market_and_weekend_jobs_execute(tmp_path):
    now = lambda: SATURDAY
    orch = JobOrchestrator(tmp_path, handlers=build_handlers(_services(tmp_path, now=now)), now_fn=now)
    due = orch.due_jobs(SATURDAY)
    assert "WEEKEND_SESSION_ANALYSIS" in due
    assert "WEEKEND_WATCH_CONSTRUCT" in due
    assert "MARKET_OPEN_CONDITIONAL_VALIDATE" not in due
    rows = orch.tick(SATURDAY)
    names = {row["job"] for row in rows}
    assert "WEEKEND_SESSION_ANALYSIS" in names
    assert all(row.get("placement_attempted") is False for row in rows)

    pre = JobOrchestrator(tmp_path / "pre", now_fn=lambda: FRIDAY_PRE)
    assert "PREMARKET_NEWS" in pre.due_jobs(FRIDAY_PRE)
    post = JobOrchestrator(tmp_path / "post", now_fn=lambda: FRIDAY_POST)
    assert "POSTMARKET_CLOSE_ANALYSIS" in post.due_jobs(FRIDAY_POST)
    night = JobOrchestrator(tmp_path / "night", now_fn=lambda: FRIDAY_NIGHT)
    assert "OVERNIGHT_NEWS" in night.due_jobs(FRIDAY_NIGHT)
    holiday = JobOrchestrator(tmp_path / "hol", now_fn=lambda: LABOR_DAY)
    assert "WEEKEND_SESSION_ANALYSIS" in holiday.due_jobs(LABOR_DAY)


def test_latest_session_analysis_creates_watch_and_persists(tmp_path):
    now = lambda: SATURDAY
    candidates = lambda: [{"ticker": "QUAL", "score": 77.0, "thesis": "Quality vs cash", "price": 50.0, "name": "Quality Co"}]
    services = _services(tmp_path, now=now, candidates=candidates)
    orch = JobOrchestrator(tmp_path, handlers=build_handlers(services), now_fn=now)
    row = orch.run_job("WEEKEND_SESSION_ANALYSIS", now=SATURDAY)
    assert row["status"] == "OK"
    item = services.watch_store.by_ticker("QUAL")
    assert item is not None
    assert item.status in {WatchStatus.WATCH, WatchStatus.WAITING_FOR_OPEN, WatchStatus.DISCOVERED}
    assert item.conditional_plan is not None
    restarted = WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    again = restarted.by_ticker("QUAL")
    assert again is not None
    assert again.watch_id == item.watch_id
    assert again.research_thesis == "Quality vs cash"


def test_unchanged_watch_does_not_spend_ai(tmp_path):
    now = lambda: FRIDAY_OPEN
    services = _services(tmp_path, now=now)
    item = services.watch.upsert_from_candidate(ticker="QUAL", thesis="hold", last_price=50.0, status=WatchStatus.WATCH)
    context = {"ticker": "QUAL", "thesis": "hold", "price": 50.0}
    allow, why = services.watch.should_spend_ai(item, context=context, triggers=[], ai_allowed=True, budget_exhausted=False)
    assert allow is False
    assert why in {"unchanged_context", "no_material_change", "cooldown"}


def test_material_event_triggers_reassessment(tmp_path):
    now = lambda: FRIDAY_OPEN
    services = _services(tmp_path, now=now)
    item = services.watch.upsert_from_candidate(ticker="QUAL", thesis="hold", last_price=50.0, status=WatchStatus.WATCH)
    triggers = services.watch.detect_triggers(item, price=60.0, regular_hours_open=True)
    assert ReassessTrigger.PRICE_MOVE in triggers
    allow, why = services.watch.should_spend_ai(
        item,
        context={"ticker": "QUAL", "price": 60.0, "thesis": "hold"},
        triggers=triggers,
        ai_allowed=True,
        budget_exhausted=False,
    )
    assert allow is True
    assert why == "material_event"


def test_market_open_validates_conditional_plan(tmp_path):
    now = lambda: FRIDAY_OPEN

    def quotes(tickers):
        return {"QUAL": {"price": 49.0, "spread_bps": 10.0, "dollar_volume": 5_000_000, "adverse_catalyst": False}}

    services = _services(tmp_path, now=now, quotes=quotes)
    services.watch.upsert_from_candidate(
        ticker="QUAL",
        thesis="next session if cheap",
        last_price=50.0,
        off_hours=True,
        session_id="2026-08-27",
        next_session_id="2026-08-28",
        status=WatchStatus.WAITING_FOR_OPEN,
    )
    orch = JobOrchestrator(tmp_path, handlers=build_handlers(services), now_fn=now)
    row = orch.run_job("MARKET_OPEN_CONDITIONAL_VALIDATE", now=FRIDAY_OPEN)
    assert row["approvals_created"] == 1
    pending = services.approval_store.pending()
    assert len(pending) == 1
    assert pending[0].ticker == "QUAL"
    assert pending[0].placed_order is False
    assert services.watch_store.by_ticker("QUAL").status is WatchStatus.APPROVAL_REQUIRED


def test_invalid_condition_does_not_create_approval(tmp_path):
    now = lambda: FRIDAY_OPEN

    def quotes(tickers):
        return {"QUAL": {"price": 80.0, "spread_bps": 10.0, "dollar_volume": 5_000_000, "adverse_catalyst": False}}

    services = _services(tmp_path, now=now, quotes=quotes)
    services.watch.upsert_from_candidate(
        ticker="QUAL",
        last_price=50.0,
        off_hours=True,
        status=WatchStatus.WAITING_FOR_OPEN,
    )
    orch = JobOrchestrator(tmp_path, handlers=build_handlers(services), now_fn=now)
    row = orch.run_job("MARKET_OPEN_CONDITIONAL_VALIDATE", now=FRIDAY_OPEN)
    assert row["approvals_created"] == 0
    assert services.approval_store.pending() == []
    assert services.watch_store.by_ticker("QUAL").status is not WatchStatus.APPROVAL_REQUIRED


def test_off_hours_does_not_treat_liquidity_as_executable(tmp_path):
    now = lambda: SATURDAY

    def quotes(tickers):
        return {"QUAL": {"price": 49.0, "spread_bps": 10.0, "dollar_volume": 5_000_000}}

    services = _services(tmp_path, now=now, quotes=quotes)
    services.watch.upsert_from_candidate(ticker="QUAL", last_price=50.0, off_hours=True, status=WatchStatus.WAITING_FOR_OPEN)
    orch = JobOrchestrator(tmp_path, handlers=build_handlers(services), now_fn=now)
    row = orch.run_job("MARKET_OPEN_CONDITIONAL_VALIDATE", now=SATURDAY)
    assert row.get("create_approval") is False or row.get("approvals_created") in {0, None}
    assert services.approval_store.pending() == []


def test_approval_persists_expires_and_cannot_place(tmp_path):
    frozen = {"t": datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)}

    def now():
        return frozen["t"]

    store = LiveApprovalStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    engine = LiveApprovalEngine(store, journal=tmp_path / "logs" / "approval.jsonl", now_fn=now)
    item = engine.create(ticker="QUAL", proposed_action="BUY", current_quote=50.0, ttl_hours=1)
    assert store.get(item.approval_id).status is LiveApprovalStatus.PENDING
    frozen["t"] = frozen["t"] + timedelta(hours=2)
    expired = engine.expire_due()
    assert len(expired) == 1
    assert store.get(item.approval_id).status is LiveApprovalStatus.EXPIRED
    with pytest.raises(Exception):
        engine.attempt_place(item.approval_id)
    item2 = engine.create(ticker="MSFT", proposed_action="BUY", ttl_hours=24)
    decided = engine.record_decision(item2.approval_id, LiveApprovalStatus.APPROVED, note="ok")
    assert decided.status is APPROVED_HOLD
    assert decided.placed_order is False
    assert decided.broker_submitted is False
    assert decided.execution_attempted is False
    assert LIVE_ORDER_PLACEMENT is False


def test_approve_reject_dashboard_does_not_place(tmp_path):
    from tests.test_dashboard import _client, _decide_payload

    store = LiveApprovalStore(tmp_path, runtime_mode=RuntimeMode.PAPER)
    engine = LiveApprovalEngine(store, journal=tmp_path / "logs" / "approval.jsonl")
    item = engine.create(ticker="QUAL", proposed_action="BUY", reason="starter", ai_rationale="advisory", supporting_thesis="quality")
    client = _client(tmp_path)
    html = client.get(f"/approvals/{item.approval_id}")
    assert html.status_code == 200
    text = html.get_data(as_text=True)
    assert "APPROVE" in text
    assert "REJECT" in text
    home = client.get("/").get_data(as_text=True)
    assert "TRADE APPROVAL REQUIRED" in home
    assert "QUAL" in client.get("/approvals").get_data(as_text=True)
    res = client.post(f"/api/approvals/{item.approval_id}/approve", json=_decide_payload(client, note="no money movement"))
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["placed_order"] is False
    assert body["packet"]["status"] == LiveApprovalStatus.APPROVED_AWAITING_EXECUTION_IMPLEMENTATION.value
    assert body["packet"]["broker_submitted"] is False
    stored = store.get(item.approval_id)
    assert stored.status is APPROVED_HOLD
    assert stored.placed_order is False
    other = engine.create(ticker="IONQ", proposed_action="BUY")
    rej = client.post(f"/api/approvals/{other.approval_id}/reject", json=_decide_payload(client, note="no"))
    assert rej.get_json()["packet"]["status"] == LiveApprovalStatus.REJECTED.value
    assert "place_equity_order" not in (tmp_path / "logs" / "approval.jsonl").read_text(encoding="utf-8")


def test_auth_loss_fails_closed_and_recovery_works(tmp_path):
    state = {"bound": False}

    def bootstrap(**kwargs):
        if state["bound"]:
            return ReadonlyBrokerRuntime(bound=True)
        return ReadonlyBrokerRuntime(bound=False, initialization_error="auth lost")

    notify = NotificationEngine(NotificationStore(tmp_path))
    conn = ConnectionManager(bootstrap=bootstrap, notify=notify, root=tmp_path)
    with pytest.raises(Exception):
        conn.ensure()
    assert conn.connected is False
    kinds = {n.kind for n in notify.store.all()}
    assert NotificationKind.BROKER_CONNECTION_LOST in kinds
    state["bound"] = True
    recovered = conn.ensure(force=True)
    assert recovered.bound is True
    assert conn.connected is True


def test_ai_budget_exhaustion_does_not_stop_runtime(tmp_path):
    now = lambda: SATURDAY
    calls = {"n": 0}

    def ai_call(*_a, **_k):
        calls["n"] += 1
        return {"cost": 1.0, "thesis": "should not run"}

    services = _services(tmp_path, now=now, exhausted=True)
    services.ai_call_fn = ai_call
    runtime = AgentRuntime(
        tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        now_fn=now,
        sleep_fn=lambda _s: None,
        max_cycles=2,
        services=services,
        handlers=build_handlers(services),
        connection=services.connection,
        budget_exhausted=True,
        ai_allowed=False,
    )
    runtime.run()
    assert runtime.cycles == 2
    assert calls["n"] == 0
    health = load_health(tmp_path)
    assert health["LIVE_ORDER_PLACEMENT"] is False


def test_no_paper_contamination_and_no_live_order_path(tmp_path):
    live = WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    paper = WatchStore(tmp_path, runtime_mode=RuntimeMode.PAPER)
    WatchEngine(live).upsert_from_candidate(ticker="QUAL", thesis="live")
    WatchEngine(paper).upsert_from_candidate(ticker="NVDA", thesis="paper")
    assert live.by_ticker("QUAL") is not None
    assert live.by_ticker("NVDA") is None
    assert paper.by_ticker("NVDA") is not None
    assert paper.by_ticker("QUAL") is None
    assert inspect_agent_packages_for_forbidden_calls() == []
    assert LIVE_ORDER_PLACEMENT is False
    names = {spec.name for spec in catalog()}
    assert "WEEKEND_SESSION_ANALYSIS" in names
    assert "MARKET_OPEN_CONDITIONAL_VALIDATE" in names


def test_malformed_candidate_is_rejected_before_ai(tmp_path):
    now = lambda: SATURDAY
    services = _services(tmp_path, now=now, candidates=lambda: [None, {"ticker": "FAKE"}, {"ticker": "QUAL", "score": 70}])
    orch = JobOrchestrator(tmp_path, handlers=build_handlers(services), now_fn=now)
    row = orch.run_job("WEEKEND_SESSION_ANALYSIS", now=SATURDAY)
    assert row["status"] == "OK"
    assert services.watch_store.by_ticker("FAKE") is None
    assert services.watch_store.by_ticker("QUAL") is not None
