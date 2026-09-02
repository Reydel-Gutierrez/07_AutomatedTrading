"""Miami / Eastern Time display helpers for the dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[misc, assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[misc, assignment]

TZ_NAME = "Eastern Time (ET)"
TZ_SHORT = "ET"


def _sunday_on_or_after(year: int, month: int, day: int) -> datetime:
    stamp = datetime(year, month, day, tzinfo=timezone.utc)
    return stamp + timedelta(days=(6 - stamp.weekday()) % 7)


def _eastern_dst(utc: datetime) -> bool:
    year = utc.year
    start = _sunday_on_or_after(year, 3, 8).replace(hour=7)
    end = _sunday_on_or_after(year, 11, 1).replace(hour=6)
    return start <= utc < end


def _to_eastern(dt: datetime) -> datetime:
    utc = dt.astimezone(timezone.utc)
    try:
        if ZoneInfo is not None:
            return utc.astimezone(ZoneInfo("America/New_York"))
    except (Exception, ZoneInfoNotFoundError):  # noqa: BLE001
        pass
    offset = timedelta(hours=-4 if _eastern_dst(utc) else -5)
    return utc.astimezone(timezone(offset))


def parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return _to_eastern(dt)


def _clock(dt: datetime, *, seconds: bool) -> str:
    hour = dt.strftime("%I").lstrip("0") or "12"
    minute = dt.strftime("%M")
    ampm = dt.strftime("%p")
    if seconds:
        return f"{hour}:{minute}:{dt.strftime('%S')} {ampm}"
    return f"{hour}:{minute} {ampm}"


def format_et(
    value: Any,
    *,
    seconds: bool = False,
    time_only: bool = False,
    date_only: bool = False,
) -> str:
    dt = parse_dt(value)
    if dt is None:
        if value in (None, ""):
            return "—"
        return str(value)
    if time_only:
        return _clock(dt, seconds=seconds)
    date = f"{dt.strftime('%b')} {dt.day}, {dt.year}"
    if date_only:
        return date
    return f"{date}, {_clock(dt, seconds=seconds)}"


def format_relative(value: Any, *, now: datetime | None = None) -> str:
    dt = parse_dt(value)
    if dt is None:
        return "—"
    current = parse_dt(now) if now is not None else _to_eastern(datetime.now(timezone.utc))
    if current is None:
        return "—"
    delta = current - dt
    secs = int(delta.total_seconds())
    future = secs < 0
    secs = abs(secs)
    if secs < 45:
        return "just now" if not future else "soon"
    if secs < 3600:
        stamp = f"{secs // 60}m"
    elif secs < 86400:
        stamp = f"{secs // 3600}h"
    else:
        stamp = f"{secs // 86400}d"
    return f"in {stamp}" if future else f"{stamp} ago"


def ts_sort_key(value: Any) -> str:
    dt = parse_dt(value)
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).isoformat()


def newest_first(rows: Iterable[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    items = list(rows)

    def key(row: dict[str, Any]) -> str:
        return max((ts_sort_key(row.get(name)) for name in keys), default="")

    return sorted(items, key=key, reverse=True)
