"""LIVE-only repair for historical no_named_decision ADVANCE watches.

Never calls Terra/research. Never touches PAPER/legacy. Never forces BUY.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_portfolio.decision.reasoner import ScriptedDecisionReasoner
from agentic_portfolio.decision.repair import (
    KNOWN_AFFECTED_SYMBOLS,
    detect_no_named_decision_symbols,
    repair_missing_named_decisions,
)
from agentic_portfolio.discovery.store import CandidateStore, ResearchQueue
from agentic_portfolio.journal import read_jsonl
from agentic_portfolio.lifecycle import log_lifecycle
from agentic_portfolio.live_approval import LiveApprovalStatus
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.types import ResearchConclusion
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, RuntimeMode, discovery_state_dir
from agentic_portfolio.schemas import CandidateStatus, ResearchQueueStatus
from agentic_portfolio.watch.store import WatchStore
from agentic_portfolio.watch.types import WatchItem, WatchStatus
from tests.conftest import ctx
from tests.test_candidate_lifecycle import _put_candidate, _put_queue, _stores
from tests.test_decision import _cash_spy_only_payload, _payload, _report
from tests.test_production_pipeline import _services


NOW = datetime(2026, 9, 2, 14, 25, tzinfo=timezone.utc)
TS = NOW.isoformat()


class _BoomResearch:
    def reason(self, request):
        raise AssertionError("Terra/research must not be called")


def _fresh_report(symbol: str, candidate_id: str):
    report = _report(symbol, rid=f"res-{symbol}", conclusion=ResearchConclusion.ADVANCE_TO_THESIS)
    report.candidate_id = candidate_id
    report.stale_after = (NOW + timedelta(days=10)).isoformat()
    return report


def _watch(symbol: str, candidate_id: str, *, paper: bool = False) -> WatchItem:
    return WatchItem(
        watch_id=f"w-{symbol.lower()}-{'paper' if paper else 'live'}",
        ticker=symbol,
        created_at=TS,
        last_updated=TS,
        status=WatchStatus.WATCH,
        candidate_id=candidate_id,
        research_id=f"res-{symbol}",
        runtime_mode="PAPER" if paper else "LIVE",
        paper_environment=paper,
        reason_for_watch="no_named_decision",
        reasons=["no_named_decision"],
    )


def _seed_named_gap(root: Path, symbol: str, *, last_error: bool = True):
    cstore, qstore, _ = _stores(root)
    cand = _put_candidate(cstore, symbol, CandidateStatus.WATCHING)
    entry = _put_queue(qstore, cand, ResearchQueueStatus.COMPLETED, skipped_reason="no_named_decision")
    if last_error:
        qstore.set_status(entry.queue_id, ResearchQueueStatus.COMPLETED, last_error="no_named_decision", skipped_reason="no_named_decision")
    ResearchStore(root).save(_fresh_report(symbol, cand.candidate_id))
    WatchStore(root, runtime_mode=RuntimeMode.LIVE).save(_watch(symbol, cand.candidate_id))
    return cand


def test_paper_mode_is_a_no_op(tmp_path):
    _seed_named_gap(tmp_path, "MA")
    result = repair_missing_named_decisions(
        root=tmp_path,
        runtime_mode=RuntimeMode.PAPER,
        decision_reasoner=ScriptedDecisionReasoner(_payload("MA", decision="WATCH", alloc=0)),
        context_fn=lambda: ctx(500),
        now=NOW,
    )
    assert result.skipped_not_live is True
    assert result.repaired == 0
    assert result.terra_called is False
    item = WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE).by_ticker("MA")
    assert item is not None
    assert item.reason_for_watch == "no_named_decision"


def test_detection_is_generic_not_a_hardcoded_symbol_list(tmp_path):
    _seed_named_gap(tmp_path, "QUAL")
    log_lifecycle(symbol="QUAL", source="decision_error", reason="no_named_decision", root=tmp_path)
    live = discovery_state_dir(tmp_path, mode=RuntimeMode.LIVE)
    found = detect_no_named_decision_symbols(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        candidates=CandidateStore(live / "candidates.json", runtime_mode=RuntimeMode.LIVE.value),
        queue=ResearchQueue(live / "research_queue.json", runtime_mode=RuntimeMode.LIVE.value),
        watch_store=WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE),
        research_store=ResearchStore(tmp_path),
    )
    assert "QUAL" in found
    assert "QUAL" not in KNOWN_AFFECTED_SYMBOLS


def test_repair_named_watch_uses_existing_research_only(tmp_path):
    cand = _seed_named_gap(tmp_path, "MA")
    watch, approvals, notify = _services(tmp_path, now=NOW)
    result = repair_missing_named_decisions(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_payload("MA", decision="WATCH", alloc=0)),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert result.repaired == 1
    assert result.terra_called is False
    assert result.research_called is False
    assert result.forced_buy is False
    assert result.paper_state_touched is False
    item = watch.store.by_ticker("MA")
    assert item is not None
    assert item.reason_for_watch == "decision_watch"
    assert approvals.store.pending() == []
    assert len(ResearchStore(tmp_path).by_symbol("MA")) == 1
    second = repair_missing_named_decisions(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_payload("MA", decision="BUY", alloc=5.0)),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert second.skipped_already_repaired >= 1
    assert second.repaired == 0
    assert approvals.store.pending() == []
    assert watch.store.by_ticker("MA").reason_for_watch == "decision_watch"
    assert cand.candidate_id


def test_repair_named_buy_goes_to_risk_gate_not_placement(tmp_path):
    _seed_named_gap(tmp_path, "CRM")
    watch, approvals, notify = _services(tmp_path, now=NOW)
    result = repair_missing_named_decisions(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_payload("CRM", decision="BUY", alloc=5.0)),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert result.repaired == 1
    pending = approvals.store.pending()
    assert pending
    assert pending[0].proposed_action == "BUY"
    assert pending[0].status is LiveApprovalStatus.PENDING
    assert pending[0].placed_order is False
    assert LIVE_ORDER_PLACEMENT is False
    assert result.forced_buy is False


def test_repair_cash_only_stays_failed_closed(tmp_path):
    _seed_named_gap(tmp_path, "MSFT")
    watch, approvals, notify = _services(tmp_path, now=NOW)
    result = repair_missing_named_decisions(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_cash_spy_only_payload("MSFT", "CASH")),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert result.failed == 1
    assert result.repaired == 0
    assert approvals.store.pending() == []
    item = watch.store.by_ticker("MSFT")
    assert item is not None
    assert item.reason_for_watch == "no_named_decision"
    assert item.status is WatchStatus.WATCH


def test_repair_refuses_research_kwargs(tmp_path):
    _seed_named_gap(tmp_path, "ANET")
    with pytest.raises(RuntimeError, match="must not collect research"):
        repair_missing_named_decisions(
            root=tmp_path,
            runtime_mode=RuntimeMode.LIVE,
            decision_reasoner=ScriptedDecisionReasoner(_payload("ANET", decision="WATCH", alloc=0)),
            research_reasoner=_BoomResearch(),
            context_fn=lambda: ctx(500),
            now=NOW,
        )


def test_repair_does_not_touch_paper_or_legacy_state(tmp_path):
    _seed_named_gap(tmp_path, "LLY")
    paper_watch = WatchStore(tmp_path, runtime_mode=RuntimeMode.PAPER)
    paper_watch.save(_watch("GME", "cand-gme", paper=True))
    legacy = ResearchQueue(tmp_path / "state" / "research_queue.json", runtime_mode=RuntimeMode.PAPER.value)
    from agentic_portfolio.schemas import DiscoveryPriority, ResearchQueueEntry, Sleeve

    legacy.enqueue(
        ResearchQueueEntry(
            queue_id="legacy-gme",
            candidate_id="cand-gme",
            symbol="GME",
            provisional_sleeve=Sleeve.CORE_GROWTH,
            discovery_score=50.0,
            priority=DiscoveryPriority.LOW,
            why_research_warranted="legacy",
            required_research_areas=["business_quality"],
            enqueued_at=TS,
            freshness_deadline=(NOW + timedelta(hours=72)).isoformat(),
            status=ResearchQueueStatus.QUEUED,
            last_error="no_named_decision",
        )
    )
    paper_before = (tmp_path / "state" / "watch" / "items" / "w-gme-paper.json").read_text(encoding="utf-8")
    legacy_before = (tmp_path / "state" / "research_queue.json").read_text(encoding="utf-8")
    watch, approvals, notify = _services(tmp_path, now=NOW)
    repair_missing_named_decisions(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_payload("LLY", decision="WATCH", alloc=0)),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert (tmp_path / "state" / "watch" / "items" / "w-gme-paper.json").read_text(encoding="utf-8") == paper_before
    assert (tmp_path / "state" / "research_queue.json").read_text(encoding="utf-8") == legacy_before
    assert paper_watch.by_ticker("GME").reason_for_watch == "no_named_decision"
    assert watch.store.by_ticker("LLY").reason_for_watch == "decision_watch"


def test_repair_known_legacy_symbols_and_logs_each(tmp_path):
    for symbol in KNOWN_AFFECTED_SYMBOLS:
        _seed_named_gap(tmp_path, symbol)

    def _named_watch(request):
        symbol = request.reports[0]["symbol"]
        return _payload(symbol, decision="WATCH", alloc=0)

    watch, approvals, notify = _services(tmp_path, now=NOW)
    result = repair_missing_named_decisions(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_named_watch),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert result.repaired == len(KNOWN_AFFECTED_SYMBOLS)
    assert set(result.symbols) == set(KNOWN_AFFECTED_SYMBOLS)
    assert approvals.store.pending() == []
    rows = read_jsonl(tmp_path / "logs" / "named_decision_repair.jsonl")
    logged = {row["symbol"] for row in rows if row.get("type") == "NAMED_DECISION_REPAIR"}
    assert logged == set(KNOWN_AFFECTED_SYMBOLS)
    for symbol in KNOWN_AFFECTED_SYMBOLS:
        item = watch.store.by_ticker(symbol)
        assert item is not None
        assert item.reason_for_watch == "decision_watch"
        assert item.status is WatchStatus.WATCH
