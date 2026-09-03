"""Start-of-day NAV anchored to U.S. equity trading sessions, not midnight.

Persists across process restarts. Weekends and NYSE holidays do not create
a new SOD snapshot. Early-close days keep the same session_id.

If the calendar cannot identify the session, this module fails safe:
it will not roll SOD / daily-risk state.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentic_portfolio.calendar import (
    SESSION_TZ_NAME,
    MarketCalendar,
    NyseEquityCalendar,
    TradingSession,
    is_new_session,
)
from agentic_portfolio.paths import project_root


def session_state_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "state" / "session_state.json"


@dataclass
class SessionNavState:
    session_id: str | None
    session_date: str | None
    timezone: str
    sod_nav: float | None
    sod_anchored_at: str | None
    calendar_provider: str
    calendar_available: bool
    fail_safe: bool
    fail_safe_reason: str | None
    last_observed_nav: float | None
    last_observed_at: str | None
    last_observed_cash: float | None = None
    session_external_capital_flow: float = 0.0
    note: str = (
        "SOD NAV is session-based (America/New_York). "
        "Do not reset daily-risk state on calendar midnight or weekends."
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionNavState:
        return cls(
            session_id=data.get("session_id"),
            session_date=data.get("session_date"),
            timezone=data.get("timezone", SESSION_TZ_NAME),
            sod_nav=data.get("sod_nav"),
            sod_anchored_at=data.get("sod_anchored_at"),
            calendar_provider=data.get("calendar_provider", "nyse_builtin_v1"),
            calendar_available=bool(data.get("calendar_available", True)),
            fail_safe=bool(data.get("fail_safe", False)),
            fail_safe_reason=data.get("fail_safe_reason"),
            last_observed_nav=data.get("last_observed_nav"),
            last_observed_at=data.get("last_observed_at"),
            last_observed_cash=data.get("last_observed_cash"),
            session_external_capital_flow=float(data.get("session_external_capital_flow") or 0.0),
            note=data.get("note", cls.__dataclass_fields__["note"].default),
        )


def load_session_state(path: Path | None = None) -> SessionNavState | None:
    p = path or session_state_path()
    if not p.exists():
        return None
    return SessionNavState.from_dict(json.loads(p.read_text(encoding="utf-8")))


def save_session_state(state: SessionNavState, path: Path | None = None) -> Path:
    p = path or session_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    return p


def observe_nav_for_session(
    *,
    current_nav: float,
    now: datetime,
    prior: SessionNavState | None = None,
    calendar: MarketCalendar | None = None,
    persist_path: Path | None = None,
    incremental_external_flow: float = 0.0,
    current_cash: float | None = None,
) -> SessionNavState:
    """Update SOD using the official session calendar.

    New SOD = first observation on a new valid trading date (before/at open
    is the intended capture; a later first observation of that session is used
    if the process was down at the open). Early close does not create a second
    SOD. Non-trading days keep the prior SOD.
    """
    cal = calendar or NyseEquityCalendar()
    ts = now.astimezone(timezone.utc).isoformat() if now.tzinfo else None
    if ts is None:
        # Naive clock: fail safe, do not roll SOD.
        kept = prior or SessionNavState(
            session_id=None,
            session_date=None,
            timezone=SESSION_TZ_NAME,
            sod_nav=None,
            sod_anchored_at=None,
            calendar_provider=getattr(cal, "provider_id", "unknown"),
            calendar_available=False,
            fail_safe=True,
            fail_safe_reason="naive_datetime",
            last_observed_nav=current_nav,
            last_observed_at=None,
            last_observed_cash=current_cash,
            session_external_capital_flow=float(prior.session_external_capital_flow) if prior else 0.0,
        )
        if persist_path is not None:
            save_session_state(kept, persist_path)
        return kept

    rolled, session, reason = is_new_session(
        prior_session_id=prior.session_id if prior else None,
        now=now,
        calendar=cal,
    )
    if session is None:
        kept = SessionNavState(
            session_id=prior.session_id if prior else None,
            session_date=prior.session_date if prior else None,
            timezone=SESSION_TZ_NAME,
            sod_nav=prior.sod_nav if prior else None,
            sod_anchored_at=prior.sod_anchored_at if prior else None,
            calendar_provider=getattr(cal, "provider_id", "unknown"),
            calendar_available=False,
            fail_safe=True,
            fail_safe_reason=reason or "calendar_unavailable",
            last_observed_nav=current_nav,
            last_observed_at=ts,
            last_observed_cash=current_cash,
            session_external_capital_flow=(
                float(prior.session_external_capital_flow or 0.0) + float(incremental_external_flow or 0.0)
                if prior
                else 0.0
            ),
        )
        if persist_path is not None:
            save_session_state(kept, persist_path)
        return kept

    if rolled:
        sod = float(current_nav)
        anchored = ts
        fail_safe = False
        fail_reason = None
    else:
        sod = prior.sod_nav if prior and prior.sod_nav is not None else float(current_nav)
        anchored = prior.sod_anchored_at if prior and prior.sod_anchored_at else ts
        # If we had no prior SOD on the same session, this observation becomes the anchor.
        if prior is None or prior.sod_nav is None:
            sod = float(current_nav)
            anchored = ts
        fail_safe = False
        fail_reason = None

    state = SessionNavState(
        session_id=session.session_id,
        session_date=session.session_date.isoformat(),
        timezone=SESSION_TZ_NAME,
        sod_nav=sod,
        sod_anchored_at=anchored,
        calendar_provider=session.calendar_provider,
        calendar_available=True,
        fail_safe=fail_safe,
        fail_safe_reason=fail_reason,
        last_observed_nav=current_nav,
        last_observed_at=ts,
        last_observed_cash=current_cash,
        session_external_capital_flow=0.0 if rolled or prior is None or prior.sod_nav is None else (
            float(prior.session_external_capital_flow or 0.0) + float(incremental_external_flow or 0.0)
        ),
    )
    # Preserve last trading session id on non-trading days so SOD does not reset.
    if reason == "non_trading_day" and prior and prior.sod_nav is not None:
        state = SessionNavState(
            session_id=prior.session_id or session.session_id,
            session_date=prior.session_date or session.session_date.isoformat(),
            timezone=SESSION_TZ_NAME,
            sod_nav=prior.sod_nav,
            sod_anchored_at=prior.sod_anchored_at,
            calendar_provider=session.calendar_provider,
            calendar_available=True,
            fail_safe=False,
            fail_safe_reason=reason,
            last_observed_nav=current_nav,
            last_observed_at=ts,
            last_observed_cash=current_cash,
            session_external_capital_flow=float(prior.session_external_capital_flow or 0.0) + float(incremental_external_flow or 0.0),
        )

    if persist_path is not None:
        save_session_state(state, persist_path)
    return state


def trading_session_snapshot(now: datetime, calendar: MarketCalendar | None = None) -> TradingSession | None:
    cal = calendar or NyseEquityCalendar()
    try:
        return cal.current_or_last_session(now)
    except Exception:
        return None
