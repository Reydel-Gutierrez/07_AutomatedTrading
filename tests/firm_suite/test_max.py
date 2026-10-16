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


def test_first_run_fetches_missing_history(firm):
    _, m = firm.registry.mandate("max")
    firm.registry.update_mandate("max", {**m, "universe": SMALL["universe"]}, by="test", note="small universe")
    calls = []
    firm.market.refresh_bars = lambda syms, days=400: calls.append(sorted(syms)) or 0
    Max().inputs(firm, {**m, "universe": SMALL["universe"]})
    assert calls == [sorted(SMALL["universe"])]
    r = Max().run(ctx(CLOSE_ET, closes={"UPFLAT": UPFLAT}))
    assert not r.intents and r.detail["missing_history"] == ["DOWN", "UPDIP"] and "no price history yet" in r.summary


def test_no_reentry_right_after_a_stop():
    # Stopped out this morning (session 0): the 15:15 run must not buy it back.
    c = ctx(CLOSE_ET)
    c.extras["stopped_out"] = {"UPDIP": "2026-10-13T14:35:00+00:00"}
    r = Max().run(c)
    assert not r.intents and "UPDIP" in r.detail["cooling_off"]
    # Three sessions later it is fair game again.
    c.extras["stopped_out"] = {"UPDIP": "2026-10-08T14:35:00+00:00"}
    assert [i.symbol for i in Max().run(c).intents] == ["UPDIP"]


def test_at_most_max_per_group_names_that_move_together():
    m = {**SMALL, "universe": ["UPDIP", "DIP2", "DIP3", "UPFLAT"], "groups": {"UPDIP": "chips", "DIP2": "chips", "DIP3": "chips"}, "max_per_group": 2}
    closes = {**CLOSES, "DIP2": [x * 1.5 for x in UPDIP], "DIP3": [x * 2 for x in UPDIP]}
    c = ctx(CLOSE_ET, closes=closes)
    c = ManagerContext(**{**c.__dict__, "mandate": m})
    r = Max().run(c)
    assert len(r.intents) == 2 and r.detail["group_full"] == {next(s for s in ("UPDIP", "DIP2", "DIP3") if s not in {i.symbol for i in r.intents}): "chips"}
    # Already holding two chips names: the third is refused even with free slots.
    pos = {s: {"avg_cost": closes[s][-1], "opened_at": "2026-10-12T19:20:00+00:00"} for s in ("DIP2", "DIP3")}
    held = ManagerContext(**{**ctx(CLOSE_ET, closes=closes, holdings={"DIP2": "1", "DIP3": "1"}, positions=pos).__dict__, "mandate": m})
    r2 = Max().run(held)
    assert not any(i.side == "buy" for i in r2.intents)


def test_stop_outs_are_read_from_filled_stop_orders(firm):
    _, m = firm.registry.mandate("max")
    with firm.db.tx() as conn:
        conn.execute(
            "INSERT INTO orders (client_ref, book, manager_id, symbol, side, quantity, status, reason, created_at, updated_at)"
            " VALUES ('t1', 'SIM', 'max', 'SMH', 'sell', '1', 'FILLED', 'stop hit: 1 ≤ 2', ?, ?)",
            ("2026-10-13T14:35:00+00:00", "2026-10-13T14:35:00+00:00"),
        )
    firm.market.refresh_bars = lambda syms, days=400: 0
    assert Max().inputs(firm, m)["stopped_out"] == {"SMH": "2026-10-13T14:35:00+00:00"}
