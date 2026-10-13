"""Forex time: the market week (Sunday 17:00 to Friday 17:00 New York) and the trading day,
which rolls at 17:00 New York (the daily rollover, when financing is charged)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class _EasternRules(tzinfo):
    """US Eastern time by rule (2nd Sunday of March to 1st Sunday of November), for hosts without tzdata."""

    @staticmethod
    def _nth_sunday(year: int, month: int, n: int) -> date:
        d = date(year, month, 1)
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)

    def _is_dst(self, dt: datetime) -> bool:
        d = dt.date()
        return self._nth_sunday(d.year, 3, 2) <= d < self._nth_sunday(d.year, 11, 1)

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=-4 if dt is not None and self._is_dst(dt) else -5)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=1 if dt is not None and self._is_dst(dt) else 0)

    def tzname(self, dt: datetime | None) -> str:
        return "EDT" if dt is not None and self._is_dst(dt) else "EST"


try:
    NY: tzinfo = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:  # e.g. Windows without the tzdata package
    NY = _EasternRules()


def ny(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(NY)


def market_open(ts: int) -> bool:
    t = ny(ts)
    wd, h = t.weekday(), t.hour  # Mon=0 .. Sun=6
    if wd == 5:
        return False
    if wd == 6:
        return h >= 17
    if wd == 4:
        return h < 17
    return True


def trading_day(ts: int) -> date:
    """The forex trading day a moment belongs to (a day ends at 17:00 New York)."""
    return (ny(ts) + timedelta(hours=7)).date()


def minutes_ny(ts: int) -> int:
    t = ny(ts)
    return t.hour * 60 + t.minute


def in_window(ts: int, start: str, end: str) -> bool:
    """New York clock window "HH:MM"-"HH:MM"; may wrap past midnight."""
    m = minutes_ny(ts)
    a = int(start[:2]) * 60 + int(start[3:])
    b = int(end[:2]) * 60 + int(end[3:])
    return a <= m < b if a <= b else (m >= a or m < b)


def rollover_window(ts: int) -> bool:
    """16:55-17:10 New York: spreads blow out; no new entries."""
    return in_window(ts, "16:55", "17:10")


def is_friday_after(ts: int, hhmm: str) -> bool:
    t = ny(ts)
    return t.weekday() == 4 and minutes_ny(ts) >= int(hhmm[:2]) * 60 + int(hhmm[3:])


def weekday_ny(ts: int) -> str:
    return ny(ts).strftime("%a")
