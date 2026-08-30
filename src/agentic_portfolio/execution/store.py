"""Persist paper OrderPlans. Never overwrite history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_portfolio.paths import project_root
from agentic_portfolio.schemas import to_dict


def order_plans_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "state" / "order_plans"


class OrderPlanStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = order_plans_dir(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.json"
        self._index = self._load_index()

    def _load_index(self) -> dict[str, Any]:
        if self._index_path.exists():
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        return {"by_id": {}, "by_symbol": {}, "by_date": {}, "by_run": {}}

    def _save_index(self) -> None:
        self._index_path.write_text(json.dumps(self._index, indent=2, default=str), encoding="utf-8")

    def path_for(self, run_id: str) -> Path:
        return self.root / f"{run_id}.json"

    def save(self, run_id: str, record: dict[str, Any]) -> Path:
        path = self.path_for(run_id)
        if path.exists():
            raise FileExistsError(f"order plan run already exists: {run_id}")
        path.write_text(json.dumps(to_dict(record), indent=2, default=str), encoding="utf-8")
        self._index.setdefault("by_id", {})[run_id] = {
            "created_at": record.get("created_at"),
            "path": path.name,
            "symbols": record.get("symbols") or [],
        }
        day = str(record.get("created_at") or "")[:10]
        if day:
            self._index.setdefault("by_date", {}).setdefault(day, []).append(run_id)
        self._index.setdefault("by_run", {})[run_id] = path.name
        for symbol in record.get("symbols") or []:
            self._index.setdefault("by_symbol", {}).setdefault(str(symbol).upper(), []).append(run_id)
        self._save_index()
        return path

    def get(self, run_id: str) -> dict[str, Any] | None:
        path = self.path_for(run_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def all_ids(self) -> list[str]:
        items = self._index.get("by_id") or {}
        return sorted(items, key=lambda i: str((items.get(i) or {}).get("created_at") or ""), reverse=True)

    def all_runs(self) -> list[dict[str, Any]]:
        return [r for run_id in self.all_ids() if (r := self.get(run_id))]
