"""24/7 agent runtime, watchlist, live approvals, and session-aware jobs."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_portfolio.adapters.portfolio_facts import LiveDataUnavailable, StaticPortfolioFetcher
from agentic_portfolio.adapters.readonly_runtime import ReadonlyBrokerRuntime
from agentic_portfolio.agent.activity import read_activity
from agentic_portfolio.agent.connection import ConnectionManager
from agentic_portfolio.agent.handlers import AgentServices, build_handlers
from agentic_portfolio.agent.heartbeat import load_health
from agentic_portfolio.agent.jobs import catalog, specs_by_name
from agentic_portfolio.agent.orchestrator import JobOrchestrator
from agentic_portfolio.agent.runtime import AgentRuntime, refresh_live_from_connection
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
from agentic_portfolio.watch import ConditionalPlan, ReassessTrigger, WatchEngine, WatchItem, WatchStatus, WatchStore
from agentic_portfolio.watch.types import parse_iso

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

    open_orch = JobOrchestrator(tmp_path / "open", now_fn=lambda: FRIDAY_OPEN)
    open_due = open_orch.due_jobs(FRIDAY_OPEN)
    assert "RESEARCH_QUEUE_WORKER" in open_due
    assert "CANDIDATE_DISCOVERY" in open_due
    assert "AI_REASSESS_IF_WARRANTED" in open_due
    preview = {row["job"]: row for row in open_orch.scheduled_preview(FRIDAY_OPEN)}
    assert preview["RESEARCH_QUEUE_WORKER"]["valid_for_phase"] is True
    assert preview["CANDIDATE_DISCOVERY"]["mode"] == "lightweight"
    assert preview["CANDIDATE_DISCOVERY"]["every_minutes"] == 15


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


def test_waiting_for_open_schedules_next_regular_open_not_sleeve_interval(tmp_path):
    from datetime import date, time

    pre = datetime(2026, 9, 1, 8, 49, tzinfo=EASTERN)
    services = _services(tmp_path, now=lambda: pre)
    item = services.watch.upsert_from_candidate(
        ticker="HD",
        thesis="keep watching",
        last_price=180.0,
        status=WatchStatus.WATCH,
        off_hours=True,
        prepare_conditional_plan=False,
        sleeve="OPPORTUNISTIC",
    )
    assert item.status is WatchStatus.WAITING_FOR_OPEN
    nxt = parse_iso(item.next_review_at)
    assert nxt is not None
    local = nxt.astimezone(EASTERN)
    assert local.date() == date(2026, 9, 1)
    assert local.time() == time(9, 30)
    # Opportunistic WATCH interval is 48h; that must not apply to WAITING_FOR_OPEN.
    assert local.date() != date(2026, 9, 3)


def test_watching_during_rth_uses_sleeve_interval(tmp_path):
    rth = datetime(2026, 9, 1, 10, 30, tzinfo=EASTERN)
    services = _services(tmp_path, now=lambda: rth)
    item = services.watch.upsert_from_candidate(
        ticker="HD",
        thesis="keep watching",
        last_price=180.0,
        status=WatchStatus.WATCH,
        off_hours=False,
        prepare_conditional_plan=False,
        sleeve="OPPORTUNISTIC",
    )
    assert item.status is WatchStatus.WATCH
    nxt = parse_iso(item.next_review_at)
    assert nxt is not None
    hours = (nxt - rth).total_seconds() / 3600.0
    assert 40.0 <= hours <= 56.0


def _persist_waiting_for_open(
    tmp_path: Path,
    *,
    ticker="HD",
    next_review: str,
    thesis="keep watching HD",
    sleeve="OPPORTUNISTIC",
    last_updated="2026-09-01T08:00:00-04:00",
    watch_id="w-hd-pre-fix",
    status=WatchStatus.WAITING_FOR_OPEN,
    plan: bool = False,
) -> WatchStore:
    store = WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    item = WatchItem(
        watch_id=watch_id,
        ticker=ticker,
        status=status,
        created_at="2026-09-01T08:00:00-04:00",
        last_updated=last_updated,
        research_thesis=thesis,
        invalidating_conditions=["break of thesis"],
        sleeve=sleeve,
        source_candidate_score=61.2,
        expiration="2026-09-15T00:00:00+00:00",
        next_review_at=next_review,
        last_ai_at="2026-08-31T16:00:00+00:00",
        last_ai_cost=0.12,
        last_context_hash="abc123",
        last_price=180.0,
        LIVE_ORDER_PLACEMENT=False,
    )
    if plan:
        item.conditional_plan = ConditionalPlan(
            max_price=181.8,
            max_spread_bps=50.0,
            min_dollar_volume=1_000_000.0,
            require_no_adverse_catalyst=True,
            require_cash_available=True,
            require_risk_gate_pass=True,
            require_regular_hours_quotes=True,
        )
    store.save(item)
    return store


def test_waiting_for_open_pre_fix_rows_migrate_on_engine_init(tmp_path):
    from datetime import date, time

    pre = datetime(2026, 9, 1, 8, 49, tzinfo=EASTERN)
    store = _persist_waiting_for_open(tmp_path, next_review="2026-09-03T12:49:00+00:00")
    watching = WatchItem(
        watch_id="w-msft-watch",
        ticker="MSFT",
        status=WatchStatus.WATCH,
        created_at="2026-09-01T08:00:00-04:00",
        last_updated="2026-09-01T08:00:00-04:00",
        research_thesis="core compounder",
        sleeve="CORE_GROWTH",
        next_review_at="2026-09-08T08:00:00-04:00",
        expiration="2026-09-15T00:00:00+00:00",
        LIVE_ORDER_PLACEMENT=False,
    )
    store.save(watching)

    engine = WatchEngine(store, now_fn=lambda: pre)
    hd = store.by_ticker("HD")
    nxt = parse_iso(hd.next_review_at)
    local = nxt.astimezone(EASTERN)
    assert hd.status is WatchStatus.WAITING_FOR_OPEN
    assert local.date() == date(2026, 9, 1)
    assert local.time() == time(9, 30)
    assert hd.research_thesis == "keep watching HD"
    assert hd.invalidating_conditions == ["break of thesis"]
    assert hd.sleeve == "OPPORTUNISTIC"
    assert hd.source_candidate_score == 61.2
    assert hd.expiration == "2026-09-15T00:00:00+00:00"
    assert hd.last_ai_at == "2026-08-31T16:00:00+00:00"
    assert hd.last_ai_cost == 0.12
    assert hd.last_context_hash == "abc123"
    assert hd.LIVE_ORDER_PLACEMENT is False
    msft = store.by_ticker("MSFT")
    assert msft.status is WatchStatus.WATCH
    assert msft.next_review_at == "2026-09-08T08:00:00-04:00"
    allow, _why = engine.should_spend_ai(
        hd,
        context={"ticker": "HD", "thesis": hd.research_thesis, "price": 180.0},
        triggers=[],
        ai_allowed=True,
        budget_exhausted=False,
    )
    assert allow is False


def test_waiting_for_open_migrates_on_runtime_startup(tmp_path):
    from datetime import date, time

    pre = datetime(2026, 9, 1, 8, 49, tzinfo=EASTERN)
    _persist_waiting_for_open(tmp_path, next_review="2026-09-03T12:49:00+00:00")
    runtime = AgentRuntime(
        tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        now_fn=lambda: pre,
        sleep_fn=lambda _s: None,
        max_cycles=0,
    )
    hd = runtime.services.watch_store.by_ticker("HD")
    local = parse_iso(hd.next_review_at).astimezone(EASTERN)
    assert local.date() == date(2026, 9, 1)
    assert local.time() == time(9, 30)
    assert hd.research_thesis == "keep watching HD"
    assert hd.last_ai_at == "2026-08-31T16:00:00+00:00"
    assert LIVE_ORDER_PLACEMENT is False


def test_waiting_for_open_stale_past_review_becomes_next_regular_open(tmp_path):
    from datetime import date, time

    pre = datetime(2026, 9, 1, 8, 49, tzinfo=EASTERN)
    store = _persist_waiting_for_open(tmp_path, next_review="2026-08-31T13:30:00+00:00")
    WatchEngine(store, now_fn=lambda: pre)
    local = parse_iso(store.by_ticker("HD").next_review_at).astimezone(EASTERN)
    assert local.date() == date(2026, 9, 1)
    assert local.time() == time(9, 30)


def test_waiting_for_open_after_hours_migrates_to_next_trading_open(tmp_path):
    from datetime import date, time

    friday_after = datetime(2026, 9, 4, 17, 0, tzinfo=EASTERN)
    store = _persist_waiting_for_open(tmp_path, next_review="2026-09-06T17:00:00-04:00")
    WatchEngine(store, now_fn=lambda: friday_after)
    local = parse_iso(store.by_ticker("HD").next_review_at).astimezone(EASTERN)
    assert local.date() == date(2026, 9, 8)
    assert local.time() == time(9, 30)


def test_waiting_for_open_weekend_and_holiday_skip_to_next_session(tmp_path):
    from datetime import date, time

    saturday = datetime(2026, 9, 5, 12, 0, tzinfo=EASTERN)
    store = _persist_waiting_for_open(tmp_path, next_review="2026-09-07T12:00:00-04:00")
    WatchEngine(store, now_fn=lambda: saturday)
    local = parse_iso(store.by_ticker("HD").next_review_at).astimezone(EASTERN)
    assert local.date() == date(2026, 9, 8)
    assert local.time() == time(9, 30)

    holiday_root = tmp_path / "holiday"
    labor = datetime(2026, 9, 7, 12, 0, tzinfo=EASTERN)
    holiday_store = _persist_waiting_for_open(holiday_root, next_review="2026-09-09T12:00:00-04:00")
    WatchEngine(holiday_store, now_fn=lambda: labor)
    holiday_local = parse_iso(holiday_store.by_ticker("HD").next_review_at).astimezone(EASTERN)
    assert holiday_local.date() == date(2026, 9, 8)
    assert holiday_local.time() == time(9, 30)


def test_waiting_for_open_migration_is_idempotent_and_skips_correct_rows(tmp_path):
    pre = datetime(2026, 9, 1, 8, 49, tzinfo=EASTERN)
    store = _persist_waiting_for_open(tmp_path, next_review="2026-09-03T12:49:00+00:00")
    engine = WatchEngine(store, now_fn=lambda: pre)
    hd = store.by_ticker("HD")
    first_review = hd.next_review_at
    first_updated = hd.last_updated
    assert engine.reconcile_waiting_for_open_schedules() == []
    again = store.by_ticker("HD")
    assert again.next_review_at == first_review
    assert again.last_updated == first_updated
    WatchEngine(store, now_fn=lambda: pre)
    third = store.by_ticker("HD")
    assert third.next_review_at == first_review
    assert third.last_updated == first_updated
    assert third.last_ai_at == "2026-08-31T16:00:00+00:00"


def test_rth_liquidity_fail_does_not_keep_stale_opportunistic_review(tmp_path):
    """Production HD: WAITING_FOR_OPEN + Sep 3 stamp → WAITING_FOR_LIQUIDITY cannot keep Sep 3."""
    from datetime import date

    rth = datetime(2026, 9, 1, 10, 0, tzinfo=EASTERN)
    stale = "2026-09-03T04:11:00+00:00"
    store = _persist_waiting_for_open(tmp_path, next_review=stale, plan=True)
    engine = WatchEngine(store, now_fn=lambda: rth)
    hd = store.by_ticker("HD")
    hd.status = WatchStatus.WAITING_FOR_OPEN
    hd.next_review_at = stale
    store.save(hd)

    result = engine.evaluate_conditions(
        hd,
        regular_hours_open=True,
        price=180.0,
        spread_bps=10.0,
        dollar_volume=100_000,
        adverse_catalyst=False,
        cash_available=True,
        risk_pass=True,
    )
    assert result["remain"] == WatchStatus.WAITING_FOR_LIQUIDITY.value
    hd = store.by_ticker("HD")
    assert hd.status is WatchStatus.WAITING_FOR_LIQUIDITY
    nxt = parse_iso(hd.next_review_at)
    assert nxt is not None
    assert nxt.astimezone(EASTERN).date() != date(2026, 9, 3)
    hours = (nxt - rth).total_seconds() / 3600.0
    assert 0 < hours <= 0.5
    assert hd.sleeve == "OPPORTUNISTIC"
    assert hd.research_thesis == "keep watching HD"
    assert hd.expiration == "2026-09-15T00:00:00+00:00"
    assert hd.last_ai_at == "2026-08-31T16:00:00+00:00"
    assert hd.LIVE_ORDER_PLACEMENT is False
    allow, _why = engine.should_spend_ai(
        hd,
        context={"ticker": "HD", "thesis": hd.research_thesis, "price": 180.0},
        triggers=[],
        ai_allowed=True,
        budget_exhausted=False,
    )
    assert allow is False


def test_rth_startup_then_liquidity_fail_clears_stale_sep3(tmp_path):
    from datetime import date

    rth = datetime(2026, 9, 1, 10, 0, tzinfo=EASTERN)
    stale = "2026-09-03T04:11:00+00:00"
    _persist_waiting_for_open(tmp_path, next_review=stale, plan=True)

    def quotes(tickers):
        return {"HD": {"price": 180.0, "spread_bps": 10.0, "dollar_volume": 100_000, "adverse_catalyst": False}}

    services = _services(tmp_path, now=lambda: rth, quotes=quotes)
    orch = JobOrchestrator(tmp_path, handlers=build_handlers(services), now_fn=lambda: rth)
    row = orch.run_job("MARKET_OPEN_CONDITIONAL_VALIDATE", now=rth)
    assert row["approvals_created"] == 0
    hd = services.watch_store.by_ticker("HD")
    assert hd.status is WatchStatus.WAITING_FOR_LIQUIDITY
    nxt = parse_iso(hd.next_review_at)
    assert nxt.astimezone(EASTERN).date() != date(2026, 9, 3)
    hours = (nxt - rth).total_seconds() / 3600.0
    assert 0 < hours <= 0.5


def test_conditional_wait_states_schedule_intrasession_not_sleeve_interval(tmp_path):
    rth = datetime(2026, 9, 1, 10, 0, tzinfo=EASTERN)
    store = _persist_waiting_for_open(
        tmp_path,
        next_review="2026-09-03T04:11:00+00:00",
        plan=True,
    )
    engine = WatchEngine(store, now_fn=lambda: rth)
    hd = store.by_ticker("HD")
    hd.next_review_at = "2026-09-03T04:11:00+00:00"
    engine.set_status(hd, WatchStatus.WAITING_FOR_PRICE, reason="conditions_failed:price")
    price_hours = (parse_iso(store.by_ticker("HD").next_review_at) - rth).total_seconds() / 3600.0
    assert 0 < price_hours <= 0.5
    engine.set_status(hd, WatchStatus.WAITING_FOR_CATALYST, reason="conditions_failed:catalyst")
    cat_hours = (parse_iso(store.by_ticker("HD").next_review_at) - rth).total_seconds() / 3600.0
    assert 0 < cat_hours <= 0.5
    assert store.by_ticker("HD").last_ai_at == "2026-08-31T16:00:00+00:00"


def test_market_open_trigger_does_not_spend_ai_without_material_evidence(tmp_path):
    now = lambda: FRIDAY_OPEN
    services = _services(tmp_path, now=now)
    item = services.watch.upsert_from_candidate(
        ticker="HD",
        thesis="keep watching",
        last_price=180.0,
        off_hours=True,
        prepare_conditional_plan=False,
        status=WatchStatus.WAITING_FOR_OPEN,
    )
    triggers = services.watch.detect_triggers(item, price=180.0, regular_hours_open=True)
    assert ReassessTrigger.MARKET_OPEN_AFTER_OFFHOURS in triggers
    allow, why = services.watch.should_spend_ai(
        item,
        context={"ticker": "HD", "price": 180.0, "thesis": "keep watching"},
        triggers=triggers,
        ai_allowed=True,
        budget_exhausted=False,
    )
    assert allow is False
    assert why in {"unchanged_context", "no_material_change"}
    promoted = services.watch.promote_waiting_for_open(regular_hours_open=True)
    assert any(p.ticker == "HD" for p in promoted)
    assert services.watch_store.by_ticker("HD").status is WatchStatus.WATCH


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
    assert body["packet"]["status"] == LiveApprovalStatus.APPROVED_EXECUTION_DISABLED.value
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
    assert NotificationKind.BROKER_CONNECTION_RESTORED in {n.kind for n in notify.store.all()}


def test_healthy_broker_reconnect_does_not_force_reinitialize(tmp_path):
    from agentic_portfolio.agent.handlers import AgentServices, build_handlers
    from agentic_portfolio.agent.session import classify_market_phase

    calls = {"n": 0}

    def bootstrap(**kwargs):
        calls["n"] += 1
        return ReadonlyBrokerRuntime(bound=True)

    notify = NotificationEngine(NotificationStore(tmp_path), now_fn=lambda: SATURDAY)
    conn = ConnectionManager(bootstrap=bootstrap, notify=notify, root=tmp_path, now_fn=lambda: SATURDAY)
    conn.ensure()
    assert conn.connected is True
    first = calls["n"]
    services = AgentServices(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        watch=WatchEngine(WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE), now_fn=lambda: SATURDAY),
        watch_store=WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE),
        approvals=LiveApprovalEngine(LiveApprovalStore(tmp_path, runtime_mode=RuntimeMode.LIVE), now_fn=lambda: SATURDAY),
        approval_store=LiveApprovalStore(tmp_path, runtime_mode=RuntimeMode.LIVE),
        notify=notify,
        connection=conn,
        now_fn=lambda: SATURDAY,
    )
    handlers = build_handlers(services)
    session = classify_market_phase(SATURDAY)
    row = handlers["BROKER_RECONNECT"]({"job": "BROKER_RECONNECT", "session": session})
    assert row["status"] == "OK"
    assert row.get("reconnected") is False
    assert calls["n"] == first
    restores = [n for n in notify.store.all() if n.kind is NotificationKind.BROKER_CONNECTION_RESTORED]
    assert len(restores) == 1


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


FORBIDDEN_MCP_TOOLS = (
    "place_equity_order",
    "review_equity_order",
    "cancel_equity_order",
    "place_option_order",
    "review_option_order",
    "preview_crypto_order",
    "place_crypto_order",
    "cancel_crypto_order",
)

WRITE_MARKERS = FORBIDDEN_MCP_TOOLS + ("deposit", "withdrawal", "withdraw", "transfer")


def _msft_position():
    from tests.test_live_mode import _accounts, _portfolio, _positions, _quotes

    return StaticPortfolioFetcher(
        accounts=_accounts(),
        portfolio=_portfolio(nav=1513.67, cash=1000.0, bp=1000.0),
        positions=_positions([{"symbol": "MSFT", "quantity": "1", "average_buy_price": "500"}]),
        quotes=_quotes(("MSFT", 513.67), ("SPY", 769.39)),
        orders={"data": {"orders": []}},
    )


def _bound_runtime(fetcher, *, bound=True, error=None):
    return ReadonlyBrokerRuntime(
        bound=bound,
        mode="READ_ONLY",
        fetcher=fetcher if bound else None,
        initialization_error=error,
    )


def _wired_runtime(tmp_path: Path, *, now, fetcher=None, bootstrap=None, **kwargs):
    client = fetcher if fetcher is not None else _msft_position()

    def _bootstrap(**_kwargs):
        if bootstrap is not None:
            return bootstrap(**_kwargs)
        return _bound_runtime(client)

    notify = NotificationEngine(NotificationStore(tmp_path), now_fn=now)
    conn = ConnectionManager(bootstrap=_bootstrap, notify=notify, root=tmp_path, now_fn=now)
    runtime = AgentRuntime(
        tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        now_fn=now,
        sleep_fn=lambda _s: None,
        connection=conn,
        ai_allowed=False,
        **kwargs,
    )
    return runtime, client


def test_production_runtime_wires_live_refresh_fn(tmp_path):
    src = inspect.getsource(AgentRuntime.__init__)
    assert "refresh_fn=refresh_live" in src.replace(" ", "") or "refresh_fn=refresh_live" in src
    assert "refresh_live_from_connection" in src
    refresh_src = inspect.getsource(refresh_live_from_connection)
    assert "connection.ensure" in refresh_src
    assert "bound.fetcher" in refresh_src or "runtime.fetcher" in refresh_src
    assert "refresh_live_portfolio" in refresh_src
    runtime, _fetcher = _wired_runtime(tmp_path, now=lambda: SATURDAY)
    assert runtime.services.refresh_fn is not None
    assert runtime.services.quotes_fn is not None
    assert runtime.services.candidates_fn is None
    assert LIVE_ORDER_PLACEMENT is False
    assert runtime.runtime_mode is RuntimeMode.LIVE


def test_live_refresh_uses_bound_readonly_fetcher(tmp_path):
    fetcher = _msft_position()
    runtime, bound_fetcher = _wired_runtime(tmp_path, now=lambda: SATURDAY, fetcher=fetcher)
    assert bound_fetcher is fetcher
    result = runtime.services.refresh_fn()
    assert runtime.connection.connected is True
    assert runtime.connection.snapshot()["mode"] == "READ_ONLY"
    assert runtime.connection.snapshot()["bound"] is True
    assert fetcher.calls[0] == "get_accounts"
    assert "get_portfolio" in fetcher.calls
    assert "get_equity_positions" in fetcher.calls
    assert result.context.current_nav == 1513.67
    assert result.placement_disabled is True
    for tool in FORBIDDEN_MCP_TOOLS:
        assert tool not in fetcher.calls
        assert tool not in result.tools_used


def test_successful_mcp_refresh_persists_and_dashboard_reads_it(tmp_path, monkeypatch):
    from agentic_portfolio.dashboard.app import create_app
    from agentic_portfolio.dashboard.queries import dashboard_state, dashboard_view
    from agentic_portfolio.live.store import LivePortfolioStore
    from tests.test_family import _admin
    from tests.test_live_mode import _write_paper

    _write_paper(tmp_path, 10000.0)
    runtime, fetcher = _wired_runtime(tmp_path, now=lambda: SATURDAY)
    row = runtime.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    assert row["status"] == "OK"
    assert row["nav"] == 1513.67
    assert row["cash"] == 1000.0
    assert row["buying_power"] == 1000.0
    assert row["placement_attempted"] is False
    book = LivePortfolioStore(tmp_path).current_book()
    assert book is not None
    assert book["context"]["current_nav"] == 1513.67
    assert book["paper_environment"] is False
    positions = book["context"]["positions"]
    assert positions[0]["symbol"] == "MSFT"
    monkeypatch.setenv("AGENTIC_RUNTIME_MODE", "LIVE")
    monkeypatch.setenv("DASHBOARD_ENVIRONMENT", "LIVE")
    view = dashboard_view(dashboard_state(tmp_path))
    assert view["live_data_unavailable"] is False
    assert view["nav"] == 1513.67
    assert view["cash"] == 1000.0
    assert view["buying_power"] == 1000.0
    assert view["positions"][0]["symbol"] == "MSFT"
    assert view["live_order_placement_enabled"] is False
    html = _admin(create_app(tmp_path).test_client()).get("/").get_data(as_text=True)
    assert '<div class="halt-banner">LIVE DATA UNAVAILABLE</div>' not in html
    assert "$1,513.67" in html
    assert "$1,000.00" in html
    assert "MSFT" in html
    assert "$10,000.00" not in html
    for tool in FORBIDDEN_MCP_TOOLS:
        assert tool not in fetcher.calls


def test_weekend_portfolio_review_performs_readonly_refresh(tmp_path):
    spec = specs_by_name()["WEEKEND_PORTFOLIO_REVIEW"]
    assert spec.requires_broker is True
    handler_src = inspect.getsource(build_handlers)
    assert "_portfolio_review" in handler_src
    assert 'WEEKEND_PORTFOLIO_REVIEW' in handler_src
    runtime, fetcher = _wired_runtime(tmp_path, now=lambda: SATURDAY)
    orch = JobOrchestrator(tmp_path, handlers=build_handlers(runtime.services), now_fn=lambda: SATURDAY)
    assert "WEEKEND_PORTFOLIO_REVIEW" in orch.due_jobs(SATURDAY)
    assert "LIVE_ACCOUNT_REFRESH" in orch.due_jobs(SATURDAY)
    row = orch.run_job("WEEKEND_PORTFOLIO_REVIEW", now=SATURDAY)
    assert row["status"] == "OK"
    assert row.get("skipped") != "no_refresh"
    assert row["nav"] == 1513.67
    assert row["executable_liquidity"] is False
    assert row["placement_attempted"] is False
    assert "get_accounts" in fetcher.calls
    assert "get_portfolio" in fetcher.calls
    stored = (tmp_path / "state" / "live_book" / "current.json").read_text(encoding="utf-8")
    assert "1513.67" in stored
    for tool in FORBIDDEN_MCP_TOOLS:
        assert tool not in fetcher.calls


def test_live_account_refresh_runs_when_markets_are_closed():
    spec = specs_by_name()["LIVE_ACCOUNT_REFRESH"]
    assert MarketPhase.WEEKEND in spec.phases
    assert MarketPhase.OVERNIGHT in spec.phases
    assert MarketPhase.AFTER_CLOSE in spec.phases
    assert MarketPhase.HOLIDAY in spec.phases
    assert MarketPhase.MARKET_OPEN in spec.phases
    assert spec.requires_broker is True
    post = specs_by_name()["POSTMARKET_RECONCILE"]
    assert post.requires_broker is True


def test_mcp_failure_fails_closed_and_keeps_runtime_alive(tmp_path, monkeypatch):
    from agentic_portfolio.dashboard.queries import dashboard_state, dashboard_view
    from tests.test_live_mode import _write_paper

    _write_paper(tmp_path, 10000.0)

    def boom(**_kwargs):
        raise LiveDataUnavailable("mcp unreachable")

    runtime, _unused = _wired_runtime(tmp_path, now=lambda: SATURDAY, bootstrap=boom)
    row = runtime.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    assert row["status"] == "FAIL_CLOSED"
    assert row["placement_attempted"] is False
    assert not (tmp_path / "state" / "live_book" / "current.json").exists()
    kinds = {item.get("type") for item in read_activity(tmp_path)}
    assert "LIVE_REFRESH_FAILED" in kinds
    runtime.max_cycles = 2
    runtime.run()
    assert runtime.cycles == 2
    assert runtime.fatal is False
    monkeypatch.setenv("AGENTIC_RUNTIME_MODE", "LIVE")
    monkeypatch.setenv("DASHBOARD_ENVIRONMENT", "LIVE")
    view = dashboard_view(dashboard_state(tmp_path))
    assert view["live_data_unavailable"] is True
    assert view["nav"] is None
    assert view["nav"] != 10000.0
    assert view["kpis"]["portfolio_value"]["display"] == "LIVE DATA UNAVAILABLE"


def test_malformed_portfolio_and_missing_positions_fail_closed(tmp_path, monkeypatch):
    from agentic_portfolio.dashboard.queries import dashboard_state, dashboard_view
    from tests.test_live_mode import _accounts, _positions, _quotes

    bad = StaticPortfolioFetcher(
        accounts=_accounts(),
        portfolio={"data": {"total_value": "not-a-number", "cash": "x", "buying_power": {}}},
        positions=_positions(),
        quotes=_quotes(("SPY", 769.39)),
    )
    runtime, _ = _wired_runtime(tmp_path, now=lambda: SATURDAY, fetcher=bad)
    row = runtime.orchestrator.run_job("WEEKEND_PORTFOLIO_REVIEW", now=SATURDAY)
    assert row["status"] == "FAIL_CLOSED"
    assert not (tmp_path / "state" / "live_book" / "current.json").exists()

    missing = StaticPortfolioFetcher(
        accounts=_accounts(),
        portfolio={"data": {"total_value": "500", "cash": "500", "buying_power": {"buying_power": "500"}}},
        positions=None,
        quotes=_quotes(("SPY", 769.39)),
    )
    runtime2, _ = _wired_runtime(tmp_path / "pos", now=lambda: SATURDAY, fetcher=missing)
    row2 = runtime2.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    assert row2["status"] == "FAIL_CLOSED"
    assert not (tmp_path / "pos" / "state" / "live_book" / "current.json").exists()

    wrong = StaticPortfolioFetcher(
        accounts=_accounts(number="000000000"),
        portfolio={"data": {"total_value": "500", "cash": "500", "buying_power": {"buying_power": "500"}}},
        positions=_positions(),
        quotes=_quotes(("SPY", 769.39)),
    )
    runtime3, _ = _wired_runtime(tmp_path / "id", now=lambda: SATURDAY, fetcher=wrong)
    row3 = runtime3.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    assert row3["status"] == "FAIL_CLOSED"

    monkeypatch.setenv("AGENTIC_RUNTIME_MODE", "LIVE")
    monkeypatch.setenv("DASHBOARD_ENVIRONMENT", "LIVE")
    view = dashboard_view(dashboard_state(tmp_path))
    assert view["live_data_unavailable"] is True
    assert view["nav"] is None


def test_paper_portfolio_never_leaks_into_live_dashboard_via_runtime(tmp_path, monkeypatch):
    from agentic_portfolio.dashboard.queries import dashboard_state, dashboard_view
    from tests.test_live_mode import _write_paper

    _write_paper(tmp_path, 10000.0)
    runtime, fetcher = _wired_runtime(tmp_path, now=lambda: SATURDAY)
    runtime.orchestrator.run_job("WEEKEND_PORTFOLIO_REVIEW", now=SATURDAY)
    monkeypatch.setenv("AGENTIC_RUNTIME_MODE", "LIVE")
    monkeypatch.setenv("DASHBOARD_ENVIRONMENT", "LIVE")
    view = dashboard_view(dashboard_state(tmp_path))
    symbols = {p["symbol"] for p in view["positions"]}
    assert view["nav"] == 1513.67
    assert view["nav"] != 10000.0
    assert "NVDA" not in symbols
    assert "NKE" not in symbols
    assert "MSFT" in symbols
    assert view["paper_environment"] is False
    for tool in WRITE_MARKERS:
        assert tool not in fetcher.calls
    assert inspect_agent_packages_for_forbidden_calls() == []
    assert LIVE_ORDER_PLACEMENT is False


def test_refresh_never_invokes_order_or_transfer_tools(tmp_path):
    class GuardedFetcher(StaticPortfolioFetcher):
        def __getattr__(self, name):
            self.calls.append(name)
            raise AssertionError(f"unexpected MCP surface {name}")

    from tests.test_live_mode import _accounts, _portfolio, _positions, _quotes

    fetcher = GuardedFetcher(
        accounts=_accounts(),
        portfolio=_portfolio(nav=500.0, cash=500.0, bp=500.0),
        positions=_positions(),
        quotes=_quotes(("SPY", 769.39)),
        orders={"data": {"orders": []}},
    )
    runtime, _ = _wired_runtime(tmp_path, now=lambda: SATURDAY, fetcher=fetcher)
    runtime.services.refresh_fn()
    runtime.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    runtime.orchestrator.run_job("WEEKEND_PORTFOLIO_REVIEW", now=SATURDAY)
    for tool in WRITE_MARKERS:
        assert tool not in fetcher.calls
    assert all("place" not in c and "review" not in c and "cancel" not in c for c in fetcher.calls)
