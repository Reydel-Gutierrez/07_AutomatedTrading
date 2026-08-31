"""Notification fan-out. Dashboard store is the first sink; others can be added later."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import uuid4

from agentic_portfolio.notify.store import NotificationStore
from agentic_portfolio.notify.types import Notification, NotificationKind, NotificationSink


class NotificationEngine:
    def __init__(self, store: NotificationStore, *, sinks: Iterable[NotificationSink] | None = None, now_fn=None) -> None:
        self.store = store
        self.sinks = list(sinks or [])
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._last_kind: dict[str, str] = {}

    def emit(self, kind: NotificationKind | str, *, title: str, body: str, payload: dict[str, Any] | None = None, dedupe: bool = True) -> Notification:
        wanted = NotificationKind(str(kind.value if isinstance(kind, NotificationKind) else kind))
        stamp = self._now()
        if stamp.tzinfo is None:
            from datetime import timezone as tz

            stamp = stamp.replace(tzinfo=tz.utc)
        key = wanted.value
        if dedupe and self._last_kind.get(key) == body:
            existing = next((n for n in self.store.unread() if n.kind == wanted), None)
            if existing is not None:
                return existing
        item = Notification(
            notification_id=str(uuid4()),
            kind=wanted,
            title=title,
            body=body,
            created_at=stamp.isoformat(),
            payload=dict(payload or {}),
        )
        self.store.add(item)
        self._last_kind[key] = body
        for sink in self.sinks:
            sink.emit(item)
        return item
