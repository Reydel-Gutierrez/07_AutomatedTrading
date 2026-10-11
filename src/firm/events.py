"""The firm's single narrative and audit stream.

Every manager decision, desk step, reconcile result, and owner action is one
row. The dashboard activity feed, the CIO's tools, and agent animations read
only from here, so an agent can only describe what actually happened.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from firm.clock import Clock, iso, utc_now
from firm.db import Database

log = logging.getLogger(__name__)

# Event types that also go to the owner's phone (when Telegram is configured).
ALERT_TYPES = frozenset(
    {
        "ORDER_FILLED",
        "ORDER_REJECTED",
        "ORDER_UNKNOWN",
        "KILL_SWITCH_ENGAGED",
        "KILL_SWITCH_RELEASED",
        "RECONCILE_HOLD",
        "RECONCILE_CLEARED",
        "FOREIGN_ORDER",
        "EXTERNAL_FLOW",
        "BROKER_DISCONNECTED",
        "BROKER_APPROVAL_REQUIRED",
        "MANAGER_DEGRADED",
        "DRAWDOWN_BRAKE",
        "DRAWDOWN_ALERT",
        "PROPOSAL_CREATED",
        "AI_PROVIDER_UNAVAILABLE",
        "TURNOVER_LIMIT",
        "SLEEVE_PAUSED",
        "BACKUP_FAILED",
        "SERVICE_STARTED",
    }
)


@dataclass(frozen=True)
class Event:
    id: int
    ts: str
    actor: str
    type: str
    severity: str
    manager_id: str | None
    symbol: str | None
    summary: str
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "actor": self.actor,
            "type": self.type,
            "severity": self.severity,
            "manager_id": self.manager_id,
            "symbol": self.symbol,
            "summary": self.summary,
            "payload": self.payload,
        }


Sink = Callable[[Event], None]


class EventLog:
    def __init__(self, db: Database, *, clock: Clock = utc_now, sinks: Iterable[Sink] = ()) -> None:
        self.db = db
        self.clock = clock
        self.sinks = list(sinks)

    def emit(
        self,
        type: str,
        summary: str,
        *,
        actor: str = "system",
        manager_id: str | None = None,
        symbol: str | None = None,
        severity: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> Event:
        ts = iso(self.clock())
        body = json.dumps(payload or {}, default=str, sort_keys=True)
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO events (ts, actor, type, severity, manager_id, symbol, summary, payload_json)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (ts, actor, type, severity, manager_id, symbol, summary, body),
            )
            event_id = int(cur.lastrowid)
        event = Event(event_id, ts, actor, type, severity, manager_id, symbol, summary, payload or {})
        for sink in self.sinks:
            try:
                sink(event)
            except Exception:  # noqa: BLE001 — a notification failure never blocks the firm
                log.warning("event sink failed for %s", type, exc_info=True)
        return event

    def recent(
        self,
        *,
        after_id: int = 0,
        limit: int = 100,
        manager_id: str | None = None,
        types: Iterable[str] | None = None,
        before_id: int | None = None,
    ) -> list[Event]:
        sql = "SELECT * FROM events WHERE id > ?"
        params: list[Any] = [after_id]
        if before_id is not None:
            sql += " AND id < ?"
            params.append(before_id)
        if manager_id:
            sql += " AND manager_id = ?"
            params.append(manager_id)
        kinds = list(types or [])
        if kinds:
            sql += f" AND type IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        order = "ASC" if after_id and before_id is None else "DESC"
        sql += f" ORDER BY id {order} LIMIT ?"
        params.append(limit)
        return [_row(r) for r in self.db.query(sql, tuple(params))]

    def last_of(self, type: str, *, manager_id: str | None = None) -> Event | None:
        sql = "SELECT * FROM events WHERE type = ?"
        params: list[Any] = [type]
        if manager_id:
            sql += " AND manager_id = ?"
            params.append(manager_id)
        row = self.db.one(sql + " ORDER BY id DESC LIMIT 1", tuple(params))
        return _row(row) if row else None


def _row(row: Any) -> Event:
    return Event(
        id=int(row["id"]),
        ts=row["ts"],
        actor=row["actor"],
        type=row["type"],
        severity=row["severity"],
        manager_id=row["manager_id"],
        symbol=row["symbol"],
        summary=row["summary"],
        payload=json.loads(row["payload_json"] or "{}"),
    )
