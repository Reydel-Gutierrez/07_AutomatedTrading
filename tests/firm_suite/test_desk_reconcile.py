from __future__ import annotations

from datetime import datetime, timezone

import pytest

from firm.core import Firm
from firm.ledger import FIRM, SUSPENSE
from firm.managers.base import OrderIntent
from firm.money import D

from tests.firm_suite.fakes import FakeRobinhood


@pytest.fixture
def rh(clock):
    return FakeRobinhood(clock=clock)


def _firm(db, clock, rh, *, writes=True, env=None):
    return Firm(db, reader=rh, writer=(lambda: rh) if writes else (lambda: None), clock=clock, environ=env or {})


def _open(firm, rh, tim_amount="909.00", mode="LIVE"):
    firm.reconciler.bootstrap(rh)
    firm.allocate("tim", D(tim_amount), by="owner")
    firm.registry.set_mode("tim", mode, by="owner")


def buy(amount, symbol="VTI", book="LIVE"):
    return OrderIntent("tim", book, symbol, "buy", notional=D(amount), reason="test")


def test_live_buy_fills_and_books_match_broker(db, clock, rh):
    firm = _firm(db, clock, rh)
    _open(firm, rh)
    result = firm.desk.submit(buy("100"))
    assert result.status == "FILLED"
    assert "place_equity_order" in rh.calls
    assert firm.ledger.holdings("LIVE", "tim")["tim"]["VTI"] > 0
    rec = firm.reconciler.run(rh)
    assert rec.status == "OK", rec.reasons
    assert firm.ledger.cash("LIVE", "tim") == D("909") - D("100.00")  # dollar order debits its fills


def test_kill_switch_and_disabled_writes_block_without_broker_calls(db, clock, rh):
    firm = _firm(db, clock, rh, writes=False)
    _open(firm, rh)
    res = firm.desk.submit(buy("50"))
    assert res.status == "BLOCKED" and "BROKER_WRITES_DISABLED" in res.reasons[0]
    assert "place_equity_order" not in rh.calls
    firm.controls.engage("drill", by="owner")
    assert firm.desk.submit(buy("50")).reasons == ["KILL_SWITCH"]
    firm2 = _firm(db, clock, rh, env={"AGENTIC_KILL_SWITCH": "1"})
    assert firm2.controls.kill_switch().engaged


def test_market_closed_and_wide_spread_defer_without_an_order(db, clock, rh):
    firm = _firm(db, clock, rh)
    _open(firm, rh)
    clock.set(datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc))  # Saturday
    assert firm.desk.submit(buy("50")).status == "DEFERRED"
    clock.set(datetime(2026, 10, 13, 13, 45, tzinfo=timezone.utc))  # 9:45 ET, before the 10:00 window
    assert firm.desk.submit(buy("50")).reasons == ["OUTSIDE_WINDOW"]
    clock.set(datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc))
    rh.spread_bps = D("80")
    firm.market._cache.clear()
    assert firm.desk.submit(buy("50")).status == "DEFERRED"
    rh.spread_bps, rh.quote_age_seconds = D("4"), 120
    firm.market._cache.clear()
    assert firm.desk.submit(buy("50")).reasons[0].startswith("QUOTE_STALE")
    assert db.one("SELECT COUNT(*) AS n FROM orders")["n"] == 0


def test_manager_cannot_spend_unallocated_or_sell_others_shares(db, clock, rh):
    firm = _firm(db, clock, rh)
    _open(firm, rh, tim_amount="100")
    res = firm.desk.submit(buy("150"))
    assert res.status == "REJECTED" and res.reasons[0].startswith("INSUFFICIENT_MANAGER_CASH")
    res = firm.desk.submit(OrderIntent("tim", "LIVE", "VTI", "sell", quantity=D("1"), reason="t"))
    assert res.reasons[0].startswith("INSUFFICIENT_MANAGER_SHARES")
    res = firm.desk.submit(buy("10", symbol="NVDA"))
    assert res.reasons[0].startswith("NOT_IN_MANDATE")


def test_ambiguous_submission_trips_kill_switch_then_recovers_by_ref(db, clock, rh):
    firm = _firm(db, clock, rh)
    _open(firm, rh)
    rh.place_mode = "ambiguous_but_placed"
    res = firm.desk.submit(buy("100"))
    assert res.status == "UNKNOWN"
    assert firm.controls.kill_switch().engaged
    assert firm.reconciler.run(rh).status in {"OK", "HOLD"}  # poll finds the order by ref_id
    order = firm.desk.order(res.order_id)
    assert order["status"] == "FILLED"
    assert firm.reconciler.run(rh).status == "OK"
    assert firm.controls.kill_switch().engaged  # release stays a human action


def test_broker_reject_and_review_alert(db, clock, rh):
    firm = _firm(db, clock, rh)
    _open(firm, rh)
    rh.review_checks = [{"type": "pattern_day_trader"}]
    assert firm.desk.submit(buy("10")).status == "REJECTED"
    rh.review_checks = []
    rh.place_mode = "reject"
    assert firm.desk.submit(buy("10")).status == "REJECTED"
    assert firm.ledger.free_cash("LIVE", "tim") == D("909")


def test_robinhood_app_approval_setting_blocks_orders(db, clock, rh):
    firm = _firm(db, clock, rh)
    _open(firm, rh)
    rh.approval_required = True
    res = firm.desk.submit(buy("10"))
    assert res.reasons == ["ROBINHOOD_APPROVAL_REQUIRED"]
    assert firm.events.last_of("BROKER_APPROVAL_REQUIRED") is not None


def test_partial_fill_reserves_only_the_remainder_then_completes(db, clock, rh):
    firm = _firm(db, clock, rh)
    _open(firm, rh)
    rh.place_mode = "partial"
    res = firm.desk.submit(buy("100"))
    assert res.status == "SUBMITTED"
    free = firm.ledger.free_cash("LIVE", "tim")
    assert D("808") < free < D("810")  # 909 - ~50 filled - ~50 reserved
    assert firm.reconciler.run(rh).status == "IN_FLIGHT"
    rh.complete(firm.desk.order(res.order_id)["broker_order_id"])
    assert firm.reconciler.run(rh).status == "OK"
    assert firm.desk.order(res.order_id)["status"] == "FILLED"


def test_deposit_dividend_withdrawal_and_rounding_classification(db, clock, rh):
    firm = _firm(db, clock, rh)
    _open(firm, rh)
    firm.desk.submit(buy("700"))
    assert firm.reconciler.run(rh).status == "OK"
    # Deposit: shows up as pending at Robinhood.
    rh.deposit("50")
    rh.pending_deposits = D("50")
    assert firm.reconciler.run(rh).status == "OK"
    assert firm.ledger.balances("LIVE")["tim"] > D("209")  # 90% of the deposit allocated to Tim
    # Dividend-sized credit with holdings.
    rh.deposit("1.85")
    firm.market._cache.clear()
    rec = firm.reconciler.run(rh)
    assert rec.status == "OK" and any("dividend" in a for a in rec.actions)
    # Rounding: a few cents off right after a fill.
    firm.desk.submit(buy("20"))
    rh.deposit("0.03")
    assert any("rounding" in a for a in firm.reconciler.run(rh).actions)
    # Withdrawal larger than unallocated → suspense + hold.
    unalloc = firm.ledger.free_cash("LIVE", FIRM)
    rh.cash -= unalloc + D("20")
    rec = firm.reconciler.run(rh)
    assert rec.status == "HOLD" and firm.controls.reconcile_hold()["active"]
    assert firm.desk.submit(buy("10")).reasons == ["RECONCILE_HOLD"]
    firm.reconciler.resolve_suspense_cash(as_kind="manager", manager="tim", by="owner", note="came from Tim")
    assert firm.reconciler.run(rh).status == "OK"
    assert not firm.controls.reconcile_hold()["active"]


def test_foreign_order_holds_buys(db, clock, rh):
    firm = _firm(db, clock, rh)
    _open(firm, rh)
    rh.foreign_order("VTI", "buy", "0.1", placed_agent="user")
    rec = firm.reconciler.run(rh)
    assert rec.status == "HOLD"
    assert any(r.startswith("FOREIGN_ORDER") for r in rec.reasons)
    assert firm.events.last_of("FOREIGN_ORDER") is not None


def test_account_mismatch_trips_kill_switch(db, clock, rh):
    firm = _firm(db, clock, rh)
    _open(firm, rh)
    rh.wrong_account = True
    assert firm.reconciler.run(rh).status == "HOLD"
    assert firm.controls.kill_switch().engaged


def test_sim_book_fills_at_quote_and_never_touches_the_broker(db, clock, rh):
    firm = _firm(db, clock, rh)
    _open(firm, rh, mode="SIM")
    res = firm.desk.submit(buy("100", book="SIM"))
    assert res.status == "FILLED"
    assert "place_equity_order" not in rh.calls and "review_equity_order" not in rh.calls
    assert firm.ledger.holdings("SIM", "tim")["tim"]["VTI"] > 0
    assert firm.ledger.holdings("LIVE", "tim") == {}
    assert firm.desk.submit(buy("100", book="LIVE")).reasons[0].startswith("MODE_MISMATCH")


def test_bootstrap_refuses_with_open_broker_orders_and_parks_positions(db, clock, rh):
    rh.foreign_order("VTI", "buy", "1")
    firm = _firm(db, clock, rh)
    firm.reconciler.bootstrap(rh)
    assert firm.ledger.holdings("LIVE", SUSPENSE)[SUSPENSE]["VTI"] == D("1")
    assert firm.controls.reconcile_hold()["active"]


def test_crash_between_submit_and_ack_becomes_unknown(db, clock, rh):
    firm = _firm(db, clock, rh)
    _open(firm, rh)
    oid = firm.desk._persist(buy("10"))
    firm.desk._update(oid, status="SUBMITTED")
    clock.advance(minutes=11)
    firm.desk.poll(rh)
    assert firm.desk.order(oid)["status"] == "UNKNOWN"
    assert firm.controls.kill_switch().engaged


def test_drawdown_brake_ignores_opening_allocations_and_deposits_but_catches_real_losses(db, clock, rh):
    firm = _firm(db, clock, rh)
    _open(firm, rh)  # opening balance + allocation to Tim
    assert firm.reconciler.run(rh).status == "OK"
    assert not firm.controls.brake().get("engaged")
    firm.desk.submit(buy("900"))
    rh.deposit("500")
    rh.pending_deposits = D("500")
    firm.reconciler.run(rh)
    assert not firm.controls.brake().get("engaged")
    assert D(firm.settings.get("hwm")["value"]) >= D("1509")
    # A real 50% fall in the holdings is ~30% of the firm: the brake engages.
    rh.prices["VTI"] = rh.prices["VTI"] / 2
    firm.market._cache.clear()
    firm.reconciler.run(rh)
    assert firm.controls.brake().get("engaged")
