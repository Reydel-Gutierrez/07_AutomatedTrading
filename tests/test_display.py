"""Dashboard Eastern Time formatting and newest-first sorting."""

from datetime import datetime, timezone

from agentic_portfolio.dashboard.display import format_et, format_relative, newest_first, parse_dt


def test_format_et_converts_utc_to_miami_eastern_ampm():
    summer = "2026-09-02T19:45:34+00:00"
    winter = "2026-01-02T19:45:00+00:00"
    assert format_et(summer) == "Sep 2, 2026, 3:45 PM"
    assert format_et(summer, seconds=True) == "Sep 2, 2026, 3:45:34 PM"
    assert format_et(summer, time_only=True) == "3:45 PM"
    assert format_et(winter) == "Jan 2, 2026, 2:45 PM"
    assert format_et(None) == "—"
    miami = parse_dt(summer)
    assert miami is not None
    assert miami.hour == 15
    assert miami.minute == 45


def test_newest_first_puts_recent_dates_ahead_of_older():
    rows = [
        {"symbol": "OLD", "decided_at": "2026-09-01T16:40:00+00:00"},
        {"symbol": "NEW", "decided_at": "2026-09-02T16:45:00+00:00"},
        {"symbol": "MID", "decided_at": "2026-09-01T20:00:00+00:00"},
        {"symbol": "NONE", "decided_at": None},
    ]
    ordered = newest_first(rows, "decided_at")
    assert [row["symbol"] for row in ordered] == ["NEW", "MID", "OLD", "NONE"]


def test_relative_time_uses_minutes_ago():
    now = datetime(2026, 9, 2, 16, 0, tzinfo=timezone.utc)
    stamp = "2026-09-02T15:37:00+00:00"
    assert format_relative(stamp, now=now) == "23m ago"
