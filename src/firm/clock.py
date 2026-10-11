"""Time helpers. Everything is stored in UTC ISO-8601; schedules run in US Eastern."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Callable

from firm.calendar import EASTERN, NyseEquityCalendar, TradingSession

Clock = Callable[[], datetime]

CALENDAR = NyseEquityCalendar()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def et(dt: datetime) -> datetime:
    return dt.astimezone(EASTERN)


def et_date(dt: datetime) -> date:
    return et(dt).date()


def session_today(dt: datetime) -> TradingSession | None:
    return CALENDAR.session_for(dt)


def is_regular_hours(dt: datetime) -> bool:
    session = CALENDAR.session_for(dt)
    if session is None:
        return False
    t = et(dt).time()
    return session.regular_open <= t < session.close_time


def in_window(dt: datetime, start: time, end: time) -> bool:
    """Inside [start, end) ET on a trading day, clipped to an early close."""
    session = CALENDAR.session_for(dt)
    if session is None:
        return False
    t = et(dt).time()
    return start <= t < min(end, session.close_time)


def parse_hhmm(text: str) -> time:
    hh, mm = str(text).split(":")
    return time(int(hh), int(mm))
