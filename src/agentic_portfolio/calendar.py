"""U.S. equities trading-session calendar.

Timezone assumption
-------------------
All session semantics use America/New_York (US Eastern). Offsets:
  EST = UTC−5, EDT = UTC−4.
DST: second Sunday of March 02:00 ET → first Sunday of November 02:00 ET
(US Energy Policy Act of 2005 rule). A ZoneInfo("America/New_York") provider
may replace EasternOffsetTz later without changing TradingSession identity.

Fail-safe
---------
If the calendar cannot determine whether a timestamp is a valid session
(missing clock, unsupported year, etc.), callers MUST NOT roll start-of-day
NAV or reset daily-risk state. Weekends and NYSE holidays never mint a session.

The calendar object is replaceable: pass any MarketCalendar-compatible type.
This module does not fetch holidays from a network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Protocol


SESSION_TZ_NAME = "America/New_York"
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# NYSE published full-day holidays. Ad-hoc closures (national mourning) are
# not in this table; an unknown extra close must be handled as fail-safe by
# the operator, not guessed.
_SUPPORTED_YEARS = range(2020, 2041)


class EasternOffsetTz(tzinfo):
    """Deterministic US Eastern tzinfo so tests do not depend on tzdata."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(hours=-5)
        return timedelta(hours=-4 if self._is_dst(dt) else -5)

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None or not self._is_dst(dt):
            return timedelta(0)
        return timedelta(hours=1)

    def tzname(self, dt: datetime | None) -> str:
        return "EDT" if dt is not None and self._is_dst(dt) else "EST"

    def _is_dst(self, dt: datetime) -> bool:
        d = dt.date() if isinstance(dt, datetime) else dt
        year = d.year
        start = _nth_weekday(year, 3, 6, 2)  # 2nd Sunday of March
        end = _nth_weekday(year, 11, 6, 1)  # 1st Sunday of November
        # DST starts 02:00 local on `start`; naive compare by date is enough
        # for session-date purposes (all session bounds are after 02:00).
        return start <= d < end


EASTERN = EasternOffsetTz()


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """weekday: Mon=0 … Sun=6. n is 1-based."""
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    d += timedelta(weeks=n - 1)
    return d


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def easter_gregorian(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    if d.weekday() == 5:  # Saturday → Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday → Monday
        return d + timedelta(days=1)
    return d


def nyse_full_close_dates(year: int) -> set[date]:
    if year not in _SUPPORTED_YEARS:
        raise ValueError(f"NYSE calendar year {year} is outside supported range")
    holidays = {
        _observed(date(year, 1, 1)),  # New Year's Day
        _nth_weekday(year, 1, 0, 3),  # MLK Day
        _nth_weekday(year, 2, 0, 3),  # Presidents' Day
        easter_gregorian(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed(date(year, 6, 19)),  # Juneteenth
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas
    }
    # New Year's Day falling on Saturday is observed the previous Friday (Dec 31).
    ny = date(year, 1, 1)
    if ny.weekday() == 5:
        holidays.add(date(year - 1, 12, 31))
    return holidays


def nyse_early_close_dates(year: int) -> set[date]:
    """Sessions that close 13:00 ET. Still the same trading day / SOD anchor."""
    if year not in _SUPPORTED_YEARS:
        raise ValueError(f"NYSE calendar year {year} is outside supported range")
    out: set[date] = set()
    closes = nyse_full_close_dates(year)
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    black_friday = thanksgiving + timedelta(days=1)
    if black_friday not in closes and black_friday.weekday() < 5:
        out.add(black_friday)
    christmas = date(year, 12, 25)
    eve = date(year, 12, 24)
    if eve.weekday() < 5 and eve not in closes:
        out.add(eve)
    # Day before Independence Day when July 4 is a weekday.
    jul4 = date(year, 7, 4)
    jul3 = date(year, 7, 3)
    if jul4.weekday() < 5 and jul3 not in closes and jul3.weekday() < 5:
        out.add(jul3)
    nye = date(year, 12, 31)
    if nye.weekday() < 5 and nye not in closes:
        out.add(nye)
    return out - closes


@dataclass(frozen=True)
class TradingSession:
    """One official U.S. equities regular session.

    Early-close days keep the same session_id (the trading date).
    """

    session_id: str  # YYYY-MM-DD in America/New_York
    session_date: date
    timezone: str = SESSION_TZ_NAME
    regular_open: time = REGULAR_OPEN
    regular_close: time = REGULAR_CLOSE
    is_early_close: bool = False
    calendar_provider: str = "nyse_builtin_v1"

    @property
    def close_time(self) -> time:
        return EARLY_CLOSE if self.is_early_close else self.regular_close


class MarketCalendar(Protocol):
    provider_id: str
    timezone_name: str

    def session_on(self, d: date) -> TradingSession | None: ...

    def session_for(self, dt: datetime) -> TradingSession | None:
        """Session whose trading date is this ET calendar date, if a trading day.

        Returns None on weekends/holidays. Does not invent a session.
        """

    def current_or_last_session(self, dt: datetime) -> TradingSession | None:
        """If dt falls on a trading day (any hour), that session.
        Otherwise the most recent prior trading session (Fri after close,
        weekends, holidays). None only if calendar cannot decide.
        """

    def next_session(self, dt: datetime) -> TradingSession | None: ...


class NyseEquityCalendar:
    """Built-in NYSE regular-hours calendar. Replaceable via MarketCalendar."""

    provider_id = "nyse_builtin_v1"
    timezone_name = SESSION_TZ_NAME

    def __init__(self, tz: tzinfo | None = None) -> None:
        self.tz = tz or EASTERN

    def _as_et(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            # Fail closed: naive datetimes are not silently treated as local.
            raise ValueError("datetime must be timezone-aware for session logic")
        return dt.astimezone(self.tz)

    def session_on(self, d: date) -> TradingSession | None:
        if d.year not in _SUPPORTED_YEARS:
            return None
        if d.weekday() >= 5:
            return None
        try:
            closes = nyse_full_close_dates(d.year)
            # Observed Friday Dec 31 for next year's Saturday New Year.
            if d.year + 1 in _SUPPORTED_YEARS:
                closes |= {x for x in nyse_full_close_dates(d.year + 1) if x.year == d.year}
        except ValueError:
            return None
        if d in closes:
            return None
        try:
            early = d in nyse_early_close_dates(d.year)
        except ValueError:
            early = False
        return TradingSession(
            session_id=d.isoformat(),
            session_date=d,
            is_early_close=early,
            calendar_provider=self.provider_id,
        )

    def session_for(self, dt: datetime) -> TradingSession | None:
        try:
            et = self._as_et(dt)
        except (ValueError, OverflowError):
            return None
        return self.session_on(et.date())

    def current_or_last_session(self, dt: datetime) -> TradingSession | None:
        try:
            et = self._as_et(dt)
        except (ValueError, OverflowError):
            return None
        d = et.date()
        for _ in range(15):
            s = self.session_on(d)
            if s is not None:
                return s
            d = d - timedelta(days=1)
            if d.year not in _SUPPORTED_YEARS:
                return None
        return None

    def latest_completed_session(self, dt: datetime) -> TradingSession | None:
        """Most recently finished regular session.

        During an open session this is the prior trading day, not today.
        After the close, on weekends, and on holidays it is the last
        session that actually completed.
        """
        try:
            et = self._as_et(dt)
        except (ValueError, OverflowError):
            return None
        current = self.session_on(et.date())
        if current is None:
            return self.current_or_last_session(dt)
        if et.time() >= current.close_time:
            return current
        d = current.session_date - timedelta(days=1)
        for _ in range(15):
            prior = self.session_on(d)
            if prior is not None:
                return prior
            d = d - timedelta(days=1)
            if d.year not in _SUPPORTED_YEARS:
                return None
        return None

    def next_session(self, dt: datetime) -> TradingSession | None:
        try:
            et = self._as_et(dt)
        except (ValueError, OverflowError):
            return None
        d = et.date()
        today = self.session_on(d)
        if today is not None:
            # Next *new* session after this one.
            d = d + timedelta(days=1)
        for _ in range(15):
            s = self.session_on(d)
            if s is not None:
                return s
            d = d + timedelta(days=1)
            if d.year not in _SUPPORTED_YEARS:
                return None
        return None

    def next_regular_open(self, dt: datetime) -> datetime | None:
        """UTC instant of the next regular-hours open, including today's if still pre-open.

        `next_session` always skips a trading day once that calendar date exists, even
        before the bell. WAITING_FOR_OPEN must not inherit that skip.
        """
        try:
            et = self._as_et(dt)
        except (ValueError, OverflowError):
            return None
        today = self.session_on(et.date())
        if today is not None:
            open_local = datetime.combine(today.session_date, today.regular_open, tzinfo=self.tz)
            close_local = datetime.combine(today.session_date, today.close_time, tzinfo=self.tz)
            if et < open_local:
                return open_local.astimezone(timezone.utc)
            if et < close_local:
                return et.astimezone(timezone.utc)
        nxt = self.next_session(dt)
        if nxt is None:
            return None
        open_local = datetime.combine(nxt.session_date, nxt.regular_open, tzinfo=self.tz)
        return open_local.astimezone(timezone.utc)


def is_new_session(
    *,
    prior_session_id: str | None,
    now: datetime,
    calendar: MarketCalendar | None = None,
) -> tuple[bool, TradingSession | None, str]:
    """Whether SOD should roll to a new trading-session anchor.

    A new session exists only when `now` (ET) falls on a valid trading date
    whose session_id differs from the persisted one. Weekends and holidays
    return (False, last_session, reason) — they do not reset daily risk.

    If the calendar cannot resolve a session, returns
    (False, None, "calendar_unavailable") so callers fail safe.
    """
    cal = calendar or NyseEquityCalendar()
    try:
        current = cal.session_for(now)
        last = cal.current_or_last_session(now)
    except Exception:
        return False, None, "calendar_unavailable"
    if last is None:
        return False, None, "calendar_unavailable"
    if current is None:
        # Weekend/holiday: stay on last completed session. No fake SOD.
        return False, last, "non_trading_day"
    if prior_session_id is None:
        return True, current, "first_anchor"
    if current.session_id != prior_session_id:
        return True, current, "new_trading_session"
    return False, current, "same_session"


def is_regular_hours(dt: datetime, calendar: MarketCalendar | None = None) -> bool:
    """True when `dt` falls inside an official NYSE regular session (not pre/post/weekend)."""
    cal = calendar or NyseEquityCalendar()
    try:
        if dt.tzinfo is None:
            return False
        et = dt.astimezone(EASTERN)
    except (ValueError, OverflowError, OSError):
        return False
    session = cal.session_for(dt)
    if session is None:
        return False
    return REGULAR_OPEN <= et.time() < session.close_time


def next_regular_open_at(dt: datetime, calendar: MarketCalendar | None = None) -> datetime:
    """When WAITING_FOR_OPEN should next reassess: the next regular open, not a multi-day WATCH interval."""
    cal = calendar or NyseEquityCalendar()
    stamp = cal.next_regular_open(dt) if hasattr(cal, "next_regular_open") else None
    if stamp is not None:
        return stamp
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
