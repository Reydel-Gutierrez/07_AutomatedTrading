"""Persist approval packets. Create is append-only; status updates rewrite the same id with history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_portfolio.approval.types import OPEN_STATUSES, ApprovalPacket, ApprovalStatus, packet_from_dict
from agentic_portfolio.paths import project_root
from agentic_portfolio.schemas import to_dict


def approval_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "state" / "approval_packets"


class ApprovalStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = approval_dir(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.json"
        self._index = self._load_index()

    def _load_index(self) -> dict[str, Any]:
        if self._index_path.exists():
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        return {
            "by_id": {},
            "by_symbol": {},
            "by_status": {},
            "by_order_plan_id": {},
            "by_date": {},
            "by_run": {},
        }

    def _save_index(self) -> None:
        self._index_path.write_text(json.dumps(self._index, indent=2, default=str), encoding="utf-8")

    def path_for(self, approval_id: str) -> Path:
        return self.root / f"{approval_id}.json"

    def _index_packet(self, packet: ApprovalPacket | dict[str, Any]) -> None:
        data = to_dict(packet) if not isinstance(packet, dict) else packet
        approval_id = str(data["approval_id"])
        symbol = str(data.get("symbol") or "").upper()
        status = str(data.get("status") or "")
        order_plan_id = None
        refs = data.get("evidence_refs") or {}
        summary = data.get("order_plan_summary") or {}
        order_plan_id = refs.get("order_plan_id") or summary.get("order_plan_id")
        meta = {
            "created_at": data.get("created_at"),
            "path": f"{approval_id}.json",
            "symbol": symbol,
            "status": status,
            "action": data.get("action"),
            "order_plan_id": order_plan_id,
        }
        self._index.setdefault("by_id", {})[approval_id] = meta
        if symbol:
            ids = self._index.setdefault("by_symbol", {}).setdefault(symbol, [])
            if approval_id not in ids:
                ids.append(approval_id)
        for bucket, items in list(self._index.setdefault("by_status", {}).items()):
            self._index["by_status"][bucket] = [i for i in items if i != approval_id]
        self._index.setdefault("by_status", {}).setdefault(status, []).append(approval_id)
        if order_plan_id:
            self._index.setdefault("by_order_plan_id", {})[str(order_plan_id)] = approval_id
        day = str(data.get("created_at") or "")[:10]
        if day:
            days = self._index.setdefault("by_date", {}).setdefault(day, [])
            if approval_id not in days:
                days.append(approval_id)

    def save(self, packet: ApprovalPacket) -> Path:
        path = self.path_for(packet.approval_id)
        if path.exists():
            raise FileExistsError(f"approval packet already exists: {packet.approval_id}")
        path.write_text(json.dumps(to_dict(packet), indent=2, default=str), encoding="utf-8")
        self._index_packet(packet)
        self._save_index()
        return path

    def update(self, packet: ApprovalPacket) -> Path:
        path = self.path_for(packet.approval_id)
        if not path.exists():
            raise FileNotFoundError(f"approval packet missing: {packet.approval_id}")
        path.write_text(json.dumps(to_dict(packet), indent=2, default=str), encoding="utf-8")
        self._index_packet(packet)
        self._save_index()
        return path

    def save_run(self, run_id: str, record: dict[str, Any]) -> Path:
        path = self.root / f"run_{run_id}.json"
        if path.exists():
            raise FileExistsError(f"approval run already exists: {run_id}")
        path.write_text(json.dumps(to_dict(record), indent=2, default=str), encoding="utf-8")
        self._index.setdefault("by_run", {})[run_id] = path.name
        self._save_index()
        return path

    def get(self, approval_id: str) -> dict[str, Any] | None:
        path = self.path_for(approval_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def get_packet(self, approval_id: str) -> ApprovalPacket | None:
        raw = self.get(approval_id)
        return packet_from_dict(raw) if raw else None

    def by_order_plan_id(self, order_plan_id: str) -> ApprovalPacket | None:
        approval_id = (self._index.get("by_order_plan_id") or {}).get(str(order_plan_id))
        return self.get_packet(approval_id) if approval_id else None

    def open_for_symbol(self, symbol: str) -> list[ApprovalPacket]:
        ids = (self._index.get("by_symbol") or {}).get(str(symbol).upper(), [])
        out: list[ApprovalPacket] = []
        for approval_id in ids:
            packet = self.get_packet(approval_id)
            if packet is not None and packet.status in OPEN_STATUSES:
                out.append(packet)
        return out

    def all_ids(self) -> list[str]:
        items = self._index.get("by_id") or {}
        return sorted(items, key=lambda i: str((items.get(i) or {}).get("created_at") or ""), reverse=True)

    def all_packets(self) -> list[ApprovalPacket]:
        return [p for approval_id in self.all_ids() if (p := self.get_packet(approval_id))]

    def by_status(self, status: ApprovalStatus | str) -> list[ApprovalPacket]:
        key = status.value if isinstance(status, ApprovalStatus) else str(status)
        ids = (self._index.get("by_status") or {}).get(key, [])
        return [p for approval_id in ids if (p := self.get_packet(approval_id))]
