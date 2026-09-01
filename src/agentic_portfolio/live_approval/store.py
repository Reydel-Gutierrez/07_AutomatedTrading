"""Persist LIVE approval queue items."""

from __future__ import annotations

from pathlib import Path

from agentic_portfolio.agent.persist import atomic_write_json, read_json
from agentic_portfolio.ai.locks import FileLock
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import RuntimeMode, get_active_runtime
from agentic_portfolio.live_approval.types import LiveApproval, live_approval_from_dict


def live_approval_dir(root: Path | None = None, *, mode: RuntimeMode | str | None = None) -> Path:
    base = root or project_root()
    current = mode or get_active_runtime()
    if isinstance(current, str):
        current = RuntimeMode(current)
    if current is RuntimeMode.LIVE:
        return base / "state" / "live_ai" / "approvals"
    return base / "state" / "live_approvals"


class LiveApprovalStore:
    def __init__(self, root: Path | None = None, *, runtime_mode: RuntimeMode | str | None = None) -> None:
        self.base = root or project_root()
        self.runtime_mode = runtime_mode or get_active_runtime()
        if isinstance(self.runtime_mode, str):
            self.runtime_mode = RuntimeMode(self.runtime_mode)
        self.root = live_approval_dir(self.base, mode=self.runtime_mode)
        self.items_dir = self.root / "items"
        self.lock_dir = self.root / "locks"
        self.items_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.json"
        self._index = read_json(self._index_path, {"by_id": {}, "by_ticker": {}, "by_status": {}})

    def path_for(self, approval_id: str) -> Path:
        return self.items_dir / f"{approval_id}.json"

    def _save_index(self) -> None:
        atomic_write_json(self._index_path, self._index)

    def save(self, item: LiveApproval) -> Path:
        path = self.path_for(item.approval_id)
        atomic_write_json(path, item.to_dict())
        self._index.setdefault("by_id", {})[item.approval_id] = {
            "ticker": item.ticker,
            "status": item.status.value,
            "created_at": item.created_at,
        }
        ids = self._index.setdefault("by_ticker", {}).setdefault(item.ticker, [])
        if item.approval_id not in ids:
            ids.append(item.approval_id)
        for bucket, values in list(self._index.setdefault("by_status", {}).items()):
            self._index["by_status"][bucket] = [i for i in values if i != item.approval_id]
        self._index.setdefault("by_status", {}).setdefault(item.status.value, []).append(item.approval_id)
        self._save_index()
        return path

    def get(self, approval_id: str) -> LiveApproval | None:
        path = self.path_for(approval_id)
        if not path.exists():
            return None
        data = read_json(path, None)
        if not isinstance(data, dict):
            return None
        return live_approval_from_dict(data)

    def all(self) -> list[LiveApproval]:
        items: list[LiveApproval] = []
        for approval_id in list((self._index.get("by_id") or {}).keys()):
            item = self.get(approval_id)
            if item is not None:
                items.append(item)
        items.sort(key=lambda row: row.created_at, reverse=True)
        return items

    def pending(self) -> list[LiveApproval]:
        from agentic_portfolio.live_approval.types import LiveApprovalStatus

        return [item for item in self.all() if item.status == LiveApprovalStatus.PENDING]

    def lock_for(self, ticker: str, proposed_action: str) -> FileLock:
        safe = f"{str(ticker).upper()}_{str(proposed_action).upper()}".replace(":", "_")
        return FileLock(self.lock_dir / f"{safe}.lock")

    def pending_for(
        self,
        *,
        ticker: str,
        proposed_action: str,
        watch_id: str | None = None,
    ) -> list[LiveApproval]:
        ticker_u = str(ticker).upper()
        action_u = str(proposed_action).upper()
        rows = [
            item
            for item in self.pending()
            if item.ticker == ticker_u and str(item.proposed_action).upper() == action_u
        ]
        if not watch_id:
            return rows
        watched = [item for item in rows if item.watch_id == watch_id]
        return watched or rows
