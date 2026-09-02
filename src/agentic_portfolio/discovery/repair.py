"""Restore LIVE candidate status when it disagrees with the live queue and artifacts.

Does not migrate legacy state/research_queue.json into LIVE. History is never deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentic_portfolio.discovery.store import ACTIVE_QUEUE_STATUSES, CandidateStore, ResearchQueue
from agentic_portfolio.lifecycle import log_lifecycle
from agentic_portfolio.research.operational import last_valid_investment_report, looks_like_operational_failure_report
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.types import ResearchConclusion, ResearchReport, ResearchStatus
from agentic_portfolio.runtime import RuntimeMode, discovery_state_dir
from agentic_portfolio.schemas import Candidate, CandidateStatus, ResearchQueueEntry, ResearchQueueStatus
from agentic_portfolio.watch.store import WatchStore
from agentic_portfolio.watch.types import TERMINAL_WATCH, WatchItem, WatchStatus


REPAIR_REASON = "promoted_without_active_queue"
LEGACY_QUEUE_NAME = "research_queue.json"


@dataclass
class CandidateLifecycleRepairResult:
    inspected: int = 0
    repaired: int = 0
    unchanged: int = 0
    skipped_not_live: bool = False
    legacy_queue_consulted: bool = False
    symbols: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "inspected": self.inspected,
            "repaired": self.repaired,
            "unchanged": self.unchanged,
            "skipped_not_live": self.skipped_not_live,
            "legacy_queue_consulted": self.legacy_queue_consulted,
            "symbols": list(self.symbols),
            "details": list(self.details),
        }


def repair_promoted_candidate_consistency(
    *,
    root: Path,
    candidates: CandidateStore,
    queue: ResearchQueue,
    runtime_mode: RuntimeMode | str,
    research_store: ResearchStore | None = None,
    watch_store: WatchStore | None = None,
    persist: bool = True,
    journal: Path | None = None,
) -> CandidateLifecycleRepairResult:
    """LIVE only. Restore PROMOTED_TO_RESEARCH rows that have no active live queue work."""
    result = CandidateLifecycleRepairResult()
    mode = runtime_mode if isinstance(runtime_mode, RuntimeMode) else RuntimeMode(str(runtime_mode).upper())
    if mode is not RuntimeMode.LIVE:
        result.skipped_not_live = True
        return result
    live_dir = discovery_state_dir(Path(root), mode=RuntimeMode.LIVE)
    live_queue_path = (live_dir / "research_queue.json").resolve()
    if queue.path.resolve() != live_queue_path:
        result.skipped_not_live = True
        return result
    reports = research_store or ResearchStore(Path(root))
    watches = watch_store
    for cand in candidates.all():
        if cand.status is not CandidateStatus.PROMOTED_TO_RESEARCH:
            continue
        result.inspected += 1
        active = queue.active_entry(symbol=cand.symbol, candidate_id=cand.candidate_id)
        if active is not None and active.candidate_id == cand.candidate_id and active.status in ACTIVE_QUEUE_STATUSES:
            result.unchanged += 1
            continue
        latest = queue.latest_for_candidate_id(cand.candidate_id)
        restored, why = resolve_promoted_without_active_queue(
            cand,
            latest=latest,
            research_store=reports,
            watch_store=watches,
        )
        if restored is CandidateStatus.PROMOTED_TO_RESEARCH:
            result.unchanged += 1
            continue
        if persist:
            candidates.set_status(cand.candidate_id, restored, reason=why)
            log_lifecycle(
                symbol=cand.symbol,
                source="candidate_lifecycle_repair",
                reason=why,
                from_status=CandidateStatus.PROMOTED_TO_RESEARCH.value,
                to_status=restored.value,
                extra={"candidate_id": cand.candidate_id, "queue_id": latest.queue_id if latest else None},
                root=Path(root),
            )
        result.repaired += 1
        result.symbols.append(cand.symbol)
        result.details.append(
            {
                "symbol": cand.symbol,
                "candidate_id": cand.candidate_id,
                "from_status": CandidateStatus.PROMOTED_TO_RESEARCH.value,
                "to_status": restored.value,
                "reason": why,
                "queue_status": latest.status.value if latest else None,
            }
        )
    return result


def resolve_promoted_without_active_queue(
    candidate: Candidate,
    *,
    latest: ResearchQueueEntry | None,
    research_store: ResearchStore | None,
    watch_store: WatchStore | None,
) -> tuple[CandidateStatus, str]:
    """Map a stuck PROMOTED row onto canonical live artifacts. Never reads legacy queue files."""
    watch = watch_store.by_ticker(candidate.symbol) if watch_store is not None else None
    reports = _reports_for(research_store, candidate)
    if latest is not None and latest.status is ResearchQueueStatus.REJECTED:
        reason = latest.skipped_reason or "queue_rejected"
        return CandidateStatus.REJECTED, reason
    if latest is not None and latest.status in {ResearchQueueStatus.NEED_MORE_DATA, ResearchQueueStatus.INCONCLUSIVE}:
        return CandidateStatus.RESEARCH_INCONCLUSIVE, "queue_need_more_data"
    if latest is not None and latest.status is ResearchQueueStatus.EXPIRED:
        return CandidateStatus.EXPIRED, "queue_expired"
    if latest is not None and latest.status is ResearchQueueStatus.COMPLETED:
        return _status_from_completed_artifacts(reports, watch)
    if latest is not None and latest.status is ResearchQueueStatus.DROPPED:
        from_art, why = _status_from_completed_artifacts(reports, watch)
        if why != "completed_no_downstream_artifacts":
            return from_art, why
        return CandidateStatus.SHORTLISTED, "queue_dropped"
    from_art, why = _status_from_completed_artifacts(reports, watch)
    if why != "completed_no_downstream_artifacts":
        return from_art, why
    return CandidateStatus.SHORTLISTED, "promoted_without_queue"


def _reports_for(research_store: ResearchStore | None, candidate: Candidate) -> list[ResearchReport]:
    if research_store is None:
        return []
    rows = list(research_store.by_candidate(candidate.candidate_id) or [])
    if not rows:
        rows = list(research_store.by_symbol(candidate.symbol) or [])
    return [r for r in rows if not looks_like_operational_failure_report(r)]


def _status_from_completed_artifacts(
    reports: list[ResearchReport],
    watch: WatchItem | None,
) -> tuple[CandidateStatus, str]:
    valid = last_valid_investment_report(reports)
    latest = sorted(reports, key=lambda r: r.started_at or "", reverse=True)[0] if reports else None
    active_watch = watch is not None and watch.status not in TERMINAL_WATCH
    if valid is not None and valid.research_conclusion is ResearchConclusion.REJECT:
        return CandidateStatus.REJECTED, "research_rejected"
    if latest is not None and (
        latest.research_conclusion is ResearchConclusion.NEED_MORE_DATA
        or latest.research_status is ResearchStatus.RESEARCH_INCONCLUSIVE
    ):
        if valid is None:
            return CandidateStatus.RESEARCH_INCONCLUSIVE, "research_inconclusive"
    if valid is not None and valid.research_conclusion is ResearchConclusion.KEEP_WATCHING:
        return CandidateStatus.WATCHING, "research_keep_watching"
    if valid is not None and valid.research_conclusion is ResearchConclusion.ADVANCE_TO_THESIS:
        if active_watch and watch is not None and watch.status in {
            WatchStatus.APPROVAL_REQUIRED,
            WatchStatus.READY_FOR_RISK_GATE,
        }:
            return CandidateStatus.RESEARCH_COMPLETE, "research_advanced_approval"
        if active_watch:
            return CandidateStatus.WATCHING, "research_advanced_watch"
        return CandidateStatus.RESEARCH_COMPLETE, "research_advanced"
    if active_watch:
        return CandidateStatus.WATCHING, "active_watch"
    if valid is not None or latest is not None:
        return CandidateStatus.RESEARCH_COMPLETE, "research_complete"
    return CandidateStatus.RESEARCH_COMPLETE, "completed_no_downstream_artifacts"
