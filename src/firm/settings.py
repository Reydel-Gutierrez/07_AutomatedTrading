"""Owner-editable firm settings, versioned in the database (not in git)."""

from __future__ import annotations

import json
from typing import Any

from firm.clock import Clock, iso, utc_now
from firm.db import Database

DEFAULTS: dict[str, Any] = {
    # Firm-wide hard limits. Managers' mandates sit inside these.
    "firm_limits": {
        "cash_only": True,  # never spend margin buying power
        "regular_hours_only": True,
        "window_start_et": "10:00",
        "window_end_et": "15:30",
        "drawdown_brake_pct": 0.20,  # firm buys stop at -20% from the flow-adjusted high water mark
        "max_quote_age_seconds": 15,
        "rounding_trueup_usd": "0.50",  # unexplained cash diffs below this, with no open orders, are booked as rounding
        "dividend_max_pct_of_position": 0.02,
    },
    # How newly detected deposits are split (fractions of the deposit; remainder stays unallocated).
    "deposit_split": {"tim": 0.90},
    "ai_budget": {"monthly_usd": 25.0, "per_agent": {"cio": 0.5, "alex": 0.35, "max": 0.1, "tim": 0.05}},
    "kill_switch": {"engaged": False, "reason": None, "by": None, "at": None},
    "drawdown_brake": {"engaged": False, "hwm": None, "at": None},
}


class Settings:
    def __init__(self, db: Database, *, clock: Clock = utc_now) -> None:
        self.db = db
        self.clock = clock

    def get(self, key: str) -> Any:
        row = self.db.one("SELECT value_json FROM settings WHERE key = ?", (key,))
        if row is None:
            return json.loads(json.dumps(DEFAULTS.get(key)))
        return json.loads(row["value_json"])

    def put(self, key: str, value: Any, *, by: str) -> int:
        body = json.dumps(value, sort_keys=True, default=str)
        with self.db.tx() as conn:
            row = conn.execute("SELECT version FROM settings WHERE key = ?", (key,)).fetchone()
            version = int(row["version"]) + 1 if row else 1
            conn.execute(
                "INSERT INTO settings (key, value_json, version, updated_at, updated_by) VALUES (?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, version=excluded.version,"
                " updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (key, body, version, iso(self.clock()), by),
            )
        return version

    def meta(self, key: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT version, updated_at, updated_by FROM settings WHERE key = ?", (key,))
        return dict(row) if row else None
