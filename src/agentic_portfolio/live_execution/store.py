"""Atomic intent/order persistence and exclusive execution locks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_portfolio.agent.persist import atomic_write_json, read_json
from agentic_portfolio.live_execution.types import (
    BrokerOrderRecord,
    ExecutionIntent,
    intent_from_dict,
    order_from_dict,
)
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import RuntimeMode, get_active_runtime


class IntentLock:
    """Exclusive lock for one execution intent. Fail closed on contention."""

    def __init__(self, path: Path, *, stale_seconds: int = 120) -> None:
        self.path = path
        self.stale_seconds = stale_seconds
        self.held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = self.path.open("x")
            fd.write("locked")
            fd.close()
            self.held = True
            return True
        except FileExistsError:
            try:
                age = self.path.stat().st_mtime
            except OSError:
                return False
            import time

            if time.time() - age > self.stale_seconds:
                try:
                    self.path.unlink()
                except OSError:
                    return False
                return self.acquire()
            return False

    def release(self) -> None:
        if not self.held:
            return
        try:
            self.path.unlink()
        except OSError:
            pass
        self.held = False


def execution_dir(root: Path | None = None, *, mode: RuntimeMode | str | None = None) -> Path:
    base = root or project_root()
    current = mode or get_active_runtime()
    if isinstance(current, str):
        current = RuntimeMode(current)
    if current is RuntimeMode.LIVE:
        return base / "state" / "live_execution"
    return base / "state" / "paper_execution"


class ExecutionStore:
    def __init__(self, root: Path | None = None, *, runtime_mode: RuntimeMode | str | None = None) -> None:
        self.base = root or project_root()
        self.runtime_mode = runtime_mode or get_active_runtime()
        if isinstance(self.runtime_mode, str):
            self.runtime_mode = RuntimeMode(self.runtime_mode)
        self.root = execution_dir(self.base, mode=self.runtime_mode)
        self.intents_dir = self.root / "intents"
        self.orders_dir = self.root / "orders"
        self.lock_dir = self.root / "locks"
        self.intents_dir.mkdir(parents=True, exist_ok=True)
        self.orders_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.json"
        self._index = read_json(self._index_path, {"by_approval": {}, "by_intent": {}, "by_broker_order": {}})

    def intent_id_for(self, approval_id: str) -> str:
        return f"exec:{approval_id}"

    def lock_for(self, intent_id: str, *, stale_seconds: int = 120) -> IntentLock:
        safe = intent_id.replace(":", "_")
        return IntentLock(self.lock_dir / f"{safe}.lock", stale_seconds=stale_seconds)

    def get_intent(self, intent_id: str) -> ExecutionIntent | None:
        path = self.intents_dir / f"{intent_id.replace(':', '_')}.json"
        data = read_json(path, None)
        if not isinstance(data, dict):
            return None
        return intent_from_dict(data)

    def intent_for_approval(self, approval_id: str) -> ExecutionIntent | None:
        intent_id = self._index.get("by_approval", {}).get(approval_id) or self.intent_id_for(approval_id)
        return self.get_intent(intent_id)

    def save_intent(self, intent: ExecutionIntent) -> ExecutionIntent:
        path = self.intents_dir / f"{intent.intent_id.replace(':', '_')}.json"
        atomic_write_json(path, intent.to_dict())
        self._index.setdefault("by_approval", {})[intent.approval_id] = intent.intent_id
        self._index.setdefault("by_intent", {})[intent.intent_id] = {
            "approval_id": intent.approval_id,
            "status": intent.status.value if hasattr(intent.status, "value") else str(intent.status),
            "broker_order_id": intent.broker_order_id,
        }
        if intent.broker_order_id:
            self._index.setdefault("by_broker_order", {})[intent.broker_order_id] = intent.intent_id
        atomic_write_json(self._index_path, self._index)
        return intent

    def get_order(self, order_id: str) -> BrokerOrderRecord | None:
        path = self.orders_dir / f"{order_id}.json"
        data = read_json(path, None)
        if not isinstance(data, dict):
            return None
        return order_from_dict(data)

    def orders(self) -> list[BrokerOrderRecord]:
        rows: list[BrokerOrderRecord] = []
        for path in self.orders_dir.glob("*.json"):
            data = read_json(path, None)
            if isinstance(data, dict):
                rows.append(order_from_dict(data))
        rows.sort(key=lambda r: r.updated_at or "", reverse=True)
        return rows

    def intents(self) -> list[ExecutionIntent]:
        rows: list[ExecutionIntent] = []
        for path in self.intents_dir.glob("*.json"):
            data = read_json(path, None)
            if isinstance(data, dict):
                rows.append(intent_from_dict(data))
        rows.sort(key=lambda r: r.created_at or "", reverse=True)
        return rows

    def save_order(self, order: BrokerOrderRecord) -> BrokerOrderRecord:
        atomic_write_json(self.orders_dir / f"{order.order_id}.json", order.to_dict())
        if order.broker_order_id:
            self._index.setdefault("by_broker_order", {})[order.broker_order_id] = order.intent_id
            atomic_write_json(self._index_path, self._index)
        return order

    def order_for_intent(self, intent_id: str) -> BrokerOrderRecord | None:
        for order in self.orders():
            if order.intent_id == intent_id:
                return order
        return None
