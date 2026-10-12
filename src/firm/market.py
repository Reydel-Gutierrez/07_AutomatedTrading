"""Market data shared by every manager: quotes with a short cache, daily bars persisted."""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from decimal import Decimal

from firm.broker.errors import BrokerUnavailable
from firm.broker.types import Bar, BrokerReader, Quote
from firm.clock import Clock, utc_now
from firm.db import Database
from firm.money import D, s

QUOTE_CACHE_SECONDS = 5.0


class MarketData:
    def __init__(self, reader: BrokerReader | None, db: Database, *, clock: Clock = utc_now) -> None:
        self.reader = reader
        self.db = db
        self.clock = clock
        self._cache: dict[str, tuple[datetime, Quote]] = {}
        self._lock = threading.Lock()

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Fresh-enough quotes. Raises BrokerUnavailable when the broker cannot be reached."""
        now = self.clock()
        want = sorted({x.upper() for x in symbols})
        with self._lock:
            missing = [x for x in want if x not in self._cache or (now - self._cache[x][0]).total_seconds() > QUOTE_CACHE_SECONDS]
        if missing:
            if self.reader is None:
                raise BrokerUnavailable("no market data source")
            fetched = self.reader.quotes(missing)
            with self._lock:
                for sym, q in fetched.items():
                    self._cache[sym] = (now, q)
        with self._lock:
            return {x: self._cache[x][1] for x in want if x in self._cache}

    def quote_age_seconds(self, quote: Quote) -> float | None:
        if quote.observed_at is None:
            return None
        return (self.clock() - quote.observed_at).total_seconds()

    # -- daily bars (persisted) ---------------------------------------------
    def store_bars(self, symbol: str, bars: list[Bar]) -> None:
        with self.db.tx() as conn:
            for b in bars:
                conn.execute(
                    "INSERT INTO market_bars (symbol, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(symbol, date) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low,"
                    " close=excluded.close, volume=excluded.volume",
                    (symbol.upper(), b.date, s(b.open), s(b.high), s(b.low), s(b.close), str(b.volume)),
                )

    def bars(self, symbol: str, *, start: date, end: date | None = None) -> list[Bar]:
        rows = self.db.query(
            "SELECT * FROM market_bars WHERE symbol = ? AND date >= ? AND date <= ? ORDER BY date",
            (symbol.upper(), start.isoformat(), (end or date.max).isoformat()),
        )
        return [Bar(r["date"], D(r["open"]), D(r["high"]), D(r["low"]), D(r["close"]), int(r["volume"] or 0)) for r in rows]

    def refresh_bars(self, symbols: list[str], *, days: int = 400) -> int:
        """Fetch recent daily bars from the broker into the local cache. Returns bars stored."""
        if self.reader is None:
            return 0
        start = (self.clock() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
        stored = 0
        for sym, bars in self.reader.daily_bars(symbols, start=start).items():
            self.store_bars(sym, bars)
            stored += len(bars)
        return stored

    def recent_closes(self, symbols: list[str], *, sessions: int) -> dict[str, list[float]]:
        """The last `sessions` stored daily closes per symbol, oldest first."""
        out: dict[str, list[float]] = {}
        for sym in symbols:
            rows = self.db.query("SELECT close FROM market_bars WHERE symbol = ? ORDER BY date DESC LIMIT ?", (sym.upper(), sessions))
            if rows:
                out[sym.upper()] = [float(r["close"]) for r in reversed(rows)]
        return out

    def last_bar_date(self, symbol: str) -> str | None:
        row = self.db.one("SELECT max(date) AS d FROM market_bars WHERE symbol = ?", (symbol.upper(),))
        return row["d"] if row and row["d"] else None

    def close_on(self, symbol: str, day: date) -> Decimal | None:
        row = self.db.one("SELECT close FROM market_bars WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1", (symbol.upper(), day.isoformat()))
        return D(row["close"]) if row else None
