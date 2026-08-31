"""Persistent AI usage ledger. Restarting the app cannot reset the monthly cap."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from agentic_portfolio.ai.config import money
from agentic_portfolio.ai.locks import FileLock
from agentic_portfolio.ai.pricing import quantize
from agentic_portfolio.ai.types import UsageRecord
from agentic_portfolio.calendar import EASTERN
from agentic_portfolio.journal import append_jsonl, read_jsonl
from agentic_portfolio.paths import project_root
from agentic_portfolio.schemas import to_dict


def month_key(now: datetime | None = None) -> str:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(EASTERN).strftime("%Y-%m")


def day_key(now: datetime | None = None) -> str:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(EASTERN).strftime("%Y-%m-%d")


class UsageLedger:
    def __init__(self, root: Path | None = None, *, config: dict[str, Any] | None = None) -> None:
        self.base = root or project_root()
        rel = ((config or {}).get("budget") or {}).get("ledger_dir") or "state/ai_budget"
        self.root = self.base / rel
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(self.root / "ledger.lock")

    def jsonl_path(self) -> Path:
        return self.root / "usage.jsonl"

    def month_path(self, month: str | None = None) -> Path:
        return self.root / f"month_{month or month_key()}.json"

    def load_month(self, month: str | None = None, *, now: datetime | None = None) -> dict[str, Any]:
        key = month or month_key(now)
        path = self.month_path(key)
        if not path.exists():
            return {
                "month": key,
                "spent": "0",
                "reserved": [],
                "calls": 0,
                "daily": {},
                "by_provider": {},
                "by_model": {},
            }
        return json.loads(path.read_text(encoding="utf-8"))

    def save_month(self, payload: dict[str, Any]) -> None:
        key = str(payload["month"])
        path = self.month_path(key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)

    def records(self, *, month: str | None = None) -> list[dict[str, Any]]:
        rows = read_jsonl(self.jsonl_path())
        if month is None:
            return rows
        prefix = month
        return [row for row in rows if str(row.get("month") or str(row.get("timestamp") or "")[:7]) == prefix]

    def append(self, record: UsageRecord | dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        payload = to_dict(record) if not isinstance(record, dict) else dict(record)
        payload.setdefault("id", str(uuid4()))
        payload.setdefault("month", month_key(now))
        payload.setdefault("day", day_key(now))
        append_jsonl(payload, self.jsonl_path())
        return payload

    def with_lock(self, fn: Callable[[], Any]) -> Any:
        with self._lock:
            return fn()
