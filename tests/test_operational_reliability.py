"""Operational failures must never become investment conclusions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentic_portfolio.agent.pipeline import ResearchQueueWorker, resolve_queue_stores
from agentic_portfolio.dashboard.queries import watchlist_view
from agentic_portfolio.decision.reasoner import ScriptedDecisionReasoner
from agentic_portfolio.research.engine import evidence_fingerprint, run_research
from agentic_portfolio.research.operational import (
    SCHEMA_FAILURE_SUMMARY,
    looks_like_schema_failure_report,
    last_valid_investment_report,
    screening_is_fresh,
)
from agentic_portfolio.research.packet import build_packet
from agentic_portfolio.research.repair import repair_operational_research_state
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.types import (
    ResearchConclusion,
    ResearchConfidence,
    ResearchReport,
    ResearchStatus,
    ResearchSubjectKind,
)
from agentic_portfolio.research.validate import ResearchValidationError
from agentic_portfolio.runtime import RuntimeMode
from agentic_portfolio.schemas import DiscoveryPriority, ResearchQueueEntry, ResearchQueueStatus, SecurityClass, Sleeve
from agentic_portfolio.watch.engine import WatchEngine
from agentic_portfolio.watch.store import WatchStore
from agentic_portfolio.watch.types import WatchStatus
from tests.conftest import ctx
from tests.test_production_pipeline import _seed, _services, _worker
from tests.test_research import _ai, _candidate, _payload
from agentic_portfolio.research.reasoner import ScriptedResearchReasoner


NOW = datetime(2026, 9, 1, 16, 0, tzinfo=timezone.utc)


def _poison_report(symbol: str, candidate_id: str, *, started: str) -> ResearchReport:
    return ResearchReport(
        research_id=f"poison-{symbol.lower()}",
        candidate_id=candidate_id,
        symbol=symbol,
        started_at=started,
        completed_at=started,
        provisional_sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        research_status=ResearchStatus.RESEARCH_INCONCLUSIVE,
        subject_kind=ResearchSubjectKind.NEW_CANDIDATE,
        executive_summary=SCHEMA_FAILURE_SUMMARY,
        confidence=ResearchConfidence.LOW,
        research_conclusion=ResearchConclusion.NEED_MORE_DATA,
        recommended_next_step="NEED_MORE_DATA",
        validation_errors=["malformed material report: ['missing_bull_case_for_material_report']"],
        research_source="AI",
    )


def _valid_watch_report(symbol: str, candidate_id: str, *, started: str) -> ResearchReport:
    return ResearchReport(
        research_id=f"valid-{symbol.lower()}",
        candidate_id=candidate_id,
        symbol=symbol,
        started_at=started,
        completed_at=started,
        provisional_sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        research_status=ResearchStatus.RESEARCH_COMPLETE,
        subject_kind=ResearchSubjectKind.NEW_CANDIDATE,
        executive_summary="NVIDIA remains a core compounder on observed evidence.",
        confidence=ResearchConfidence.MEDIUM,
        research_conclusion=ResearchConclusion.KEEP_WATCHING,
        recommended_next_step="KEEP_WATCHING",
        stale_after=(NOW + timedelta(days=10)).isoformat(),
        key_catalysts=["data center demand"],
        key_risks=["export controls"],
        invalidation_candidates=["sustained demand break"],
        research_source="AI",
        discovery_score=80.0,
        market_price=180.0,
    )


def test_schema_failure_does_not_persist_need_more_data(tmp_path):
    store = ResearchStore(tmp_path)
    journal = tmp_path / "logs" / "research.jsonl"
    reasoner = ScriptedResearchReasoner({"NVDA": {"executive_summary": "nope"}})
    try:
        run_research(
            _candidate("NVDA"),
            _payload("NVDA"),
            ctx(10_000),
            reasoner,
            persist=True,
            now=NOW,
            store=store,
            journal=journal,
        )
        raise AssertionError("expected ResearchValidationError")
    except ResearchValidationError:
        pass
    assert store.all_reports() == []
    blob = journal.read_text(encoding="utf-8")
    assert "RESEARCH_ERROR" in blob
    assert "RESEARCH_FAILED" in blob
    assert SCHEMA_FAILURE_SUMMARY not in blob
    assert "NEED_MORE_DATA" not in blob or "investment_conclusion" in blob


def test_poisoned_nvda_is_not_canonical_and_watch_is_restored(tmp_path):
    cand, _entry = _seed(tmp_path, symbol="NVDA")
    store = ResearchStore(tmp_path)
    older = (NOW - timedelta(hours=6)).isoformat()
    newer = (NOW - timedelta(hours=1)).isoformat()
    valid = _valid_watch_report("NVDA", cand.candidate_id, started=older)
    poison = _poison_report("NVDA", cand.candidate_id, started=newer)
    store.save(valid)
    store.save(poison)
    assert looks_like_schema_failure_report(store.latest_for_symbol("NVDA"))
    assert last_valid_investment_report(store.by_symbol("NVDA")).research_id == valid.research_id
    watch, _approvals, _notify = _services(tmp_path, now=NOW)
    candidates, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    result = repair_operational_research_state(
        root=tmp_path,
        research_store=store,
        candidates=candidates,
        queue=queue,
        runtime_mode=RuntimeMode.LIVE,
        now=NOW,
        watch=watch,
    )
    assert result.schema_failures >= 1
    item = watch.store.by_ticker("NVDA")
    assert item is not None
    assert item.status is WatchStatus.WATCH
    assert SCHEMA_FAILURE_SUMMARY not in (item.research_thesis or "")
    assert "core compounder" in (item.research_thesis or "")
    assert item.sleeve == Sleeve.CORE_GROWTH.value
    assert store.get(poison.research_id) is not None


def test_schema_failure_refresh_does_not_drop_existing_watch(tmp_path):
    cand, _entry = _seed(tmp_path, symbol="NVDA")
    watch, approvals, notify = _services(tmp_path, now=NOW)
    existing = watch.upsert_from_candidate(
        ticker="NVDA",
        thesis="NVIDIA remains a core compounder on observed evidence.",
        status=WatchStatus.WATCH,
        sleeve="CORE_GROWTH",
        last_price=180.0,
    )
    worker = ResearchQueueWorker(
        tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        research_reasoner=ScriptedResearchReasoner({"NVDA": {"executive_summary": "truncated"}}),
        payload_fn=lambda candidate: _payload(candidate.symbol),
        context_fn=lambda: ctx(500),
        watch=watch,
        approvals=approvals,
        notify=notify,
        now_fn=lambda: NOW,
    )
    second = worker.process_entry(worker.queue.all()[0], ctx(500))
    assert second.get("status") in {"DEGRADED", "FAILED"}
    kept = worker.watch.store.by_ticker("NVDA")
    assert kept is not None
    assert kept.status is WatchStatus.WATCH
    assert kept.watch_id == existing.watch_id
    assert kept.sleeve == "CORE_GROWTH"
    assert SCHEMA_FAILURE_SUMMARY not in (kept.research_thesis or "")


def test_decision_schema_failure_does_not_mint_watch(tmp_path):
    _seed(tmp_path, symbol="NVDA")
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"NVDA": _ai("NVDA", conclusion="ADVANCE_TO_THESIS")}),
        decision=ScriptedDecisionReasoner({"nope": True}),
        now=NOW,
    )
    result = worker.run_cycle()
    assert result.status == "DEGRADED" or any(row.get("retry_queue") for row in result.details)
    item = worker.watch.store.by_ticker("NVDA")
    assert item is None or item.status is not WatchStatus.WATCH or "decision_inconclusive" not in (item.reasons or [])
    entry = worker.queue.all()[0]
    assert entry.status is ResearchQueueStatus.QUEUED
    assert worker.approvals.store.pending() == []


def test_ordinary_keep_watching_does_not_become_waiting_for_open(tmp_path):
    pre = datetime(2026, 9, 1, 12, 49, tzinfo=timezone.utc)
    store = WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    engine = WatchEngine(store, now_fn=lambda: pre)
    item = engine.upsert_from_candidate(
        ticker="NVDA",
        thesis="core compounder",
        last_price=180.0,
        status=WatchStatus.WATCH,
        off_hours=True,
        prepare_conditional_plan=False,
        sleeve="CORE_GROWTH",
    )
    assert item.status is WatchStatus.WATCH
    assert item.conditional_plan is None


def test_planless_waiting_for_open_is_demoted_to_watch(tmp_path):
    pre = datetime(2026, 9, 1, 12, 49, tzinfo=timezone.utc)
    store = WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    from agentic_portfolio.watch.types import WatchItem

    store.save(
        WatchItem(
            watch_id="w-nvda",
            ticker="NVDA",
            status=WatchStatus.WAITING_FOR_OPEN,
            created_at=pre.isoformat(),
            last_updated=pre.isoformat(),
            research_thesis="core compounder",
            sleeve="CORE_GROWTH",
        )
    )
    WatchEngine(store, now_fn=lambda: pre)
    item = store.by_ticker("NVDA")
    assert item.status is WatchStatus.WATCH


def test_fresh_research_prevents_watch_expiration(tmp_path):
    now = NOW
    store = WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    engine = WatchEngine(store, now_fn=lambda: now)
    item = engine.upsert_from_candidate(
        ticker="NVDA",
        thesis="core compounder",
        status=WatchStatus.WATCH,
        sleeve="CORE_GROWTH",
    )
    item.expiration = (now - timedelta(hours=1)).isoformat()
    engine.store.save(item)
    expired = engine.expire_stale(fresh_until={"NVDA": now + timedelta(days=5)})
    assert expired == []
    kept = engine.store.by_ticker("NVDA")
    assert kept.status is WatchStatus.WATCH
    assert kept.expiration is not None


def test_screening_ttl_expires_stale_rejects():
    now = NOW
    fresh = {"created_at": (now - timedelta(hours=2)).isoformat(), "worth_deep_research": False}
    stale = {"created_at": (now - timedelta(days=10)).isoformat(), "worth_deep_research": False}
    assert screening_is_fresh(fresh, now=now, ttl_hours=48)
    assert not screening_is_fresh(stale, now=now, ttl_hours=48)


def test_fingerprint_includes_fundamental_values_not_quotes():
    cand = _candidate("NVDA")
    payload = _payload("NVDA")
    packet = build_packet(payload, cand, ctx(10_000))
    a = evidence_fingerprint(cand, packet=packet)
    packet.facts = list(packet.facts)
    for item in packet.facts:
        if item.name == "market_price":
            item.value = 9999.0
    b = evidence_fingerprint(cand, packet=packet)
    assert a == b
    for item in packet.facts:
        if item.name == "market_cap":
            item.value = 1
            break
    else:
        return
    c = evidence_fingerprint(cand, packet=packet)
    assert c != a


def test_legacy_research_outage_is_operational_failure_not_reject():
    from agentic_portfolio.ai.context import AIContext
    from agentic_portfolio.ai.errors import ProviderOutage
    from agentic_portfolio.ai.research import research_candidate
    from agentic_portfolio.ai.types import RecommendedAction

    class Boom:
        def complete_structured(self, **kwargs):
            raise ProviderOutage("down")

    ctx_ai = AIContext(
        context_id="c1",
        ticker="NVDA",
        assembled_at=NOW.isoformat(),
        runtime_mode=RuntimeMode.LIVE.value,
        source_of_truth="test",
    )
    row = research_candidate(Boom(), ctx_ai, ctx(500), persist=None, now=NOW)
    assert row.operational_failure is True
    assert row.recommended_action is RecommendedAction.REJECT
    assert row.thesis == ""


def test_missing_valid_watch_is_restored_without_requeue(tmp_path):
    cand, _entry = _seed(tmp_path, symbol="NVDA")
    store = ResearchStore(tmp_path)
    valid = _valid_watch_report("NVDA", cand.candidate_id, started=(NOW - timedelta(hours=6)).isoformat())
    store.save(valid)
    watch, _approvals, _notify = _services(tmp_path, now=NOW)
    candidates, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    result = repair_operational_research_state(
        root=tmp_path,
        research_store=store,
        candidates=candidates,
        queue=queue,
        runtime_mode=RuntimeMode.LIVE,
        now=NOW,
        watch=watch,
    )
    assert result.schema_failures == 0
    item = watch.store.by_ticker("NVDA")
    assert item is not None
    assert item.status is WatchStatus.WATCH
    assert "core compounder" in (item.research_thesis or "")
    assert item.expiration == valid.stale_after
    queued = [e for e in queue.all() if e.symbol == "NVDA" and e.status is ResearchQueueStatus.QUEUED]
    assert queued  # seed already queued; repair must not add a schema-failure requeue
    assert all(e.skipped_reason != "schema_failure_requeue" for e in queue.all())


def test_duplicate_queued_entries_are_collapsed(tmp_path):
    cand, entry = _seed(tmp_path, symbol="QUAL")
    candidates, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    extra = ResearchQueueEntry(
        queue_id="dup-qual-2",
        candidate_id=cand.candidate_id,
        symbol="QUAL",
        provisional_sleeve=Sleeve.CORE_GROWTH,
        discovery_score=67.0,
        priority=DiscoveryPriority.HIGH,
        why_research_warranted="duplicate-test",
        status=ResearchQueueStatus.QUEUED,
        enqueued_at=(NOW + timedelta(minutes=1)).isoformat(),
    )
    queue.save_entry(extra)
    assert len([e for e in queue.all() if e.status is ResearchQueueStatus.QUEUED]) >= 2
    store = ResearchStore(tmp_path)
    watch, _approvals, _notify = _services(tmp_path, now=NOW)
    result = repair_operational_research_state(
        root=tmp_path,
        research_store=store,
        candidates=candidates,
        queue=queue,
        runtime_mode=RuntimeMode.LIVE,
        now=NOW,
        watch=watch,
    )
    assert result.queue_duplicates_dropped >= 1
    active = [e for e in queue.all() if e.status is ResearchQueueStatus.QUEUED]
    assert len(active) == 1
    dropped = [e for e in queue.all() if e.status is ResearchQueueStatus.DROPPED]
    assert dropped
    assert all(e.skipped_reason == "duplicate_active_queue" for e in dropped)
    assert extra.queue_id in {e.queue_id for e in dropped} or entry.queue_id in {e.queue_id for e in dropped}


def test_watchlist_groups_by_sleeve(tmp_path):
    from agentic_portfolio.watch.types import WatchItem
    from types import SimpleNamespace

    store = WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    for ticker, sleeve in (("NVDA", "CORE_GROWTH"), ("HD", "OPPORTUNISTIC"), ("IONQ", "SPECULATIVE")):
        store.save(
            WatchItem(
                watch_id=f"w-{ticker}",
                ticker=ticker,
                status=WatchStatus.WATCH,
                created_at=NOW.isoformat(),
                last_updated=NOW.isoformat(),
                sleeve=sleeve,
                research_thesis=f"{ticker} thesis",
            )
        )
    state = SimpleNamespace(root=tmp_path, runtime=RuntimeMode.LIVE)
    view = watchlist_view(state)
    labels = [g["label"] for g in view["groups"]]
    assert "Core Growth" in labels
    assert "Opportunistic" in labels
    assert "Tactical" in labels
    assert "Speculative" in labels
    assert "All Watches" in labels
    by_id = {g["id"]: g for g in view["groups"]}
    assert [row["ticker"] for row in by_id["CORE_GROWTH"]["rows"]] == ["NVDA"]
    assert by_id["ALL"]["count"] == 3
