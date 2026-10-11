from __future__ import annotations

from datetime import datetime, timezone

import pytest

from firm.broker.types import Quote
from firm.core import Firm
from firm.managers.base import BookView, ManagerContext
from firm.managers.registry import TIM_DEFAULT_MANDATE
from firm.managers.tim import Tim, validate_mandate
from firm.money import D

from tests.firm_suite.fakes import FakeRobinhood

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)
PRICES = {"VTI": D("300"), "VXUS": D("70"), "BND": D("72")}


def _view(cash, holdings=None, prices=PRICES):
    quotes = {s: Quote(s, bid=p, ask=p, last=p, observed_at=NOW) for s, p in prices.items()}
    return BookView(book="SIM", cash=D(cash), holdings={k: D(v) for k, v in (holdings or {}).items()}, quotes=quotes)


def _ctx(view, mandate=None, last_run=None, now=NOW):
    return ManagerContext("tim", "SIM", now, "scheduled", mandate or TIM_DEFAULT_MANDATE, view, last_run)


def _amounts(result):
    return {i.symbol: (i.side, i.notional or i.quantity) for i in result.intents}


def test_first_run_invests_909_across_targets_keeping_the_buffer():
    r = Tim().run(_ctx(_view("909")))
    buys = {i.symbol: i.notional for i in r.intents}
    assert set(buys) == {"VTI", "VXUS", "BND"}
    assert all(i.side == "buy" for i in r.intents)
    total = sum(buys.values())
    assert D("899") <= total <= D("900")  # 909 minus the 1% buffer
    assert buys["VTI"] == pytest.approx(D("629.9"), abs=D("0.05"))
    assert buys["BND"] >= D("5")


def test_within_bands_holds():
    # Exactly on target (after buffer) → nothing to do.
    r = Tim().run(_ctx(_view("9.09", {"VTI": D("2.0993"), "VXUS": D("3.2142"), "BND": D("0.625")}), last_run={"last_quarterly": "2026Q4"}))
    assert r.intents == [] and r.activity == "holding"


def test_drift_beyond_band_sells_first_then_reruns():
    # VTI at ~80% vs 70% target.
    r = Tim().run(_ctx(_view("0", {"VTI": D("2.4"), "VXUS": D("1.8"), "BND": D("0.45")}), last_run={"last_quarterly": "2026Q4"}))
    sides = _amounts(r)
    assert sides["VTI"][0] == "sell" and len(r.intents) == 1
    assert r.rerun_after_minutes == 10
    assert "VTI" in r.summary


def test_new_cash_goes_to_underweight_without_selling():
    # VXUS a little light (inside bands); new $100 arrives.
    r = Tim().run(_ctx(_view("100", {"VTI": D("2.1"), "VXUS": D("2.9"), "BND": D("0.55")}), last_run={"last_quarterly": "2026Q4"}))
    assert all(i.side == "buy" for i in r.intents)
    assert r.intents and max(r.intents, key=lambda i: i.notional).symbol in {"VXUS", "VTI"}


def test_never_sends_below_minimum_trade():
    holdings = {"VTI": D("2.0"), "VXUS": D("3.0"), "BND": D("0.62")}  # inside the bands
    small = Tim().run(_ctx(_view("12", holdings), last_run={"last_quarterly": "2026Q4"}))
    assert small.intents == []  # $3.33 above the buffer: below the minimum trade
    r = Tim().run(_ctx(_view("20", holdings), last_run={"last_quarterly": "2026Q4"}))
    assert r.intents and all(i.side == "buy" and i.notional >= 5 for i in r.intents)
    assert Tim._allocate(D("9"), {"VTI": D("6"), "VXUS": D("4")}, D("5")) == {"VTI": D("6.00")}


def test_quarterly_rebalance_when_drift_exceeds_one_point():
    holdings = {"VTI": D("2.2"), "VXUS": D("2.9"), "BND": D("0.55")}  # VTI ~ +2pp, inside the 5pp band
    q3 = Tim().run(_ctx(_view("0", holdings), last_run={"last_quarterly": "2026Q4"}))
    assert q3.intents == []
    new_quarter = Tim().run(_ctx(_view("0", holdings), last_run={"last_quarterly": "2026Q3"}))
    assert any(i.side == "sell" and i.symbol == "VTI" for i in new_quarter.intents)
    assert new_quarter.detail["last_quarterly"] == "2026Q4"


def test_funds_removed_from_mandate_are_sold_entirely():
    mandate = {**TIM_DEFAULT_MANDATE, "targets": {"VTI": 0.75, "VXUS": 0.25}}
    r = Tim().run(_ctx(_view("0", {"VTI": D("2"), "VXUS": D("2.5"), "BND": D("1")}), mandate=mandate, last_run={"last_quarterly": "2026Q4"}))
    bnd = next(i for i in r.intents if i.symbol == "BND")
    assert bnd.side == "sell" and bnd.quantity == D("1")


def test_missing_price_means_no_trades():
    prices = {"VTI": D("300"), "VXUS": D("70")}
    r = Tim().run(_ctx(_view("909", prices=prices)))
    assert r.intents == [] and "BND" in r.summary


def test_due_once_per_trading_day_after_run_time():
    tim = Tim()
    before = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    after = datetime(2026, 10, 13, 14, 6, tzinfo=timezone.utc)  # 10:06 ET
    saturday = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
    assert not tim.due(before, TIM_DEFAULT_MANDATE, None)
    assert tim.due(after, TIM_DEFAULT_MANDATE, None)
    assert not tim.due(after, TIM_DEFAULT_MANDATE, after)
    assert not tim.due(saturday, TIM_DEFAULT_MANDATE, None)


def test_mandate_validation():
    assert validate_mandate(TIM_DEFAULT_MANDATE) == []
    assert "target weights must sum to 100%" in validate_mandate({**TIM_DEFAULT_MANDATE, "targets": {"VTI": 0.7}})


def test_end_to_end_sim_then_live_through_the_desk(db, clock):
    rh = FakeRobinhood(clock=clock)
    firm = Firm(db, reader=rh, writer=lambda: rh, clock=clock, environ={})
    firm.reconciler.bootstrap(rh)
    firm.allocate("tim", D("909"), by="owner")
    firm.registry.set_mode("tim", "SIM", by="owner")
    out = firm.run_manager("tim")
    assert [o["status"] for o in out.orders] == ["FILLED"] * 3
    assert "place_equity_order" not in rh.calls
    sim = firm.ledger.holdings("SIM", "tim")["tim"]
    assert set(sim) == {"VTI", "VXUS", "BND"}
    # Promote: the live sleeve still holds Tim's untouched cash.
    firm.registry.set_mode("tim", "LIVE", by="owner")
    out = firm.run_manager("tim")
    assert [o["status"] for o in out.orders] == ["FILLED"] * 3
    assert firm.reconciler.run(rh).status == "OK"
    assert firm.ledger.verify_lots("LIVE") == []
    # Second run the same day: nothing to do.
    out = firm.run_manager("tim")
    assert out.orders == []
    run = firm.events.last_of("MANAGER_RUN", manager_id="tim")
    assert run.payload["activity"] == "holding"


def test_manager_crash_is_isolated(db, clock):
    rh = FakeRobinhood(clock=clock)

    class Broken:
        id = "alex"

        def symbols(self, mandate):
            return []

        def due(self, *a):
            return True

        def run(self, ctx):
            raise RuntimeError("boom")

    firm = Firm(db, reader=rh, writer=lambda: rh, clock=clock, environ={}, managers={"tim": Tim(), "alex": Broken()})
    firm.reconciler.bootstrap(rh)
    firm.allocate("tim", D("500"), by="o")
    firm.allocate("alex", D("100"), by="o")
    firm.registry.set_mode("tim", "SIM", by="o")
    firm.registry.set_mode("alex", "SIM", by="o")
    bad = firm.run_manager("alex")
    good = firm.run_manager("tim")
    assert bad.error and firm.registry.get("alex").status == "DEGRADED"
    assert good.ran and good.orders
