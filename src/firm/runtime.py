"""The 24/7 service: broker connections, the job schedule, and the API server.

Jobs (all ET, calendar-aware):
  heartbeat         every minute
  broker_sync       fills, reconciliation, foreign orders: 1 min with open orders,
                    2 min in market hours, 15 min otherwise
  manager:<id>      the manager's own schedule, plus requested runs
  marks             once per trading day after 16:20
  market_bars       once per day after 17:00 (benchmark + held symbols)
  backup            once per day after 02:30 (encrypted, off-Pi, restore-verified)
"""

from __future__ import annotations

import logging
import os
import threading
import time as _time
from datetime import datetime, time, timedelta
from typing import Any, Mapping

from firm.backup import run_backup
from firm.broker import connect_reader, connect_writer, live_writes_enabled
from firm.broker.errors import BrokerUnavailable
from firm.clock import CALENDAR, Clock, et, et_date, iso, is_regular_hours, parse, utc_now
from firm.config import account_number, deployed_commit
from firm.core import Firm
from firm.db import Database
from firm.notify import telegram_sink
from firm.paths import db_path
from firm.performance import BENCHMARK
from firm.scheduler import Job, JobState, Scheduler

log = logging.getLogger(__name__)


class BrokerProxy:
    """Holds the live reader; reconnects lazily. Methods raise BrokerUnavailable while disconnected."""

    def __init__(self, connect, on_change) -> None:
        self._connect = connect
        self._client = None
        self._on_change = on_change
        self._lock = threading.Lock()
        self.connected = False
        self.last_error: str | None = None
        self._next_attempt = 0.0

    @property
    def account_number(self) -> str:
        return account_number()

    def client(self):
        with self._lock:
            if self._client is not None:
                return self._client
            if _time.monotonic() < self._next_attempt:
                raise BrokerUnavailable(self.last_error or "broker reconnect backing off")
            try:
                self._client = self._connect()
            except Exception as exc:  # noqa: BLE001
                self._next_attempt = _time.monotonic() + 60
                self._set(False, f"{type(exc).__name__}: {exc}")
                raise BrokerUnavailable(str(exc)) from exc
            self._set(True, None)
            return self._client

    def reset(self, error: str) -> None:
        with self._lock:
            self._client = None
            self._set(False, error)

    def _set(self, connected: bool, error: str | None) -> None:
        changed = connected != self.connected
        self.connected, self.last_error = connected, error
        if changed:
            self._on_change(connected, error)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            target = getattr(self.client(), name)
            try:
                return target(*args, **kwargs)
            except BrokerUnavailable as exc:
                if exc.code in {"HTTP_401", "TIMEOUT", "ROBINHOOD_MCP_UNREACHABLE"}:
                    self.reset(str(exc))
                raise

        return call


class Service:
    def __init__(self, *, environ: Mapping[str, str] | None = None, clock: Clock = utc_now, db: Database | None = None, reader=None, writer=None) -> None:
        self.environ = environ if environ is not None else os.environ
        self.clock = clock
        self.started_at = clock()
        self.db = db or Database(db_path())
        sink = telegram_sink(self.environ)
        self._writer_client = None
        self.reader = reader or BrokerProxy(lambda: connect_reader(account_number(), self.environ), self._broker_changed)
        self.firm = Firm(
            self.db,
            reader=self.reader,
            writer=writer or self._writer,
            clock=clock,
            environ=self.environ,
            sinks=[sink] if sink else [],
        )
        self.scheduler = Scheduler(clock=clock, on_failure=self._job_failed)
        self.cio = build_cio(self.firm, self.health, self.environ)
        self.research_desk = build_research(self.firm, self.environ, on_progress=self._research_progress)
        self.researching: dict[str, Any] | None = None  # in-memory presence for the Firm Space
        self.sidecars: dict[str, Any] = {}  # separate processes supervised next to the firm (Forex engine)
        self._last_failure_event: dict[str, datetime] = {}
        self._install_jobs()

    # -- broker ----------------------------------------------------------------
    def _writer(self):
        if not live_writes_enabled(self.environ):
            self._writer_client = None
            return None
        if self._writer_client is None:
            self._writer_client = connect_writer(account_number(), self.environ)
        return self._writer_client

    def _broker_changed(self, connected: bool, error: str | None) -> None:
        if connected:
            self.firm.events.emit("BROKER_CONNECTED", "Connected to Robinhood.", actor="system")
        else:
            self.firm.events.emit("BROKER_DISCONNECTED", f"Lost the Robinhood connection: {error}", actor="system", severity="warning")

    # -- jobs -------------------------------------------------------------------
    def _install_jobs(self) -> None:
        f = self.firm
        add = self.scheduler.add
        add(Job("heartbeat", lambda now, st: _every(now, st, 60), self.heartbeat, 30))
        add(Job("broker_sync", self._sync_due, self.broker_sync, 240))
        for manager_id in f.managers:
            add(Job(f"manager:{manager_id}", self._manager_due(manager_id), self._manager_run(manager_id), 600))
        if "alex" in f.managers:
            add(Job("research:alex", self._research_due, self.research, 1800))
        if self.cio is not None:
            add(Job("cio:pool_review", self._pool_review_due, self.pool_review, 900))
        add(Job("marks", lambda now, st: self._daily_after(now, "marks", time(16, 20), trading_only=True), self.marks, 300))
        add(Job("market_bars", lambda now, st: self._daily_after(now, "market_bars", time(17, 0)), self.market_bars, 300))
        add(Job("backup", lambda now, st: self._daily_after(now, "backup", time(2, 30)), self.backup, 900))

    def _sync_due(self, now: datetime, st: JobState) -> bool:
        if not self.firm.reconciler.bootstrapped():
            return False
        if self.firm.desk.open_live_orders():
            return _every(now, st, 60)
        return _every(now, st, 120 if is_regular_hours(now) else 900)

    def _daily_after(self, now: datetime, key: str, at: time, *, trading_only: bool = False) -> bool:
        if trading_only and CALENDAR.session_for(now) is None:
            return False
        local = et(now)
        if local.time() < at:
            return False
        done = (self.firm.settings.get("daily_jobs") or {}).get(key)
        return done != local.date().isoformat()

    def _mark_daily(self, key: str) -> None:
        state = self.firm.settings.get("daily_jobs") or {}
        state[key] = et_date(self.clock()).isoformat()
        self.firm.settings.put("daily_jobs", state, by="scheduler")

    def _manager_due(self, manager_id: str):
        def due(now: datetime, st: JobState) -> bool:
            row = self.firm.registry.get(manager_id)
            if row.book is None or not self.firm.reconciler.bootstrapped():
                return False
            if self.firm.due_request(manager_id):
                return True
            last = (self.firm.settings.get("manager_scheduled_runs") or {}).get(manager_id)
            _, mandate = self.firm.registry.mandate(manager_id)
            return self.firm.managers[manager_id].due(now, mandate, parse(last) if last else None)

        return due

    def _manager_run(self, manager_id: str):
        def run() -> dict[str, Any]:
            now = self.clock()
            reasons = self.firm.due_request(manager_id)
            trigger = ",".join(reasons) if reasons else "scheduled"
            if reasons:
                self.firm.clear_request(manager_id)
            runs = self.firm.settings.get("manager_scheduled_runs") or {}
            last = runs.get(manager_id)
            _, mandate = self.firm.registry.mandate(manager_id)
            # Any run once the daily slot is due also satisfies the daily schedule.
            if self.firm.managers[manager_id].due(now, mandate, parse(last) if last else None):
                runs[manager_id] = iso(now)
                self.firm.settings.put("manager_scheduled_runs", runs, by="scheduler")
            outcome = self.firm.run_manager(manager_id, trigger=trigger)
            if outcome.rerun_after_minutes and is_regular_hours(self.clock()):
                self.firm.request_run(manager_id, "rerun", after_minutes=outcome.rerun_after_minutes)
            return {"summary": outcome.summary, "orders": outcome.orders}

        return run

    # -- Graham's pool ---------------------------------------------------------
    def _pool_review_due(self, now: datetime, st: JobState) -> bool:
        """When the owner funds the pool, and weekly otherwise; trading days 10:30-15:00 ET."""
        if CALENDAR.session_for(now) is None or not (time(10, 30) <= et(now).time() < time(15, 0)):
            return False
        if st.last_started and (now - st.last_started).total_seconds() < 3600:
            return False
        state = self.firm.settings.get("cio_pool_state") or {}
        if state.get("review_requested"):
            return True
        last = state.get("last_review")
        return last is None or (now - parse(last)).days >= 7

    def pool_review(self) -> dict[str, Any]:
        reply = self.cio.review_pool()
        self.firm.settings.put("cio_pool_state", {"last_review": iso(self.clock()), "review_requested": False}, by="scheduler")
        return {"reviewed": reply is not None, "role": reply and reply.get("role")}

    # -- research ------------------------------------------------------------
    def _research_due(self, now: datetime, st: JobState) -> bool:
        """Once per trading day from 08:30 ET (or as soon as Alex is switched on), while Alex runs."""
        row = self.firm.registry.get("alex")
        if row.book is None or not self.firm.reconciler.bootstrapped() or CALENDAR.session_for(now) is None:
            return False
        if et(now).time() < time(8, 30):
            return False
        if st.last_started and (now - st.last_started).total_seconds() < 1800:
            return False
        done = (self.firm.settings.get("daily_jobs") or {}).get("research_alex")
        return done != et_date(now).isoformat()

    def _research_progress(self, symbol: str | None) -> None:
        self.researching = {"symbol": symbol, "since": iso(self.clock())} if symbol else None

    def research(self) -> dict[str, Any]:
        result = self.research_desk.run()
        self._mark_daily("research_alex")
        if result.approved or result.rejected:
            self.firm.request_run("alex", "research_updated")
        return {"researched": result.researched, "approved": result.approved, "rejected": result.rejected, "skipped": result.skipped, "bars_refreshed": result.bars_refreshed}

    def heartbeat(self) -> None:
        self.firm.settings.put(
            "heartbeat",
            {"at": iso(self.clock()), "pid": os.getpid(), "started_at": iso(self.started_at), "broker_connected": getattr(self.reader, "connected", True)},
            by="scheduler",
        )

    def broker_sync(self) -> dict[str, Any]:
        result = self.firm.reconciler.run(self.reader)
        return {"status": result.status, "reasons": result.reasons, "actions": result.actions}

    def marks(self) -> int:
        n = self.firm.take_marks()
        self._mark_daily("marks")
        return n

    def market_bars(self) -> int:
        symbols = {BENCHMARK}
        for manager_id in self.firm.managers:
            try:
                symbols |= set(self.firm.managers[manager_id].symbols(self.firm.registry.mandate(manager_id)[1]))
            except Exception:  # noqa: BLE001
                continue
        n = self.firm.market.refresh_bars(sorted(symbols))
        self._mark_daily("market_bars")
        return n

    def backup(self) -> dict[str, Any]:
        result = run_backup(self.firm, environ=self.environ)
        self._mark_daily("backup")
        return result

    def _job_failed(self, name: str, error: str) -> None:
        now = self.clock()
        last = self._last_failure_event.get(name)
        if last and now - last < timedelta(hours=1):
            return
        self._last_failure_event[name] = now
        self.firm.events.emit("JOB_FAILED", f"Background job {name} failed: {error}", actor="scheduler", severity="warning", payload={"job": name, "error": error})

    # -- status --------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        hb = self.firm.settings.get("heartbeat") or {}
        ks = self.firm.controls.kill_switch()
        return {
            "agent": "ONLINE",
            "started_at": iso(self.started_at),
            "heartbeat": hb.get("at"),
            "broker_connected": getattr(self.reader, "connected", True),
            "broker_error": getattr(self.reader, "last_error", None),
            "books_open": self.firm.reconciler.bootstrapped(),
            "kill_switch": ks.engaged,
            "reconcile_hold": bool(self.firm.controls.reconcile_hold().get("active")),
            "live_writes_enabled": live_writes_enabled(self.environ),
            "market": {"regular_hours_open": is_regular_hours(self.clock()), "trading_day": CALENDAR.session_for(self.clock()) is not None},
            "open_live_orders": len(self.firm.desk.open_live_orders()),
            "managers": {m.id: m.mode for m in self.firm.registry.all()},
            "deployed": deployed_commit(),
            "sidecars": {name: sc.status() for name, sc in self.sidecars.items()},
        }

    def jobs(self) -> dict[str, Any]:
        return {name: st.as_dict() for name, st in self.scheduler.state.items()}

    # -- loop ---------------------------------------------------------------
    def run_scheduler(self, stop: threading.Event, *, interval: float = 15.0) -> None:
        self.firm.events.emit("SERVICE_STARTED", f"Firm service started ({deployed_commit().get('sha', 'dev')[:12]}).", actor="system")
        while not stop.is_set():
            try:
                self.scheduler.tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            stop.wait(interval)
        self.scheduler.shutdown()


def build_research(firm: Firm, environ: Mapping[str, str], researcher=None, on_progress=None):
    """Alex's research desk on Claude with web search (settings.ai_models.alex)."""
    from firm.ai.budget import Budget
    from firm.research import AnthropicResearcher, ResearchDesk

    cfg = (firm.settings.get("ai_models") or {}).get("alex") or {}
    if researcher is None:
        researcher = AnthropicResearcher(api_key=str(environ.get("ANTHROPIC_API_KEY") or ""), model=str(cfg.get("model") or "claude-opus-5-5"), effort=str(cfg.get("effort") or "medium"))
    return ResearchDesk(firm, researcher, Budget(firm.db, firm.settings, clock=firm.clock), on_progress=on_progress)


def build_cio(firm: Firm, health, environ: Mapping[str, str], provider=None):
    """The CIO office, using the provider and models chosen in settings.ai_models."""
    from firm.ai.budget import Budget
    from firm.cio.agent import CIO

    cfg = (firm.settings.get("ai_models") or {}).get("cio") or {}
    name = str(cfg.get("provider") or "anthropic")
    if provider is None:
        if name == "openai":
            from firm.ai.providers import OpenAIResponses

            provider = OpenAIResponses(api_key=str(environ.get("OPENAI_API_KEY") or ""))
        else:
            from firm.ai.anthropic_provider import AnthropicMessages

            provider = AnthropicMessages(api_key=str(environ.get("ANTHROPIC_API_KEY") or ""), effort=str(cfg.get("effort") or "high"))
    defaults = {"openai": ("gpt-5.6-terra", "gpt-5.6-luna")}.get(name, ("claude-opus-5-5", "claude-haiku-5-5"))
    return CIO(
        firm,
        provider=provider,
        budget=Budget(firm.db, firm.settings, clock=firm.clock),
        health=health,
        model=cfg.get("model", defaults[0]),
        summary_model=cfg.get("summary_model", defaults[1]),
    )


def _every(now: datetime, st: JobState, seconds: float) -> bool:
    return st.last_started is None or (now - st.last_started).total_seconds() >= seconds
