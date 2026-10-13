"""OANDA bid/ask candles in marketdata.db, and the read-only series view strategies see.

Bars are canonical from OANDA (completed candles only), so live paper trading and
backtests read identical data. Daily and 4-hour bars are aligned to 17:00 New York.
"""

from __future__ import annotations

import sqlite3
import threading
from array import array
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from forex.instruments import Spec, default_spec
from forex.oanda import MAX_CANDLES, Candle, OandaClient

SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D": 86400}

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
  instrument TEXT NOT NULL, gran TEXT NOT NULL, t INTEGER NOT NULL,
  bo REAL, bh REAL, bl REAL, bc REAL, ao REAL, ah REAL, al REAL, ac REAL, volume INTEGER,
  PRIMARY KEY (instrument, gran, t)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS instruments (name TEXT PRIMARY KEY, raw_json TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


def iso_z(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Bars:
    """Completed candles for one instrument and granularity, oldest first.

    `end` limits what a strategy may see (bars [0, end)); indicators are computed once on
    the full arrays and cached, which is safe because every indicator is causal.
    """

    instrument: str
    gran: str
    t: Sequence[int]
    bo: Sequence[float]
    bh: Sequence[float]
    bl: Sequence[float]
    bc: Sequence[float]
    ao: Sequence[float]
    ah: Sequence[float]
    al: Sequence[float]
    ac: Sequence[float]
    end: int = -1
    _cache: dict[Any, Any] = field(default_factory=dict, repr=False)
    _mid: dict[str, list[float]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.end < 0:
            self.end = len(self.t)

    @property
    def dur(self) -> int:
        return SECONDS[self.gran]

    def mid(self, part: str) -> Sequence[float]:
        if part not in self._mid:
            b, a = getattr(self, "b" + part), getattr(self, "a" + part)
            self._mid[part] = array("d", ((x + y) / 2 for x, y in zip(b, a)))
        return self._mid[part]

    @property
    def o(self) -> Sequence[float]:
        return self.mid("o")

    @property
    def h(self) -> Sequence[float]:
        return self.mid("h")

    @property
    def l(self) -> Sequence[float]:  # noqa: E743
        return self.mid("l")

    @property
    def c(self) -> Sequence[float]:
        return self.mid("c")

    def __len__(self) -> int:
        return self.end

    def ind(self, key: Any, fn: Callable[["Bars"], list]) -> list:
        """Cached indicator series over the full arrays (read it at index < end)."""
        if key not in self._cache:
            self._cache[key] = fn(self)
        return self._cache[key]

    def at(self, series: list, back: int = 1):
        """Value `back` bars from the visible end (1 = the last completed bar)."""
        i = self.end - back
        return series[i] if 0 <= i < len(series) else None

    def view(self, end: int) -> "Bars":
        """The same data, visible up to `end` (shares arrays and caches)."""
        v = Bars(self.instrument, self.gran, self.t, self.bo, self.bh, self.bl, self.bc, self.ao, self.ah, self.al, self.ac, end)
        v._cache, v._mid = self._cache, self._mid
        return v

    def end_at(self, ts: int) -> int:
        """Number of bars that have closed by time ts."""
        return bisect_right(self.t, ts - self.dur)

    def close_time(self, i: int) -> int:
        return self.t[i] + self.dur


class MarketStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- instruments -----------------------------------------------------------
    def save_instruments(self, raws: Iterable[dict]) -> None:
        import json

        now = iso_z(int(datetime.now(timezone.utc).timestamp()))
        with self._lock, self.conn:
            for r in raws:
                self.conn.execute("INSERT OR REPLACE INTO instruments VALUES (?,?,?)", (r["name"], json.dumps(r), now))

    def spec(self, name: str) -> Spec:
        import json

        row = self.conn.execute("SELECT raw_json FROM instruments WHERE name = ?", (name,)).fetchone()
        return Spec.from_oanda(json.loads(row[0])) if row else default_spec(name)

    # -- candles -----------------------------------------------------------------
    def upsert(self, instrument: str, gran: str, candles: Iterable[Candle]) -> int:
        rows = [
            (instrument, gran, int(c.time.timestamp()), float(c.bid.o), float(c.bid.h), float(c.bid.l), float(c.bid.c), float(c.ask.o), float(c.ask.h), float(c.ask.l), float(c.ask.c), c.volume)
            for c in candles
            if c.complete and c.bid and c.ask
        ]
        if rows:
            with self._lock, self.conn:
                self.conn.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def last_time(self, instrument: str, gran: str) -> int | None:
        row = self.conn.execute("SELECT max(t) FROM candles WHERE instrument = ? AND gran = ?", (instrument, gran)).fetchone()
        return row[0] if row and row[0] is not None else None

    def first_time(self, instrument: str, gran: str) -> int | None:
        row = self.conn.execute("SELECT min(t) FROM candles WHERE instrument = ? AND gran = ?", (instrument, gran)).fetchone()
        return row[0] if row and row[0] is not None else None

    def count(self, instrument: str, gran: str) -> int:
        return self.conn.execute("SELECT count(*) FROM candles WHERE instrument = ? AND gran = ?", (instrument, gran)).fetchone()[0]

    def bars(self, instrument: str, gran: str, *, start: int | None = None, end: int | None = None, last: int | None = None) -> Bars:
        if last is not None:
            rows = self.conn.execute(
                "SELECT * FROM (SELECT t,bo,bh,bl,bc,ao,ah,al,ac FROM candles WHERE instrument=? AND gran=? ORDER BY t DESC LIMIT ?) ORDER BY t",
                (instrument, gran, last),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT t,bo,bh,bl,bc,ao,ah,al,ac FROM candles WHERE instrument=? AND gran=? AND t >= ? AND t < ? ORDER BY t",
                (instrument, gran, start or 0, end or 2**62),
            ).fetchall()
        cols = list(zip(*rows)) if rows else [()] * 9
        # compact arrays: years of 15-minute bars must fit comfortably on the Pi
        return Bars(instrument, gran, array("q", cols[0]), *[array("d", c) for c in cols[1:]])

    # -- fetching from OANDA --------------------------------------------------------
    def backfill(self, client: OandaClient, instrument: str, gran: str, *, since: int, until: int | None = None, progress: Callable[[str], None] | None = None) -> int:
        """Fill [since, until) forward in pages of up to 5,000 completed candles."""
        cursor = max(since, (self.last_time(instrument, gran) or 0) + 1)
        stop = until or int(datetime.now(timezone.utc).timestamp())
        total = 0
        while cursor < stop:
            batch = client.candles(instrument, gran, start=iso_z(cursor), count=MAX_CANDLES)
            done = [c for c in batch if c.complete]
            if not done:
                break
            total += self.upsert(instrument, gran, done)
            nxt = int(done[-1].time.timestamp()) + 1
            if nxt <= cursor or len(batch) < MAX_CANDLES:
                break
            cursor = nxt
            if progress:
                progress(f"{instrument} {gran} through {iso_z(cursor)}")
        return total

    def refresh(self, client: OandaClient, instrument: str, gran: str, *, count: int = 5) -> int:
        """Fetch the most recent candles (completed ones are stored). Returns rows stored."""
        return self.upsert(instrument, gran, client.candles(instrument, gran, count=count))
