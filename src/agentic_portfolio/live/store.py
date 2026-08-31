"""Persist LIVE Robinhood portfolio snapshots. Isolated from the paper book."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_portfolio.paths import project_root
from agentic_portfolio.schemas import to_dict


def live_book_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "state" / "live_book"


class LivePortfolioStore:
    def __init__(self, root: Path | None = None) -> None:
        self.base = root or project_root()
        self.root = live_book_dir(self.base)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "snapshots").mkdir(parents=True, exist_ok=True)

    def current_path(self) -> Path:
        return self.root / "current.json"

    def hwm_path(self) -> Path:
        return self.root / "hwm_state.json"

    def session_path(self) -> Path:
        return self.root / "session_state.json"

    def history_path(self) -> Path:
        return self.root / "nav_history.json"

    def mcp_path(self) -> Path:
        return self.root / "last_mcp_read.json"

    def current_book(self) -> dict[str, Any] | None:
        path = self.current_path()
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save_snapshot(self, snapshot_id: str, record: dict[str, Any]) -> Path:
        payload = to_dict(record)
        snap_path = self.root / "snapshots" / f"{snapshot_id}.json"
        if snap_path.exists():
            raise FileExistsError(f"live snapshot already exists: {snapshot_id}")
        snap_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        self.current_path().write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return snap_path

    def save_mcp(self, payload: dict[str, Any]) -> Path:
        path = self.mcp_path()
        path.write_text(json.dumps(to_dict(payload), indent=2, default=str), encoding="utf-8")
        return path
