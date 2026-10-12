"""Per-manager virtual portfolios on one brokerage account.

The ledger is append-only. A transaction is a set of entries sharing a
``txn_id``; each entry moves cash and/or shares into (+) or out of (-) a
bucket. Buckets: ``firm`` (unallocated cash), ``suspense`` (unexplained
broker deltas), and one per manager.

Two books share the schema: LIVE (real money, reconciled to Robinhood) and
SIM (simulated execution). Capital movements — opening balance, external
flows, allocations — are mirrored into SIM so a manager's simulated results
start from exactly the capital it has live. Trades and dividends are
book-specific.

Lots are FIFO and maintained in the same transaction as the fill, and can be
rebuilt from the ledger at any time (``verify_lots``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable
from uuid import uuid4

from firm.clock import Clock, iso, utc_now
from firm.db import Database
from firm.money import QTY_TOLERANCE, ZERO, D, cents, s

FIRM = "firm"
SUSPENSE = "suspense"
BOOKS = ("LIVE", "SIM")

# Kinds mirrored into SIM when posted to LIVE.
CAPITAL_KINDS = frozenset({"OPENING_BALANCE", "EXTERNAL_IN", "EXTERNAL_OUT", "ALLOCATE", "DEALLOCATE"})


class LedgerError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    bucket: str
    cash: Decimal = ZERO
    symbol: str | None = None
    quantity: Decimal = ZERO
    price: Decimal | None = None


@dataclass(frozen=True)
class Lot:
    id: int
    symbol: str
    opened_at: str
    quantity: Decimal
    open_quantity: Decimal
    cost_basis: Decimal
    realized_pnl: Decimal

    @property
    def open_cost(self) -> Decimal:
        if self.quantity == 0:
            return ZERO
        return self.cost_basis * self.open_quantity / self.quantity


class Ledger:
    def __init__(self, db: Database, *, clock: Clock = utc_now) -> None:
        self.db = db
        self.clock = clock

    # -- low level ---------------------------------------------------------
    def _post(
        self,
        conn,
        *,
        book: str,
        kind: str,
        entries: Iterable[Entry],
        by: str,
        memo: str | None = None,
        order_id: int | None = None,
        execution_id: str | None = None,
        txn_id: str | None = None,
        ts: str | None = None,
    ) -> tuple[str, list[int]]:
        if book not in BOOKS:
            raise LedgerError(f"unknown book {book}")
        txn = txn_id or uuid4().hex
        stamp = ts or iso(self.clock())
        ids = []
        for e in entries:
            cur = conn.execute(
                "INSERT INTO ledger (txn_id, ts, book, kind, bucket, symbol, quantity, cash, price, order_id, execution_id, memo, created_by)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    txn,
                    stamp,
                    book,
                    kind,
                    e.bucket,
                    e.symbol,
                    s(e.quantity),
                    s(e.cash),
                    s(e.price) if e.price is not None else None,
                    order_id,
                    execution_id,
                    memo,
                    by,
                ),
            )
            ids.append(int(cur.lastrowid))
        return txn, ids

    def _capital(self, *, kind: str, entries: list[Entry], by: str, memo: str | None, books: Iterable[str]) -> str:
        txn = uuid4().hex
        with self.db.tx() as conn:
            for book in books:
                # LIVE governs. A SIM sleeve that is fully invested may go negative on a
                # mirrored deallocation; its manager then raises cash on the next run.
                if book == "LIVE":
                    self._check_cash(conn, book, entries)
                self._post(conn, book=book, kind=kind, entries=entries, by=by, memo=memo, txn_id=f"{txn}:{book}")
        return txn

    def _check_cash(self, conn, book: str, entries: list[Entry]) -> None:
        for e in entries:
            if e.cash < 0 and e.bucket != SUSPENSE:
                have = self._cash(conn, book, e.bucket) - self._reserved(conn, book, e.bucket)
                if have + e.cash < 0:
                    raise LedgerError(f"{book}:{e.bucket} has ${cents(have)} free; cannot move ${cents(-e.cash)}")

    # -- capital (mirrored LIVE -> SIM) ----------------------------------
    def opening_balance(self, amount: Decimal, *, by: str, memo: str = "opening balance from broker") -> str:
        return self._capital(kind="OPENING_BALANCE", entries=[Entry(FIRM, cents(amount))], by=by, memo=memo, books=BOOKS)

    def external_in(self, amount: Decimal, *, by: str, memo: str) -> str:
        if amount <= 0:
            raise LedgerError("external_in amount must be positive")
        return self._capital(kind="EXTERNAL_IN", entries=[Entry(FIRM, cents(amount))], by=by, memo=memo, books=BOOKS)

    def external_out(self, amount: Decimal, *, by: str, memo: str) -> str:
        if amount <= 0:
            raise LedgerError("external_out amount must be positive")
        return self._capital(kind="EXTERNAL_OUT", entries=[Entry(FIRM, -cents(amount))], by=by, memo=memo, books=BOOKS)

    def allocate(self, manager: str, amount: Decimal, *, by: str, memo: str | None = None) -> str:
        amt = cents(amount)
        if amt <= 0:
            raise LedgerError("allocation must be positive")
        return self._capital(kind="ALLOCATE", entries=[Entry(FIRM, -amt), Entry(manager, amt)], by=by, memo=memo, books=BOOKS)

    def deallocate(self, manager: str, amount: Decimal, *, by: str, memo: str | None = None) -> str:
        """Return a manager's free cash to unallocated. Checked against the LIVE and SIM books."""
        amt = cents(amount)
        if amt <= 0:
            raise LedgerError("deallocation must be positive")
        return self._capital(kind="DEALLOCATE", entries=[Entry(manager, -amt), Entry(FIRM, amt)], by=by, memo=memo, books=BOOKS)

    def transfer(self, src: str, dst: str, amount: Decimal, *, kind: str, by: str, memo: str | None = None) -> str:
        """Move free cash between two buckets (e.g. the CIO's pool and a manager), mirrored in both books."""
        amt = cents(amount)
        if amt <= 0 or src == dst:
            raise LedgerError("transfer needs a positive amount between two different buckets")
        return self._capital(kind=kind, entries=[Entry(src, -amt), Entry(dst, amt)], by=by, memo=memo, books=BOOKS)

    # -- book-specific -------------------------------------------------------
    def adjust(self, book: str, bucket: str, cash: Decimal, *, kind: str, by: str, memo: str) -> str:
        """Single-bucket cash adjustment (rounding true-up, suspense, dividend residue)."""
        with self.db.tx() as conn:
            txn, _ = self._post(conn, book=book, kind=kind, entries=[Entry(bucket, D(cash))], by=by, memo=memo)
        return txn

    def dividend(self, book: str, symbol: str, shares_by_manager: dict[str, Decimal], amount: Decimal, *, by: str, memo: str) -> str:
        """Split a cash distribution pro rata to holders' shares; rounding residue goes to the largest holder."""
        total_shares = sum(shares_by_manager.values(), ZERO)
        if total_shares <= 0:
            raise LedgerError(f"no holders of {symbol} for a dividend")
        amt = cents(amount)
        parts = {m: cents(amt * q / total_shares) for m, q in shares_by_manager.items() if q > 0}
        residue = amt - sum(parts.values(), ZERO)
        if residue:
            largest = max(parts, key=lambda m: shares_by_manager[m])
            parts[largest] += residue
        entries = [Entry(m, v, symbol=symbol) for m, v in parts.items()]
        with self.db.tx() as conn:
            txn, _ = self._post(conn, book=book, kind="DIVIDEND", entries=entries, by=by, memo=memo)
        return txn

    def record_fill(
        self,
        *,
        book: str,
        manager: str,
        order_id: int,
        execution_id: str,
        side: str,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        fees: Decimal = ZERO,
        ts: str | None = None,
    ) -> bool:
        """Post one execution and update lots. Idempotent on (book, execution_id). Returns False if already posted."""
        qty, px, fee = D(quantity), D(price), D(fees)
        if qty <= 0 or px <= 0:
            raise LedgerError("fill quantity and price must be positive")
        gross = cents(qty * px)
        with self.db.tx() as conn:
            seen = conn.execute(
                "SELECT 1 FROM ledger WHERE book = ? AND execution_id = ? AND bucket = ?", (book, execution_id, manager)
            ).fetchone()
            if seen:
                return False
            stamp = ts or iso(self.clock())
            if side == "buy":
                entry = Entry(manager, -(gross + fee), symbol=symbol, quantity=qty, price=px)
                _, (ledger_id,) = self._post(
                    conn, book=book, kind="BUY", entries=[entry], by="desk", order_id=order_id, execution_id=execution_id, ts=stamp
                )
                conn.execute(
                    "INSERT INTO lots (book, manager_id, symbol, opened_at, quantity, open_quantity, cost_basis, source_ledger_id)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (book, manager, symbol, stamp, s(qty), s(qty), s(gross + fee), ledger_id),
                )
            elif side == "sell":
                held = self._quantity(conn, book, manager, symbol)
                if qty > held + QTY_TOLERANCE:
                    raise LedgerError(f"{manager} sells {qty} {symbol} but holds {held}")
                proceeds = gross - fee
                entry = Entry(manager, proceeds, symbol=symbol, quantity=-qty, price=px)
                self._post(conn, book=book, kind="SELL", entries=[entry], by="desk", order_id=order_id, execution_id=execution_id, ts=stamp)
                self._consume_lots(conn, book, manager, symbol, qty, proceeds)
            else:
                raise LedgerError(f"bad side {side}")
        return True

    def _consume_lots(self, conn, book: str, manager: str, symbol: str, qty: Decimal, proceeds: Decimal) -> None:
        remaining = qty
        rows = conn.execute(
            "SELECT * FROM lots WHERE book = ? AND manager_id = ? AND symbol = ? ORDER BY opened_at, id",
            (book, manager, symbol),
        ).fetchall()
        for row in rows:
            open_q = D(row["open_quantity"])
            if open_q <= 0 or remaining <= 0:
                continue
            take = min(open_q, remaining)
            basis = D(row["cost_basis"]) * take / D(row["quantity"])
            share_of_proceeds = proceeds * take / qty
            conn.execute(
                "UPDATE lots SET open_quantity = ?, realized_pnl = ? WHERE id = ?",
                (s(open_q - take), s(D(row["realized_pnl"]) + share_of_proceeds - basis), row["id"]),
            )
            remaining -= take
        if remaining > QTY_TOLERANCE:
            raise LedgerError(f"lots for {manager}:{symbol} short by {remaining}")

    # -- reads -----------------------------------------------------------------
    def _cash(self, conn, book: str, bucket: str) -> Decimal:
        rows = conn.execute("SELECT cash FROM ledger WHERE book = ? AND bucket = ?", (book, bucket)).fetchall()
        return sum((D(r["cash"]) for r in rows), ZERO)

    def _quantity(self, conn, book: str, bucket: str, symbol: str) -> Decimal:
        rows = conn.execute(
            "SELECT quantity FROM ledger WHERE book = ? AND bucket = ? AND symbol = ?", (book, bucket, symbol)
        ).fetchall()
        return sum((D(r["quantity"]) for r in rows), ZERO)

    def _reserved(self, conn, book: str, bucket: str) -> Decimal:
        """Cash committed to open BUY orders not yet filled."""
        rows = conn.execute(
            "SELECT notional, quantity, average_price, filled_quantity FROM orders"
            " WHERE book = ? AND manager_id = ? AND side = 'buy' AND status IN ('PENDING','SUBMITTED','UNKNOWN')",
            (book, bucket),
        ).fetchall()
        total = ZERO
        for r in rows:
            if not r["notional"]:
                continue
            filled = D(r["filled_quantity"]) * D(r["average_price"]) if r["average_price"] else ZERO
            total += max(D(r["notional"]) - filled, ZERO)
        return total

    def cash(self, book: str, bucket: str) -> Decimal:
        return self._cash(self.db.read(), book, bucket)

    def free_cash(self, book: str, bucket: str) -> Decimal:
        conn = self.db.read()
        return self._cash(conn, book, bucket) - self._reserved(conn, book, bucket)

    def balances(self, book: str) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for r in self.db.query("SELECT bucket, cash FROM ledger WHERE book = ?", (book,)):
            out[r["bucket"]] += D(r["cash"])
        return dict(out)

    def holdings(self, book: str, bucket: str | None = None) -> dict[str, dict[str, Decimal]]:
        """{bucket: {symbol: quantity}} for non-zero positions."""
        sql = "SELECT bucket, symbol, quantity FROM ledger WHERE book = ? AND symbol IS NOT NULL AND quantity != '0'"
        params: list = [book]
        if bucket:
            sql += " AND bucket = ?"
            params.append(bucket)
        acc: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
        for r in self.db.query(sql, tuple(params)):
            acc[r["bucket"]][r["symbol"]] += D(r["quantity"])
        return {b: {sym: q for sym, q in syms.items() if abs(q) > QTY_TOLERANCE} for b, syms in acc.items()}

    def firm_positions(self, book: str) -> dict[str, Decimal]:
        total: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for syms in self.holdings(book).values():
            for sym, q in syms.items():
                total[sym] += q
        return {k: v for k, v in total.items() if abs(v) > QTY_TOLERANCE}

    def holders(self, book: str, symbol: str) -> dict[str, Decimal]:
        return {b: syms[symbol] for b, syms in self.holdings(book).items() if symbol in syms and syms[symbol] > 0}

    def open_lots(self, book: str, manager: str, symbol: str | None = None) -> list[Lot]:
        sql = "SELECT * FROM lots WHERE book = ? AND manager_id = ? AND open_quantity != '0'"
        params: list = [book, manager]
        if symbol:
            sql += " AND symbol = ?"
            params.append(symbol)
        return [
            Lot(r["id"], r["symbol"], r["opened_at"], D(r["quantity"]), D(r["open_quantity"]), D(r["cost_basis"]), D(r["realized_pnl"]))
            for r in self.db.query(sql + " ORDER BY opened_at, id", tuple(params))
        ]

    def realized_pnl(self, book: str, manager: str) -> Decimal:
        rows = self.db.query("SELECT realized_pnl FROM lots WHERE book = ? AND manager_id = ?", (book, manager))
        return sum((D(r["realized_pnl"]) for r in rows), ZERO)

    def net_capital(self, book: str, bucket: str, *, since_id: int = 0, until_id: int | None = None) -> Decimal:
        """Capital moved into a bucket (allocations, external flows) — the TWR cash-flow term."""
        sql = "SELECT cash FROM ledger WHERE book = ? AND bucket = ? AND kind IN ('OPENING_BALANCE','EXTERNAL_IN','EXTERNAL_OUT','ALLOCATE','DEALLOCATE','CIO_GRANT','CIO_RECALL') AND id > ?"
        params: list = [book, bucket, since_id]
        if until_id is not None:
            sql += " AND id <= ?"
            params.append(until_id)
        return sum((D(r["cash"]) for r in self.db.query(sql, tuple(params))), ZERO)

    def verify_lots(self, book: str) -> list[str]:
        """Lots must agree with ledger quantities per (manager, symbol). Returns problems."""
        problems = []
        holdings = self.holdings(book)
        lot_qty: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        for r in self.db.query("SELECT manager_id, symbol, open_quantity FROM lots WHERE book = ?", (book,)):
            lot_qty[(r["manager_id"], r["symbol"])] += D(r["open_quantity"])
        keys = set(lot_qty) | {(b, sym) for b, syms in holdings.items() for sym in syms}
        for bucket, sym in sorted(keys):
            ledger_q = holdings.get(bucket, {}).get(sym, ZERO)
            if abs(ledger_q - lot_qty[(bucket, sym)]) > QTY_TOLERANCE:
                problems.append(f"{book}:{bucket}:{sym} ledger={ledger_q} lots={lot_qty[(bucket, sym)]}")
        return problems
