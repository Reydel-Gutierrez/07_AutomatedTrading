"""Candidate lifecycle is monotonic. Rediscovery must not regress stable status."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_portfolio.discovery.engine import run_discovery
from agentic_portfolio.discovery.repair import repair_promoted_candidate_consistency
from agentic_portfolio.discovery.store import ACTIVE_QUEUE_STATUSES, CandidateStore, DiscoveryRunStore, ResearchQueue
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.types import (
    ResearchConclusion,
    ResearchConfidence,
    ResearchReport,
    ResearchStatus,
    ResearchSubjectKind,
)
from agentic_portfolio.runtime import RuntimeMode, discovery_state_dir
from agentic_portfolio.schemas import (
    CandidateStatus,
    DiscoveryPriority,
    MarketRegime,
    ResearchQueueEntry,
    ResearchQueueStatus,
    SecurityClass,
    Sleeve,
)
from agentic_portfolio.watch.store import WatchStore
from agentic_portfolio.watch.types import WatchItem, WatchStatus
from tests.conftest import ctx
from tests.test_discovery import _quality_core
from tests.test_research import _candidate

NOW = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
TS = NOW.isoformat()


def _stores(root: Path):
    d = discovery_state_dir(root, mode=RuntimeMode.LIVE)
    d.mkdir(parents=True, exist_ok=True)
    return (
        CandidateStore(d / "candidates.json", runtime_mode=RuntimeMode.LIVE.value),
        ResearchQueue(d / "research_queue.json", runtime_mode=RuntimeMode.LIVE.value),
        DiscoveryRunStore(d / "discovery_runs.json", runtime_mode=RuntimeMode.LIVE.value),
    )


def _put_candidate(store: CandidateStore, symbol: str, status: CandidateStatus, *, cid: str | None = None, price: float = 10.0, **kwargs) -> Candidate:
    cand = _candidate(symbol, cid=cid or f"cand-{symbol.lower()}", price=price)
    cand.status = status
    cand.priority = DiscoveryPriority.HIGH
    if status is CandidateStatus.REJECTED:
        cand.rejection_reason = kwargs.pop("rejection_reason", "luna_screen_rejected")
    for key, value in kwargs.items():
        setattr(cand, key, value)
    return store.upsert(cand, allow_status_transition=True)


def _put_queue(
    queue: ResearchQueue,
    cand: Candidate,
    status: ResearchQueueStatus,
    *,
    enqueued_at: datetime | None = None,
    skipped_reason: str | None = None,
) -> ResearchQueueEntry:
    stamp = enqueued_at or NOW
    entry = ResearchQueueEntry(
        queue_id=f"q-{cand.candidate_id}",
        candidate_id=cand.candidate_id,
        symbol=cand.symbol,
        provisional_sleeve=cand.provisional_sleeve,
        discovery_score=cand.discovery_score,
        priority=cand.priority,
        why_research_warranted="test",
        required_research_areas=["business_quality"],
        enqueued_at=stamp.isoformat(),
        freshness_deadline=(stamp + timedelta(hours=72)).isoformat(),
        status=status,
        skipped_reason=skipped_reason,
        last_attempt_at=stamp.isoformat(),
    )
    queue.enqueue(entry)
    if status is not ResearchQueueStatus.QUEUED:
        return queue.set_status(entry.queue_id, status, skipped_reason=skipped_reason, last_attempt_at=stamp.isoformat())
    return queue.get(entry.queue_id) or entry


def _rediscover(root: Path, symbol: str, *, now: datetime | None = None, price: float = 85.0):
    cstore, qstore, rstore = _stores(root)
    snap = _quality_core(symbol)
    snap.current_price = price
    return run_discovery(
        [snap],
        ctx(500),
        persist=True,
        promote_shortlist=True,
        now=now or NOW,
        candidate_store=cstore,
        queue_store=qstore,
        run_store=rstore,
        regime=MarketRegime.unknown(observed_at=(now or NOW).isoformat()),
    )


def _report(
    symbol: str,
    candidate_id: str,
    conclusion: ResearchConclusion,
    *,
    status: ResearchStatus = ResearchStatus.RESEARCH_COMPLETE,
) -> ResearchReport:
    return ResearchReport(
        research_id=f"r-{symbol.lower()}",
        candidate_id=candidate_id,
        symbol=symbol,
        started_at=TS,
        completed_at=TS,
        provisional_sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        research_status=status,
        subject_kind=ResearchSubjectKind.NEW_CANDIDATE,
        executive_summary=f"{symbol} canonical research outcome.",
        confidence=ResearchConfidence.MEDIUM,
        research_conclusion=conclusion,
        recommended_next_step=conclusion.value,
    )


def test_a_rejected_candidate_rediscovered_remains_rejected(tmp_path):
    cstore, qstore, _ = _stores(tmp_path)
    cand = _put_candidate(cstore, "JPM", CandidateStatus.REJECTED, price=10.0)
    _put_queue(qstore, cand, ResearchQueueStatus.REJECTED, skipped_reason="luna_screen_rejected")
    _rediscover(tmp_path, "JPM", price=185.0)
    rec = CandidateStore(cstore.path, runtime_mode=RuntimeMode.LIVE.value).current_for_symbol("JPM")
    assert rec is not None
    assert rec.candidate_id == cand.candidate_id
    assert rec.status is CandidateStatus.REJECTED
    assert rec.rejection_reason == "luna_screen_rejected"
    assert rec.current_price == 185.0
    assert not any(
        e.status in ACTIVE_QUEUE_STATUSES
        for e in ResearchQueue(qstore.path).all()
        if e.candidate_id == cand.candidate_id
    )


def test_b_watching_candidate_rediscovered_remains_watching(tmp_path):
    cstore, qstore, _ = _stores(tmp_path)
    cand = _put_candidate(cstore, "MSFT", CandidateStatus.WATCHING, price=10.0)
    _put_queue(qstore, cand, ResearchQueueStatus.COMPLETED)
    _rediscover(tmp_path, "MSFT", price=420.0)
    rec = CandidateStore(cstore.path, runtime_mode=RuntimeMode.LIVE.value).current_for_symbol("MSFT")
    assert rec.status is CandidateStatus.WATCHING
    assert rec.candidate_id == cand.candidate_id
    assert rec.current_price == 420.0
    assert not any(e.status in ACTIVE_QUEUE_STATUSES for e in ResearchQueue(qstore.path).all())


def test_c_research_complete_rediscovered_remains_stable(tmp_path):
    cstore, qstore, _ = _stores(tmp_path)
    cand = _put_candidate(cstore, "VTI", CandidateStatus.RESEARCH_COMPLETE, price=10.0)
    _put_queue(qstore, cand, ResearchQueueStatus.COMPLETED)
    incoming = _candidate("VTI", cid="ignored", price=250.0)
    incoming.status = CandidateStatus.PROMOTED_TO_RESEARCH
    saved = cstore.upsert(incoming)
    assert saved.status is CandidateStatus.RESEARCH_COMPLETE
    assert saved.current_price == 250.0
    _rediscover(tmp_path, "VTI", price=251.0)
    rec = CandidateStore(cstore.path, runtime_mode=RuntimeMode.LIVE.value).current_for_symbol("VTI")
    assert rec.status is CandidateStatus.RESEARCH_COMPLETE
    assert rec.current_price == 251.0
    assert cand.candidate_id == rec.candidate_id


def test_d_research_inconclusive_does_not_silently_regress(tmp_path):
    cstore, qstore, _ = _stores(tmp_path)
    cand = _put_candidate(cstore, "CRM", CandidateStatus.RESEARCH_INCONCLUSIVE, price=10.0)
    _put_queue(qstore, cand, ResearchQueueStatus.NEED_MORE_DATA)
    _rediscover(tmp_path, "CRM", now=NOW + timedelta(hours=1), price=90.0)
    rec = CandidateStore(cstore.path, runtime_mode=RuntimeMode.LIVE.value).current_for_symbol("CRM")
    assert rec.status is CandidateStatus.RESEARCH_INCONCLUSIVE
    assert rec.current_price == 90.0
    assert not any(e.status in ACTIVE_QUEUE_STATUSES for e in ResearchQueue(qstore.path).all())


def test_e_promoted_requires_active_queue_after_reconciliation(tmp_path):
    cstore, qstore, _ = _stores(tmp_path)
    cand = _put_candidate(cstore, "LLY", CandidateStatus.PROMOTED_TO_RESEARCH)
    _put_queue(qstore, cand, ResearchQueueStatus.COMPLETED)
    ResearchStore(tmp_path).save(_report("LLY", cand.candidate_id, ResearchConclusion.KEEP_WATCHING))
    WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE).save(
        WatchItem(
            watch_id="w-lly",
            ticker="LLY",
            created_at=TS,
            last_updated=TS,
            status=WatchStatus.WATCH,
            candidate_id=cand.candidate_id,
            research_id="r-lly",
            runtime_mode="LIVE",
        )
    )
    repair_promoted_candidate_consistency(
        root=tmp_path,
        candidates=cstore,
        queue=qstore,
        runtime_mode=RuntimeMode.LIVE,
        research_store=ResearchStore(tmp_path),
        watch_store=WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE),
    )
    rec = CandidateStore(cstore.path, runtime_mode=RuntimeMode.LIVE.value).current_for_symbol("LLY")
    assert rec.status is not CandidateStatus.PROMOTED_TO_RESEARCH
    if rec.status is CandidateStatus.PROMOTED_TO_RESEARCH:
        active = ResearchQueue(qstore.path).active_entry(symbol="LLY", candidate_id=cand.candidate_id)
        assert active is not None and active.status in ACTIVE_QUEUE_STATUSES


def test_f_explicit_legitimate_requeue_remains_possible(tmp_path):
    cstore, qstore, _ = _stores(tmp_path)
    watching = _put_candidate(cstore, "ANET", CandidateStatus.WATCHING)
    _put_queue(qstore, watching, ResearchQueueStatus.COMPLETED)
    fresh = ResearchQueueEntry(
        queue_id="q-anet-reopen",
        candidate_id=watching.candidate_id,
        symbol="ANET",
        provisional_sleeve=Sleeve.CORE_GROWTH,
        discovery_score=80.0,
        priority=DiscoveryPriority.HIGH,
        why_research_warranted="explicit_reopen",
        required_research_areas=["business_quality"],
        enqueued_at=(NOW + timedelta(hours=200)).isoformat(),
        freshness_deadline=(NOW + timedelta(hours=272)).isoformat(),
        status=ResearchQueueStatus.QUEUED,
        research_generation=2,
    )
    stored = qstore.enqueue(fresh)
    assert stored.status in ACTIVE_QUEUE_STATUSES
    reopened = cstore.reopen_for_research(watching.candidate_id, reason="explicit_reopen")
    assert reopened.status is CandidateStatus.PROMOTED_TO_RESEARCH

    inconclusive = _put_candidate(cstore, "SYK", CandidateStatus.RESEARCH_INCONCLUSIVE)
    _put_queue(qstore, inconclusive, ResearchQueueStatus.NEED_MORE_DATA, enqueued_at=NOW)
    later = _rediscover(tmp_path, "SYK", now=NOW + timedelta(hours=25), price=91.0)
    rec = CandidateStore(cstore.path, runtime_mode=RuntimeMode.LIVE.value).current_for_symbol("SYK")
    assert rec.status is CandidateStatus.PROMOTED_TO_RESEARCH
    assert later.queue
    assert any(
        e.status in ACTIVE_QUEUE_STATUSES and e.candidate_id == rec.candidate_id
        for e in ResearchQueue(qstore.path).all()
    )


def test_g_live_repair_fixes_terminal_queue_promoted_candidates(tmp_path):
    cstore, qstore, _ = _stores(tmp_path)
    research = ResearchStore(tmp_path)
    watches = WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE)

    jpm = _put_candidate(cstore, "JPM", CandidateStatus.PROMOTED_TO_RESEARCH)
    _put_queue(qstore, jpm, ResearchQueueStatus.REJECTED, skipped_reason="luna_screen_rejected")

    msft = _put_candidate(cstore, "MSFT", CandidateStatus.PROMOTED_TO_RESEARCH)
    _put_queue(qstore, msft, ResearchQueueStatus.COMPLETED)
    research.save(_report("MSFT", msft.candidate_id, ResearchConclusion.KEEP_WATCHING))
    watches.save(
        WatchItem(
            watch_id="w-msft",
            ticker="MSFT",
            created_at=TS,
            last_updated=TS,
            status=WatchStatus.WATCH,
            candidate_id=msft.candidate_id,
            research_id="r-msft",
            runtime_mode="LIVE",
        )
    )

    crm = _put_candidate(cstore, "CRM", CandidateStatus.PROMOTED_TO_RESEARCH)
    _put_queue(qstore, crm, ResearchQueueStatus.NEED_MORE_DATA)
    research.save(
        _report("CRM", crm.candidate_id, ResearchConclusion.NEED_MORE_DATA, status=ResearchStatus.RESEARCH_INCONCLUSIVE)
    )

    vti = _put_candidate(cstore, "VTI", CandidateStatus.PROMOTED_TO_RESEARCH)
    _put_queue(qstore, vti, ResearchQueueStatus.COMPLETED)
    research.save(_report("VTI", vti.candidate_id, ResearchConclusion.ADVANCE_TO_THESIS))
    watches.save(
        WatchItem(
            watch_id="w-vti",
            ticker="VTI",
            created_at=TS,
            last_updated=TS,
            status=WatchStatus.APPROVAL_REQUIRED,
            candidate_id=vti.candidate_id,
            research_id="r-vti",
            runtime_mode="LIVE",
        )
    )

    first = repair_promoted_candidate_consistency(
        root=tmp_path,
        candidates=cstore,
        queue=qstore,
        runtime_mode=RuntimeMode.LIVE,
        research_store=research,
        watch_store=watches,
    )
    second = repair_promoted_candidate_consistency(
        root=tmp_path,
        candidates=CandidateStore(cstore.path, runtime_mode=RuntimeMode.LIVE.value),
        queue=ResearchQueue(qstore.path, runtime_mode=RuntimeMode.LIVE.value),
        runtime_mode=RuntimeMode.LIVE,
        research_store=ResearchStore(tmp_path),
        watch_store=WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE),
    )
    reloaded = CandidateStore(cstore.path, runtime_mode=RuntimeMode.LIVE.value)
    assert reloaded.current_for_symbol("JPM").status is CandidateStatus.REJECTED
    assert reloaded.current_for_symbol("MSFT").status is CandidateStatus.WATCHING
    assert reloaded.current_for_symbol("CRM").status is CandidateStatus.RESEARCH_INCONCLUSIVE
    assert reloaded.current_for_symbol("VTI").status is CandidateStatus.RESEARCH_COMPLETE
    assert first.repaired == 4
    assert second.repaired == 0
    assert first.legacy_queue_consulted is False
    for rec in reloaded.all():
        if rec.status is CandidateStatus.PROMOTED_TO_RESEARCH:
            active = ResearchQueue(qstore.path).active_entry(symbol=rec.symbol, candidate_id=rec.candidate_id)
            assert active is not None


def test_h_legacy_state_queue_is_never_treated_as_live(tmp_path):
    cstore, qstore, _ = _stores(tmp_path)
    cand = _put_candidate(cstore, "GME", CandidateStatus.PROMOTED_TO_RESEARCH)
    _put_queue(qstore, cand, ResearchQueueStatus.COMPLETED)
    ResearchStore(tmp_path).save(_report("GME", cand.candidate_id, ResearchConclusion.KEEP_WATCHING))
    WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE).save(
        WatchItem(
            watch_id="w-gme",
            ticker="GME",
            created_at=TS,
            last_updated=TS,
            status=WatchStatus.WATCH,
            candidate_id=cand.candidate_id,
            research_id="r-gme",
            runtime_mode="LIVE",
        )
    )
    legacy = ResearchQueue(tmp_path / "state" / "research_queue.json", runtime_mode=RuntimeMode.LIVE.value)
    legacy.enqueue(
        ResearchQueueEntry(
            queue_id="legacy-gme",
            candidate_id=cand.candidate_id,
            symbol="GME",
            provisional_sleeve=Sleeve.CORE_GROWTH,
            discovery_score=80.0,
            priority=DiscoveryPriority.HIGH,
            why_research_warranted="legacy recovered queue",
            required_research_areas=["business_quality"],
            enqueued_at=NOW.isoformat(),
            freshness_deadline=(NOW + timedelta(hours=72)).isoformat(),
            status=ResearchQueueStatus.QUEUED,
        )
    )
    paper = repair_promoted_candidate_consistency(
        root=tmp_path,
        candidates=cstore,
        queue=qstore,
        runtime_mode=RuntimeMode.PAPER,
        research_store=ResearchStore(tmp_path),
        watch_store=WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE),
    )
    assert paper.skipped_not_live is True
    assert CandidateStore(cstore.path, runtime_mode=RuntimeMode.LIVE.value).current_for_symbol("GME").status is CandidateStatus.PROMOTED_TO_RESEARCH

    live = repair_promoted_candidate_consistency(
        root=tmp_path,
        candidates=cstore,
        queue=qstore,
        runtime_mode=RuntimeMode.LIVE,
        research_store=ResearchStore(tmp_path),
        watch_store=WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE),
    )
    rec = CandidateStore(cstore.path, runtime_mode=RuntimeMode.LIVE.value).current_for_symbol("GME")
    assert rec.status is CandidateStatus.WATCHING
    assert live.legacy_queue_consulted is False
    assert any(e.queue_id == "legacy-gme" and e.status is ResearchQueueStatus.QUEUED for e in legacy.all())
    assert not any(e.status in ACTIVE_QUEUE_STATUSES for e in ResearchQueue(qstore.path).all())
