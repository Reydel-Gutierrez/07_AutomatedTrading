"""Persist Robinhood ReviewResult. Create is append-only; never places."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_portfolio.paths import project_root
from agentic_portfolio.review.types import ReviewResult, result_from_dict
from agentic_portfolio.schemas import to_dict


def review_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "state" / "robinhood_reviews"


class ReviewStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = review_dir(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.json"
        self._index = self._load_index()

    def _load_index(self) -> dict[str, Any]:
        if self._index_path.exists():
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        return {
            "by_id": {},
            "by_approval_id": {},
            "by_order_plan_id": {},
            "by_symbol": {},
            "by_status": {},
            "by_date": {},
            "by_run": {},
        }

    def _save_index(self) -> None:
        self._index_path.write_text(json.dumps(self._index, indent=2, default=str), encoding="utf-8")

    def path_for(self, review_id: str) -> Path:
        return self.root / f"{review_id}.json"

    def _index_result(self, result: ReviewResult | dict[str, Any]) -> None:
        data = to_dict(result) if not isinstance(result, dict) else result
        review_id = str(data["review_id"])
        symbol = str(data.get("symbol") or "").upper()
        status = str(data.get("status") or "")
        approval_id = str(data.get("approval_id") or "")
        order_plan_id = str(data.get("order_plan_id") or "")
        meta = {
            "reviewed_at": data.get("reviewed_at"),
            "path": f"{review_id}.json",
            "symbol": symbol,
            "status": status,
            "approval_id": approval_id,
            "order_plan_id": order_plan_id,
            "order_placed": False,
        }
        self._index.setdefault("by_id", {})[review_id] = meta
        if approval_id:
            ids = self._index.setdefault("by_approval_id", {}).setdefault(approval_id, [])
            if review_id not in ids:
                ids.append(review_id)
        if order_plan_id:
            self._index.setdefault("by_order_plan_id", {})[order_plan_id] = review_id
        if symbol:
            ids = self._index.setdefault("by_symbol", {}).setdefault(symbol, [])
            if review_id not in ids:
                ids.append(review_id)
        for bucket, items in list(self._index.setdefault("by_status", {}).items()):
            self._index["by_status"][bucket] = [i for i in items if i != review_id]
        self._index.setdefault("by_status", {}).setdefault(status, []).append(review_id)
        day = str(data.get("reviewed_at") or "")[:10]
        if day:
            days = self._index.setdefault("by_date", {}).setdefault(day, [])
            if review_id not in days:
                days.append(review_id)

    def save(self, result: ReviewResult) -> Path:
        path = self.path_for(result.review_id)
        if path.exists():
            raise FileExistsError(f"review result already exists: {result.review_id}")
        path.write_text(json.dumps(to_dict(result), indent=2, default=str), encoding="utf-8")
        self._index_result(result)
        self._save_index()
        return path

    def save_run(self, run_id: str, record: dict[str, Any]) -> Path:
        path = self.root / f"run_{run_id}.json"
        if path.exists():
            raise FileExistsError(f"review run already exists: {run_id}")
        path.write_text(json.dumps(to_dict(record), indent=2, default=str), encoding="utf-8")
        self._index.setdefault("by_run", {})[run_id] = path.name
        self._save_index()
        return path

    def get(self, review_id: str) -> dict[str, Any] | None:
        path = self.path_for(review_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def get_result(self, review_id: str) -> ReviewResult | None:
        raw = self.get(review_id)
        return result_from_dict(raw) if raw else None

    def by_approval_id(self, approval_id: str) -> list[ReviewResult]:
        ids = (self._index.get("by_approval_id") or {}).get(str(approval_id), [])
        out: list[ReviewResult] = []
        for review_id in ids:
            result = self.get_result(review_id)
            if result is not None:
                out.append(result)
        return out

    def all_ids(self) -> list[str]:
        items = self._index.get("by_id") or {}
        return sorted(items, key=lambda i: str((items.get(i) or {}).get("reviewed_at") or ""), reverse=True)

    def all_results(self) -> list[ReviewResult]:
        return [r for review_id in self.all_ids() if (r := self.get_result(review_id))]
