"""Production Research Queue worker.

Consumes promoted candidates and drives:

Candidate → ResearchReport → DRAFT thesis → PortfolioDecision → ProposedAction → RiskGate → LiveApproval

Never places an order. Never treats a Candidate as a BUY.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from json import dumps as json_dumps
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from agentic_portfolio.ai.config import load_ai_config, pipeline_limits
from agentic_portfolio.ai.context import assemble_context
from agentic_portfolio.ai.errors import AIError, BudgetDenied, BudgetExhausted
from agentic_portfolio.ai.gateway import AIGateway
from agentic_portfolio.ai.reasoners import GatewayDecisionReasoner, GatewayResearchReasoner
from agentic_portfolio.ai.schemas import SCREENING_SCHEMA
from agentic_portfolio.ai.screening import SCREEN_INSTRUCTIONS, screening_from_payload
from agentic_portfolio.ai.store import AIArtifactStore
from agentic_portfolio.ai.types import BudgetMode, ModelRole
from agentic_portfolio.context import portfolio_context_from_dict
from agentic_portfolio.decision.engine import run_portfolio_decision
from agentic_portfolio.decision.reasoner import DecisionReasoner
from agentic_portfolio.decision.store import DecisionStore
from agentic_portfolio.decision.types import GatedAction
from agentic_portfolio.decision.validate import DecisionValidationError
from agentic_portfolio.discovery.freshness import is_queue_expired, normalize_queue_freshness
from agentic_portfolio.discovery.store import CandidateStore, ResearchQueue
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.live.isolation import detect_paper_contamination
from agentic_portfolio.live.store import LivePortfolioStore
from agentic_portfolio.live_approval import LiveApprovalEngine
from agentic_portfolio.notify import NotificationEngine, NotificationKind
from agentic_portfolio.paper_fill.store import PaperFillStore
from agentic_portfolio.calendar import is_regular_hours
from agentic_portfolio.policy import load_research_config
from agentic_portfolio.research.collect import collect_research_payload
from agentic_portfolio.research.engine import evidence_fingerprint, run_research
from agentic_portfolio.research.packet import ResearchPayload, build_packet
from agentic_portfolio.research.reasoner import ResearchReasoner
from agentic_portfolio.research.repair import invalidate_and_requeue_collector_bug_reports
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.sufficiency import evaluate_evidence_sufficiency, looks_like_pre_fix_need_more_data
from agentic_portfolio.research.types import ResearchConclusion, ResearchFreshness, ResearchReport, ResearchStatus
from agentic_portfolio.research.validate import ResearchValidationError
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, LIVE_SOURCE_OF_TRUTH, RuntimeMode, discovery_state_dir, live_placement_enabled
from agentic_portfolio.schemas import (
    Candidate,
    CandidateStatus,
    Decision,
    DiscoveryPriority,
    GateVerdict,
    PortfolioContext,
    ResearchQueueEntry,
    ResearchQueueStatus,
    ThesisRecord,
)
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry
from agentic_portfolio.watch import WatchEngine, WatchStatus
from agentic_portfolio.watch.types import WatchItem

PRIORITY_RANK = {
    DiscoveryPriority.URGENT_RESEARCH: 0,
    DiscoveryPriority.HIGH: 1,
    DiscoveryPriority.MEDIUM: 2,
    DiscoveryPriority.LOW: 3,
}

RESEARCHING_STATES = {ResearchQueueStatus.RESEARCHING, ResearchQueueStatus.IN_PROGRESS}
TERMINAL_QUEUE = {
    ResearchQueueStatus.COMPLETED,
    ResearchQueueStatus.REJECTED,
    ResearchQueueStatus.NEED_MORE_DATA,
    ResearchQueueStatus.INCONCLUSIVE,
    ResearchQueueStatus.EXPIRED,
    ResearchQueueStatus.DROPPED,
}
RISK_PERMIT = {GateVerdict.PASS, GateVerdict.REQUIRES_ENHANCED_REVIEW}
ACTIONABLE = {Decision.BUY, Decision.ADD, Decision.REDUCE, Decision.SELL}
PAPER_NAV_LEAK = 10_000.0
STALE_CLAIM_SECONDS = 900


@dataclass
class PipelineCycleResult:
    job: str
    status: str
    items_considered: int = 0
    items_processed: int = 0
    ai_calls: int = 0
    reports_created: int = 0
    watches_created: int = 0
    theses_created: int = 0
    proposals_created: int = 0
    rejections: int = 0
    skipped_reason: str | None = None
    placement_attempted: bool = False
    LIVE_ORDER_PLACEMENT: bool = False
    max_items: int | None = None
    symbols: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job": self.job,
            "status": self.status,
            "items_considered": self.items_considered,
            "items_processed": self.items_processed,
            "ai_calls": self.ai_calls,
            "reports_created": self.reports_created,
            "watches_created": self.watches_created,
            "theses_created": self.theses_created,
            "proposals_created": self.proposals_created,
            "rejection_count": self.rejections,
            "skipped": self.skipped_reason,
            "skipped_reason": self.skipped_reason,
            "placement_attempted": False,
            "LIVE_ORDER_PLACEMENT": LIVE_ORDER_PLACEMENT,
            "max_items": self.max_items,
            "symbols": list(self.symbols),
            "details": list(self.details),
        }


def resolve_queue_stores(
    root: Path,
    *,
    runtime_mode: RuntimeMode | str,
) -> tuple[CandidateStore, ResearchQueue]:
    """LIVE binds only to state/live_ai. Never falls back to PAPER/legacy state/."""
    return primary_queue_stores(root, runtime_mode=runtime_mode)


def primary_queue_stores(
    root: Path,
    *,
    runtime_mode: RuntimeMode | str,
) -> tuple[CandidateStore, ResearchQueue]:
    """LIVE production queue under state/live_ai. Never falls back to recovered state/ rows."""
    mode = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode).upper()
    base = Path(root)
    primary_dir = discovery_state_dir(base, mode=RuntimeMode(mode))
    return (
        CandidateStore(primary_dir / "candidates.json", runtime_mode=mode),
        ResearchQueue(primary_dir / "research_queue.json", runtime_mode=mode),
    )


def inspect_research_queues(root: Path, *, runtime_mode: RuntimeMode | str) -> dict[str, Any]:
    """Show production, recovered-legacy, and worker-bound queues without mutating them."""
    mode = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode).upper()
    base = Path(root)
    prod_candidates, prod_queue = primary_queue_stores(base, runtime_mode=mode)
    worker_candidates, worker_queue = resolve_queue_stores(base, runtime_mode=mode)
    legacy_queue = ResearchQueue(base / "state" / "research_queue.json", runtime_mode=mode)
    prod_path = prod_queue.path.resolve()
    worker_path = worker_queue.path.resolve()
    legacy_path = legacy_queue.path.resolve()
    return {
        "production_path": str(prod_queue.path),
        "legacy_path": str(legacy_queue.path),
        "worker_bound_path": str(worker_queue.path),
        "worker_uses_legacy_fallback": worker_path != prod_path,
        "legacy_distinct": legacy_path != prod_path,
        "production": prod_queue,
        "legacy": legacy_queue,
        "worker": worker_queue,
        "production_candidates": prod_candidates,
        "worker_candidates": worker_candidates,
    }


def load_live_context(root: Path, *, runtime_mode: RuntimeMode | str) -> PortfolioContext | None:
    mode = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode).upper()
    if mode != RuntimeMode.LIVE.value:
        return None
    book = LivePortfolioStore(root).current_book()
    if not isinstance(book, dict):
        return None
    raw = book.get("context")
    if not isinstance(raw, dict):
        return None
    ctx = portfolio_context_from_dict(raw)
    paper = PaperFillStore(root).current_book()
    leaks = detect_paper_contamination(book, paper, runtime_mode=RuntimeMode.LIVE)
    if leaks:
        raise RuntimeError("paper state leaked into LIVE research pipeline: " + ", ".join(leaks))
    if abs(float(ctx.current_nav) - PAPER_NAV_LEAK) < 0.01 and not ctx.positions:
        # $10,000 empty book is the isolated paper default, never LIVE NAV.
        raise RuntimeError("refusing paper $10,000 NAV as LIVE account value")
    return ctx


class ResearchQueueWorker:
    """Idempotent, restart-safe consumer of QUEUED research-queue entries."""

    def __init__(
        self,
        root: Path,
        *,
        runtime_mode: RuntimeMode | str,
        gateway: AIGateway | None = None,
        research_reasoner: ResearchReasoner | None = None,
        decision_reasoner: DecisionReasoner | None = None,
        payload_fn: Callable[[Candidate], ResearchPayload] | None = None,
        context_fn: Callable[[], PortfolioContext | None] | None = None,
        watch: WatchEngine | None = None,
        approvals: LiveApprovalEngine | None = None,
        notify: NotificationEngine | None = None,
        now_fn: Callable[[], datetime] | None = None,
        fetcher: Any = None,
        max_items: int | None = None,
        stale_claim_seconds: int = STALE_CLAIM_SECONDS,
    ) -> None:
        self.root = Path(root)
        self.runtime_mode = runtime_mode if isinstance(runtime_mode, RuntimeMode) else RuntimeMode(str(runtime_mode).upper())
        self.gateway = gateway
        self.research_reasoner = research_reasoner
        self.decision_reasoner = decision_reasoner
        self.payload_fn = payload_fn
        self.context_fn = context_fn
        self.watch = watch
        self.approvals = approvals
        self.notify = notify
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.fetcher = fetcher
        self.max_items = max_items
        self.stale_claim_seconds = stale_claim_seconds
        self.reload_stores()
        self.research_store = ResearchStore(self.root)
        self.decision_store = DecisionStore(self.root, runtime_mode=self.runtime_mode.value)
        self.theses = ThesisRegistry(
            self.root / "state" / "thesis_registry.json",
            runtime_mode=self.runtime_mode.value,
        )
        self.sleeves = SleeveRegistry(self.root / "state" / "sleeve_registry.json")
        self.ai_store = AIArtifactStore(self.root, runtime_mode=self.runtime_mode)
        self.journal = self.root / "logs" / "pipeline.jsonl"

    def reload_stores(self) -> None:
        """Re-read queue files each cycle so discovery writes are not missed after restart."""
        self.candidates, self.queue = resolve_queue_stores(self.root, runtime_mode=self.runtime_mode)

    def now(self) -> datetime:
        stamp = self._now()
        if stamp.tzinfo is None:
            return stamp.replace(tzinfo=timezone.utc)
        return stamp

    def _reasoner(self) -> ResearchReasoner:
        if self.research_reasoner is not None:
            return self.research_reasoner
        if self.gateway is not None:
            return GatewayResearchReasoner(self.gateway)
        raise RuntimeError("no research reasoner configured")

    def _decision(self) -> DecisionReasoner:
        if self.decision_reasoner is not None:
            return self.decision_reasoner
        if self.gateway is not None:
            return GatewayDecisionReasoner(self.gateway)
        raise RuntimeError("no decision reasoner configured")

    def _context(self) -> PortfolioContext | None:
        if self.context_fn is not None:
            return self.context_fn()
        return load_live_context(self.root, runtime_mode=self.runtime_mode)

    def _payload(self, candidate: Candidate) -> ResearchPayload:
        if self.payload_fn is not None:
            return self.payload_fn(candidate)
        if self.fetcher is not None:
            return collect_research_payload(candidate.symbol, self.fetcher, now=self.now())
        raise RuntimeError(f"no research payload source for {candidate.symbol}")

    def _luna_screen(self, candidate: Candidate, packet, context: PortfolioContext) -> dict[str, Any]:
        """Cheap Luna screen before Terra. Outages defer; they do not look like a reject."""
        if self.gateway is None or self.research_reasoner is not None:
            return {"outcome": "proceed", "ai_calls": 0}
        existing = self.ai_store.latest_for_ticker("screenings", candidate.symbol)
        if existing is not None:
            if existing.get("worth_deep_research") is False:
                return {"outcome": "reject", "ai_calls": 0, "screening_id": existing.get("screening_id")}
            if existing.get("worth_deep_research") is True:
                return {"outcome": "proceed", "ai_calls": 0, "screening_id": existing.get("screening_id")}
        facts = {item.name: item.value for item in packet.facts}
        derived = {item.name: item.value for item in packet.derived_metrics}
        snap = {
            "current_price": facts.get("market_price"),
            "bid": facts.get("bid") or facts.get("bid_price"),
            "ask": facts.get("ask") or facts.get("ask_price"),
            "previous_close": facts.get("previous_close"),
            "name": facts.get("legal_name"),
            "sector": facts.get("sector_label_raw"),
            "description": facts.get("description"),
            "market_cap": facts.get("market_cap"),
            "pe_ratio": facts.get("pe_ratio"),
            "average_volume": facts.get("average_volume"),
            "return_5d": derived.get("return_5d"),
            "return_21d": derived.get("return_21d"),
            "rsi": facts.get("rsi"),
            "sma_50": facts.get("sma_50"),
            "sma_200": facts.get("sma_200"),
            "news_headlines": facts.get("news_headlines") or [],
        }
        ctx = assemble_context(
            candidate.symbol,
            context,
            now_iso=self.now().isoformat(),
            runtime_mode=self.runtime_mode,
            snapshot=snap,
            discovery={
                "discovery_score": candidate.discovery_score,
                "provisional_sleeve": candidate.provisional_sleeve.value if candidate.provisional_sleeve else None,
            },
        )
        try:
            result = self.gateway.complete_structured(
                role=ModelRole.SCREENING,
                purpose="candidate_screening",
                schema_name="screening",
                schema=SCREENING_SCHEMA,
                messages=[
                    {"role": "system", "content": SCREEN_INSTRUCTIONS},
                    {"role": "user", "content": json_dumps(ctx.to_prompt_dict(), default=str)},
                ],
                ticker=candidate.symbol,
            )
        except (BudgetDenied, BudgetExhausted, AIError) as exc:
            return {"outcome": "defer", "reason": str(exc), "ai_calls": 0}
        row = screening_from_payload(
            result.payload,
            provider=result.provider,
            model=result.model,
            cost=result.actual_cost,
            context_id=ctx.context_id,
            screening_id=str(uuid4()),
            runtime_mode=self.runtime_mode.value,
        )
        try:
            self.ai_store.save_screening(
                row.screening_id,
                {**row.__dict__, "created_at": self.now().isoformat(), "context_id": ctx.context_id, "ticker": row.ticker},
            )
        except FileExistsError:
            pass
        except Exception:  # noqa: BLE001
            pass
        if not row.worth_deep_research:
            return {"outcome": "reject", "ai_calls": 1, "screening_id": row.screening_id}
        return {"outcome": "proceed", "ai_calls": 1, "screening_id": row.screening_id}

    def screen_cycle(self, *, job: str = "LUNA_SCREEN") -> dict[str, Any]:
        """Run Luna against queued names without calling Terra."""
        result = {"job": job, "status": "OK", "ai_calls": 0, "screened": 0, "rejected": 0, "placement_attempted": False}
        blocked, why = self._budget_blocked()
        if blocked:
            result["status"] = "BLOCKED"
            result["skipped"] = why
            return result
        if self.gateway is None:
            result["status"] = "SKIPPED_NO_WORK"
            result["skipped"] = "no_ai_gateway"
            return result
        context = self._context()
        if context is None:
            result["status"] = "BLOCKED"
            result["skipped"] = "missing_live_context"
            return result
        self.reload_stores()
        for entry in self.pending_entries():
            candidate = self.candidates.get(entry.candidate_id) or self.candidates.active_for_symbol(entry.symbol)
            if candidate is None:
                continue
            try:
                payload = self._payload(candidate)
                packet = build_packet(payload, candidate, context)
                if not evaluate_evidence_sufficiency(packet).sufficient:
                    continue
                luna = self._luna_screen(candidate, packet, context)
            except Exception:  # noqa: BLE001
                continue
            result["ai_calls"] += int(luna.get("ai_calls") or 0)
            result["screened"] += 1
            if luna.get("outcome") == "reject":
                result["rejected"] += 1
                self.queue.set_status(entry.queue_id, ResearchQueueStatus.REJECTED, skipped_reason="luna_screen_rejected")
        return result

    def _budget_blocked(self) -> tuple[bool, str]:
        if self.gateway is None:
            return False, ""
        status = self.gateway.budget.status()
        if status.mode is BudgetMode.EXHAUSTED:
            return True, "budget_exhausted"
        return False, ""

    def reclaim_stale_claims(self, now: datetime | None = None) -> int:
        stamp = now or self.now()
        reclaimed = 0
        for entry in self.queue.all():
            if entry.status not in RESEARCHING_STATES:
                continue
            claimed = entry.claimed_at
            if not claimed:
                self.queue.set_status(entry.queue_id, ResearchQueueStatus.QUEUED, claimed_at=None)
                reclaimed += 1
                continue
            try:
                started = datetime.fromisoformat(str(claimed).replace("Z", "+00:00"))
            except ValueError:
                self.queue.set_status(entry.queue_id, ResearchQueueStatus.QUEUED, claimed_at=None)
                reclaimed += 1
                continue
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if (stamp - started).total_seconds() >= self.stale_claim_seconds:
                self.queue.set_status(entry.queue_id, ResearchQueueStatus.QUEUED, claimed_at=None, skipped_reason="stale_claim_reclaimed")
                reclaimed += 1
        return reclaimed

    def pending_entries(self, now: datetime | None = None) -> list[ResearchQueueEntry]:
        stamp = now or self.now()
        rows: list[ResearchQueueEntry] = []
        for entry in self.queue.all():
            if entry.status in TERMINAL_QUEUE:
                continue
            if entry.status in RESEARCHING_STATES:
                continue
            if entry.status is not ResearchQueueStatus.QUEUED:
                continue
            prior = entry.freshness_deadline
            entry = normalize_queue_freshness(entry)
            if entry.freshness_deadline != prior:
                self.queue.save_entry(entry)
            if is_queue_expired(entry, stamp):
                self.queue.set_status(entry.queue_id, ResearchQueueStatus.EXPIRED, skipped_reason="freshness_deadline")
                continue
            rows.append(entry)
        rows.sort(
            key=lambda e: (
                1 if e.deferred_due_to_research_queue_overlap else 0,
                PRIORITY_RANK.get(e.priority, 9),
                -float(e.discovery_score or 0),
                e.enqueued_at or "",
            )
        )
        return rows

    def run_cycle(self, *, job: str = "RESEARCH_QUEUE_WORKER", max_items: int | None = None) -> PipelineCycleResult:
        result = PipelineCycleResult(job=job, status="OK", LIVE_ORDER_PLACEMENT=LIVE_ORDER_PLACEMENT)
        blocked, why = self._budget_blocked()
        if blocked:
            result.status = "BLOCKED"
            result.skipped_reason = why
            self._notify(
                NotificationKind.AI_BUDGET_EXHAUSTED if why == "budget_exhausted" else NotificationKind.AI_BUDGET_CRITICAL,
                title="AI budget blocked research",
                body="Research queue worker skipped AI calls. Runtime continues.",
                payload={"job": job, "reason": why},
            )
            return result
        if self.research_reasoner is None and self.gateway is None:
            result.status = "DEGRADED"
            result.skipped_reason = "no_ai_gateway"
            return result
        self.reload_stores()
        context = self._context()
        if context is None:
            result.status = "BLOCKED"
            result.skipped_reason = "missing_live_context"
            return result
        if self.runtime_mode is RuntimeMode.LIVE:
            if abs(float(context.current_nav) - PAPER_NAV_LEAK) < 0.01 and not context.positions:
                result.status = "FAILED"
                result.skipped_reason = "paper_nav_refused"
                return result
        self.reclaim_stale_claims()
        repair = invalidate_and_requeue_collector_bug_reports(
            root=self.root,
            research_store=self.research_store,
            candidates=self.candidates,
            queue=self.queue,
            runtime_mode=self.runtime_mode,
            now=self.now(),
            journal=self.root / "logs" / "research.jsonl",
        )
        if repair.requeued:
            result.details.append({"status": "COLLECTOR_REPAIR_REQUEUE", **repair.as_dict()})
            self.reload_stores()
        pending = self.pending_entries()
        result.items_considered = len(pending)
        if not pending:
            result.status = "SKIPPED_NO_WORK"
            result.skipped_reason = "empty_queue"
            return result
        limits = pipeline_limits(load_ai_config())
        cap = max_items if max_items is not None else (self.max_items if self.max_items is not None else int(limits.get("max_deep_research") or 2))
        if self.gateway is not None:
            mode = self.gateway.budget.status().mode
            if mode is BudgetMode.CRITICAL:
                cap = min(cap, 1)
            elif mode is BudgetMode.CONSERVING:
                cap = min(cap, 1)
        result.max_items = cap
        for entry in pending[: max(0, cap)]:
            row = self.process_entry(entry, context)
            result.items_processed += 1
            result.details.append(row)
            result.symbols.append(entry.symbol)
            result.ai_calls += int(row.get("ai_calls") or 0)
            result.reports_created += int(row.get("reports_created") or 0)
            result.watches_created += int(row.get("watches_created") or 0)
            result.theses_created += int(row.get("theses_created") or 0)
            result.proposals_created += int(row.get("proposals_created") or 0)
            result.rejections += int(row.get("rejected") or 0)
            if row.get("status") == "FAILED":
                result.status = "DEGRADED"
        append_jsonl(
            {
                "type": "RESEARCH_QUEUE_CYCLE",
                "job": job,
                "status": result.status,
                "processed": result.items_processed,
                "placement_attempted": False,
            },
            self.journal,
        )
        return result

    def process_entry(self, entry: ResearchQueueEntry, context: PortfolioContext) -> dict[str, Any]:
        candidate = self.candidates.get(entry.candidate_id) or self.candidates.active_for_symbol(entry.symbol)
        if candidate is None:
            self.queue.set_status(entry.queue_id, ResearchQueueStatus.DROPPED, skipped_reason="missing_candidate")
            return {"symbol": entry.symbol, "status": "FAILED", "reason": "missing_candidate"}
        try:
            payload = self._payload(candidate)
        except Exception as exc:  # noqa: BLE001
            self.queue.set_status(
                entry.queue_id,
                ResearchQueueStatus.QUEUED,
                last_error=f"{type(exc).__name__}: {exc}",
                claimed_at=None,
                skipped_reason="payload_failed",
            )
            return {"symbol": candidate.symbol, "status": "FAILED", "reason": f"{type(exc).__name__}: {exc}"}
        packet = build_packet(payload, candidate, context)
        fingerprint = evidence_fingerprint(candidate, payload=payload, packet=packet)
        existing = self.research_store.by_candidate(candidate.candidate_id) or self.research_store.by_symbol(candidate.symbol)
        complete = [
            r
            for r in existing
            if r.research_status
            in {ResearchStatus.RESEARCH_COMPLETE, ResearchStatus.RESEARCH_REJECTED, ResearchStatus.RESEARCH_INCONCLUSIVE}
            and not looks_like_pre_fix_need_more_data(r)
        ]
        if complete:
            report = sorted(complete, key=lambda r: r.started_at, reverse=True)[0]
            same_evidence = (report.evidence_fingerprint or fingerprint) == fingerprint
            retry_due = _inconclusive_retry_due(report, entry, self.now())
            if same_evidence and not retry_due:
                self.queue.set_status(
                    entry.queue_id,
                    _queue_status_for(report),
                    research_id=report.research_id,
                    evidence_fingerprint=fingerprint,
                    skipped_reason="idempotent_existing_report",
                )
                return self._apply_report(report, candidate, context, entry, ai_calls=0, duplicate=True)
            if same_evidence and report.research_status is ResearchStatus.RESEARCH_COMPLETE and not retry_due:
                self.queue.set_status(
                    entry.queue_id,
                    _queue_status_for(report),
                    research_id=report.research_id,
                    evidence_fingerprint=fingerprint,
                    skipped_reason="idempotent_existing_report",
                )
                return self._apply_report(report, candidate, context, entry, ai_calls=0, duplicate=True)

        stamp = self.now()
        try:
            self.research_store.save_packet(packet)
        except Exception:  # noqa: BLE001 — packet persist must not block research
            pass
        sufficiency = evaluate_evidence_sufficiency(packet)
        luna_calls = 0
        if sufficiency.sufficient:
            luna = self._luna_screen(candidate, packet, context)
            if luna["outcome"] == "defer":
                self.queue.set_status(
                    entry.queue_id,
                    ResearchQueueStatus.QUEUED,
                    last_error=str(luna.get("reason") or "luna_unavailable"),
                    claimed_at=None,
                    skipped_reason="ai_unavailable",
                )
                return {
                    "symbol": candidate.symbol,
                    "status": "FAILED",
                    "reason": str(luna.get("reason") or "ai_unavailable"),
                    "ai_calls": int(luna.get("ai_calls") or 0),
                    "skipped_reason": "ai_unavailable",
                }
            if luna["outcome"] == "reject":
                self.queue.set_status(
                    entry.queue_id,
                    ResearchQueueStatus.REJECTED,
                    claimed_at=None,
                    skipped_reason="luna_screen_rejected",
                )
                self.candidates.set_status(candidate.candidate_id, CandidateStatus.REJECTED, reason="luna_screen_rejected")
                return {
                    "symbol": candidate.symbol,
                    "status": "OK",
                    "ai_calls": int(luna.get("ai_calls") or 0),
                    "reports_created": 0,
                    "rejected": 1,
                    "conclusion": "SCREEN_REJECTED",
                    "screening_id": luna.get("screening_id"),
                }
            luna_calls = int(luna.get("ai_calls") or 0)
        self.queue.set_status(
            entry.queue_id,
            ResearchQueueStatus.RESEARCHING,
            claimed_at=stamp.isoformat(),
            last_attempt_at=stamp.isoformat(),
            attempt_count=int(entry.attempt_count or 0) + 1,
            evidence_fingerprint=fingerprint,
        )
        try:
            out = run_research(
                candidate,
                payload,
                context,
                self._reasoner(),
                queue_entry=entry,
                store=self.research_store,
                candidate_store=self.candidates,
                queue_store=self.queue,
                persist=True,
                now=stamp,
                journal=self.root / "logs" / "research.jsonl",
            )
        except (ResearchValidationError, BudgetDenied, BudgetExhausted) as exc:
            self.queue.set_status(
                entry.queue_id,
                ResearchQueueStatus.QUEUED,
                last_error=str(exc),
                claimed_at=None,
                skipped_reason="provider_or_schema_failure",
            )
            self._notify(
                NotificationKind.SERVICE_ERROR,
                title=f"Research call failed — {candidate.symbol}",
                body=str(exc),
                payload={"symbol": candidate.symbol, "reason": str(exc)},
            )
            return {"symbol": candidate.symbol, "status": "DEGRADED", "reason": str(exc), "ai_calls": luna_calls, "rejected": 0}
        except AIError as exc:
            self.queue.set_status(
                entry.queue_id,
                ResearchQueueStatus.QUEUED,
                last_error=f"{type(exc).__name__}: {exc}",
                claimed_at=None,
                skipped_reason="ai_unavailable",
            )
            return {
                "symbol": candidate.symbol,
                "status": "FAILED",
                "reason": f"{type(exc).__name__}: {exc}",
                "ai_calls": luna_calls,
                "skipped_reason": "ai_unavailable",
            }
        except Exception as exc:  # noqa: BLE001 — one candidate must not kill the cycle
            self.queue.set_status(
                entry.queue_id,
                ResearchQueueStatus.QUEUED,
                last_error=f"{type(exc).__name__}: {exc}",
                claimed_at=None,
                skipped_reason="retry_after_failure",
            )
            return {"symbol": candidate.symbol, "status": "FAILED", "reason": f"{type(exc).__name__}: {exc}"}

        report = out.report
        report.evidence_fingerprint = report.evidence_fingerprint or fingerprint
        terra_calls = 0 if report.research_source == "deterministic" else 1
        self._persist_ai_research(report, candidate)
        self.queue.set_status(
            entry.queue_id,
            _queue_status_for(report),
            research_id=report.research_id,
            claimed_at=None,
            evidence_fingerprint=report.evidence_fingerprint,
        )
        if report.research_source != "deterministic":
            self._notify(
                NotificationKind.RESEARCH_COMPLETED,
                title=f"Research completed — {report.symbol}",
                body=f"{report.symbol}: {report.research_conclusion.value if report.research_conclusion else report.research_status.value}",
                payload={
                    "symbol": report.symbol,
                    "research_id": report.research_id,
                    "research_source": report.research_source,
                    "provider": report.provider,
                    "model": report.model,
                    "ai_call_id": report.ai_call_id,
                },
            )
        return self._apply_report(report, candidate, context, entry, ai_calls=luna_calls + terra_calls, duplicate=False)

    def _apply_report(
        self,
        report: ResearchReport,
        candidate: Candidate,
        context: PortfolioContext,
        entry: ResearchQueueEntry,
        *,
        ai_calls: int,
        duplicate: bool,
    ) -> dict[str, Any]:
        conclusion = report.research_conclusion
        row: dict[str, Any] = {
            "symbol": report.symbol,
            "status": "OK",
            "ai_calls": 0 if duplicate else ai_calls,
            "reports_created": 0 if duplicate else 1,
            "watches_created": 0,
            "theses_created": 0,
            "proposals_created": 0,
            "rejected": 0,
            "conclusion": conclusion.value if conclusion else None,
            "duplicate": duplicate,
        }
        if conclusion is ResearchConclusion.REJECT:
            row["rejected"] = 1
            self.candidates.set_status(candidate.candidate_id, CandidateStatus.REJECTED, reason="research_rejected")
            self._watch_from_research(candidate, report, status=WatchStatus.REJECTED, reason="research_rejected")
            self._notify(
                NotificationKind.RESEARCH_REJECTED,
                title=f"Candidate rejected — {report.symbol}",
                body=report.executive_summary or "Research concluded REJECT.",
                payload={"symbol": report.symbol, "research_id": report.research_id},
            )
            return row
        if conclusion in {ResearchConclusion.NEED_MORE_DATA} or report.research_status is ResearchStatus.RESEARCH_INCONCLUSIVE:
            row["rejected"] = 0
            self.candidates.set_status(candidate.candidate_id, CandidateStatus.RESEARCH_INCONCLUSIVE, reason="need_more_data")
            return row
        if conclusion is ResearchConclusion.KEEP_WATCHING:
            created = self._watch_from_research(candidate, report, status=WatchStatus.WATCH, reason="keep_watching")
            row["watches_created"] = 1 if created else 0
            self.candidates.set_status(candidate.candidate_id, CandidateStatus.WATCHING, reason="keep_watching")
            return row
        if conclusion is not ResearchConclusion.ADVANCE_TO_THESIS:
            return row
        if duplicate and self.approvals is not None:
            pending = [a for a in self.approvals.store.pending() if a.ticker.upper() == report.symbol.upper()]
            if pending:
                row["approval_id"] = pending[0].approval_id
                row["decision"] = "EXISTING_PENDING_APPROVAL"
                self.candidates.set_status(candidate.candidate_id, CandidateStatus.RESEARCH_COMPLETE)
                return row
        return self._decide(report, candidate, context, row)

    def _decide(
        self,
        report: ResearchReport,
        candidate: Candidate,
        context: PortfolioContext,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            decided = run_portfolio_decision(
                [report],
                context,
                self._decision(),
                theses=self.theses,
                sleeves=self.sleeves,
                store=self.decision_store,
                persist=True,
                now=self.now(),
                journal=self.root / "logs" / "thesis_decision.jsonl",
            )
        except DecisionValidationError as exc:
            row["status"] = "DEGRADED"
            row["reason"] = str(exc)
            self._watch_from_research(candidate, report, status=WatchStatus.WATCH, reason="decision_inconclusive")
            row["watches_created"] = 1
            return row
        row["theses_created"] = len(decided.theses)
        row["ai_calls"] = int(row.get("ai_calls") or 0) + 1
        name = next((d for d in decided.decisions if d.symbol.upper() == report.symbol.upper()), None)
        thesis = next((t for t in decided.theses if t.symbol.upper() == report.symbol.upper()), None)
        if name is None:
            self._watch_from_research(candidate, report, thesis=thesis, status=WatchStatus.WATCH, reason="no_named_decision")
            row["watches_created"] = 1
            return row
        if name.decision in {Decision.REJECT}:
            row["rejected"] = 1
            self._watch_from_research(candidate, report, thesis=thesis, status=WatchStatus.REJECTED, reason="portfolio_reject")
            self.candidates.set_status(candidate.candidate_id, CandidateStatus.REJECTED, reason="portfolio_reject")
            return row
        if name.decision in {Decision.WATCH, Decision.NO_ACTION, Decision.HOLD}:
            created = self._watch_from_research(
                candidate,
                report,
                thesis=thesis,
                status=WatchStatus.WATCH,
                reason=f"decision_{name.decision.value.lower()}",
            )
            row["watches_created"] = 1 if created else 0
            row["decision"] = name.decision.value
            self.candidates.set_status(candidate.candidate_id, CandidateStatus.WATCHING, reason=f"decision_{name.decision.value.lower()}")
            return row
        if name.decision not in ACTIONABLE:
            row["decision"] = name.decision.value
            return row
        gated = next((g for g in decided.gated_actions if g.proposed_action.symbol.upper() == report.symbol.upper()), None)
        if gated is None:
            self._watch_from_research(candidate, report, thesis=thesis, status=WatchStatus.WATCH, reason="no_proposed_action")
            row["watches_created"] = 1
            row["decision"] = name.decision.value
            return row
        verdict = gated.risk.verdict
        if verdict not in RISK_PERMIT:
            row["decision"] = name.decision.value
            row["risk_verdict"] = verdict.value if hasattr(verdict, "value") else str(verdict)
            self._watch_from_research(candidate, report, thesis=thesis, status=WatchStatus.WATCH, reason="risk_gate_blocked")
            row["watches_created"] = 1
            self._notify(
                NotificationKind.RISK_GATE_BLOCKED,
                title=f"Risk Gate blocked {report.symbol}",
                body=f"{report.symbol} {name.decision.value} blocked: {row['risk_verdict']}",
                payload={"symbol": report.symbol, "verdict": row["risk_verdict"]},
            )
            return row
        approval = self._create_approval(gated, report, candidate, context, thesis, name)
        if approval is None:
            row["reason"] = "approval_not_created"
            return row
        watch = self._watch_from_research(
            candidate,
            report,
            thesis=thesis,
            status=WatchStatus.APPROVAL_REQUIRED,
            reason="approval_created",
            approval_id=approval.approval_id,
        )
        if watch:
            row["watches_created"] = 1
        row["proposals_created"] = 1
        row["decision"] = name.decision.value
        row["approval_id"] = approval.approval_id
        row["risk_verdict"] = verdict.value if hasattr(verdict, "value") else str(verdict)
        self.candidates.set_status(candidate.candidate_id, CandidateStatus.RESEARCH_COMPLETE)
        self._notify(
            NotificationKind.TRADE_PROPOSAL,
            title=f"TRADE APPROVAL REQUIRED — {report.symbol}",
            body=(
                f"{report.symbol} {name.decision.value} "
                f"{name.desired_allocation_pct or 0:.2f}% NAV. Approving does not place an order."
            ),
            payload={"symbol": report.symbol, "approval_id": approval.approval_id},
        )
        return row

    def _create_approval(
        self,
        gated: GatedAction,
        report: ResearchReport,
        candidate: Candidate,
        context: PortfolioContext,
        thesis: ThesisRecord | None,
        name: Any,
    ):
        if self.approvals is None:
            return None
        action = gated.proposed_action
        nav = float(context.current_nav or 0)
        dollars = float(action.proposed_notional or 0)
        pct = float(name.desired_allocation_pct or 0)
        if pct and not dollars and nav:
            dollars = nav * (pct / 100.0)
        impact = {
            "nav": nav,
            "cash": context.cash,
            "buying_power": context.buying_power,
            "source_of_truth": LIVE_SOURCE_OF_TRUTH if self.runtime_mode is RuntimeMode.LIVE else "isolated_paper_book",
            "proposed_notional": dollars,
            "desired_allocation_pct": pct,
            "expected_resulting_position_pct": action.expected_resulting_position_pct,
            "holdings_count": context.holdings_count,
            "LIVE_ORDER_PLACEMENT": live_placement_enabled(),
        }
        gw = getattr(self._reasoner(), "last_result", None) or getattr(self._decision(), "last_result", None)
        item = self.approvals.create(
            ticker=report.symbol,
            proposed_action=action.decision.value,
            proposed_dollar_amount=dollars or None,
            proposed_allocation_pct=pct or None,
            reason=name.rationale or report.executive_summary,
            ai_rationale=report.executive_summary,
            supporting_thesis=(thesis.thesis_summary if thesis else report.executive_summary),
            current_quote=action.current_price or candidate.current_price or report.market_price,
            risk_gate_result={
                "verdict": gated.risk.verdict.value if hasattr(gated.risk.verdict, "value") else str(gated.risk.verdict),
                "reasons": [str(r) for r in (gated.risk.reasons or [])],
            },
            portfolio_impact=impact,
        )
        extras = {
            "sleeve": action.sleeve.value if action.sleeve else (candidate.provisional_sleeve.value if candidate.provisional_sleeve else None),
            "research_id": report.research_id,
            "thesis_id": thesis.thesis_id if thesis else None,
            "research_summary": report.executive_summary,
            "catalysts": list(report.key_catalysts or []),
            "key_risks": list(report.key_risks or []),
            "invalidation": list(report.invalidation_candidates or []),
            "expected_horizon": report.expected_horizon,
            "evidence_freshness": report.freshness.value if report.freshness else None,
            "provider": getattr(gw, "provider", None) if gw else None,
            "model": getattr(gw, "model", None) if gw else None,
            "quote_at_proposal": action.current_price or candidate.current_price,
            "nav_at_proposal": nav,
            "bull_case": report.bull_case.summary if report.bull_case else None,
            "base_case": report.base_case.summary if report.base_case else None,
            "bear_case": report.bear_case.summary if report.bear_case else None,
            "expected_order_type": "market",
        }
        for key, value in extras.items():
            if hasattr(item, key):
                setattr(item, key, value)
        item.portfolio_impact = {**impact, **{k: v for k, v in extras.items() if v is not None}}
        self.approvals.store.save(item)
        return item

    def _watch_from_research(
        self,
        candidate: Candidate,
        report: ResearchReport,
        *,
        thesis: ThesisRecord | None = None,
        status: WatchStatus,
        reason: str,
        approval_id: str | None = None,
    ) -> WatchItem | None:
        if self.watch is None:
            return None
        existing = self.watch.store.by_ticker(report.symbol)
        if existing is not None and thesis is not None and existing.research_thesis == (thesis.thesis_summary if thesis else existing.research_thesis):
            if existing.status not in {WatchStatus.REJECTED, WatchStatus.EXPIRED, WatchStatus.INVALIDATED} and status is WatchStatus.WATCH:
                # Prevent duplicate watch entries for the same thesis.
                existing.research_id = report.research_id
                if hasattr(existing, "thesis_id") and thesis is not None:
                    existing.thesis_id = thesis.thesis_id
                existing.last_updated = self.now().isoformat()
                if approval_id:
                    existing.approval_id = approval_id
                self.watch.store.save(existing)
                return existing
        sleeve = thesis.sleeve.value if thesis and thesis.sleeve else (
            candidate.provisional_sleeve.value if candidate.provisional_sleeve else None
        )
        off_hours = status is WatchStatus.WATCH and not is_regular_hours(self.now())
        item = self.watch.upsert_from_candidate(
            ticker=report.symbol,
            score=candidate.discovery_score,
            thesis=(thesis.thesis_summary if thesis else report.executive_summary),
            confidence=report.confidence.value if report.confidence else None,
            reasons=list(report.key_catalysts or candidate.reasons or []),
            risks=list(report.key_risks or []),
            last_price=report.market_price or candidate.current_price,
            status=status,
            off_hours=off_hours,
            prepare_conditional_plan=False,
            sleeve=sleeve,
            context={
                "ticker": report.symbol,
                "research_id": report.research_id,
                "thesis_id": thesis.thesis_id if thesis else None,
                "conclusion": report.research_conclusion.value if report.research_conclusion else None,
            },
        )
        item.entry_conditions = list(report.key_catalysts or [])
        item.invalidating_conditions = list(report.invalidation_candidates or [])
        item.reasons = list(dict.fromkeys(list(item.reasons) + [reason]))
        if hasattr(item, "candidate_id"):
            item.candidate_id = candidate.candidate_id
        if hasattr(item, "research_id"):
            item.research_id = report.research_id
        if hasattr(item, "thesis_id") and thesis is not None:
            item.thesis_id = thesis.thesis_id
        if hasattr(item, "sleeve"):
            item.sleeve = sleeve
        if hasattr(item, "catalysts"):
            item.catalysts = list(report.key_catalysts or [])
        if hasattr(item, "reason_for_watch"):
            item.reason_for_watch = reason
        if approval_id:
            item.approval_id = approval_id
        if status is WatchStatus.WATCH:
            self.watch.schedule_review(item, waiting_for_open=off_hours, sleeve=sleeve)
        self.watch.store.save(item)
        if existing is None and status is WatchStatus.WATCH:
            self._notify(
                NotificationKind.WATCH_CREATED,
                title=f"Watch created — {report.symbol}",
                body=reason,
                payload={"symbol": report.symbol, "watch_id": item.watch_id},
            )
        return item

    def _persist_ai_research(self, report: ResearchReport, candidate: Candidate) -> None:
        try:
            self.ai_store.save_research(
                report.research_id,
                {
                    "research_id": report.research_id,
                    "ticker": report.symbol,
                    "symbol": report.symbol,
                    "thesis": report.executive_summary,
                    "bull_case": report.bull_case.summary if report.bull_case else "",
                    "bear_case": report.bear_case.summary if report.bear_case else "",
                    "catalysts": list(report.key_catalysts or []),
                    "risks": list(report.key_risks or []),
                    "confidence": report.confidence.value if report.confidence else None,
                    "recommended_action": report.research_conclusion.value if report.research_conclusion else None,
                    "research_conclusion": report.research_conclusion.value if report.research_conclusion else None,
                    "created_at": report.completed_at or report.started_at,
                    "completed_at": report.completed_at,
                    "provisional_sleeve": candidate.provisional_sleeve.value if candidate.provisional_sleeve else None,
                    "candidate_id": candidate.candidate_id,
                    "runtime_mode": self.runtime_mode.value,
                    "research_source": report.research_source,
                    "provider": report.provider,
                    "model": report.model,
                    "ai_call_id": report.ai_call_id,
                    "estimated_cost": report.estimated_cost,
                    "actual_cost": report.actual_cost,
                },
            )
        except FileExistsError:
            pass
        except Exception:  # noqa: BLE001
            pass

    def _notify(self, kind: NotificationKind, *, title: str, body: str, payload: dict[str, Any] | None = None) -> None:
        if self.notify is None:
            return
        self.notify.emit(kind, title=title, body=body, payload=payload or {})

    def revalidate_watches(self, *, job: str, allow_ai: bool = False) -> dict[str, Any]:
        if self.watch is None:
            return {"job": job, "status": "SKIPPED_NO_WORK", "watch_items": 0, "skipped": "no_watch_engine"}
        items = self.watch.store.active()
        if not items:
            return {"job": job, "status": "SKIPPED_NO_WORK", "watch_items": 0, "skipped": "no_watch_items", "items_considered": 0}
        processed = 0
        expired = self.watch.expire_stale()
        opened = self.watch.promote_waiting_for_open(regular_hours_open=is_regular_hours(self.now()))
        for item in items:
            self.watch.mark_reassessed(item)
            processed += 1
        return {
            "job": job,
            "status": "OK",
            "watch_items": len(items),
            "items_considered": len(items),
            "items_processed": processed,
            "expired": len(expired),
            "promoted_from_waiting_for_open": len(opened),
            "ai_calls": 0,
            "placement_attempted": False,
        }

    def revalidate_approvals(self, *, quotes: Mapping[str, Mapping[str, Any]] | None = None, context: PortfolioContext | None = None) -> dict[str, Any]:
        if self.approvals is None:
            return {"expired": 0, "superseded": 0}
        expired = self.approvals.expire_due()
        superseded = 0
        ctx = context or self._context()
        quote_map = dict(quotes or {})
        for item in list(self.approvals.store.pending()):
            reasons: list[str] = []
            quote = quote_map.get(item.ticker) or {}
            price = quote.get("price") or quote.get("last")
            prior = item.current_quote
            if price is not None and prior:
                move = abs(float(price) - float(prior)) / abs(float(prior))
                if move >= 0.03:
                    reasons.append("quote_moved")
            if ctx is not None:
                impact = dict(item.portfolio_impact or {})
                nav_at = impact.get("nav") or getattr(item, "nav_at_proposal", None)
                if nav_at is not None and abs(float(ctx.current_nav) - float(nav_at)) / max(abs(float(nav_at)), 1.0) >= 0.05:
                    reasons.append("portfolio_changed")
                bp_at = impact.get("buying_power")
                if bp_at is not None and abs(float(ctx.buying_power) - float(bp_at)) / max(abs(float(bp_at)), 1.0) >= 0.10:
                    reasons.append("buying_power_changed")
                risk_now = ctx.risk_state.value if ctx.risk_state else None
                if risk_now and str(risk_now).upper() in {"HALTED", "CRITICAL"}:
                    reasons.append("risk_state_changed")
            if not reasons:
                continue
            item.status = type(item.status).EXPIRED if hasattr(type(item.status), "EXPIRED") else item.status
            from agentic_portfolio.live_approval.types import LiveApprovalStatus

            item.status = LiveApprovalStatus.EXPIRED
            self.approvals.store.save(item)
            superseded += 1
            self._notify(
                NotificationKind.APPROVAL_SUPERSEDED,
                title=f"Approval superseded — {item.ticker}",
                body=", ".join(reasons),
                payload={"approval_id": item.approval_id, "ticker": item.ticker, "reasons": reasons},
            )
        for item in expired:
            self._notify(
                NotificationKind.APPROVAL_EXPIRED,
                title=f"Approval expired — {item.ticker}",
                body="Pending approval expired before execution.",
                payload={"approval_id": item.approval_id, "ticker": item.ticker},
            )
        return {"expired": len(expired), "superseded": superseded}


def _inconclusive_retry_due(report: ResearchReport, entry: ResearchQueueEntry, now: datetime) -> bool:
    if report.research_status is not ResearchStatus.RESEARCH_INCONCLUSIVE and report.research_conclusion is not ResearchConclusion.NEED_MORE_DATA:
        return False
    cfg = load_research_config()
    hours = float((cfg.get("reassessment") or {}).get("need_more_data_min_retry_hours") or 24)
    raw = report.completed_at or report.started_at or entry.last_attempt_at or entry.enqueued_at
    if not raw:
        return True
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    if (now - stamp).total_seconds() >= hours * 3600:
        return True
    if report.freshness is ResearchFreshness.RESEARCH_REFRESH_REQUIRED:
        return True
    return False


def _queue_status_for(report: ResearchReport) -> ResearchQueueStatus:
    if report.research_conclusion is ResearchConclusion.REJECT:
        return ResearchQueueStatus.REJECTED
    if report.research_conclusion is ResearchConclusion.NEED_MORE_DATA or report.research_status is ResearchStatus.RESEARCH_INCONCLUSIVE:
        return ResearchQueueStatus.NEED_MORE_DATA if report.research_conclusion is ResearchConclusion.NEED_MORE_DATA else ResearchQueueStatus.INCONCLUSIVE
    return ResearchQueueStatus.COMPLETED
