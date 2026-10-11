"""Manager rows, modes, and versioned mandates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from firm.clock import Clock, iso, utc_now
from firm.db import Database
from firm.events import EventLog

MODES = ("INACTIVE", "SIM", "LIVE", "PAUSED")

ROSTER = {
    "tim": ("Tim", "Core Investment Manager"),
    "alex": ("Alex", "Strategic Growth Manager"),
    "max": ("Max", "Short-Term Trading Manager"),
}

TIM_DEFAULT_MANDATE: dict[str, Any] = {
    "targets": {"VTI": 0.70, "VXUS": 0.25, "BND": 0.05},
    "cash_buffer_pct": 0.01,
    "abs_band": 0.05,
    "rel_band": 0.25,
    "quarterly_band": 0.01,
    "min_trade_usd": 5,
    "run_time_et": "10:05",
    "max_orders_per_day": 6,
    "max_spread_bps": 25,
    # Long-term policy: keep investing through declines; pace big trades in a crash;
    # never sell into an extreme day (postpone rebalancing sales one day; buying continues).
    "drawdown_policy": "stay_invested",
    "crash_pacing": {"threshold": 0.15, "max_trade_pct_of_sleeve": 0.10},
    "extreme_day_pct": 0.05,
}

# Active strategies get their own sleeve stops (enforced by the Execution Desk).
ALEX_RISK_POLICY = {"drawdown_policy": "sleeve_pause", "sleeve_drawdown_pause": 0.20}
MAX_RISK_POLICY = {"drawdown_policy": "sleeve_stop", "sleeve_drawdown_pause": 0.10, "daily_loss_pause": 0.03, "per_trade_stop_required": True}

DEFAULT_MANDATES = {
    "tim": TIM_DEFAULT_MANDATE,
    "alex": {"status": "activates in Phase 3", **ALEX_RISK_POLICY},
    "max": {"status": "activates in Phase 4", **MAX_RISK_POLICY},
}


@dataclass(frozen=True)
class ManagerRow:
    id: str
    display_name: str
    role: str
    mode: str
    mode_changed_at: str
    mode_changed_by: str
    status: str
    status_detail: str | None

    @property
    def book(self) -> str | None:
        return {"SIM": "SIM", "LIVE": "LIVE"}.get(self.mode)


class Registry:
    def __init__(self, db: Database, events: EventLog, *, clock: Clock = utc_now) -> None:
        self.db = db
        self.events = events
        self.clock = clock

    def ensure_roster(self) -> None:
        now = iso(self.clock())
        with self.db.tx() as conn:
            for mid, (name, role) in ROSTER.items():
                conn.execute(
                    "INSERT OR IGNORE INTO managers (id, display_name, role, mode, mode_changed_at, mode_changed_by) VALUES (?,?,?,?,?,?)",
                    (mid, name, role, "INACTIVE", now, "system"),
                )
                if not conn.execute("SELECT 1 FROM mandates WHERE manager_id = ?", (mid,)).fetchone():
                    conn.execute(
                        "INSERT INTO mandates (manager_id, version, params_json, created_at, created_by, note) VALUES (?,?,?,?,?,?)",
                        (mid, 1, json.dumps(DEFAULT_MANDATES[mid], sort_keys=True), now, "system", "initial mandate"),
                    )

    def get(self, manager_id: str) -> ManagerRow:
        row = self.db.one("SELECT * FROM managers WHERE id = ?", (manager_id,))
        if row is None:
            raise KeyError(manager_id)
        return ManagerRow(**{k: row[k] for k in row.keys()})

    def all(self) -> list[ManagerRow]:
        return [ManagerRow(**{k: r[k] for k in r.keys()}) for r in self.db.query("SELECT * FROM managers ORDER BY rowid")]

    def set_mode(self, manager_id: str, mode: str, *, by: str, note: str = "") -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        old = self.get(manager_id).mode
        if old == mode:
            return
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE managers SET mode = ?, mode_changed_at = ?, mode_changed_by = ? WHERE id = ?",
                (mode, iso(self.clock()), by, manager_id),
            )
        self.events.emit(
            "MANAGER_MODE",
            f"{self.get(manager_id).display_name}: {old} → {mode}" + (f" ({note})" if note else ""),
            actor=by,
            manager_id=manager_id,
            severity="warning" if mode == "LIVE" else "info",
            payload={"from": old, "to": mode, "note": note},
        )

    def set_status(self, manager_id: str, status: str, detail: str | None = None) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE managers SET status = ?, status_detail = ? WHERE id = ?", (status, detail, manager_id))

    def mandate(self, manager_id: str) -> tuple[int, dict[str, Any]]:
        row = self.db.one(
            "SELECT version, params_json FROM mandates WHERE manager_id = ? AND active = 1 ORDER BY version DESC LIMIT 1",
            (manager_id,),
        )
        if row is None:
            raise KeyError(f"no mandate for {manager_id}")
        return int(row["version"]), json.loads(row["params_json"])

    def update_mandate(self, manager_id: str, params: dict[str, Any], *, by: str, note: str) -> int:
        version, old = self.mandate(manager_id)
        new_version = version + 1
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO mandates (manager_id, version, params_json, created_at, created_by, note) VALUES (?,?,?,?,?,?)",
                (manager_id, new_version, json.dumps(params, sort_keys=True), iso(self.clock()), by, note),
            )
        changed = {k: {"from": old.get(k), "to": params.get(k)} for k in set(old) | set(params) if old.get(k) != params.get(k)}
        self.events.emit(
            "MANDATE_CHANGED",
            f"{self.get(manager_id).display_name}'s mandate updated to v{new_version}: {note}",
            actor=by,
            manager_id=manager_id,
            payload={"version": new_version, "changes": changed},
        )
        return new_version

    def mandate_history(self, manager_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM mandates WHERE manager_id = ? ORDER BY version DESC", (manager_id,))
        return [{**dict(r), "params": json.loads(r["params_json"])} for r in rows]
