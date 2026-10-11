"""What the CIO remembers across conversations: visible, editable notes — never hidden state."""

from __future__ import annotations

from typing import Any

from firm.clock import Clock, iso, utc_now
from firm.db import Database
from firm.events import EventLog

KINDS = ("objective", "preference", "decision", "concern", "fact")
MAX_IN_CONTEXT = 60


class Memory:
    def __init__(self, db: Database, events: EventLog, *, clock: Clock = utc_now) -> None:
        self.db = db
        self.events = events
        self.clock = clock

    def add(self, kind: str, text: str, *, by: str, conversation_id: int | None = None) -> dict[str, Any]:
        kind = kind if kind in KINDS else "fact"
        text = " ".join(str(text).split())[:600]
        if not text:
            raise ValueError("memory text is empty")
        now = iso(self.clock())
        with self.db.tx() as conn:
            dup = conn.execute("SELECT id FROM cio_memory WHERE active = 1 AND lower(text) = lower(?)", (text,)).fetchone()
            if dup:
                return self.get(int(dup["id"]))
            cur = conn.execute(
                "INSERT INTO cio_memory (kind, text, created_by, conversation_id, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (kind, text, by, conversation_id, now, now),
            )
            mid = int(cur.lastrowid)
        self.events.emit("CIO_MEMORY", f"CIO will remember ({kind}): {text}", actor=by, payload={"memory_id": mid})
        return self.get(mid)

    def get(self, mid: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM cio_memory WHERE id = ?", (mid,))
        if row is None:
            raise KeyError(mid)
        return dict(row)

    def update(self, mid: int, *, by: str, text: str | None = None, active: bool | None = None, kind: str | None = None) -> dict[str, Any]:
        current = self.get(mid)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE cio_memory SET text = ?, active = ?, kind = ?, updated_at = ? WHERE id = ?",
                (
                    " ".join(text.split())[:600] if text is not None else current["text"],
                    int(active) if active is not None else current["active"],
                    kind if kind in KINDS else current["kind"],
                    iso(self.clock()),
                    mid,
                ),
            )
        self.events.emit("CIO_MEMORY_EDITED", f"{by} {'removed' if active is False else 'edited'} a CIO memory: {current['text'][:80]}", actor=by, payload={"memory_id": mid})
        return self.get(mid)

    def active(self, limit: int = MAX_IN_CONTEXT) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM cio_memory WHERE active = 1 ORDER BY id DESC LIMIT ?", (limit,))]

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        words = [w for w in str(query).lower().split() if len(w) > 2][:6]
        if not words:
            return self.active(limit)
        where = " OR ".join("lower(text) LIKE ?" for _ in words)
        return [dict(r) for r in self.db.query(f"SELECT * FROM cio_memory WHERE {where} ORDER BY id DESC LIMIT ?", (*[f"%{w}%" for w in words], limit))]
