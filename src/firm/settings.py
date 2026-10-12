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
        # Operational circuit breaker: LIVE notional traded per day across all managers.
        "max_daily_turnover_pct_nav": 1.0,
        # Who the investment drawdown brake stops: "all" managers, or only
        # "active_strategies" (managers whose mandate is not drawdown_policy=stay_invested).
        "drawdown_brake_scope": "all",
        # Informational alerts (firm and each sleeve) as drawdowns deepen.
        "drawdown_alert_levels": [0.10, 0.20, 0.30],
    },
    # How newly detected deposits are split (fractions of the deposit; remainder stays unallocated).
    "deposit_split": {"tim": 0.90},
    "ai_budget": {"monthly_usd": 25.0, "per_agent": {"cio": 0.5, "alex": 0.35, "max": 0.1, "tim": 0.05}},
    # Provider is configurable: "anthropic" (default) or "openai". Keys live only in the Pi env file.
    "ai_models": {
        "cio": {"provider": "anthropic", "model": "claude-opus-5-5", "summary_model": "claude-haiku-5-5", "effort": "high"},
        "alex": {"provider": "anthropic", "model": "claude-opus-5-5", "effort": "medium"},
    },
    "kill_switch": {"engaged": False, "reason": None, "by": None, "at": None},
    "drawdown_brake": {"engaged": False, "hwm": None, "at": None},
}


class Settings:
    def __init__(self, db: Database, *, clock: Clock = utc_now) -> None:
        self.db = db
        self.clock = clock

    def get(self, key: str) -> Any:
        row = self.db.one("SELECT value_json FROM settings WHERE key = ?", (key,))
        default = json.loads(json.dumps(DEFAULTS.get(key)))
        if row is None:
            return default
        stored = json.loads(row["value_json"])
        # New keys added to a dict default stay in force even if an older copy was saved.
        if isinstance(default, dict) and isinstance(stored, dict) and key == "firm_limits":
            return {**default, **stored}
        return stored

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
