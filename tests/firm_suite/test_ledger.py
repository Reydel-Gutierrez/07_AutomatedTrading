from __future__ import annotations

import random
from decimal import Decimal

import pytest

from firm.ledger import FIRM, Ledger, LedgerError
from firm.money import D, ZERO, cents

TIM = "tim"


def _order(db, book="LIVE", manager=TIM, symbol="VTI", side="buy", notional=None, status="FILLED"):
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO orders (client_ref, book, manager_id, symbol, side, notional, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (f"ref-{random.random()}", book, manager, symbol, side, notional, status, "t", "t"),
        )
        return int(cur.lastrowid)


def test_opening_and_allocation_are_mirrored_into_sim(ledger):
    ledger.opening_balance(D("1010.00"), by="migrate")
    ledger.allocate(TIM, D("909.00"), by="owner")
    for book in ("LIVE", "SIM"):
        bal = ledger.balances(book)
        assert bal[FIRM] == D("101.00")
        assert bal[TIM] == D("909.00")


def test_cannot_allocate_more_than_unallocated(ledger):
    ledger.opening_balance(D("100"), by="t")
    with pytest.raises(LedgerError):
        ledger.allocate(TIM, D("100.01"), by="owner")
    assert ledger.balances("LIVE")[FIRM] == D("100.00")


def test_buy_sell_lots_and_realized_pnl(ledger, db):
    ledger.opening_balance(D("1000"), by="t")
    ledger.allocate(TIM, D("1000"), by="t")
    oid = _order(db)
    assert ledger.record_fill(book="LIVE", manager=TIM, order_id=oid, execution_id="e1", side="buy", symbol="VTI", quantity=D("2"), price=D("100"))
    assert ledger.record_fill(book="LIVE", manager=TIM, order_id=oid, execution_id="e2", side="buy", symbol="VTI", quantity=D("1"), price=D("130"))
    # Idempotent on execution id.
    assert not ledger.record_fill(book="LIVE", manager=TIM, order_id=oid, execution_id="e2", side="buy", symbol="VTI", quantity=D("1"), price=D("130"))
    assert ledger.cash("LIVE", TIM) == D("670.00")
    sell = _order(db, side="sell")
    ledger.record_fill(book="LIVE", manager=TIM, order_id=sell, execution_id="e3", side="sell", symbol="VTI", quantity=D("2.5"), price=D("120"), fees=D("0.02"))
    # FIFO: 2 @100 (basis 200) + 0.5 of the 130 lot (basis 65) = 265 basis; proceeds 300 - 0.02
    assert ledger.realized_pnl("LIVE", TIM) == D("34.98")
    lots = ledger.open_lots("LIVE", TIM, "VTI")
    assert [l.open_quantity for l in lots] == [D("0.5")]
    assert lots[0].open_cost == D("65")
    assert ledger.holdings("LIVE")[TIM]["VTI"] == D("0.5")
    assert ledger.verify_lots("LIVE") == []


def test_cannot_sell_shares_the_manager_does_not_hold(ledger, db):
    ledger.opening_balance(D("1000"), by="t")
    ledger.allocate(TIM, D("500"), by="t")
    ledger.allocate("alex", D("500"), by="t")
    oid = _order(db, manager="alex")
    ledger.record_fill(book="LIVE", manager="alex", order_id=oid, execution_id="a1", side="buy", symbol="VTI", quantity=D("1"), price=D("100"))
    with pytest.raises(LedgerError):
        ledger.record_fill(book="LIVE", manager=TIM, order_id=oid, execution_id="t1", side="sell", symbol="VTI", quantity=D("1"), price=D("100"))
    assert ledger.firm_positions("LIVE") == {"VTI": D("1")}


def test_dividend_split_by_shares_and_rounding_residue(ledger, db):
    ledger.opening_balance(D("1000"), by="t")
    ledger.allocate(TIM, D("500"), by="t")
    ledger.allocate("alex", D("500"), by="t")
    for m, q in ((TIM, "2"), ("alex", "1")):
        oid = _order(db, manager=m)
        ledger.record_fill(book="LIVE", manager=m, order_id=oid, execution_id=f"x{m}", side="buy", symbol="VTI", quantity=D(q), price=D("100"))
    ledger.dividend("LIVE", "VTI", ledger.holders("LIVE", "VTI"), D("1.00"), by="reconcile", memo="VTI distribution")
    assert ledger.cash("LIVE", TIM) == D("300.67")  # 0.67 incl. 0.01 residue to the largest holder
    assert ledger.cash("LIVE", "alex") == D("400.33")


def test_deallocation_from_fully_invested_sim_sleeve_goes_negative_in_sim_only(ledger, db):
    ledger.opening_balance(D("100"), by="t")
    ledger.allocate(TIM, D("100"), by="t")
    oid = _order(db, book="SIM")
    ledger.record_fill(book="SIM", manager=TIM, order_id=oid, execution_id="s1", side="buy", symbol="VTI", quantity=D("1"), price=D("90"))
    ledger.deallocate(TIM, D("50"), by="owner")
    assert ledger.cash("LIVE", TIM) == D("50.00")
    assert ledger.cash("SIM", TIM) == D("-40.00")


def test_reserved_cash_blocks_double_spend(ledger, db):
    ledger.opening_balance(D("100"), by="t")
    ledger.allocate(TIM, D("100"), by="t")
    _order(db, notional="80", status="SUBMITTED")
    assert ledger.free_cash("LIVE", TIM) == D("20.00")
    with pytest.raises(LedgerError):
        ledger.deallocate(TIM, D("30"), by="owner")


def test_randomized_sequences_keep_invariants(ledger, db):
    rng = random.Random(7)
    managers = [TIM, "alex", "max"]
    prices = {"VTI": D("300"), "VXUS": D("70"), "BND": D("72")}
    ledger.opening_balance(D("5000"), by="t")
    expected_external = D("5000")
    n = 0
    for _ in range(400):
        op = rng.choice(["alloc", "dealloc", "buy", "sell", "in", "out", "div"])
        m = rng.choice(managers)
        sym = rng.choice(list(prices))
        try:
            if op == "alloc":
                ledger.allocate(m, cents(rng.uniform(1, 300)), by="t")
            elif op == "dealloc":
                ledger.deallocate(m, cents(rng.uniform(1, 200)), by="t")
            elif op == "in":
                amt = cents(rng.uniform(1, 200))
                ledger.external_in(amt, by="t", memo="dep")
                expected_external += amt
            elif op == "out":
                amt = cents(rng.uniform(1, 100))
                if ledger.free_cash("LIVE", FIRM) >= amt:
                    ledger.external_out(amt, by="t", memo="wd")
                    expected_external -= amt
            elif op == "buy":
                spend = min(ledger.free_cash("LIVE", m), cents(rng.uniform(5, 150)))
                if spend >= 5:
                    qty = (spend / prices[sym]).quantize(Decimal("0.000001"), rounding="ROUND_DOWN")
                    n += 1
                    ledger.record_fill(book="LIVE", manager=m, order_id=_order(db, manager=m, symbol=sym), execution_id=f"b{n}", side="buy", symbol=sym, quantity=qty, price=prices[sym])
            elif op == "sell":
                held = ledger.holdings("LIVE", m).get(m, {}).get(sym, ZERO)
                if held > 0:
                    qty = (held * D(rng.uniform(0.1, 1))).quantize(Decimal("0.000001"), rounding="ROUND_DOWN")
                    if qty > 0:
                        n += 1
                        ledger.record_fill(book="LIVE", manager=m, order_id=_order(db, manager=m, symbol=sym, side="sell"), execution_id=f"s{n}", side="sell", symbol=sym, quantity=qty, price=prices[sym] * D("1.01"))
            elif op == "div":
                holders = ledger.holders("LIVE", sym)
                if holders:
                    ledger.dividend("LIVE", sym, holders, cents(rng.uniform(0.01, 3)), by="t", memo="d")
        except LedgerError:
            pass
        prices[sym] *= D(rng.uniform(0.98, 1.02))
        bal = ledger.balances("LIVE")
        assert all(v >= D("-0.005") for k, v in bal.items() if k != "suspense"), bal
    assert ledger.verify_lots("LIVE") == []
    # Capital in buckets equals external capital; trading only moves value between cash and shares.
    capital = sum((ledger.net_capital("LIVE", b) for b in [FIRM, *managers]), ZERO)
    assert capital == expected_external
