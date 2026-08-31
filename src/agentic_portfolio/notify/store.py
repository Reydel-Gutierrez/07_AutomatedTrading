"""Persist dashboard notifications."""

from __future__ import annotations

from pathlib import Path

from agentic_portfolio.agent.persist import atomic_write_json, read_json
from agentic_portfolio.paths import project_root
from agentic_portfolio.notify.types import Notification, notification_from_dict


class NotificationStore:
    def __init__(self, root: Path | None = None) -> None:
        self.base = root or project_root()
        self.path = self.base / "state" / "runtime" / "notifications.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = read_json(self.path, {"items": []})

    def _save(self) -> None:
        atomic_write_json(self.path, self._data)

    def add(self, item: Notification) -> Notification:
        items = list(self._data.get("items") or [])
        items.insert(0, item.to_dict())
        self._data["items"] = items[:500]
        self._save()
        return item

    def all(self) -> list[Notification]:
        return [notification_from_dict(row) for row in self._data.get("items") or [] if isinstance(row, dict)]

    def unread(self) -> list[Notification]:
        return [item for item in self.all() if not item.read]

    def mark_read(self, notification_id: str | None = None) -> int:
        count = 0
        items = []
        for row in self._data.get("items") or []:
            if not isinstance(row, dict):
                continue
            if notification_id is None or str(row.get("notification_id")) == notification_id:
                if not row.get("read"):
                    row["read"] = True
                    count += 1
            items.append(row)
        self._data["items"] = items
        self._save()
        return count
