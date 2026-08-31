"""Persist watch items. LIVE artifacts stay under state/live_ai."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentic_portfolio.agent.persist import atomic_write_json, read_json
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import RuntimeMode, get_active_runtime
from agentic_portfolio.watch.types import WatchItem, WatchStatus, watch_from_dict


def watch_dir(root: Path | None = None, *, mode: RuntimeMode | str | None = None) -> Path:
    base = root or project_root()
    current = mode or get_active_runtime()
    if isinstance(current, str):
        current = RuntimeMode(current)
    if current is RuntimeMode.LIVE:
        return base / "state" / "live_ai" / "watch"
    return base / "state" / "watch"


class WatchStore:
    def __init__(self, root: Path | None = None, *, runtime_mode: RuntimeMode | str | None = None) -> None:
        self.base = root or project_root()
        self.runtime_mode = runtime_mode or get_active_runtime()
        if isinstance(self.runtime_mode, str):
            self.runtime_mode = RuntimeMode(self.runtime_mode)
        self.root = watch_dir(self.base, mode=self.runtime_mode)
        self.items_dir = self.root / "items"
        self.items_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.json"
        self._index = read_json(self._index_path, {"by_id": {}, "by_ticker": {}, "by_status": {}})

    def _save_index(self) -> None:
        atomic_write_json(self._index_path, self._index)

    def path_for(self, watch_id: str) -> Path:
        return self.items_dir / f"{watch_id}.json"

    def save(self, item: WatchItem) -> Path:
        path = self.path_for(item.watch_id)
        atomic_write_json(path, item.to_dict())
        meta = {"ticker": item.ticker, "status": item.status.value, "updated": item.last_updated}
        self._index.setdefault("by_id", {})[item.watch_id] = meta
        self._index.setdefault("by_ticker", {})[item.ticker] = item.watch_id
        for bucket, ids in list(self._index.setdefault("by_status", {}).items()):
            self._index["by_status"][bucket] = [i for i in ids if i != item.watch_id]
        self._index.setdefault("by_status", {}).setdefault(item.status.value, []).append(item.watch_id)
        self._save_index()
        return path

    def get(self, watch_id: str) -> WatchItem | None:
        path = self.path_for(watch_id)
        if not path.exists():
            return None
        data = read_json(path, None)
        if not isinstance(data, dict):
            return None
        return watch_from_dict(data)

    def by_ticker(self, ticker: str) -> WatchItem | None:
        watch_id = (self._index.get("by_ticker") or {}).get(str(ticker).upper())
        if not watch_id:
            return None
        return self.get(str(watch_id))

    def all(self) -> list[WatchItem]:
        items: list[WatchItem] = []
        for watch_id in list((self._index.get("by_id") or {}).keys()):
            item = self.get(watch_id)
            if item is not None:
                items.append(item)
        items.sort(key=lambda row: row.last_updated or row.created_at, reverse=True)
        return items

    def active(self) -> list[WatchItem]:
        return [item for item in self.all() if item.status not in {WatchStatus.REJECTED, WatchStatus.EXPIRED, WatchStatus.INVALIDATED}]
