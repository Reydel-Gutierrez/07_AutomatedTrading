"""Invalidate collector-bug NEED_MORE_DATA reports and re-queue for a fresh run.

Does not force BUY, does not write ProposedAction, and does not bypass Risk Gate.
History files are never overwritten. The next RESEARCH_QUEUE_WORKER cycle may
collect with the repaired adapter and emit a new report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentic_portfolio.discovery.freshness import freshness_deadline_at
from agentic_portfolio.discovery.store import CandidateStore, ResearchQueue
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.research.operational import (
    looks_like_schema_failure_report,
    last_valid_investment_report,
    report_is_still_fresh,
)
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.sufficiency import (
    BROAD_MARKET_REPAIR_SYMBOLS,
    looks_like_pre_fix_need_more_data,
)
from agentic_portfolio.research.types import ResearchConclusion, ResearchReport, ResearchStatus
from agentic_portfolio.runtime import RuntimeMode, discovery_state_dir
from agentic_portfolio.schemas import (
    Candidate,
    CandidateStatus,
    DiscoveryPriority,
    ResearchQueueEntry,
    ResearchQueueStatus,
    Sleeve,
)


REPAIR_REASON = "collector_repair_requeue"
LEDGER_NAME = "collector_repair.json"


@dataclass
class RepairResult:
    inspected: int = 0
    invalidated: int = 0
    requeued: int = 0
    skipped_already_repaired: int = 0
    symbols: list[str] = field(default_factory=list)
    research_ids: list[str] = field(default_factory=list)
    buy_actions_created: int = 0
    proposed_actions_created: int = 0
    watches_restored: int = 0
    schema_failures: int = 0
    queue_duplicates_dropped: int = 0
    candidate_status_repaired: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "inspected": self.inspected,
            "invalidated": self.invalidated,
            "requeued": self.requeued,
            "skipped_already_repaired": self.skipped_already_repaired,
            "symbols": list(self.symbols),
            "research_ids": list(self.research_ids),
            "buy_actions_created": self.buy_actions_created,
            "proposed_actions_created": self.proposed_actions_created,
            "watches_restored": self.watches_restored,
            "schema_failures": self.schema_failures,
            "queue_duplicates_dropped": self.queue_duplicates_dropped,
            "candidate_status_repaired": self.candidate_status_repaired,
            "forced_buy": False,
            "risk_gate_bypassed": False,
        }


def repair_ledger_path(root: Path, *, runtime_mode: RuntimeMode | str) -> Path:
    mode = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode).upper()
    return discovery_state_dir(Path(root), mode=RuntimeMode(mode)) / LEDGER_NAME


def load_repair_ledger(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            data.setdefault("requeued_research_ids", [])
            return data
    return {"requeued_research_ids": []}


def save_repair_ledger(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def invalidate_and_requeue_collector_bug_reports(
    *,
    root: Path,
    research_store: ResearchStore,
    candidates: CandidateStore,
    queue: ResearchQueue,
    runtime_mode: RuntimeMode | str,
    now: datetime | None = None,
    symbols: frozenset[str] | None = None,
    journal: Path | None = None,
    persist: bool = True,
) -> RepairResult:
    """Re-queue pre-fix NEED_MORE_DATA ETFs (default SPY/VTI/VOO) for a fresh collect.

    A repaired collector + ETF completeness path can then produce ADVANCE_TO_THESIS,
    KEEP_WATCHING, or REJECT. Portfolio Decision and Risk Gate still decide permission.
    """
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    want = {s.upper() for s in (symbols or BROAD_MARKET_REPAIR_SYMBOLS)}
    ledger_path = repair_ledger_path(root, runtime_mode=runtime_mode)
    ledger = load_repair_ledger(ledger_path)
    already = {str(rid) for rid in ledger.get("requeued_research_ids") or []}
    result = RepairResult()

    reports = [r for r in research_store.all_reports() if str(r.symbol or "").upper() in want]
    latest_by_symbol: dict[str, ResearchReport] = {}
    for report in reports:
        key = report.symbol.upper()
        prior = latest_by_symbol.get(key)
        if prior is None or (report.started_at or "") >= (prior.started_at or ""):
            latest_by_symbol[key] = report

    for symbol, report in sorted(latest_by_symbol.items()):
        result.inspected += 1
        if not looks_like_pre_fix_need_more_data(report):
            continue
        if report.research_id in already:
            result.skipped_already_repaired += 1
            continue
        conclusion = report.research_conclusion
        if conclusion not in {ResearchConclusion.NEED_MORE_DATA, None} and report.research_status not in {
            ResearchStatus.RESEARCH_INCONCLUSIVE,
            ResearchStatus.RESEARCH_STALE,
        }:
            continue
        candidate = candidates.active_for_symbol(symbol) or candidates.current_for_symbol(symbol)
        if candidate is None:
            continue
        entry = _requeue_candidate(candidate, queue, now=stamp)
        if entry is None:
            continue
        candidates.set_status(candidate.candidate_id, CandidateStatus.PROMOTED_TO_RESEARCH, reason=REPAIR_REASON)
        already.add(report.research_id)
        result.invalidated += 1
        result.requeued += 1
        result.symbols.append(symbol)
        result.research_ids.append(report.research_id)
        if persist:
            append_jsonl(
                {
                    "type": "COLLECTOR_REPAIR_REQUEUE",
                    "symbol": symbol,
                    "research_id": report.research_id,
                    "queue_id": entry.queue_id,
                    "research_generation": entry.research_generation,
                    "reason": REPAIR_REASON,
                    "buy_actions_created": 0,
                    "proposed_actions_created": 0,
                },
                journal or (Path(root) / "logs" / "research.jsonl"),
            )

    if persist:
        ledger["requeued_research_ids"] = sorted(already)
        ledger["updated_at"] = stamp.isoformat()
        ledger["symbols"] = sorted(set(result.symbols) | set(ledger.get("symbols") or []))
        save_repair_ledger(ledger_path, ledger)
    return result


def _requeue_candidate(candidate: Candidate, queue: ResearchQueue, *, now: datetime) -> ResearchQueueEntry | None:
    active = queue.active_entry(symbol=candidate.symbol, candidate_id=candidate.candidate_id)
    if active is not None:
        return active
    prior = queue.latest_for_symbol(candidate.symbol, candidate_id=candidate.candidate_id)
    generation = int(prior.research_generation or 1) + 1 if prior is not None else 1
    if prior is not None and prior.status in {
        ResearchQueueStatus.NEED_MORE_DATA,
        ResearchQueueStatus.INCONCLUSIVE,
        ResearchQueueStatus.EXPIRED,
        ResearchQueueStatus.DROPPED,
        ResearchQueueStatus.COMPLETED,
    }:
        prior.status = ResearchQueueStatus.QUEUED
        prior.claimed_at = None
        prior.last_error = None
        prior.skipped_reason = REPAIR_REASON
        prior.evidence_fingerprint = None
        prior.research_id = None
        prior.research_generation = generation
        prior.last_attempt_at = None
        prior.enqueued_at = now.isoformat()
        prior.freshness_deadline = freshness_deadline_at(candidate.provisional_sleeve or Sleeve.CORE_GROWTH, now)
        prior.notes = (prior.notes or "") + f" | {REPAIR_REASON}"
        return queue.save_entry(prior)
    entry = ResearchQueueEntry(
        queue_id=str(uuid4()),
        candidate_id=candidate.candidate_id,
        symbol=candidate.symbol,
        provisional_sleeve=candidate.provisional_sleeve or Sleeve.CORE_GROWTH,
        discovery_score=float(candidate.discovery_score or 0),
        priority=candidate.priority or DiscoveryPriority.HIGH,
        why_research_warranted=REPAIR_REASON,
        required_research_areas=list(candidate.required_research_areas or ["mandate", "liquidity", "tracking"]),
        freshness_deadline=freshness_deadline_at(candidate.provisional_sleeve or Sleeve.CORE_GROWTH, now),
        status=ResearchQueueStatus.QUEUED,
        enqueued_at=now.isoformat(),
        notes="Collector-repair requeue. Not a buy. Risk Gate still applies.",
        research_generation=generation,
        skipped_reason=REPAIR_REASON,
    )
    return queue.enqueue(entry)


SCHEMA_REPAIR_REASON = "schema_failure_requeue"


def repair_operational_research_state(
    *,
    root: Path,
    research_store: ResearchStore,
    candidates: CandidateStore,
    queue: ResearchQueue,
    runtime_mode: RuntimeMode | str,
    now: datetime | None = None,
    journal: Path | None = None,
    persist: bool = True,
    watch=None,
) -> RepairResult:
    """Keep poisoned reports on disk. Stop treating them as canonical. Restore last valid state."""
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    collector = invalidate_and_requeue_collector_bug_reports(
        root=root,
        research_store=research_store,
        candidates=candidates,
        queue=queue,
        runtime_mode=runtime_mode,
        now=stamp,
        journal=journal,
        persist=persist,
    )
    result = RepairResult()
    result.inspected = collector.inspected
    result.invalidated = collector.invalidated
    result.requeued = collector.requeued
    result.skipped_already_repaired = collector.skipped_already_repaired
    result.symbols = list(collector.symbols)
    result.research_ids = list(collector.research_ids)

    dropped_dupes = queue.collapse_duplicate_active_entries()
    result.queue_duplicates_dropped = len(dropped_dupes)

    ledger_path = repair_ledger_path(root, runtime_mode=runtime_mode)
    ledger = load_repair_ledger(ledger_path)
    already = {str(rid) for rid in ledger.get("requeued_research_ids") or []}
    schema_already = {str(rid) for rid in ledger.get("schema_failure_research_ids") or []}

    by_symbol: dict[str, list] = {}
    for report in research_store.all_reports():
        by_symbol.setdefault(str(report.symbol or "").upper(), []).append(report)

    for symbol, reports in sorted(by_symbol.items()):
        latest = sorted(reports, key=lambda r: r.started_at or "", reverse=True)[0]
        result.inspected += 1
        poison = looks_like_schema_failure_report(latest)
        valid = last_valid_investment_report(reports)
        if poison:
            result.schema_failures += 1
            if watch is not None and valid is not None:
                restored = _restore_watch_from_report(watch, valid, candidates=candidates, now=stamp)
                if restored:
                    result.watches_restored += 1
            if latest.research_id in schema_already:
                result.skipped_already_repaired += 1
                continue
            need_research = valid is None or not report_is_still_fresh(valid, now=stamp)
            if need_research:
                candidate = candidates.active_for_symbol(symbol) or candidates.current_for_symbol(symbol)
                if candidate is not None:
                    entry = _requeue_candidate(candidate, queue, now=stamp)
                    if entry is not None:
                        candidates.set_status(candidate.candidate_id, CandidateStatus.PROMOTED_TO_RESEARCH, reason=SCHEMA_REPAIR_REASON)
                        result.requeued += 1
                        if persist:
                            append_jsonl(
                                {
                                    "type": "SCHEMA_FAILURE_REPAIR_REQUEUE",
                                    "symbol": symbol,
                                    "research_id": latest.research_id,
                                    "valid_research_id": valid.research_id if valid else None,
                                    "queue_id": entry.queue_id,
                                    "reason": SCHEMA_REPAIR_REASON,
                                    "investment_conclusion": None,
                                },
                                journal or (Path(root) / "logs" / "research.jsonl"),
                            )
            already.add(latest.research_id)
            schema_already.add(latest.research_id)
            result.invalidated += 1
            result.symbols.append(symbol)
            result.research_ids.append(latest.research_id)
            continue
        if watch is None or valid is None:
            continue
        if not report_is_still_fresh(valid, now=stamp):
            continue
        existing = watch.store.by_ticker(symbol)
        if existing is not None and existing.status.value not in {"EXPIRED", "INVALIDATED"}:
            continue
        restored = _restore_watch_from_report(watch, valid, candidates=candidates, now=stamp)
        if restored:
            result.watches_restored += 1
            result.symbols.append(symbol)
            if persist:
                append_jsonl(
                    {
                        "type": "WATCH_RESTORED_FROM_VALID_RESEARCH",
                        "symbol": symbol,
                        "research_id": valid.research_id,
                        "reason": "missing_watch_valid_research",
                        "investment_conclusion": valid.research_conclusion.value if valid.research_conclusion else None,
                    },
                    journal or (Path(root) / "logs" / "research.jsonl"),
                )

    from agentic_portfolio.discovery.repair import repair_promoted_candidate_consistency

    watch_store = getattr(watch, "store", None)
    lifecycle = repair_promoted_candidate_consistency(
        root=root,
        candidates=candidates,
        queue=queue,
        runtime_mode=runtime_mode,
        research_store=research_store,
        watch_store=watch_store,
        persist=persist,
    )
    result.candidate_status_repaired = lifecycle.repaired
    result.inspected += lifecycle.inspected
    for symbol in lifecycle.symbols:
        if symbol not in result.symbols:
            result.symbols.append(symbol)

    if persist:
        ledger["requeued_research_ids"] = sorted(already)
        ledger["schema_failure_research_ids"] = sorted(schema_already)
        ledger["updated_at"] = stamp.isoformat()
        ledger["symbols"] = sorted(set(result.symbols) | set(ledger.get("symbols") or []))
        save_repair_ledger(ledger_path, ledger)
    return result


def _restore_watch_from_report(watch, report: ResearchReport, *, candidates: CandidateStore, now: datetime) -> bool:
    from agentic_portfolio.research.types import ResearchConclusion
    from agentic_portfolio.watch.types import WatchStatus, approaching_next_session

    if report.research_conclusion not in {ResearchConclusion.KEEP_WATCHING, ResearchConclusion.ADVANCE_TO_THESIS}:
        return False
    existing = watch.store.by_ticker(report.symbol)
    if existing is not None and approaching_next_session(existing):
        if existing.research_id != report.research_id:
            existing.research_id = report.research_id
            if not existing.research_thesis:
                existing.research_thesis = report.executive_summary
            existing.last_updated = now.isoformat()
            watch.store.save(existing)
        return False
    candidate = candidates.active_for_symbol(report.symbol) or candidates.current_for_symbol(report.symbol)
    sleeve = report.provisional_sleeve.value if report.provisional_sleeve else None
    if existing is not None and existing.sleeve:
        sleeve = existing.sleeve
    item = watch.upsert_from_candidate(
        ticker=report.symbol,
        score=report.discovery_score if candidate is None else candidate.discovery_score,
        thesis=report.executive_summary,
        confidence=report.confidence.value if report.confidence else None,
        reasons=list(report.key_catalysts or []),
        risks=list(report.key_risks or []),
        last_price=report.market_price,
        status=WatchStatus.WATCH,
        off_hours=False,
        prepare_conditional_plan=False,
        sleeve=sleeve,
        context={"ticker": report.symbol, "research_id": report.research_id, "repair": True},
    )
    item.research_id = report.research_id
    item.invalidating_conditions = list(report.invalidation_candidates or [])
    item.catalysts = list(report.key_catalysts or [])
    item.reason_for_watch = item.reason_for_watch or "restored_from_valid_research"
    if getattr(report, "stale_after", None):
        item.expiration = report.stale_after
    if candidate is not None:
        item.candidate_id = candidate.candidate_id
    watch.store.save(item)
    return True
