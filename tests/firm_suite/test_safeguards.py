from __future__ import annotations

from datetime import datetime, timezone

from firm.broker.types import Quote
from firm.core import Firm
from firm.managers.base import BookView, ManagerContext, OrderIntent
from firm.managers.registry import TIM_DEFAULT_MANDATE
from firm.managers.tim import Tim
from firm.money import D

from tests.firm_suite.fakes import FakeRobinhood

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)


def _firm(db, clock):
    rh = FakeRobinhood(clock=clock)
    f = Firm(db, reader=rh, writer=lambda: rh, clock=clock, environ={})
    f.reconciler.bootstrap(rh)
    f.allocate("tim", D("909"), by="o")
    f.registry.set_mode("tim", "LIVE", by="o")
    f.reconciler.run(rh)  # broker snapshot = NAV base for the turnover breaker
    return f, rh


def buy(amount, manager="tim"):
    return OrderIntent(manager, "LIVE", "VTI", "buy", notional=D(amount), reason="t")


def test_turnover_breaker_stops_runaway_trading(db, clock, settings):
    f, rh = _firm(db, clock)
    limits = f.settings.get("firm_limits")
    f.settings.put("firm_limits", {**limits, "max_daily_turnover_pct_nav": 0.5}, by="t")
    assert f.desk.submit(buy("400")).status == "FILLED"
    res = f.desk.submit(buy("200"))
    assert res.status == "REJECTED" and res.reasons[0].startswith("TURNOVER_LIMIT")
    assert f.events.last_of("TURNOVER_LIMIT") is not None


def test_brake_scope_exempts_only_stay_invested_and_never_operational_stops(db, clock):
    f, rh = _firm(db, clock)
    f.controls.engage_brake(nav="800", hwm="1010", drawdown=0.21)
    assert f.desk.submit(buy("10")).reasons == ["DRAWDOWN_BRAKE"]  # default scope "all": unchanged behavior
    limits = f.settings.get("firm_limits")
    f.settings.put("firm_limits", {**limits, "drawdown_brake_scope": "active_strategies"}, by="owner")
    assert f.desk.submit(buy("10")).reasons == ["DRAWDOWN_BRAKE"]  # Tim's mandate has no stay_invested policy yet
    _, mandate = f.registry.mandate("tim")
    f.registry.update_mandate("tim", {**mandate, "drawdown_policy": "stay_invested"}, by="owner", note="t")
    assert f.desk.submit(buy("10")).status == "FILLED"
    # Operational protections still stop an exempt manager.
    f.controls.set_reconcile_hold(["TEST"], {})
    assert f.desk.submit(buy("10")).reasons == ["RECONCILE_HOLD"]
    f.controls.clear_reconcile_hold()
    f.controls.engage("drill", by="owner")
    assert f.desk.submit(buy("10")).reasons == ["KILL_SWITCH"]


def test_stored_limits_missing_new_keys_still_get_safe_defaults(settings):
    settings.put("firm_limits", {"window_start_et": "10:00", "window_end_et": "15:30"}, by="old")
    limits = settings.get("firm_limits")
    assert limits["max_daily_turnover_pct_nav"] == 1.0 and limits["drawdown_brake_scope"] == "all"


def _ctx(view, mandate, drawdown=0.0):
    return ManagerContext("tim", "SIM", NOW, "scheduled", mandate, view, {"last_quarterly": "2026Q4"}, drawdown)


def _view(cash, holdings, closes=None):
    prices = {"VTI": D("300"), "VXUS": D("70"), "BND": D("72")}
    quotes = {s: Quote(s, bid=p, ask=p, last=p, observed_at=NOW, close=(closes or {}).get(s, p)) for s, p in prices.items()}
    return BookView("SIM", D(cash), holdings, quotes)


def test_crash_pacing_spreads_trades_only_deep_in_a_drawdown():
    mandate = {**TIM_DEFAULT_MANDATE, "crash_pacing": {"threshold": 0.15, "max_trade_pct_of_sleeve": 0.10}}
    view = _view("909", {})
    calm = Tim().run(_ctx(view, mandate, drawdown=0.05))
    assert max(i.notional for i in calm.intents) > D("600")
    crash = Tim().run(_ctx(view, mandate, drawdown=0.25))
    assert crash.intents and all(i.notional <= D("90.90") for i in crash.intents)


def test_volatility_pause_waits_out_extreme_days():
    mandate = {**TIM_DEFAULT_MANDATE, "volatility_pause_pct": 0.05}
    wild = Tim().run(_ctx(_view("909", {}, closes={"VTI": D("330")}), mandate))
    assert wild.intents == [] and "Extreme market day" in wild.summary
    normal = Tim().run(_ctx(_view("909", {}, closes={"VTI": D("305")}), mandate))
    assert normal.intents


def test_drawdown_alerts_inform_once_per_level_and_never_block(db, clock):
    f, rh = _firm(db, clock)
    f.desk.submit(buy("800"))
    f.take_marks()
    clock.advance(days=1)
    rh.prices["VTI"] = rh.prices["VTI"] * D("0.7")
    f.market._cache.clear()
    f.take_marks()
    alerts = f.events.recent(types=["DRAWDOWN_ALERT"], limit=10)
    assert {a.payload["level"] for a in alerts} >= {0.2}
    clock.advance(days=1)
    f.take_marks()
    assert len(f.events.recent(types=["DRAWDOWN_ALERT"], limit=10)) == len(alerts)  # no repeat at the same level
    assert not f.controls.brake().get("engaged")  # alerts never pause anything
