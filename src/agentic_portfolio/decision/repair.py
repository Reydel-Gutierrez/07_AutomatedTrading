"""LIVE-only repair for historical ADVANCE_TO_THESIS rows missing a named decision.

Re-runs Portfolio Decision against existing FRESH research/thesis artifacts.
Never calls Terra/research collection. Never touches PAPER/legacy state.
Never forces BUY. Human approval remains mandatory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agentic_portfolio.decision.reasoner import DecisionReasoner
from agentic_portfolio.decision.store import DecisionStore
from agentic_portfolio.decision.types import CASH_SYMBOL
from agentic_portfolio.decision.validate import (
    NO_NAMED_DECISION,
    is_no_named_decision_reason,
)
from agentic_portfolio.discovery.store import CandidateStore, ResearchQueue
from agentic_portfolio.journal import append_jsonl, read_jsonl
from agentic_portfolio.lifecycle import lifecycle_path, log_lifecycle
from agentic_portfolio.research.operational import last_valid_investment_report, report_is_still_fresh
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.types import ResearchConclusion, ResearchReport
from agentic_portfolio.runtime import RuntimeMode, discovery_state_dir
from agentic_portfolio.watch.store import WatchStore
from agentic_portfolio.watch.types import WatchItem


REPAIR_REASON = "no_named_decision_repair"
LEDGER_NAME = "named_decision_repair.json"
# Documented historical victims. Production detection does not filter on this list.
KNOWN_AFFECTED_SYMBOLS = ("MA", "CRM", "MSFT", "ANET", "SPGI", "LLY", "SYK", "SOFI")


@dataclass
class NamedDecisionRepairResult:
    inspected: int = 0
    repaired: int = 0
    failed: int = 0
    skipped_already_repaired: int = 0
    skipped_not_fresh: int = 0
    skipped_not_live: bool = False
    paper_state_touched: bool = False
    terra_called: bool = False
    research_called: bool = False
    forced_buy: bool = False
    symbols: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "inspected": self.inspected,
            "repaired": self.repaired,
            "failed": self.failed,
            "skipped_already_repaired": self.skipped_already_repaired,
            "skipped_not_fresh": self.skipped_not_fresh,
            "skipped_not_live": self.skipped_not_live,
            "paper_state_touched": self.paper_state_touched,
            "terra_called": self.terra_called,
            "research_called": self.research_called,
            "forced_buy": self.forced_buy,
            "risk_gate_bypassed": False,
            "auto_execution": False,
            "symbols": list(self.symbols),
            "details": list(self.details),
        }


def repair_ledger_path(root: Path, *, runtime_mode: RuntimeMode | str) -> Path:
    mode = runtime_mode if isinstance(runtime_mode, RuntimeMode) else RuntimeMode(str(runtime_mode).upper())
    return discovery_state_dir(Path(root), mode=mode) / LEDGER_NAME


def load_repair_ledger(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            data.setdefault("repaired_research_ids", [])
            data.setdefault("by_symbol", {})
            return data
    return {"repaired_research_ids": [], "by_symbol": {}}


def save_repair_ledger(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def watch_has_no_named_decision(item: WatchItem | None) -> bool:
    if item is None:
        return False
    if is_no_named_decision_reason(item.reason_for_watch):
        return True
    return any(is_no_named_decision_reason(str(reason)) for reason in (item.reasons or []))


def detect_no_named_decision_symbols(
    *,
    root: Path,
    runtime_mode: RuntimeMode | str,
    candidates: CandidateStore,
    queue: ResearchQueue,
    watch_store: WatchStore,
    research_store: ResearchStore,
    decision_store: DecisionStore | None = None,
) -> list[str]:
    """Generic detection from persisted LIVE artifacts / `no_named_decision` reasons."""
    found: set[str] = set()
    for item in watch_store.all():
        if str(getattr(item, "runtime_mode", "") or "").upper() == RuntimeMode.PAPER.value:
            continue
        if getattr(item, "paper_environment", False) is True:
            continue
        if watch_has_no_named_decision(item):
            found.add(str(item.ticker).upper())
    for entry in queue.all():
        if is_no_named_decision_reason(entry.last_error) or is_no_named_decision_reason(entry.skipped_reason):
            found.add(str(entry.symbol).upper())
    for cand in candidates.all():
        if is_no_named_decision_reason(cand.rejection_reason):
            found.add(str(cand.symbol).upper())
        if any(is_no_named_decision_reason(str(reason)) for reason in (cand.reasons or [])):
            found.add(str(cand.symbol).upper())
    for row in read_jsonl(lifecycle_path(root)):
        if is_no_named_decision_reason(row.get("reason")):
            found.add(str(row.get("symbol") or "").upper())
    for journal in (
        Path(root) / "logs" / "thesis_decision.jsonl",
        Path(root) / "logs" / "pipeline.jsonl",
    ):
        for row in read_jsonl(journal):
            blob = " ".join(str(row.get(key) or "") for key in ("reason", "type", "skipped_reason"))
            if not is_no_named_decision_reason(blob) and NO_NAMED_DECISION not in json.dumps(row, default=str):
                continue
            for symbol in row.get("symbols") or []:
                found.add(str(symbol).upper())
            if row.get("symbol"):
                found.add(str(row.get("symbol")).upper())
    found.discard("")
    found.discard(CASH_SYMBOL)
    return sorted(found)


def repair_missing_named_decisions(
    *,
    root: Path,
    runtime_mode: RuntimeMode | str,
    decision_reasoner: DecisionReasoner | None = None,
    context_fn: Callable[[], Any] | None = None,
    now: datetime | None = None,
    persist: bool = True,
    journal: Path | None = None,
    watch=None,
    approvals=None,
    notify=None,
    payload_fn: Callable[..., Any] | None = None,
    research_reasoner: Any = None,
    fetcher: Any = None,
) -> NamedDecisionRepairResult:
    """Re-run PORTFOLIO DECISION ONLY for LIVE names stuck on `no_named_decision`."""
    result = NamedDecisionRepairResult()
    mode = runtime_mode if isinstance(runtime_mode, RuntimeMode) else RuntimeMode(str(runtime_mode).upper())
    if mode is not RuntimeMode.LIVE:
        result.skipped_not_live = True
        return result
    if payload_fn is not None or research_reasoner is not None or fetcher is not None:
        raise RuntimeError("named-decision repair must not collect research or call Terra")
    if decision_reasoner is None:
        raise RuntimeError("named-decision repair requires a portfolio decision reasoner")

    base = Path(root)
    live_dir = discovery_state_dir(base, mode=RuntimeMode.LIVE)
    candidates = CandidateStore(live_dir / "candidates.json", runtime_mode=RuntimeMode.LIVE.value)
    queue = ResearchQueue(live_dir / "research_queue.json", runtime_mode=RuntimeMode.LIVE.value)
    research_store = ResearchStore(base)
    decision_store = DecisionStore(base, runtime_mode=RuntimeMode.LIVE.value)
    watches = watch.store if watch is not None else WatchStore(base, runtime_mode=RuntimeMode.LIVE)
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    ledger_path = repair_ledger_path(base, runtime_mode=RuntimeMode.LIVE)
    ledger = load_repair_ledger(ledger_path)
    already = {str(rid) for rid in ledger.get("repaired_research_ids") or []}
    log_path = journal or (base / "logs" / "named_decision_repair.jsonl")

    from agentic_portfolio.agent.pipeline import ResearchQueueWorker, load_live_context
    from agentic_portfolio.live_approval import LiveApprovalEngine, LiveApprovalStore
    from agentic_portfolio.notify import NotificationEngine, NotificationStore
    from agentic_portfolio.watch.engine import WatchEngine

    context_provider = context_fn or (lambda: load_live_context(base, runtime_mode=RuntimeMode.LIVE))
    watch_engine = watch or WatchEngine(
        watches if isinstance(watches, WatchStore) else WatchStore(base, runtime_mode=RuntimeMode.LIVE),
        journal=base / "logs" / "agent.jsonl",
        now_fn=lambda: stamp,
    )
    approval_engine = approvals
    if approval_engine is None:
        approval_engine = LiveApprovalEngine(
            LiveApprovalStore(base, runtime_mode=RuntimeMode.LIVE),
            journal=base / "logs" / "approval.jsonl",
            now_fn=lambda: stamp,
        )
    notify_engine = notify or NotificationEngine(NotificationStore(base), now_fn=lambda: stamp)

    def _refuse_research(candidate):
        raise RuntimeError("named-decision repair must not collect research or call Terra")

    worker = ResearchQueueWorker(
        base,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=decision_reasoner,
        payload_fn=_refuse_research,
        context_fn=context_provider,
        watch=watch_engine,
        approvals=approval_engine,
        notify=notify_engine,
        now_fn=lambda: stamp,
    )

    symbols = detect_no_named_decision_symbols(
        root=base,
        runtime_mode=RuntimeMode.LIVE,
        candidates=candidates,
        queue=queue,
        watch_store=watch_engine.store,
        research_store=research_store,
        decision_store=decision_store,
    )
    context = context_provider()
    if context is None:
        result.details.append({"status": "BLOCKED", "reason": "missing_live_context"})
        return result

    for symbol in symbols:
        result.inspected += 1
        report = _fresh_advance_report(research_store, symbol, now=stamp)
        if report is None:
            result.skipped_not_fresh += 1
            result.details.append({"symbol": symbol, "status": "skipped_not_fresh"})
            continue
        if report.research_id in already or _already_has_named_decision(decision_store, symbol, report.research_id):
            result.skipped_already_repaired += 1
            result.details.append({"symbol": symbol, "status": "skipped_already_repaired", "research_id": report.research_id})
            continue
        candidate = candidates.active_for_symbol(symbol) or candidates.current_for_symbol(symbol)
        if candidate is None:
            result.failed += 1
            result.details.append({"symbol": symbol, "status": "missing_candidate"})
            continue
        if _pending_approval(approval_engine, symbol):
            result.skipped_already_repaired += 1
            result.details.append({"symbol": symbol, "status": "existing_pending_approval"})
            continue
        row = {
            "symbol": symbol,
            "status": "OK",
            "ai_calls": 0,
            "reports_created": 0,
            "watches_created": 0,
            "theses_created": 0,
            "proposals_created": 0,
            "rejected": 0,
        }
        applied = worker._decide(report, candidate, context, row)
        named = str(applied.get("decision") or "")
        failed = bool(applied.get("retry_queue") or applied.get("operational_failure") or applied.get("reason") == NO_NAMED_DECISION)
        if failed or is_no_named_decision_reason(applied.get("reason")):
            result.failed += 1
            detail = {
                "symbol": symbol,
                "status": "failed",
                "reason": applied.get("reason") or NO_NAMED_DECISION,
                "research_id": report.research_id,
                "forced_buy": False,
                "approval_id": None,
            }
            result.details.append(detail)
            if persist:
                _log_repair(log_path, base, symbol, report, applied, status="failed")
            continue
        entry = queue.latest_for_candidate_id(candidate.candidate_id) or queue.latest_for_symbol(symbol)
        if entry is not None and not applied.get("retry_queue"):
            worker._finalize_queue(
                entry,
                report,
                applied,
                report.evidence_fingerprint or "",
                duplicate=True,
            )
        result.repaired += 1
        result.symbols.append(symbol)
        already.add(report.research_id)
        ledger.setdefault("repaired_research_ids", []).append(report.research_id)
        ledger.setdefault("by_symbol", {})[symbol] = {
            "research_id": report.research_id,
            "decision": named or applied.get("reason"),
            "approval_id": applied.get("approval_id"),
            "repaired_at": stamp.isoformat(),
        }
        detail = {
            "symbol": symbol,
            "status": "repaired",
            "decision": named or None,
            "research_id": report.research_id,
            "watches_created": int(applied.get("watches_created") or 0),
            "theses_created": int(applied.get("theses_created") or 0),
            "proposals_created": int(applied.get("proposals_created") or 0),
            "approval_id": applied.get("approval_id"),
            "risk_verdict": applied.get("risk_verdict"),
            "forced_buy": False,
        }
        result.details.append(detail)
        if persist:
            _log_repair(log_path, base, symbol, report, applied, status="repaired")
    if persist:
        save_repair_ledger(ledger_path, ledger)
    return result


def _fresh_advance_report(store: ResearchStore, symbol: str, *, now: datetime) -> ResearchReport | None:
    valid = last_valid_investment_report(store.by_symbol(symbol)) or store.latest_valid_for_symbol(symbol)
    if valid is None:
        return None
    if valid.research_conclusion is not ResearchConclusion.ADVANCE_TO_THESIS:
        return None
    if not report_is_still_fresh(valid, now=now):
        return None
    return valid


def _already_has_named_decision(store: DecisionStore, symbol: str, research_id: str) -> bool:
    want = symbol.upper()
    for batch in store.all_runs():
        for item in batch.get("decisions") or []:
            if str(item.get("symbol") or "").upper() != want:
                continue
            rid = item.get("research_id")
            if rid and rid != research_id:
                continue
            return True
    return False


def _pending_approval(approvals, symbol: str) -> bool:
    if approvals is None:
        return False
    store = getattr(approvals, "store", None)
    if store is None or not hasattr(store, "pending"):
        return False
    return any(str(item.ticker).upper() == symbol.upper() for item in store.pending())


def _log_repair(
    path: Path,
    root: Path,
    symbol: str,
    report: ResearchReport,
    applied: dict[str, Any],
    *,
    status: str,
) -> None:
    append_jsonl(
        {
            "type": "NAMED_DECISION_REPAIR",
            "symbol": symbol,
            "status": status,
            "reason": applied.get("reason") or REPAIR_REASON,
            "research_id": report.research_id,
            "decision": applied.get("decision"),
            "approval_id": applied.get("approval_id"),
            "forced_buy": False,
            "terra_called": False,
            "research_called": False,
            "auto_execution": False,
        },
        path,
    )
    log_lifecycle(
        symbol=symbol,
        source="named_decision_repair",
        reason=str(applied.get("reason") or REPAIR_REASON),
        extra={
            "research_id": report.research_id,
            "status": status,
            "decision": applied.get("decision"),
            "approval_id": applied.get("approval_id"),
        },
        root=root,
    )

