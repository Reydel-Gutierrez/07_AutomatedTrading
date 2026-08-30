"""Persisted investment-thesis registry.

This is a record of decisions and reviews, not a reasoning engine.
Price movement may be stored as evidence; it does not by itself change thesis status.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentic_portfolio.paths import project_root
from agentic_portfolio.schemas import (
    Decision,
    ExitPolicy,
    Sleeve,
    ThesisRecord,
    ThesisReview,
    ThesisStatus,
    to_dict,
)


def thesis_registry_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "state" / "thesis_registry.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ThesisRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or thesis_registry_path()
        self._data: dict[str, Any] = {"records": {}}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, default=str), encoding="utf-8")
        return self.path

    def get(self, thesis_id: str) -> ThesisRecord | None:
        raw = self._data.get("records", {}).get(thesis_id)
        if not raw:
            return None
        return _record_from_dict(raw)

    def active_for_symbol(self, symbol: str) -> ThesisRecord | None:
        want = symbol.upper()
        active_states = {
            ThesisStatus.ACTIVE,
            ThesisStatus.STRENGTHENED,
            ThesisStatus.UNCHANGED,
            ThesisStatus.WEAKENED,
        }
        found: ThesisRecord | None = None
        for raw in self._data.get("records", {}).values():
            if str(raw.get("symbol", "")).upper() != want:
                continue
            rec = _record_from_dict(raw)
            if rec.status in active_states:
                if found is None or rec.updated_at > found.updated_at:
                    found = rec
        return found

    def current_for_symbol(self, symbol: str) -> ThesisRecord | None:
        """Prefer live/active-like, then DRAFT, then the latest record."""
        live = self.active_for_symbol(symbol)
        if live is not None:
            return live
        recs = self.all_for_symbol(symbol)
        if not recs:
            return None
        drafts = [r for r in recs if r.status == ThesisStatus.DRAFT]
        if drafts:
            return max(drafts, key=lambda r: r.updated_at)
        return max(recs, key=lambda r: r.updated_at)

    def all_for_symbol(self, symbol: str) -> list[ThesisRecord]:
        want = symbol.upper()
        out = []
        for raw in self._data.get("records", {}).values():
            if str(raw.get("symbol", "")).upper() == want:
                out.append(_record_from_dict(raw))
        return out

    def all_records(self) -> list[ThesisRecord]:
        return [_record_from_dict(raw) for raw in self._data.get("records", {}).values()]

    def create(
        self,
        *,
        symbol: str,
        sleeve: Sleeve,
        status: ThesisStatus = ThesisStatus.DRAFT,
        decision: Decision | None = None,
        expected_horizon: str | None = None,
        thesis_summary: str | None = None,
        bull_case: str | None = None,
        base_case: str | None = None,
        bear_case: str | None = None,
        catalysts: list[str] | None = None,
        risks: list[str] | None = None,
        invalidation_conditions: list[str] | None = None,
        review_triggers: list[str] | None = None,
        exit_policy: ExitPolicy | dict[str, Any] | None = None,
        why_position_should_exist: str | None = None,
        research_id: str | None = None,
        desired_allocation_pct: float | None = None,
        confidence: str | None = None,
        supporting_evidence_refs: list[str] | None = None,
        thesis_id: str | None = None,
        created_at: str | None = None,
    ) -> ThesisRecord:
        ts = created_at or _now()
        rec = ThesisRecord(
            thesis_id=thesis_id or str(uuid4()),
            symbol=symbol.upper(),
            sleeve=sleeve,
            created_at=ts,
            updated_at=ts,
            status=status,
            decision=decision,
            expected_horizon=expected_horizon,
            thesis_summary=thesis_summary,
            bull_case=bull_case,
            base_case=base_case,
            bear_case=bear_case,
            catalysts=list(catalysts or []),
            risks=list(risks or []),
            invalidation_conditions=list(invalidation_conditions or []),
            review_triggers=list(review_triggers or []),
            exit_policy=_exit_policy_from_raw(exit_policy),
            why_position_should_exist=why_position_should_exist,
            research_id=research_id,
            desired_allocation_pct=desired_allocation_pct,
            confidence=confidence,
            supporting_evidence_refs=list(supporting_evidence_refs or []),
            review_history=[],
        )
        self._data.setdefault("records", {})[rec.thesis_id] = to_dict(rec)
        self.save()
        return rec

    def set_status(self, thesis_id: str, status: ThesisStatus, *, reason: str | None = None) -> ThesisRecord:
        rec = self.get(thesis_id)
        if rec is None:
            raise KeyError(thesis_id)
        rec.status = status
        rec.updated_at = _now()
        self._data["records"][thesis_id] = to_dict(rec)
        self.save()
        return rec

    def add_review(
        self,
        thesis_id: str,
        *,
        review_type: str,
        notes: str | None = None,
        session_id: str | None = None,
        decision_id: str | None = None,
        reviewed_at: str | None = None,
    ) -> ThesisRecord:
        rec = self.get(thesis_id)
        if rec is None:
            raise KeyError(thesis_id)
        ts = reviewed_at or _now()
        review = ThesisReview(
            review_id=str(uuid4()),
            review_type=review_type,
            reviewed_at=ts,
            session_id=session_id,
            notes=notes,
            decision_id=decision_id,
        )
        rec.review_history.append(review)
        rec.updated_at = ts
        self._data["records"][thesis_id] = to_dict(rec)
        self.save()
        return rec

    def record_price_observation(self, thesis_id: str, price: float, *, observed_at: str | None = None) -> ThesisRecord:
        """Store a price print as evidence. Does not change thesis status."""
        rec = self.get(thesis_id)
        if rec is None:
            raise KeyError(thesis_id)
        rec.last_price_observed = price
        rec.last_price_observed_at = observed_at or _now()
        rec.updated_at = rec.last_price_observed_at
        self._data["records"][thesis_id] = to_dict(rec)
        self.save()
        return rec

    def has_fresh_review(
        self,
        thesis_id: str,
        review_type: str,
        *,
        session_id: str | None = None,
        max_age_hours: float | None = 24.0,
        now: datetime | None = None,
    ) -> bool:
        rec = self.get(thesis_id)
        if rec is None:
            return False
        matching = [r for r in rec.review_history if r.review_type == review_type]
        if not matching:
            return False
        latest = max(matching, key=lambda r: r.reviewed_at)
        if session_id and latest.session_id == session_id:
            return True
        if max_age_hours is None:
            return True
        now = now or datetime.now(timezone.utc)
        try:
            ts = datetime.fromisoformat(latest.reviewed_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() <= max_age_hours * 3600


def _record_from_dict(raw: dict[str, Any]) -> ThesisRecord:
    reviews = []
    for item in raw.get("review_history") or []:
        reviews.append(
            ThesisReview(
                review_id=item["review_id"],
                review_type=item["review_type"],
                reviewed_at=item["reviewed_at"],
                session_id=item.get("session_id"),
                notes=item.get("notes"),
                decision_id=item.get("decision_id"),
            )
        )
    dec = raw.get("decision")
    return ThesisRecord(
        thesis_id=raw["thesis_id"],
        symbol=raw["symbol"],
        sleeve=Sleeve(raw["sleeve"]),
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        status=ThesisStatus(raw["status"]),
        decision=Decision(dec) if dec else None,
        expected_horizon=raw.get("expected_horizon"),
        thesis_summary=raw.get("thesis_summary"),
        bull_case=raw.get("bull_case"),
        base_case=raw.get("base_case"),
        bear_case=raw.get("bear_case"),
        catalysts=list(raw.get("catalysts") or []),
        risks=list(raw.get("risks") or []),
        invalidation_conditions=list(raw.get("invalidation_conditions") or []),
        review_triggers=list(raw.get("review_triggers") or []),
        exit_policy=_exit_policy_from_raw(raw.get("exit_policy")),
        why_position_should_exist=raw.get("why_position_should_exist"),
        research_id=raw.get("research_id"),
        desired_allocation_pct=raw.get("desired_allocation_pct"),
        confidence=raw.get("confidence"),
        supporting_evidence_refs=list(raw.get("supporting_evidence_refs") or []),
        review_history=reviews,
        last_price_observed=raw.get("last_price_observed"),
        last_price_observed_at=raw.get("last_price_observed_at"),
    )


def _exit_policy_from_raw(raw: Any) -> ExitPolicy | None:
    if raw is None:
        return None
    if isinstance(raw, ExitPolicy):
        return raw
    if not isinstance(raw, dict):
        return None
    return ExitPolicy(
        thesis_based=bool(raw.get("thesis_based", True)),
        mandatory_fixed_stop_loss=bool(raw.get("mandatory_fixed_stop_loss", False)),
        price_invalidation=raw.get("price_invalidation"),
        event_invalidation=raw.get("event_invalidation"),
        technical_invalidation=raw.get("technical_invalidation"),
        risk_invalidation=raw.get("risk_invalidation"),
        broker_stop_orders_created=False,
        notes=raw.get("notes"),
    )
