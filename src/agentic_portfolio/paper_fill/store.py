"""Persist paper fills, blotter, and paper portfolio snapshots. Never overwrite history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_portfolio.paths import project_root
from agentic_portfolio.schemas import to_dict


def paper_fills_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "state" / "paper_fills"


def paper_book_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "state" / "paper_book"


class PaperFillStore:
    def __init__(self, root: Path | None = None) -> None:
        self.base = root or project_root()
        self.root = paper_fills_dir(self.base)
        self.book_root = paper_book_dir(self.base)
        self.root.mkdir(parents=True, exist_ok=True)
        self.book_root.mkdir(parents=True, exist_ok=True)
        (self.book_root / "snapshots").mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.json"
        self._index = self._load_index()

    def _load_index(self) -> dict[str, Any]:
        if self._index_path.exists():
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        return {
            "by_id": {},
            "by_symbol": {},
            "by_date": {},
            "by_order_plan_id": {},
            "filled_order_plan_ids": [],
        }

    def _save_index(self) -> None:
        self._index_path.write_text(json.dumps(self._index, indent=2, default=str), encoding="utf-8")

    def path_for(self, run_id: str) -> Path:
        return self.root / f"{run_id}.json"

    def filled_order_plan_ids(self) -> set[str]:
        return {str(x) for x in self._index.get("filled_order_plan_ids") or []}

    def has_fill(self, order_plan_id: str) -> bool:
        return str(order_plan_id) in self.filled_order_plan_ids()

    def save(self, run_id: str, record: dict[str, Any]) -> Path:
        path = self.path_for(run_id)
        if path.exists():
            raise FileExistsError(f"paper fill run already exists: {run_id}")
        payload = to_dict(record)
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        self._index.setdefault("by_id", {})[run_id] = {
            "created_at": record.get("created_at"),
            "path": path.name,
            "symbols": record.get("symbols") or [],
            "filled": record.get("filled_count"),
        }
        day = str(record.get("created_at") or "")[:10]
        if day:
            self._index.setdefault("by_date", {}).setdefault(day, []).append(run_id)
        for symbol in record.get("symbols") or []:
            self._index.setdefault("by_symbol", {}).setdefault(str(symbol).upper(), []).append(run_id)
        for plan_id in record.get("filled_order_plan_ids") or []:
            self._index.setdefault("by_order_plan_id", {})[str(plan_id)] = run_id
            ids = self._index.setdefault("filled_order_plan_ids", [])
            if str(plan_id) not in ids:
                ids.append(str(plan_id))
        self._save_index()
        self._write_book(run_id, payload)
        self._append_blotter(payload)
        return path

    def _write_book(self, run_id: str, record: dict[str, Any]) -> None:
        snapshot = {
            "run_id": run_id,
            "created_at": record.get("created_at"),
            "paper_environment": True,
            "live_book_untouched": True,
            "context": record.get("context_after"),
            "lots": record.get("lots") or [],
            "fills": record.get("fills") or [],
            "blotter": record.get("blotter") or [],
            "reconciliation": record.get("reconciliation"),
        }
        snap_path = self.book_root / "snapshots" / f"{run_id}.json"
        snap_path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
        current = self.book_root / "current.json"
        current.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")

    def _append_blotter(self, record: dict[str, Any]) -> None:
        path = self.book_root / "blotter.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for row in record.get("blotter") or []:
                f.write(json.dumps(to_dict(row), default=str) + "\n")

    def get(self, run_id: str) -> dict[str, Any] | None:
        path = self.path_for(run_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def current_book(self) -> dict[str, Any] | None:
        path = self.book_root / "current.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def all_ids(self) -> list[str]:
        items = self._index.get("by_id") or {}
        return sorted(items, key=lambda i: str((items.get(i) or {}).get("created_at") or ""), reverse=True)

    def all_runs(self) -> list[dict[str, Any]]:
        return [r for run_id in self.all_ids() if (r := self.get(run_id))]
