from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from firm.broker.types import Bar, Quote
from firm.core import Firm
from firm.managers import validate_mandate
from firm.managers.base import BookView, ManagerContext
from firm.managers.max import DEFAULT_MANDATE, Max, rsi, sma
from firm.money import D

from tests.firm_suite.fakes import FakeRobinhood

SMALL = {**DEFAULT_MANDATE, "universe": ["UPDIP", "UPFLAT", "DOWN"]}
CLOSE_ET = datetime(2026, 10, 13, 19, 20, tzinfo=timezone.utc)  # Tue 15:20 ET (entry window)
MORNING = datetime(2026, 10, 13, 14, 35, tzinfo=timezone.utc)  # Tue 10:35 ET (stops only)


def uptrend(n=240, start=100.0, step=0.001):
    return [start * (1 + step) ** i for i in range(n)]


UPDIP = uptrend()[:-3] + [uptrend()[-4] * f for f in (0.98, 0.96, 0.94)]  # sharp dip, still above the 200-day average
UPFLAT = uptrend()
DOWN = [200 * (0.999 ** i) for i in range(240)]
CLOSES = {"UPDIP": UPDIP, "UPFLAT": UPFLAT, "DOWN": DOWN}


def quotes(prices, at):
    return {s: Quote(s, bid=D(str(round(p, 2))) - D("0.01"), ask=D(str(round(p, 2))) + D("0.01"), last=D(str(round(p, 2))), observed_at=at, close=None) for s, p in prices.items()}


def ctx(now, *, cash="1000", holdings=None, prices=None, positions=None, closes=CLOSES):
    prices = prices or {s: c[-1] for s, c in closes.items()}
    view = BookView(book="SIM", cash=D(cash), holdings={k: D(v) for k, v in (holdings or {}).items()}, quotes=quotes(prices, now))
    # Stored bars end yesterday; the live price is today's close.
    stored = {s: c[:-1] for s, c in closes.items()}
    return ManagerContext(manager_id="max", book="SIM", now=now, trigger="scheduled", mandate=SMALL, view=view,
                          extras={"closes": stored, "last_bar_dates": {s: "2026-10-12" for s in closes}, "positions": positions or {}})


def test_indicators():
    assert sma([1, 2, 3, 4], 2) == 3.5 and sma([1], 2) is None
    assert rsi(UPDIP[-30:], 2) < 10 and rsi(UPFLAT[-30:], 2) == 100.0


def test_two_runs_a_day():
    mx = Max()
    assert not mx.due(datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc), SMALL, None)  # 10:00 ET
    assert mx.due(MORNING, SMALL, None)
    assert not mx.due(MORNING + timedelta(minutes=5), SMALL, MORNING)
    assert mx.due(CLOSE_ET, SMALL, MORNING)
    assert not mx.due(CLOSE_ET + timedelta(minutes=5), SMALL, CLOSE_ET)


def test_enters_only_oversold_dips_in_uptrends_at_the_close_window():
    r = Max().run(ctx(CLOSE_ET))
    assert [(i.symbol, i.notional) for i in r.intents] == [("UPDIP", D("250.00"))]
    assert "stop" in r.intents[0].reason and r.activity == "entering"
    morning = Max().run(ctx(MORNING))
    assert not morning.intents and morning.activity == "watching"


def test_stop_fires_in_the_morning_check():
    held = {"UPFLAT": "2"}
    entry = UPFLAT[-1]
    r = Max().run(ctx(MORNING, holdings=held, prices={**{s: c[-1] for s, c in CLOSES.items()}, "UPFLAT": entry * 0.93}, positions={"UPFLAT": {"avg_cost": entry, "opened_at": "2026-10-12T19:20:00+00:00"}}))
    assert [(i.symbol, i.side) for i in r.intents] == [("UPFLAT", "sell")] and "stop hit" in r.intents[0].reason
    safe = Max().run(ctx(MORNING, holdings=held, positions={"UPFLAT": {"avg_cost": entry, "opened_at": "2026-10-12T19:20:00+00:00"}}))
    assert not safe.intents and safe.detail["stops"]["UPFLAT"] == round(entry * 0.94, 2)


def test_target_and_time_exits():
    pos = {"UPFLAT": {"avg_cost": UPFLAT[-1] * 0.97, "opened_at": "2026-10-09T19:20:00+00:00"}}
    r = Max().run(ctx(CLOSE_ET, holdings={"UPFLAT": "1"}, positions=pos))
    assert r.intents[0].symbol == "UPFLAT" and r.intents[0].reason.startswith("target")
    stale = {"UPDIP": {"avg_cost": UPDIP[-1] * 1.01, "opened_at": "2026-09-25T19:20:00+00:00"}}
    r2 = Max().run(ctx(CLOSE_ET, holdings={"UPDIP": "1"}, positions=stale))
    assert r2.intents[0].reason.startswith("time stop")


def test_mandate_requires_a_stop():
    assert validate_mandate("max", DEFAULT_MANDATE) == []
    assert any("stop" in p for p in validate_mandate("max", {**DEFAULT_MANDATE, "stop_pct": 0}))
    assert validate_mandate("max", {**DEFAULT_MANDATE, "max_positions": 5, "position_pct": 0.25})


@pytest.fixture
def firm(db, clock):
    clock.set(CLOSE_ET)
    rh = FakeRobinhood(clock=clock)
    for s, c in CLOSES.items():
        rh.prices[s] = D(str(round(c[-1], 2)))
    f = Firm(db, reader=rh, writer=lambda: None, clock=clock, environ={})
    f.reconciler.bootstrap(rh)
    return f


def test_phase4_mandate_keeps_the_approved_sleeve_stops_and_trades_in_simulation(firm):
    _, m = firm.registry.mandate("max")
    assert m["universe"] and m["stop_pct"] == 0.06
    assert m["drawdown_policy"] == "sleeve_stop" and m["sleeve_drawdown_pause"] == 0.1 and m["daily_loss_pause"] == 0.03
    firm.registry.update_mandate("max", {**m, "universe": SMALL["universe"]}, by="test", note="small universe")
    day0 = date(2026, 10, 12) - timedelta(days=len(UPDIP) - 2)
    for s, c in CLOSES.items():
        firm.market.store_bars(s, [Bar((day0 + timedelta(days=i)).isoformat(), D(str(round(x, 4))), D(str(round(x, 4))), D(str(round(x, 4))), D(str(round(x, 4))), 1) for i, x in enumerate(c[:-1])])
    firm.allocate("max", D("100"), by="reydel")
    firm.registry.set_mode("max", "SIM", by="reydel")
    out = firm.run_manager("max")
    assert out.ran, out.summary
    fills = firm.events.recent(manager_id="max", types=["ORDER_FILLED"])
    assert [e.symbol for e in fills] == ["UPDIP"] and "[SIM] BUY" in fills[0].summary
