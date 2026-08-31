"""Persist candidates, research queue, and discovery runs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentic_portfolio.agent.persist import atomic_write_json
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import LIVE_SOURCE_OF_TRUTH, PAPER_SOURCE_OF_TRUTH, RuntimeMode
from agentic_portfolio.schemas import (
    Candidate,
    CandidateStatus,
    ClassificationStatus,
    DiscoveryPriority,
    DiscoveryRun,
    DiscoverySignal,
    Freshness,
    ResearchQueueEntry,
    ResearchQueueStatus,
    SecurityClass,
    SignalDirection,
    SignalType,
    Sleeve,
    SleeveHypothesis,
    to_dict,
)


def candidates_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "state" / "candidates.json"


def research_queue_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "state" / "research_queue.json"


def discovery_runs_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "state" / "discovery_runs.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CandidateStore:
    def __init__(self, path: Path | None = None, *, runtime_mode: str | None = None) -> None:
        self.path = path or candidates_path()
        self.runtime_mode = str(runtime_mode).upper() if runtime_mode else None
        self._data: dict[str, Any] = {"records": {}}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, default=str), encoding="utf-8")
        return self.path

    def get(self, candidate_id: str) -> Candidate | None:
        raw = self._data.get("records", {}).get(candidate_id)
        return _candidate_from_dict(raw) if raw else None

    def active_for_symbol(self, symbol: str) -> Candidate | None:
        want = symbol.upper()
        live = {CandidateStatus.DISCOVERED, CandidateStatus.SHORTLISTED, CandidateStatus.PROMOTED_TO_RESEARCH}
        found: Candidate | None = None
        for raw in self._data.get("records", {}).values():
            if str(raw.get("symbol", "")).upper() != want:
                continue
            rec = _candidate_from_dict(raw)
            if rec.status in live:
                if found is None or rec.discovered_at > found.discovered_at:
                    found = rec
        return found

    def all(self) -> list[Candidate]:
        return [_candidate_from_dict(r) for r in self._data.get("records", {}).values()]

    def upsert(self, candidate: Candidate) -> Candidate:
        data = to_dict(candidate)
        if self.runtime_mode:
            data["runtime_mode"] = self.runtime_mode
            data["paper_environment"] = self.runtime_mode != RuntimeMode.LIVE.value
            data["source_of_truth"] = (
                LIVE_SOURCE_OF_TRUTH if self.runtime_mode == RuntimeMode.LIVE.value else PAPER_SOURCE_OF_TRUTH
            )
        self._data.setdefault("records", {})[candidate.candidate_id] = data
        self.save()
        return candidate

    def set_status(self, candidate_id: str, status: CandidateStatus, *, reason: str | None = None) -> Candidate:
        rec = self.get(candidate_id)
        if rec is None:
            raise KeyError(candidate_id)
        rec.status = status
        if status == CandidateStatus.REJECTED and reason:
            rec.rejection_reason = reason
        if status == CandidateStatus.EXPIRED:
            rec.freshness = Freshness.EXPIRED
        self.upsert(rec)
        return rec


class ResearchQueue:
    def __init__(self, path: Path | None = None, *, runtime_mode: str | None = None) -> None:
        self.path = path or research_queue_path()
        self.runtime_mode = str(runtime_mode).upper() if runtime_mode else None
        self._data: dict[str, Any] = {"records": {}}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, self._data)
        return self.path

    def get(self, queue_id: str) -> ResearchQueueEntry | None:
        raw = self._data.get("records", {}).get(queue_id)
        return _queue_from_dict(raw) if raw else None

    def all(self) -> list[ResearchQueueEntry]:
        return [_queue_from_dict(r) for r in self._data.get("records", {}).values()]

    def blocking_entry(self, *, symbol: str, candidate_id: str | None = None) -> ResearchQueueEntry | None:
        """Return an existing row that must not be duplicated or re-researched."""
        want = symbol.upper()
        blocked = {
            ResearchQueueStatus.QUEUED,
            ResearchQueueStatus.RESEARCHING,
            ResearchQueueStatus.IN_PROGRESS,
            ResearchQueueStatus.COMPLETED,
            ResearchQueueStatus.NEED_MORE_DATA,
            ResearchQueueStatus.INCONCLUSIVE,
        }
        for entry in self.all():
            same = entry.symbol.upper() == want or (candidate_id and entry.candidate_id == candidate_id)
            if same and entry.status in blocked:
                return entry
        return None

    def enqueue(self, entry: ResearchQueueEntry) -> ResearchQueueEntry:
        from agentic_portfolio.discovery.freshness import normalize_queue_freshness

        if not entry.queue_id:
            entry.queue_id = str(uuid4())
        if not entry.enqueued_at:
            entry.enqueued_at = _now()
        entry = normalize_queue_freshness(entry)
        data = to_dict(entry)
        if self.runtime_mode:
            data["runtime_mode"] = self.runtime_mode
            data["paper_environment"] = self.runtime_mode != RuntimeMode.LIVE.value
        self._data.setdefault("records", {})[entry.queue_id] = data
        self.save()
        return entry

    def save_entry(self, entry: ResearchQueueEntry) -> ResearchQueueEntry:
        if not entry.queue_id:
            raise ValueError("queue entry requires queue_id")
        data = to_dict(entry)
        if self.runtime_mode:
            data["runtime_mode"] = self.runtime_mode
            data["paper_environment"] = self.runtime_mode != RuntimeMode.LIVE.value
        self._data.setdefault("records", {})[entry.queue_id] = data
        self.save()
        return entry

    def set_status(self, queue_id: str, status: ResearchQueueStatus, **fields: Any) -> ResearchQueueEntry:
        rec = self.get(queue_id)
        if rec is None:
            raise KeyError(queue_id)
        rec.status = status
        for key, value in fields.items():
            if hasattr(rec, key):
                setattr(rec, key, value)
        return self.save_entry(rec)


class DiscoveryRunStore:
    def __init__(self, path: Path | None = None, *, runtime_mode: str | None = None) -> None:
        self.path = path or discovery_runs_path()
        self.runtime_mode = str(runtime_mode).upper() if runtime_mode else None
        self._data: dict[str, Any] = {"records": {}}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, default=str), encoding="utf-8")
        return self.path

    def get(self, run_id: str) -> DiscoveryRun | None:
        raw = self._data.get("records", {}).get(run_id)
        return _run_from_dict(raw) if raw else None

    def all(self) -> list[DiscoveryRun]:
        return [_run_from_dict(r) for r in self._data.get("records", {}).values()]

    def save_run(self, run: DiscoveryRun) -> DiscoveryRun:
        data = to_dict(run)
        if self.runtime_mode:
            data["runtime_mode"] = self.runtime_mode
            data["paper_environment"] = self.runtime_mode != RuntimeMode.LIVE.value
        self._data.setdefault("records", {})[run.run_id] = data
        self.save()
        return run


def _candidate_from_dict(raw: dict[str, Any]) -> Candidate:
    signals = []
    for item in raw.get("signals") or []:
        signals.append(
            DiscoverySignal(
                signal_type=SignalType(item["signal_type"]),
                name=item["name"],
                value=item.get("value"),
                direction=SignalDirection(item.get("direction") or "NEUTRAL"),
                strength=float(item.get("strength") or 0),
                observed_at=item.get("observed_at"),
                source=item.get("source"),
                evidence_ref=item.get("evidence_ref"),
            )
        )
    alts = []
    for item in raw.get("alternative_sleeves") or []:
        alts.append(
            SleeveHypothesis(
                sleeve=Sleeve(item["sleeve"]),
                reason=item.get("reason") or "",
                confidence=item.get("confidence") or "LOW",
            )
        )
    sc = raw.get("security_class")
    cs = raw.get("classification_status")
    return Candidate(
        candidate_id=raw["candidate_id"],
        symbol=raw["symbol"],
        discovered_at=raw["discovered_at"],
        discovery_source=raw.get("discovery_source") or "",
        provisional_sleeve=Sleeve(raw["provisional_sleeve"]),
        security_class=SecurityClass(sc) if sc else None,
        classification_status=ClassificationStatus(cs) if cs else None,
        current_price=raw.get("current_price"),
        market_cap=raw.get("market_cap"),
        sector=raw.get("sector"),
        liquidity_status=raw.get("liquidity_status") or "UNKNOWN",
        discovery_score=float(raw.get("discovery_score") or 0),
        priority=DiscoveryPriority(raw.get("priority") or "LOW"),
        reasons=list(raw.get("reasons") or []),
        signals=signals,
        supporting_evidence_refs=list(raw.get("supporting_evidence_refs") or []),
        known_risks=list(raw.get("known_risks") or []),
        event_flags=list(raw.get("event_flags") or []),
        freshness=Freshness(raw.get("freshness") or "FRESH"),
        status=CandidateStatus(raw.get("status") or "DISCOVERED"),
        sleeve_reason=raw.get("sleeve_reason"),
        sleeve_confidence=raw.get("sleeve_confidence"),
        primary_provisional_sleeve=Sleeve(raw["primary_provisional_sleeve"]) if raw.get("primary_provisional_sleeve") else None,
        alternative_sleeves=alts,
        discovery_sources=list(raw.get("discovery_sources") or []),
        channels=list(raw.get("channels") or []),
        research_questions=list(raw.get("research_questions") or []),
        initial_observations=list(raw.get("initial_observations") or []),
        rejection_reason=raw.get("rejection_reason"),
        rejection_evidence=list(raw.get("rejection_evidence") or []),
        action_blocked_reason=raw.get("action_blocked_reason"),
        industry=raw.get("industry"),
        thesis_type=raw.get("thesis_type"),
        expires_at=raw.get("expires_at"),
        score_breakdown=dict(raw.get("score_breakdown") or {}),
        overlap_penalty=float(raw.get("overlap_penalty") or 0),
        required_research_areas=list(raw.get("required_research_areas") or []),
        comparison_group_id=raw.get("comparison_group_id"),
        overlap_warnings=list(raw.get("overlap_warnings") or []),
        deferred_due_to_overlap=bool(raw.get("deferred_due_to_overlap") or False),
    )


def _queue_from_dict(raw: dict[str, Any]) -> ResearchQueueEntry:
    status_raw = str(raw.get("status") or "QUEUED")
    try:
        status = ResearchQueueStatus(status_raw)
    except ValueError:
        status = ResearchQueueStatus.QUEUED
    return ResearchQueueEntry(
        queue_id=raw["queue_id"],
        candidate_id=raw["candidate_id"],
        symbol=raw["symbol"],
        provisional_sleeve=Sleeve(raw["provisional_sleeve"]),
        discovery_score=float(raw.get("discovery_score") or 0),
        priority=DiscoveryPriority(raw.get("priority") or "LOW"),
        why_research_warranted=raw.get("why_research_warranted") or "",
        required_research_areas=list(raw.get("required_research_areas") or []),
        freshness_deadline=raw.get("freshness_deadline"),
        status=status,
        enqueued_at=raw.get("enqueued_at"),
        notes=raw.get("notes"),
        comparison_group_id=raw.get("comparison_group_id"),
        overlap_warnings=list(raw.get("overlap_warnings") or []),
        deferred_due_to_research_queue_overlap=bool(
            raw.get("deferred_due_to_research_queue_overlap") or False
        ),
        research_id=raw.get("research_id"),
        attempt_count=int(raw.get("attempt_count") or 0),
        last_error=raw.get("last_error"),
        last_attempt_at=raw.get("last_attempt_at"),
        claimed_at=raw.get("claimed_at"),
        skipped_reason=raw.get("skipped_reason"),
    )


def _run_from_dict(raw: dict[str, Any]) -> DiscoveryRun:
    return DiscoveryRun(
        run_id=raw["run_id"],
        started_at=raw["started_at"],
        completed_at=raw.get("completed_at"),
        market_session_context=dict(raw.get("market_session_context") or {}),
        risk_state=raw.get("risk_state"),
        sources_queried=list(raw.get("sources_queried") or []),
        symbols_evaluated=list(raw.get("symbols_evaluated") or []),
        candidates_created=list(raw.get("candidates_created") or []),
        candidates_rejected=list(raw.get("candidates_rejected") or []),
        candidates_promoted=list(raw.get("candidates_promoted") or []),
        errors=list(raw.get("errors") or []),
        data_freshness=raw.get("data_freshness"),
        conclusion=raw.get("conclusion"),
        regime_status=raw.get("regime_status"),
        theses_created=int(raw.get("theses_created") or 0),
        buy_actions_created=int(raw.get("buy_actions_created") or 0),
        execution_attempted=bool(raw.get("execution_attempted") or False),
    )
