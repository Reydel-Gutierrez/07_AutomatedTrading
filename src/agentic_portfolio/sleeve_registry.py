"""Persisted sleeve registry.

A symbol cannot silently move from TACTICAL to CORE_GROWTH because a tactical
thesis failed. Reassignment is an explicit SLEEVE_RECLASSIFICATION_REVIEW
decision. Risk checks use the new sleeve only after that decision is recorded
as approved under the current execution mode.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.paths import project_root
from agentic_portfolio.schemas import (
    Sleeve,
    SleeveAssignmentStatus,
    SleeveReclassificationEvent,
    SleeveRecord,
    to_dict,
)


class SleeveReclassificationRequired(Exception):
    """Raised when a caller attempts to change sleeve without an explicit review."""


def sleeve_registry_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "state" / "sleeve_registry.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SleeveRegistry:
    def __init__(self, path: Path | None = None, journal_path: Path | None = None) -> None:
        self.path = path or sleeve_registry_path()
        self.journal_path = journal_path
        self._data: dict[str, Any] = {"records": {}, "reclassifications": []}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, default=str), encoding="utf-8")
        return self.path

    def get(self, symbol: str) -> SleeveRecord | None:
        raw = self._data.get("records", {}).get(symbol.upper())
        if not raw:
            return None
        return SleeveRecord(
            symbol=raw["symbol"],
            sleeve=Sleeve(raw["sleeve"]),
            assigned_at=raw["assigned_at"],
            thesis_id=raw.get("thesis_id"),
            status=SleeveAssignmentStatus(raw.get("status") or SleeveAssignmentStatus.PROPOSED.value),
            source_decision_id=raw.get("source_decision_id"),
            last_reviewed_at=raw.get("last_reviewed_at"),
            quantity=raw.get("quantity"),
            market_value=raw.get("market_value"),
        )

    def all_records(self) -> list[SleeveRecord]:
        return [self.get(s) for s in self._data.get("records", {}) if self.get(s)]  # type: ignore[misc]

    def assign(
        self,
        *,
        symbol: str,
        sleeve: Sleeve,
        thesis_id: str | None = None,
        status: SleeveAssignmentStatus = SleeveAssignmentStatus.PROPOSED,
        source_decision_id: str | None = None,
        assigned_at: str | None = None,
    ) -> SleeveRecord:
        existing = self.get(symbol)
        if existing is not None and existing.sleeve != sleeve and existing.status not in {
            SleeveAssignmentStatus.CLOSED,
            SleeveAssignmentStatus.REJECTED,
        }:
            raise SleeveReclassificationRequired(
                f"{symbol.upper()} is {existing.sleeve.value}; changing to {sleeve.value} "
                "requires SLEEVE_RECLASSIFICATION_REVIEW"
            )
        ts = assigned_at or _now()
        rec = SleeveRecord(
            symbol=symbol.upper(),
            sleeve=sleeve,
            assigned_at=existing.assigned_at if existing and existing.sleeve == sleeve else ts,
            thesis_id=thesis_id if thesis_id is not None else (existing.thesis_id if existing else None),
            status=status,
            source_decision_id=source_decision_id,
            last_reviewed_at=ts,
        )
        self._data.setdefault("records", {})[symbol.upper()] = to_dict(rec)
        self.save()
        return rec

    def propose_reclassification(
        self,
        *,
        symbol: str,
        new_sleeve: Sleeve,
        reason: str,
        new_thesis_id: str | None,
        review: str,
        approved: bool,
        timestamp: str | None = None,
        decision_id: str | None = None,
    ) -> SleeveReclassificationEvent:
        existing = self.get(symbol)
        if existing is None:
            raise KeyError(f"{symbol} has no sleeve assignment")
        ts = timestamp or _now()
        event = SleeveReclassificationEvent(
            decision_id=decision_id or str(uuid4()),
            symbol=symbol.upper(),
            old_sleeve=existing.sleeve,
            new_sleeve=new_sleeve,
            reason=reason,
            new_thesis_id=new_thesis_id,
            review_flag="SLEEVE_RECLASSIFICATION_REVIEW",
            timestamp=ts,
            approved=approved,
            review=review,
        )
        self._data.setdefault("reclassifications", []).append(to_dict(event))
        jp = self.journal_path or (project_root() / "logs" / "sleeve_reclassification.jsonl")
        append_jsonl(to_dict(event), jp)
        if approved:
            rec = SleeveRecord(
                symbol=existing.symbol,
                sleeve=new_sleeve,
                assigned_at=ts,
                thesis_id=new_thesis_id,
                status=SleeveAssignmentStatus.ACTIVE,
                source_decision_id=event.decision_id,
                last_reviewed_at=ts,
            )
            self._data["records"][symbol.upper()] = to_dict(rec)
        self.save()
        return event

    def set_status(self, symbol: str, status: SleeveAssignmentStatus) -> SleeveRecord:
        rec = self.get(symbol)
        if rec is None:
            raise KeyError(symbol)
        rec.status = status
        rec.last_reviewed_at = _now()
        self._data["records"][symbol.upper()] = to_dict(rec)
        self.save()
        return rec

    def effective_sleeve(self, symbol: str) -> Sleeve | None:
        rec = self.get(symbol)
        if rec is None:
            return None
        if rec.status in {SleeveAssignmentStatus.CLOSED, SleeveAssignmentStatus.REJECTED}:
            return None
        return rec.sleeve
