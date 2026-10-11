from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from firm.money import D
from firm.runtime import Service
from firm.scheduler import Job, Scheduler

from tests.firm_suite.fakes import FakeRobinhood


def _service(db, clock, rh):
    return Service(environ={}, clock=clock, db=db, reader=rh, writer=lambda: rh)


def test_nothing_trades_before_the_books_are_opened(db, clock):
    rh = FakeRobinhood(clock=clock)
    svc = _service(db, clock, rh)
    svc.firm.registry.set_mode("tim", "SIM", by="t")
    started = svc.scheduler.tick(wait=True)
    assert "manager:tim" not in started and "broker_sync" not in started


def test_a_trading_day_end_to_end(db, clock):
    rh = FakeRobinhood(clock=clock)
    svc = _service(db, clock, rh)
    f = svc.firm
    clock.set(datetime(2026, 10, 13, 13, 50, tzinfo=timezone.utc))
    f.reconciler.bootstrap(rh)
    f.allocate("tim", D("909"), by="owner")
    f.registry.set_mode("tim", "LIVE", by="owner")
    # 09:50 ET: the allocation request fires but the desk defers (before the window).
    svc.scheduler.tick(wait=True)
    assert f.ledger.holdings("LIVE", "tim") == {}
    # 10:06 ET: Tim's scheduled run invests; broker sync reconciles.
    clock.set(datetime(2026, 10, 13, 14, 6, tzinfo=timezone.utc))
    svc.scheduler.tick(wait=True)
    assert set(f.ledger.holdings("LIVE", "tim")["tim"]) == {"VTI", "VXUS", "BND"}
    clock.set(datetime(2026, 10, 13, 14, 9, tzinfo=timezone.utc))
    svc.scheduler.tick(wait=True)
    assert svc.scheduler.state["broker_sync"].last_result["status"] == "OK"
    # Later the same day: no second scheduled run.
    clock.set(datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc))
    assert "manager:tim" not in svc.scheduler.tick(wait=True)
    # 16:25 ET: marks.
    clock.set(datetime(2026, 10, 13, 20, 25, tzinfo=timezone.utc))
    svc.scheduler.tick(wait=True)
    assert f.performance.marks("LIVE", "tim")[-1].nav > D("900")
    assert f.performance.marks("LIVE", "_firm")[-1].nav > D("1000")


def test_scheduler_isolates_a_hanging_and_a_failing_job(clock):
    sched = Scheduler(clock=clock)
    ran = []
    gate = threading.Event()
    sched.add(Job("hang", lambda now, st: True, lambda: gate.wait(5), timeout_seconds=0.1))
    sched.add(Job("boom", lambda now, st: True, lambda: 1 / 0))
    sched.add(Job("ok", lambda now, st: True, lambda: ran.append(1)))
    sched.tick()
    time.sleep(0.2)
    clock.advance(seconds=1)
    sched.tick()
    assert ran, "the healthy job ran despite the others"
    assert sched.state["boom"].last_ok is False and "ZeroDivisionError" in sched.state["boom"].last_error
    gate.set()
    sched.shutdown()


def test_broker_outage_does_not_crash_jobs(db, clock):
    rh = FakeRobinhood(clock=clock)
    svc = _service(db, clock, rh)
    svc.firm.reconciler.bootstrap(rh)
    svc.firm.allocate("tim", D("500"), by="o")
    svc.firm.registry.set_mode("tim", "SIM", by="o")
    rh.unreachable = True
    clock.set(datetime(2026, 10, 13, 14, 6, tzinfo=timezone.utc))
    svc.scheduler.tick(wait=True)
    assert svc.scheduler.state["broker_sync"].last_result["status"] == "UNAVAILABLE"
    assert svc.firm.events.last_of("MANAGER_WAITING", manager_id="tim") is not None
