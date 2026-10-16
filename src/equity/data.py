"""Daily bars for the class, read from the firm's market cache (firm.db, read only).

The firm fetches the bars (split-adjusted, regular hours, from Robinhood) in its 17:15 New
York job; this engine never talks to a broker. A Panel aligns every symbol to one trading
calendar (the benchmark's dates); a day a symbol did not trade (or before it listed) is None.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Iterable, Sequence

CALENDAR_SYMBOL = "SPY"


def epoch(day: str) -> int:
    """A trading day as epoch seconds (21:00 UTC, after the close), for the shared metrics."""
    return int(datetime.combine(date.fromisoformat(day), time(21, 0), tzinfo=timezone.utc).timestamp())


@dataclass
class Series:
    o: list[float | None]
    h: list[float | None]
    l: list[float | None]
    c: list[float | None]

    def first(self) -> int | None:
        return next((i for i, x in enumerate(self.c) if x is not None), None)


@dataclass
class Panel:
    dates: list[str]
    series: dict[str, Series]

    @property
    def times(self) -> list[int]:
        return [epoch(d) for d in self.dates]

    def symbols(self) -> list[str]:
        return sorted(self.series)

    def index_on_or_before(self, day: str) -> int:
        lo, hi = 0, len(self.dates)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.dates[mid] <= day:
                lo = mid + 1
            else:
                hi = mid
        return lo - 1


def build_panel(rows: dict[str, list[tuple[str, float, float, float, float]]], calendar: Sequence[str] | None = None) -> Panel:
    """rows: symbol -> [(date, open, high, low, close)] in any order."""
    cal = list(calendar) if calendar is not None else sorted({d for r in rows.get(CALENDAR_SYMBOL, []) for d in [r[0]]})
    pos = {d: i for i, d in enumerate(cal)}
    out: dict[str, Series] = {}
    for sym, bars in rows.items():
        s = Series([None] * len(cal), [None] * len(cal), [None] * len(cal), [None] * len(cal))
        for d, o, h, l, c in bars:
            i = pos.get(d)
            if i is None or not c or c <= 0:
                continue
            s.o[i], s.h[i], s.l[i], s.c[i] = o or c, h or c, l or c, c
        out[sym] = s
    return Panel(cal, out)


def load_panel(path: Path, symbols: Iterable[str]) -> Panel:
    wanted = sorted({s.upper() for s in symbols} | {CALENDAR_SYMBOL})
    rows: dict[str, list[tuple[str, float, float, float, float]]] = {}
    if not path.exists():
        return Panel([], {})
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    with closing(conn):
        for sym in wanted:
            rows[sym] = [
                (r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                for r in conn.execute("SELECT date, open, high, low, close FROM market_bars WHERE symbol = ? ORDER BY date", (sym,))
            ]
    return build_panel(rows)


def coverage(panel: Panel) -> dict[str, dict[str, object]]:
    out = {}
    for sym, s in panel.series.items():
        idx = [i for i, x in enumerate(s.c) if x is not None]
        out[sym] = {"bars": len(idx), "first": panel.dates[idx[0]] if idx else None, "last": panel.dates[idx[-1]] if idx else None}
    return out
