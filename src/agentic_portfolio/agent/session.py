"""Market/session phases for 24/7 orchestration.

The service stays running on weekends and holidays. Phase only selects work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from enum import Enum
from typing import Any

from agentic_portfolio.calendar import EASTERN, NyseEquityCalendar, REGULAR_OPEN, utc_now

PREMARKET_START = time(4, 0)
OVERNIGHT_START = time(20, 0)


class MarketPhase(str, Enum):
    MARKET_OPEN = "MARKET_OPEN"
    PREMARKET = "PREMARKET"
    AFTER_CLOSE = "AFTER_CLOSE"
    OVERNIGHT = "OVERNIGHT"
    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"


OFF_HOURS = {MarketPhase.PREMARKET, MarketPhase.AFTER_CLOSE, MarketPhase.OVERNIGHT, MarketPhase.WEEKEND, MarketPhase.HOLIDAY}
CLOSED_SESSIONS = {MarketPhase.WEEKEND, MarketPhase.HOLIDAY, MarketPhase.OVERNIGHT, MarketPhase.AFTER_CLOSE, MarketPhase.PREMARKET}


@dataclass(frozen=True)
class SessionSnapshot:
    phase: MarketPhase
    observed_at: str
    timezone: str
    trading_session: bool
    regular_hours_open: bool
    session_id: str | None
    session_date: str | None
    latest_completed_session: str | None
    next_session_id: str | None
    weekday: str
    is_early_close: bool
    reason: str
    executable_liquidity: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "observed_at": self.observed_at,
            "timezone": self.timezone,
            "trading_session": self.trading_session,
            "regular_hours_open": self.regular_hours_open,
            "session_id": self.session_id,
            "session_date": self.session_date,
            "latest_completed_session": self.latest_completed_session,
            "next_session_id": self.next_session_id,
            "weekday": self.weekday,
            "is_early_close": self.is_early_close,
            "reason": self.reason,
            "executable_liquidity": self.executable_liquidity,
        }


def classify_market_phase(now: datetime | None = None, *, calendar: NyseEquityCalendar | None = None) -> SessionSnapshot:
    stamp = now or utc_now()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    cal = calendar or NyseEquityCalendar()
    et = stamp.astimezone(EASTERN)
    current = cal.session_for(stamp)
    last = cal.current_or_last_session(stamp)
    completed = cal.latest_completed_session(stamp)
    nxt = cal.next_session(stamp)
    weekday = et.strftime("%A")
    clock = et.time()

    if et.weekday() >= 5:
        phase = MarketPhase.WEEKEND
        reason = "weekend"
    elif current is None:
        phase = MarketPhase.HOLIDAY
        reason = "holiday"
    elif clock < PREMARKET_START:
        phase = MarketPhase.OVERNIGHT
        reason = "overnight_before_premarket"
    elif clock < REGULAR_OPEN:
        phase = MarketPhase.PREMARKET
        reason = "pre_open"
    elif clock < current.close_time:
        phase = MarketPhase.MARKET_OPEN
        reason = "regular_hours"
    elif clock < OVERNIGHT_START:
        phase = MarketPhase.AFTER_CLOSE
        reason = "after_close"
    else:
        phase = MarketPhase.OVERNIGHT
        reason = "overnight_after_close"

    regular = phase is MarketPhase.MARKET_OPEN
    return SessionSnapshot(
        phase=phase,
        observed_at=stamp.isoformat(),
        timezone="America/New_York",
        trading_session=current is not None,
        regular_hours_open=regular,
        session_id=current.session_id if current else (last.session_id if last else None),
        session_date=(current.session_date.isoformat() if current else (last.session_date.isoformat() if last else None)),
        latest_completed_session=completed.session_id if completed else None,
        next_session_id=nxt.session_id if nxt else None,
        weekday=weekday,
        is_early_close=bool(current.is_early_close) if current else False,
        reason=reason,
        executable_liquidity=regular,
    )
