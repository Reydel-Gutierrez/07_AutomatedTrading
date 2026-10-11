"""Daily marks and time-weighted returns, per manager and for the firm.

A mark is taken after each close. Capital moved into or out of a bucket
(allocations, deposits, withdrawals) is a flow, not performance:

    r_t = (NAV_t − flow_t) / NAV_{t−1} − 1        (flows assumed at the close)

Returns chain geometrically. The S&P 500 benchmark uses SPY closes on the
same dates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from firm.db import Database
from firm.ledger import FIRM, SUSPENSE, Ledger
from firm.market import MarketData
from firm.money import ZERO, D, cents, s
from firm.settings import Settings

BENCHMARK = "SPY"


@dataclass(frozen=True)
class Mark:
    date: str
    nav: Decimal
    cash: Decimal
    invested: Decimal
    net_flow: Decimal
    benchmark_close: Decimal | None


class Performance:
    def __init__(self, db: Database, ledger: Ledger, market: MarketData, settings: Settings) -> None:
        self.db = db
        self.ledger = ledger
        self.market = market
        self.settings = settings

    def take_marks(self, day: date, *, closes: dict[str, Decimal], benchmark_close: Decimal | None, books=("LIVE", "SIM")) -> int:
        """Record one mark per manager and one for the firm, per book. Idempotent per date."""
        written = 0
        for book in books:
            last_id = int((self.db.one("SELECT MAX(id) AS id FROM ledger WHERE book = ?", (book,)) or {"id": 0})["id"] or 0)
            key = f"marks_watermark_{book}"
            since = int((self.settings.get(key) or {}).get("ledger_id") or 0)
            balances = self.ledger.balances(book)
            holdings = self.ledger.holdings(book)
            buckets = sorted((set(balances) | set(holdings)) - {SUSPENSE})
            firm_nav = firm_cash = firm_inv = ZERO
            firm_flow = ZERO
            for bucket in buckets:
                cash = balances.get(bucket, ZERO)
                invested = sum((q * closes.get(sym, ZERO) for sym, q in holdings.get(bucket, {}).items()), ZERO)
                # Firm-internal moves (allocations) are flows for a manager but net to zero for the firm.
                flow = self.ledger.net_capital(book, bucket, since_id=since, until_id=last_id)
                self._write(day, book, bucket, cash + invested, cash, invested, flow, benchmark_close)
                firm_nav += cash + invested
                firm_cash += cash
                firm_inv += invested
                firm_flow += flow
                written += 1
            self._write(day, book, "_firm", firm_nav, firm_cash, firm_inv, firm_flow, benchmark_close)
            self.settings.put(key, {"ledger_id": last_id, "date": day.isoformat()}, by="marks")
            written += 1
        return written

    def _write(self, day: date, book: str, bucket: str, nav, cash, invested, flow, bench) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO marks (date, book, bucket, nav, cash, invested, net_flow, benchmark_close) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(date, book, bucket) DO UPDATE SET nav=excluded.nav, cash=excluded.cash, invested=excluded.invested,"
                " net_flow=excluded.net_flow + marks.net_flow, benchmark_close=excluded.benchmark_close",
                (day.isoformat(), book, bucket, s(cents(nav)), s(cents(cash)), s(cents(invested)), s(cents(flow)), s(bench) if bench is not None else None),
            )

    def marks(self, book: str, bucket: str) -> list[Mark]:
        rows = self.db.query("SELECT * FROM marks WHERE book = ? AND bucket = ? ORDER BY date", (book, bucket))
        return [Mark(r["date"], D(r["nav"]), D(r["cash"]), D(r["invested"]), D(r["net_flow"]), D(r["benchmark_close"]) if r["benchmark_close"] else None) for r in rows]

    @staticmethod
    def twr(marks: list[Mark]) -> Decimal | None:
        growth = Decimal(1)
        started = False
        prev: Mark | None = None
        for m in marks:
            if prev is not None and prev.nav > 0:
                growth *= (m.nav - m.net_flow) / prev.nav
                started = True
            prev = m
        return growth - 1 if started else None

    @staticmethod
    def benchmark_return(marks: list[Mark]) -> Decimal | None:
        """SPY return over the span where the bucket had capital."""
        live = [m for m in marks if m.nav > 0 and m.benchmark_close]
        if len(live) < 2:
            return None
        return live[-1].benchmark_close / live[0].benchmark_close - 1

    def summary(self, book: str, bucket: str) -> dict[str, Any]:
        ms = self.marks(book, bucket)
        twr = self.twr(ms)
        bench = self.benchmark_return(ms)
        return {
            "marks": len(ms),
            "since": ms[0].date if ms else None,
            "twr": float(twr) if twr is not None else None,
            "benchmark": float(bench) if bench is not None else None,
            "excess": float(twr - bench) if twr is not None and bench is not None else None,
            "series": [{"date": m.date, "nav": float(m.nav), "flow": float(m.net_flow), "spy": float(m.benchmark_close) if m.benchmark_close else None} for m in ms[-400:]],
        }
