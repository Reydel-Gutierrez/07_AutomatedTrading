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
    assert f.desk.submit(buy("10")).reasons == ["DRAWDOWN_BRAKE"]  # scope "all": every manager stops
    limits = f.settings.get("firm_limits")
    f.settings.put("firm_limits", {**limits, "drawdown_brake_scope": "active_strategies"}, by="owner")
    assert f.desk.submit(buy("10")).status == "FILLED"  # Tim's mandate: drawdown_policy = stay_invested
    assert f.desk.submit(buy("10", manager="alex")).reasons == ["DRAWDOWN_BRAKE"]  # Alex keeps the brake
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


def test_extreme_days_never_block_buying_and_postpone_sales_at_most_one_day():
    mandate = TIM_DEFAULT_MANDATE  # extreme_day_pct 0.05
    crash = {"VTI": D("330"), "VXUS": D("75")}  # VTI -9%, VXUS -7% vs yesterday's close
    buys = Tim().run(_ctx(_view("909", {}, closes=crash), mandate))
    assert buys.intents and all(i.side == "buy" for i in buys.intents)  # new cash is invested on a crash day
    # Overweight BND after the crash: the rebalancing sale is postponed, cash still invested.
    holdings = {"VTI": D("2.0"), "VXUS": D("2.5"), "BND": D("2.0")}
    day1 = Tim().run(_ctx(_view("30", holdings, closes=crash), mandate))
    assert not any(i.side == "sell" for i in day1.intents)
    assert day1.detail["sales_postponed_on"] == "2026-10-13" and "postponed" in day1.summary
    nxt = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
    day2 = Tim().run(ManagerContext("tim", "SIM", nxt, "scheduled", mandate, _view("0", holdings, closes=crash), {**day1.detail}, 0.0))
    assert any(i.side == "sell" and i.symbol == "BND" for i in day2.intents)  # never postponed twice in a row


def test_turnover_is_one_sided_so_a_full_rebalance_fits(db, clock):
    f, rh = _firm(db, clock)
    assert f.desk.submit(buy("900")).status == "FILLED"  # initial investment: 89% of NAV
    held = f.ledger.holdings("LIVE", "tim")["tim"]["VTI"]
    sell = OrderIntent("tim", "LIVE", "VTI", "sell", quantity=held, reason="swap")
    assert f.desk.submit(sell).status == "FILLED"  # sells counted separately from buys
    res = f.desk.submit(buy("200"))
    assert res.status == "REJECTED" and res.reasons[0].startswith("TURNOVER_LIMIT: buys")  # buys over 100% today
    assert len(f.events.recent(types=["TURNOVER_LIMIT"], limit=5)) == 1
    f.desk.submit(buy("150"))
    assert len(f.events.recent(types=["TURNOVER_LIMIT"], limit=5)) == 1  # alerted once per day


def test_active_strategy_sleeve_pause_needs_owner_resume(db, clock):
    f, rh = _firm(db, clock)
    _, m = f.registry.mandate("tim")
    f.registry.update_mandate("tim", {**m, "drawdown_policy": "sleeve_pause", "sleeve_drawdown_pause": 0.20}, by="t", note="exercise an active policy")
    f.desk.submit(buy("800"))
    f.take_marks()
    clock.advance(days=1)
    rh.prices["VTI"] = rh.prices["VTI"] * D("0.6")
    f.market._cache.clear()
    f.take_marks()
    res = f.desk.submit(buy("5"))
    assert res.reasons[0].startswith("SLEEVE_DRAWDOWN_PAUSE")
    assert f.events.last_of("SLEEVE_PAUSED") is not None
    rh.prices["VTI"] = rh.prices["VTI"] / D("0.6")  # recovery alone does not resume
    f.market._cache.clear()
    assert f.desk.submit(buy("5")).reasons[0].startswith("SLEEVE_DRAWDOWN_PAUSE")
    sell = OrderIntent("tim", "LIVE", "VTI", "sell", quantity=D("0.1"), reason="exit")
    assert f.desk.submit(sell).status == "FILLED"  # exits continue while paused


def test_daily_loss_pause_for_short_term_strategies(db, clock):
    f, rh = _firm(db, clock)
    _, m = f.registry.mandate("tim")
    f.registry.update_mandate("tim", {**m, "drawdown_policy": "sleeve_stop", "daily_loss_pause": 0.03}, by="t", note="exercise a daily stop")
    f.desk.submit(buy("800"))
    f.take_marks()
    clock.advance(days=1)
    rh.prices["VTI"] = rh.prices["VTI"] * D("0.95")
    f.market._cache.clear()
    assert f.desk.submit(buy("5")).reasons[0].startswith("DAILY_LOSS_PAUSE")


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
