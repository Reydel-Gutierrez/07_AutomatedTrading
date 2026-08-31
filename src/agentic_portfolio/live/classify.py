"""Classify legacy/scripted/PAPER artifacts so they cannot drive LIVE decisions.

Does not delete production state. Tags records for audit/history.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentic_portfolio.agent.persist import atomic_write_json
from agentic_portfolio.discovery.store import CandidateStore, ResearchQueue
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.runtime import RuntimeMode, discovery_state_dir
from agentic_portfolio.schemas import CandidateStatus, ResearchQueueStatus


def classify_non_production_artifacts(root: Path, *, runtime_mode: RuntimeMode | str = RuntimeMode.LIVE) -> dict[str, Any]:
    mode = runtime_mode if isinstance(runtime_mode, RuntimeMode) else RuntimeMode(str(runtime_mode).upper())
    base = Path(root)
    stamp = datetime.now(timezone.utc).isoformat()
    live_dir = discovery_state_dir(base, mode=mode)
    paper_queue = ResearchQueue(base / "state" / "research_queue.json")
    live_queue = ResearchQueue(live_dir / "research_queue.json", runtime_mode=mode.value)
    live_candidates = CandidateStore(live_dir / "candidates.json", runtime_mode=mode.value)
    reports = ResearchStore(base)
    rows: list[dict[str, Any]] = []

    for entry in paper_queue.all():
        rows.append(
            {
                "kind": "queue",
                "id": entry.queue_id,
                "symbol": entry.symbol,
                "classification": "legacy_paper_queue",
                "production": False,
                "path": str(paper_queue.path),
            }
        )
    seen_fp: dict[str, str] = {}
    for entry in live_queue.all():
        key = f"{entry.symbol}:{(entry.evidence_fingerprint or '')}:{entry.status.value}"
        if key in seen_fp and entry.status in {ResearchQueueStatus.QUEUED, ResearchQueueStatus.RESEARCHING, ResearchQueueStatus.IN_PROGRESS}:
            live_queue.set_status(entry.queue_id, ResearchQueueStatus.DROPPED, skipped_reason="duplicate_active_queue")
            rows.append({"kind": "queue", "id": entry.queue_id, "symbol": entry.symbol, "classification": "duplicate_active_dropped", "production": False})
            continue
        seen_fp[key] = entry.queue_id
        if entry.status is ResearchQueueStatus.RESEARCHING and not entry.claimed_at:
            live_queue.set_status(entry.queue_id, ResearchQueueStatus.QUEUED, skipped_reason="stale_researching_unclaimed")
            rows.append({"kind": "queue", "id": entry.queue_id, "symbol": entry.symbol, "classification": "reclaimed_stuck_researching", "production": True})
    for cand in live_candidates.all():
        if cand.status is CandidateStatus.PROMOTED_TO_RESEARCH:
            active = live_queue.active_entry(symbol=cand.symbol, candidate_id=cand.candidate_id)
            latest = live_queue.latest_for_symbol(cand.symbol, candidate_id=cand.candidate_id)
            if active is None and latest is not None and latest.status is ResearchQueueStatus.COMPLETED:
                live_candidates.set_status(cand.candidate_id, CandidateStatus.WATCHING, reason="stale_promoted_after_complete")
                rows.append({"kind": "candidate", "id": cand.candidate_id, "symbol": cand.symbol, "classification": "stale_promoted_reclassified_watching", "production": True})
    for report in reports.all_reports():
        scripted = str(report.research_source or "").lower() == "scripted"
        paper = str(getattr(report, "runtime_mode", None) or "").upper() == RuntimeMode.PAPER.value
        if scripted or paper:
            rows.append(
                {
                    "kind": "report",
                    "id": report.research_id,
                    "symbol": report.symbol,
                    "classification": "scripted" if scripted else "paper_research",
                    "production": False,
                }
            )
    payload = {"classified_at": stamp, "runtime_mode": mode.value, "rows": rows, "count": len(rows)}
    path = live_dir / "artifact_classification.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)
    return payload
