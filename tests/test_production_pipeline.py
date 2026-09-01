"""Production research-queue pipeline: Candidate → Report → Thesis → Decision → RiskGate → Approval.

Never places a live order. Uses scripted reasoners / ScriptedProvider only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_portfolio.agent.handlers import AgentServices, build_handlers
from agentic_portfolio.agent.jobs import specs_by_name
from agentic_portfolio.agent.orchestrator import JobOrchestrator
from agentic_portfolio.agent.pipeline import (
    ResearchQueueWorker,
    inspect_research_queues,
    primary_queue_stores,
    resolve_queue_stores,
)
from agentic_portfolio.agent.safety import inspect_agent_packages_for_forbidden_calls
from agentic_portfolio.ai.budget import BudgetManager
from agentic_portfolio.ai.config import load_ai_config
from agentic_portfolio.ai.errors import BudgetExhausted
from agentic_portfolio.ai.gateway import AIGateway, build_gateway
from agentic_portfolio.ai.ledger import UsageLedger
from agentic_portfolio.ai.providers.anthropic import AnthropicProvider
from agentic_portfolio.ai.providers.openai import OpenAIProvider
from agentic_portfolio.ai.providers.scripted import ScriptedProvider
from agentic_portfolio.ai.reasoners import GatewayResearchReasoner
from agentic_portfolio.ai.types import BudgetMode
from agentic_portfolio.dashboard.queries import dashboard_state, dashboard_view, research_view
from agentic_portfolio.decision.reasoner import ScriptedDecisionReasoner
from agentic_portfolio.discovery.freshness import normalize_queue_freshness
from agentic_portfolio.discovery.safety import candidate_cannot_become_buy
from agentic_portfolio.discovery.store import CandidateStore, ResearchQueue
from agentic_portfolio.live.store import LivePortfolioStore
from agentic_portfolio.live_approval import LiveApprovalEngine, LiveApprovalStatus, LiveApprovalStore
from agentic_portfolio.notify import NotificationEngine, NotificationKind, NotificationStore
from agentic_portfolio.research.packet import ResearchPayload
from agentic_portfolio.research.reasoner import ScriptedResearchReasoner
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.types import (
    ResearchConclusion,
    ResearchConfidence,
    ResearchReport,
    ResearchStatus,
    ResearchSubjectKind,
)
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, RuntimeMode, discovery_state_dir
from agentic_portfolio.schemas import (
    CandidateStatus,
    Decision,
    DiscoveryPriority,
    ResearchQueueEntry,
    ResearchQueueStatus,
    RiskState,
    SecurityClass,
    Sleeve,
    ThesisStatus,
    to_dict,
)
from agentic_portfolio.watch import WatchEngine, WatchStatus, WatchStore
from tests.conftest import ctx
from tests.test_ai_gateway import SCREEN
from tests.test_decision import _payload as _decision_payload
from tests.test_research import _ai, _candidate, _etf_payload, _payload

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def _live_book(root: Path, nav: float = 500.0) -> None:
    store = LivePortfolioStore(root)
    context = ctx(nav)
    store.current_path().write_text(
        json.dumps({"context": to_dict(context), "created_at": NOW.isoformat(), "source_of_truth": "live_robinhood_account"}, indent=2, default=str),
        encoding="utf-8",
    )


def _seed(root: Path, symbol="QUAL", *, sleeve=Sleeve.CORE_GROWTH, score=72.0, cid=None, **candidate_kwargs):
    cand = _candidate(symbol, sleeve=sleeve, score=score, cid=cid, **candidate_kwargs)
    cand.status = CandidateStatus.PROMOTED_TO_RESEARCH
    cand.priority = DiscoveryPriority.HIGH
    d = discovery_state_dir(root, mode=RuntimeMode.LIVE)
    d.mkdir(parents=True, exist_ok=True)
    candidates = CandidateStore(d / "candidates.json", runtime_mode=RuntimeMode.LIVE.value)
    queue = ResearchQueue(d / "research_queue.json", runtime_mode=RuntimeMode.LIVE.value)
    candidates.upsert(cand)
    entry = ResearchQueueEntry(
        queue_id=f"q-{cand.candidate_id}",
        candidate_id=cand.candidate_id,
        symbol=cand.symbol,
        provisional_sleeve=cand.provisional_sleeve,
        discovery_score=cand.discovery_score,
        priority=cand.priority,
        why_research_warranted="Promoted by discovery.",
        required_research_areas=["business_quality"],
        enqueued_at=NOW.isoformat(),
        freshness_deadline=(NOW + timedelta(hours=72)).isoformat(),
        status=ResearchQueueStatus.QUEUED,
    )
    queue.enqueue(entry)
    return cand, queue.get(entry.queue_id)


def _services(root: Path, *, now=None):
    clock = now or NOW
    watch_store = WatchStore(root, runtime_mode=RuntimeMode.LIVE)
    approval_store = LiveApprovalStore(root, runtime_mode=RuntimeMode.LIVE)
    notify = NotificationEngine(NotificationStore(root), now_fn=lambda: clock)
    return (
        WatchEngine(watch_store, journal=root / "logs" / "agent.jsonl", now_fn=lambda: clock),
        LiveApprovalEngine(approval_store, journal=root / "logs" / "approval.jsonl", now_fn=lambda: clock),
        notify,
    )


def _worker(
    root: Path,
    *,
    research=None,
    decision=None,
    gateway=None,
    nav=500.0,
    halted=False,
    now=None,
):
    clock = now or NOW
    watch, approvals, notify = _services(root, now=clock)
    context = ctx(nav)
    if halted:
        context.risk_state = RiskState.HALTED

    def payload_fn(candidate):
        return _payload(candidate.symbol)

    return ResearchQueueWorker(
        root,
        runtime_mode=RuntimeMode.LIVE,
        gateway=gateway,
        research_reasoner=research,
        decision_reasoner=decision,
        payload_fn=payload_fn,
        context_fn=lambda: context,
        watch=watch,
        approvals=approvals,
        notify=notify,
        now_fn=lambda: clock,
    )


def _buy_reasoners(symbol="QUAL"):
    return ScriptedResearchReasoner({symbol: _ai(symbol, conclusion="ADVANCE_TO_THESIS")}), ScriptedDecisionReasoner(
        _decision_payload(symbol, decision="BUY", alloc=5.0)
    )


def test_a_promoted_candidate_enters_research_queue(tmp_path):
    cand, entry = _seed(tmp_path)
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    found = queue.get(entry.queue_id)
    assert found is not None
    assert found.status is ResearchQueueStatus.QUEUED
    assert found.symbol == cand.symbol
    assert found.candidate_id == cand.candidate_id


def test_b_queued_candidate_is_consumed(tmp_path):
    _seed(tmp_path)
    research, decision = _buy_reasoners()
    result = _worker(tmp_path, research=research, decision=decision).run_cycle()
    assert result.items_processed == 1
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    assert queue.all()[0].status is ResearchQueueStatus.COMPLETED


def test_c_ai_research_goes_through_gateway(tmp_path):
    _seed(tmp_path, symbol="QUAL")
    scripted = ScriptedProvider(
        {
            "screening:QUAL": {**SCREEN, "ticker": "QUAL"},
            "research_report:QUAL": _ai("QUAL", conclusion="KEEP_WATCHING"),
        },
        name="openai",
    )
    gw = build_gateway(tmp_path, providers={"openai": scripted}, runtime_mode=RuntimeMode.LIVE, now_fn=lambda: NOW)
    worker = _worker(tmp_path, gateway=gw, decision=ScriptedDecisionReasoner(_decision_payload("QUAL", decision="WATCH", alloc=0)))
    result = worker.run_cycle()
    assert result.ai_calls >= 2
    assert scripted.calls
    assert [c.schema_name for c in scripted.calls] == ["screening", "research_report"]
    assert scripted.calls[0].purpose == "candidate_screening"
    assert scripted.calls[1].purpose == "deep_research"
    assert worker.ai_store.screenings()


def test_d_budget_ledger_is_charged(tmp_path):
    _seed(tmp_path)
    scripted = ScriptedProvider(
        {
            "screening:QUAL": {**SCREEN, "ticker": "QUAL"},
            "research_report:QUAL": _ai("QUAL", conclusion="REJECT"),
        },
        name="openai",
    )
    gw = build_gateway(tmp_path, providers={"openai": scripted}, runtime_mode=RuntimeMode.LIVE, now_fn=lambda: NOW)
    before = gw.budget.status().spent
    _worker(tmp_path, gateway=gw).run_cycle()
    after = gw.budget.status().spent
    assert after > before
    assert after <= Decimal("10")


def test_e_budget_cap_prevents_further_calls(tmp_path):
    _seed(tmp_path)
    cfg = load_ai_config()
    ledger = UsageLedger(tmp_path, config=cfg)
    data = ledger.load_month(now=NOW)
    data["spent"] = "10.00"
    ledger.save_month(data)
    scripted = ScriptedProvider({"*": _ai("QUAL")}, name="openai")
    gw = AIGateway(budget=BudgetManager(ledger, cfg, now_fn=lambda: NOW), providers={"openai": scripted}, config=cfg, runtime_mode=RuntimeMode.LIVE.value)
    assert gw.budget.status().mode is BudgetMode.EXHAUSTED
    result = _worker(tmp_path, gateway=gw).run_cycle()
    assert result.status == "BLOCKED"
    assert result.skipped_reason == "budget_exhausted"
    assert scripted.calls == []
    with __import__("pytest").raises(BudgetExhausted):
        gw.complete_structured(role="research", purpose="deep_research", schema_name="research_report", messages=[{"role": "user", "content": "x"}], ticker="QUAL")


def test_f_restart_does_not_reset_spending(tmp_path):
    cfg = load_ai_config()
    ledger = UsageLedger(tmp_path, config=cfg)
    data = ledger.load_month(now=NOW)
    data["spent"] = "6.25"
    ledger.save_month(data)
    restarted = BudgetManager(UsageLedger(tmp_path, config=cfg), cfg, now_fn=lambda: NOW)
    assert restarted.status().spent == Decimal("6.25")
    assert restarted.status().remaining == Decimal("3.75")


def test_g_successful_research_creates_report(tmp_path):
    _seed(tmp_path)
    research, decision = _buy_reasoners()
    worker = _worker(tmp_path, research=research, decision=decision)
    worker.run_cycle()
    reports = worker.research_store.by_symbol("QUAL")
    assert reports
    assert reports[0].research_status is ResearchStatus.RESEARCH_COMPLETE


def test_h_poor_research_becomes_reject(tmp_path):
    _seed(tmp_path)
    worker = _worker(tmp_path, research=ScriptedResearchReasoner({"QUAL": _ai("QUAL", conclusion="REJECT")}))
    result = worker.run_cycle()
    assert result.rejections == 1
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    assert queue.all()[0].status is ResearchQueueStatus.REJECTED
    assert worker.approvals.store.pending() == []


def test_i_incomplete_evidence_need_more_data(tmp_path):
    _seed(tmp_path)
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"QUAL": _ai("QUAL", conclusion="NEED_MORE_DATA", missing=["get_financials"])}),
    )
    worker.payload_fn = lambda candidate: _payload(candidate.symbol, rich=False)
    result = worker.run_cycle()
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    status = queue.all()[0].status
    assert status in {ResearchQueueStatus.NEED_MORE_DATA, ResearchQueueStatus.INCONCLUSIVE}
    assert worker.approvals.store.pending() == []
    assert result.proposals_created == 0


def test_j_valid_research_creates_draft_thesis(tmp_path):
    _seed(tmp_path)
    research, decision = _buy_reasoners()
    worker = _worker(tmp_path, research=research, decision=decision)
    result = worker.run_cycle()
    assert result.theses_created >= 1
    theses = worker.theses.all_records()
    assert theses
    assert theses[0].status is ThesisStatus.DRAFT


def test_k_watch_creates_persistent_watch_item(tmp_path):
    _seed(tmp_path)
    worker = _worker(tmp_path, research=ScriptedResearchReasoner({"QUAL": _ai("QUAL", conclusion="KEEP_WATCHING")}))
    result = worker.run_cycle()
    assert result.watches_created == 1
    item = worker.watch.store.by_ticker("QUAL")
    assert item is not None
    assert item.status in {WatchStatus.WATCH, WatchStatus.WAITING_FOR_OPEN}
    assert item.research_thesis


def test_keep_watching_waiting_for_open_schedules_next_regular_open(tmp_path):
    from datetime import date, time

    from agentic_portfolio.calendar import EASTERN
    from agentic_portfolio.watch.types import parse_iso

    pre = datetime(2026, 9, 1, 12, 49, tzinfo=timezone.utc)  # 08:49 ET Tuesday
    _seed(tmp_path, symbol="HD", sleeve=Sleeve.OPPORTUNISTIC)
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"HD": _ai("HD", conclusion="KEEP_WATCHING")}),
        now=pre,
    )
    result = worker.run_cycle()
    assert result.watches_created == 1
    item = worker.watch.store.by_ticker("HD")
    assert item is not None
    assert item.status is WatchStatus.WAITING_FOR_OPEN
    nxt = parse_iso(item.next_review_at)
    assert nxt is not None
    local = nxt.astimezone(EASTERN)
    assert local.date() == date(2026, 9, 1)
    assert local.time() == time(9, 30)
    # Opportunistic WATCH interval is 48h and must not apply here.
    assert local.date() != date(2026, 9, 3)


def test_keep_watching_during_rth_uses_sleeve_watch_interval(tmp_path):
    from agentic_portfolio.watch.types import parse_iso

    rth = datetime(2026, 9, 1, 14, 30, tzinfo=timezone.utc)  # 10:30 ET Tuesday
    _seed(tmp_path, symbol="HD", sleeve=Sleeve.OPPORTUNISTIC)
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"HD": _ai("HD", conclusion="KEEP_WATCHING")}),
        now=rth,
    )
    result = worker.run_cycle()
    assert result.watches_created == 1
    item = worker.watch.store.by_ticker("HD")
    assert item is not None
    assert item.status is WatchStatus.WATCH
    nxt = parse_iso(item.next_review_at)
    assert nxt is not None
    hours = (nxt - rth).total_seconds() / 3600.0
    assert 40.0 <= hours <= 56.0


def test_l_buy_creates_proposed_action_not_direct_order(tmp_path):
    _seed(tmp_path)
    research, decision = _buy_reasoners()
    worker = _worker(tmp_path, research=research, decision=decision)
    result = worker.run_cycle()
    assert result.proposals_created == 1
    pending = worker.approvals.store.pending()
    assert pending
    assert pending[0].proposed_action == "BUY"
    assert pending[0].placed_order is False
    assert pending[0].broker_submitted is False
    journal = (tmp_path / "logs").rglob("*.jsonl")
    blob = "".join(p.read_text(encoding="utf-8") for p in journal if p.exists())
    assert "place_equity_order" not in blob


def test_m_risk_gate_denial_prevents_approval(tmp_path):
    _seed(tmp_path)
    research, _ = _buy_reasoners()
    worker = _worker(tmp_path, research=research, decision=ScriptedDecisionReasoner(_decision_payload("QUAL", decision="BUY", alloc=5.0)), halted=True)
    result = worker.run_cycle()
    assert result.proposals_created == 0
    assert worker.approvals.store.pending() == []


def test_n_risk_gate_permit_creates_approval_packet(tmp_path):
    _seed(tmp_path)
    research, decision = _buy_reasoners()
    result = _worker(tmp_path, research=research, decision=decision).run_cycle()
    assert result.proposals_created == 1
    assert result.details[0]["risk_verdict"] in {"PASS", "REQUIRES_ENHANCED_REVIEW"}


def test_o_approval_expiration_works(tmp_path):
    watch, approvals, _notify = _services(tmp_path)
    frozen = {"t": NOW}

    def now():
        return frozen["t"]

    engine = LiveApprovalEngine(approvals.store, journal=tmp_path / "logs" / "approval.jsonl", now_fn=now)
    item = engine.create(ticker="QUAL", proposed_action="BUY", current_quote=50.0, ttl_hours=1)
    frozen["t"] = NOW + timedelta(hours=2)
    expired = engine.expire_due()
    assert len(expired) == 1
    assert approvals.store.get(item.approval_id).status is LiveApprovalStatus.EXPIRED


def test_p_changed_quote_supersedes_approval(tmp_path):
    _seed(tmp_path)
    research, decision = _buy_reasoners()
    worker = _worker(tmp_path, research=research, decision=decision)
    worker.run_cycle()
    pending = worker.approvals.store.pending()
    assert pending
    quote = float(pending[0].current_quote or 100)
    out = worker.revalidate_approvals(quotes={"QUAL": {"price": quote * 1.10}}, context=ctx(500))
    assert out["superseded"] >= 1
    assert worker.approvals.store.get(pending[0].approval_id).status is LiveApprovalStatus.EXPIRED


def test_q_duplicate_scheduler_run_does_not_duplicate_ai_research(tmp_path):
    _seed(tmp_path)
    calls = {"n": 0}

    class Counting(ScriptedResearchReasoner):
        def reason(self, request):
            calls["n"] += 1
            return super().reason(request)

    research = Counting({"QUAL": _ai("QUAL", conclusion="KEEP_WATCHING")})
    worker = _worker(tmp_path, research=research)
    first = worker.run_cycle()
    assert calls["n"] == 1
    assert first.ai_calls == 1
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    entry = queue.all()[0]
    queue.set_status(entry.queue_id, ResearchQueueStatus.QUEUED, skipped_reason=None)
    second = worker.run_cycle()
    assert calls["n"] == 1
    assert second.ai_calls == 0
    assert second.details[0].get("duplicate") is True


def test_r_restart_safely_resumes_queue(tmp_path):
    cand, entry = _seed(tmp_path)
    d = discovery_state_dir(tmp_path, mode=RuntimeMode.LIVE)
    queue = ResearchQueue(d / "research_queue.json", runtime_mode=RuntimeMode.LIVE.value)
    queue.set_status(entry.queue_id, ResearchQueueStatus.RESEARCHING, claimed_at=(NOW - timedelta(hours=1)).isoformat())
    worker = _worker(tmp_path, research=ScriptedResearchReasoner({cand.symbol: _ai(cand.symbol, conclusion="REJECT")}))
    reclaimed = worker.reclaim_stale_claims(NOW)
    assert reclaimed == 1
    result = worker.run_cycle()
    assert result.items_processed == 1


def test_s_live_nav_uses_500_not_paper_10k(tmp_path):
    _seed(tmp_path)
    research, decision = _buy_reasoners()
    worker = _worker(tmp_path, research=research, decision=decision, nav=500)
    worker.run_cycle()
    pending = worker.approvals.store.pending()[0]
    assert pending.nav_at_proposal == 500
    assert pending.portfolio_impact["nav"] == 500
    assert pending.portfolio_impact["nav"] != 10_000
    refused = _worker(tmp_path, research=research, decision=decision, nav=10_000).run_cycle()
    assert refused.status == "FAILED"
    assert refused.skipped_reason == "paper_nav_refused"


def test_t_no_live_order_placement(tmp_path):
    _seed(tmp_path)
    research, decision = _buy_reasoners()
    result = _worker(tmp_path, research=research, decision=decision).run_cycle()
    assert result.placement_attempted is False
    assert LIVE_ORDER_PLACEMENT is False
    assert result.LIVE_ORDER_PLACEMENT is False
    pending = result.details[0]
    assert "approval_id" in pending


def test_u_no_account_mutating_broker_tools_in_pipeline():
    from agentic_portfolio.paths import project_root

    assert inspect_agent_packages_for_forbidden_calls() == []
    text = (project_root() / "src" / "agentic_portfolio" / "agent" / "pipeline.py").read_text(encoding="utf-8")
    for tool in ("place_equity_order(", "cancel_equity_order(", "review_equity_order("):
        assert tool not in text


def test_v_no_candidate_to_buy_shortcut():
    from agentic_portfolio.paths import project_root

    cand = _candidate("QUAL")
    try:
        candidate_cannot_become_buy(cand)
        raised = False
    except Exception:
        raised = True
    assert raised
    text = (project_root() / "src" / "agentic_portfolio" / "agent" / "pipeline.py").read_text(encoding="utf-8")
    assert "Candidate → ResearchReport" in text or "Never treats a Candidate as a BUY" in text
    assert "run_portfolio_decision" in text


def test_w_dashboard_renders_pipeline_states(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_RUNTIME_MODE", "LIVE")
    monkeypatch.setenv("DASHBOARD_ENVIRONMENT", "LIVE")
    _live_book(tmp_path, 500)
    _seed(tmp_path)
    state = dashboard_state(tmp_path)
    view = dashboard_view(state)
    research = research_view(state)
    assert "pipeline" in view
    assert view["pipeline"]["research"]["queued"] >= 1
    assert research["queue"]
    assert view["live_order_placement_enabled"] is False


def test_x_no_action_produces_no_trade(tmp_path):
    _seed(tmp_path)
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"QUAL": _ai("QUAL", conclusion="ADVANCE_TO_THESIS")}),
        decision=ScriptedDecisionReasoner(_decision_payload("QUAL", decision="NO_ACTION", alloc=0)),
    )
    result = worker.run_cycle()
    assert result.proposals_created == 0
    assert worker.approvals.store.pending() == []


def test_y_watch_produces_no_order_plan(tmp_path):
    _seed(tmp_path)
    worker = _worker(tmp_path, research=ScriptedResearchReasoner({"QUAL": _ai("QUAL", conclusion="KEEP_WATCHING")}))
    result = worker.run_cycle()
    assert result.proposals_created == 0
    assert worker.approvals.store.pending() == []
    plans = list((tmp_path / "state").rglob("*order_plan*"))
    assert not any(p.is_file() and p.stat().st_size > 2 and "BUY" in p.read_text(encoding="utf-8") for p in plans)


def test_z_empty_queue_skipped_no_work(tmp_path):
    result = _worker(tmp_path, research=ScriptedResearchReasoner({})).run_cycle()
    assert result.status == "SKIPPED_NO_WORK"
    assert result.skipped_reason == "empty_queue"
    assert result.items_processed == 0


def test_e2e_buy_pending_approval_no_place(tmp_path):
    _live_book(tmp_path, 500)
    _seed(tmp_path)
    research, decision = _buy_reasoners()
    worker = _worker(tmp_path, research=research, decision=decision, nav=500)
    result = worker.run_cycle()
    pending = worker.approvals.store.pending()
    assert pending
    assert pending[0].status is LiveApprovalStatus.PENDING
    assert pending[0].placed_order is False
    assert pending[0].nav_at_proposal == 500
    assert LIVE_ORDER_PLACEMENT is False
    assert result.placement_attempted is False
    assert pending[0].approved_does_not_place_order is True


def test_e2e_reject_no_approval(tmp_path):
    _seed(tmp_path, symbol="JOBY")
    worker = _worker(tmp_path, research=ScriptedResearchReasoner({"JOBY": _ai("JOBY", conclusion="REJECT")}))
    result = worker.run_cycle()
    assert result.rejections == 1
    assert worker.approvals.store.pending() == []


def test_e2e_watch_then_premarket_prepare(tmp_path):
    _seed(tmp_path, symbol="GAP")
    worker = _worker(tmp_path, research=ScriptedResearchReasoner({"GAP": _ai("GAP", conclusion="KEEP_WATCHING")}))
    worker.run_cycle()
    item = worker.watch.store.by_ticker("GAP")
    assert item is not None
    prepared = worker.revalidate_watches(job="PREMARKET_THESIS_REVALIDATE")
    assert prepared["watch_items"] >= 1
    assert prepared["status"] == "OK"


def test_freshness_deadline_not_before_enqueue():
    entry = ResearchQueueEntry(
        queue_id="q1",
        candidate_id="c1",
        symbol="GAP",
        provisional_sleeve=Sleeve.CORE_GROWTH,
        discovery_score=70,
        priority=DiscoveryPriority.HIGH,
        why_research_warranted="test",
        enqueued_at="2026-08-31T12:00:00+00:00",
        freshness_deadline="2026-08-29T16:00:00+00:00",
    )
    fixed = normalize_queue_freshness(entry)
    assert fixed.freshness_deadline > fixed.enqueued_at


def test_stale_deadline_before_enqueue_is_repaired_not_expired(tmp_path):
    cand, entry = _seed(tmp_path, symbol="GAP")
    d = discovery_state_dir(tmp_path, mode=RuntimeMode.LIVE)
    queue = ResearchQueue(d / "research_queue.json", runtime_mode=RuntimeMode.LIVE.value)
    rec = queue.get(entry.queue_id)
    rec.freshness_deadline = "2026-08-29T16:00:00+00:00"
    rec.enqueued_at = NOW.isoformat()
    queue.save_entry(rec)
    worker = _worker(tmp_path, research=ScriptedResearchReasoner({"GAP": _ai("GAP", conclusion="REJECT")}))
    result = worker.run_cycle()
    assert result.status != "SKIPPED_NO_WORK"
    loaded = queue.get(entry.queue_id)
    # Re-read after worker persist
    queue = ResearchQueue(d / "research_queue.json", runtime_mode=RuntimeMode.LIVE.value)
    loaded = queue.get(entry.queue_id)
    assert loaded.status is not ResearchQueueStatus.EXPIRED
    assert loaded.status is ResearchQueueStatus.REJECTED


def test_research_queue_worker_is_scheduled():
    names = specs_by_name()
    assert "RESEARCH_QUEUE_WORKER" in names
    assert names["RESEARCH_QUEUE_WORKER"].allow_ai is True
    from agentic_portfolio.agent.jobs import catalog_preview, research_queue_max_items
    from agentic_portfolio.agent.session import MarketPhase

    assert MarketPhase.OVERNIGHT in names["RESEARCH_QUEUE_WORKER"].phases
    assert MarketPhase.MARKET_OPEN in names["RESEARCH_QUEUE_WORKER"].phases
    assert MarketPhase.MARKET_OPEN in names["CANDIDATE_DISCOVERY"].phases
    assert names["RESEARCH_QUEUE_WORKER"].every_minutes == 30
    assert names["CANDIDATE_DISCOVERY"].open_every_minutes == 15
    assert names["CANDIDATE_DISCOVERY"].cadence_for(MarketPhase.MARKET_OPEN) == "interval"
    assert names["CANDIDATE_DISCOVERY"].mode_for(MarketPhase.MARKET_OPEN) == "lightweight"
    assert names["CANDIDATE_DISCOVERY"].mode_for(MarketPhase.AFTER_CLOSE) == "broad"
    assert research_queue_max_items(MarketPhase.MARKET_OPEN) == 1
    assert research_queue_max_items(MarketPhase.OVERNIGHT) == 2
    preview = catalog_preview(MarketPhase.MARKET_OPEN)
    jobs = {row["job"]: row for row in preview}
    assert jobs["RESEARCH_QUEUE_WORKER"]["valid_for_phase"] is True
    assert jobs["RESEARCH_QUEUE_WORKER"]["every_minutes"] == 30
    assert jobs["CANDIDATE_DISCOVERY"]["valid_for_phase"] is True
    assert jobs["CANDIDATE_DISCOVERY"]["mode"] == "lightweight"
    assert jobs["CANDIDATE_DISCOVERY"]["every_minutes"] == 15
    assert "AI_REASSESS_IF_WARRANTED" in jobs


def test_handlers_include_research_queue_worker(tmp_path):
    watch_store = WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    from agentic_portfolio.agent.connection import ConnectionManager
    from agentic_portfolio.adapters.readonly_runtime import ReadonlyBrokerRuntime

    notify = NotificationEngine(NotificationStore(tmp_path), now_fn=lambda: NOW)
    services = AgentServices(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        watch=WatchEngine(watch_store, now_fn=lambda: NOW),
        watch_store=watch_store,
        approvals=LiveApprovalEngine(LiveApprovalStore(tmp_path, runtime_mode=RuntimeMode.LIVE), now_fn=lambda: NOW),
        approval_store=LiveApprovalStore(tmp_path, runtime_mode=RuntimeMode.LIVE),
        notify=notify,
        connection=ConnectionManager(bootstrap=lambda **k: ReadonlyBrokerRuntime(bound=True), now_fn=lambda: NOW),
        now_fn=lambda: NOW,
        ai_allowed=False,
    )
    handlers = build_handlers(services)
    assert "RESEARCH_QUEUE_WORKER" in handlers
    row = handlers["RESEARCH_QUEUE_WORKER"]({"job": "RESEARCH_QUEUE_WORKER"})
    assert row["status"] == "BLOCKED"
    assert row["skipped"] == "ai_disabled"


def _orch_services(root: Path, *, now, research=None, decision=None, gateway=None, discovery_fn=None, nav=500.0, exhausted=False):
    from agentic_portfolio.adapters.readonly_runtime import ReadonlyBrokerRuntime
    from agentic_portfolio.agent.connection import ConnectionManager

    watch, approvals, notify = _services(root)
    context = ctx(nav)
    return AgentServices(
        root=root,
        runtime_mode=RuntimeMode.LIVE,
        watch=watch,
        watch_store=watch.store,
        approvals=approvals,
        approval_store=approvals.store,
        notify=notify,
        connection=ConnectionManager(bootstrap=lambda **k: ReadonlyBrokerRuntime(bound=True), now_fn=now),
        now_fn=now,
        research_reasoner=research,
        decision_reasoner=decision,
        gateway=gateway,
        context_fn=lambda: context,
        payload_fn=lambda candidate: _payload(candidate.symbol),
        last_context=context,
        discovery_fn=discovery_fn,
        ai_allowed=not exhausted,
        budget_exhausted=exhausted,
    )


def test_queued_research_is_consumed_during_market_open(tmp_path):
    from agentic_portfolio.calendar import EASTERN
    from agentic_portfolio.agent.session import MarketPhase, classify_market_phase

    friday_open = datetime(2026, 8, 28, 10, 0, tzinfo=EASTERN)
    assert classify_market_phase(friday_open).phase is MarketPhase.MARKET_OPEN
    _seed(tmp_path, symbol="QUAL")
    research = ScriptedResearchReasoner({"QUAL": _ai("QUAL", conclusion="KEEP_WATCHING")})
    now = lambda: friday_open
    orch = JobOrchestrator(tmp_path, handlers=build_handlers(_orch_services(tmp_path, now=now, research=research)), now_fn=now)
    due = orch.due_jobs(friday_open)
    assert "RESEARCH_QUEUE_WORKER" in due
    row = orch.run_job("RESEARCH_QUEUE_WORKER", now=friday_open)
    assert row["status"] in {"OK", "DEGRADED"}
    assert row["items_processed"] == 1
    assert row["max_items"] == 1
    assert row["LIVE_ORDER_PLACEMENT"] is False
    assert row["placement_attempted"] is False
    assert LIVE_ORDER_PLACEMENT is False
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    assert queue.all()[0].status is ResearchQueueStatus.COMPLETED


def test_market_open_research_cycle_processes_only_one_item(tmp_path):
    from agentic_portfolio.calendar import EASTERN

    friday_open = datetime(2026, 8, 28, 10, 0, tzinfo=EASTERN)
    _seed(tmp_path, symbol="QUAL")
    _seed(tmp_path, symbol="MSFT")
    research = ScriptedResearchReasoner(
        {
            "QUAL": _ai("QUAL", conclusion="KEEP_WATCHING"),
            "MSFT": _ai("MSFT", conclusion="KEEP_WATCHING"),
        }
    )
    now = lambda: friday_open
    orch = JobOrchestrator(tmp_path, handlers=build_handlers(_orch_services(tmp_path, now=now, research=research)), now_fn=now)
    row = orch.run_job("RESEARCH_QUEUE_WORKER", now=friday_open)
    assert row["max_items"] == 1
    assert row["items_processed"] == 1
    assert row["items_considered"] == 2
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    statuses = {e.symbol: e.status for e in queue.all()}
    completed = [s for s in statuses.values() if s is ResearchQueueStatus.COMPLETED]
    queued = [s for s in statuses.values() if s is ResearchQueueStatus.QUEUED]
    assert len(completed) == 1
    assert len(queued) == 1
    assert LIVE_ORDER_PLACEMENT is False


def test_off_hours_research_cycle_still_allows_two_items(tmp_path):
    from agentic_portfolio.calendar import EASTERN

    saturday = datetime(2026, 8, 29, 14, 0, tzinfo=EASTERN)
    _seed(tmp_path, symbol="QUAL")
    _seed(tmp_path, symbol="MSFT")
    _seed(tmp_path, symbol="AAPL")
    research = ScriptedResearchReasoner(
        {
            "QUAL": _ai("QUAL", conclusion="KEEP_WATCHING"),
            "MSFT": _ai("MSFT", conclusion="KEEP_WATCHING"),
            "AAPL": _ai("AAPL", conclusion="KEEP_WATCHING"),
        }
    )
    now = lambda: saturday
    orch = JobOrchestrator(tmp_path, handlers=build_handlers(_orch_services(tmp_path, now=now, research=research)), now_fn=now)
    row = orch.run_job("RESEARCH_QUEUE_WORKER", now=saturday)
    assert row["max_items"] == 2
    assert row["items_processed"] == 2


def test_market_open_hard_cap_still_blocks_ai(tmp_path):
    from agentic_portfolio.calendar import EASTERN

    friday_open = datetime(2026, 8, 28, 10, 0, tzinfo=EASTERN)
    _seed(tmp_path)
    cfg = load_ai_config()
    ledger = UsageLedger(tmp_path, config=cfg)
    data = ledger.load_month(now=NOW)
    data["spent"] = "10.00"
    ledger.save_month(data)
    scripted = ScriptedProvider({"*": _ai("QUAL")}, name="openai")
    gw = AIGateway(
        budget=BudgetManager(ledger, cfg, now_fn=lambda: NOW),
        providers={"openai": scripted},
        config=cfg,
        runtime_mode=RuntimeMode.LIVE.value,
    )
    assert gw.budget.status().mode is BudgetMode.EXHAUSTED
    now = lambda: friday_open
    orch = JobOrchestrator(tmp_path, handlers=build_handlers(_orch_services(tmp_path, now=now, gateway=gw)), now_fn=now)
    row = orch.run_job("RESEARCH_QUEUE_WORKER", now=friday_open)
    assert row["status"] == "BLOCKED"
    assert row.get("skipped") == "budget_exhausted" or row.get("skipped_reason") == "budget_exhausted"
    assert scripted.calls == []
    assert LIVE_ORDER_PLACEMENT is False
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    assert queue.all()[0].status is ResearchQueueStatus.QUEUED


def test_market_open_does_not_duplicate_candidates_or_reports(tmp_path):
    from agentic_portfolio.calendar import EASTERN

    friday_open = datetime(2026, 8, 28, 10, 0, tzinfo=EASTERN)
    _seed(tmp_path, symbol="QUAL")
    research = ScriptedResearchReasoner({"QUAL": _ai("QUAL", conclusion="KEEP_WATCHING")})
    now = lambda: friday_open
    services = _orch_services(tmp_path, now=now, research=research)
    orch = JobOrchestrator(tmp_path, handlers=build_handlers(services), now_fn=now)
    first = orch.run_job("RESEARCH_QUEUE_WORKER", now=friday_open)
    assert first["items_processed"] == 1
    assert first["reports_created"] == 1
    worker = services._pipeline_worker
    reports = worker.research_store.by_symbol("QUAL")
    assert len(reports) == 1
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    entry = queue.all()[0]
    queue.set_status(entry.queue_id, ResearchQueueStatus.QUEUED, skipped_reason=None)
    second = orch.run_job("RESEARCH_QUEUE_WORKER", now=friday_open)
    assert second["details"][0].get("duplicate") is True
    assert second["reports_created"] == 0
    assert len(worker.research_store.by_symbol("QUAL")) == 1
    assert LIVE_ORDER_PLACEMENT is False
    assert first["placement_attempted"] is False
    assert second["placement_attempted"] is False


def test_market_open_discovery_is_lightweight_and_live_placement_off(tmp_path):
    from agentic_portfolio.calendar import EASTERN

    friday_open = datetime(2026, 8, 28, 10, 0, tzinfo=EASTERN)
    friday_post = datetime(2026, 8, 28, 16, 30, tzinfo=EASTERN)
    calls: list[dict] = []

    def discovery_fn(sources=None, lightweight=False):
        calls.append({"sources": sources, "lightweight": lightweight})

        class _Run:
            run = None

        return _Run()

    now = lambda: friday_open
    orch = JobOrchestrator(
        tmp_path,
        handlers=build_handlers(_orch_services(tmp_path, now=now, research=ScriptedResearchReasoner({}), discovery_fn=discovery_fn)),
        now_fn=now,
    )
    due = orch.due_jobs(friday_open)
    assert "CANDIDATE_DISCOVERY" in due
    assert "RESEARCH_QUEUE_WORKER" in due
    assert "AI_REASSESS_IF_WARRANTED" in due
    preview = {row["job"]: row for row in orch.scheduled_preview(friday_open)}
    assert preview["RESEARCH_QUEUE_WORKER"]["valid_for_phase"] is True
    assert preview["CANDIDATE_DISCOVERY"]["mode"] == "lightweight"
    open_row = orch.run_job("CANDIDATE_DISCOVERY", now=friday_open)
    assert open_row["mode"] == "lightweight"
    assert open_row["LIVE_ORDER_PLACEMENT"] is False
    post = JobOrchestrator(
        tmp_path / "post",
        handlers=build_handlers(_orch_services(tmp_path / "post", now=lambda: friday_post, discovery_fn=discovery_fn)),
        now_fn=lambda: friday_post,
    )
    post_row = post.run_job("CANDIDATE_DISCOVERY", now=friday_post)
    assert post_row["mode"] == "broad"
    assert calls[0]["lightweight"] is True
    assert calls[1]["lightweight"] is False
    assert LIVE_ORDER_PLACEMENT is False


def test_live_ai_outage_defers_without_persisting_research_report(tmp_path):
    _seed(tmp_path, symbol="SBSW")
    gw = build_gateway(
        tmp_path,
        providers={"openai": OpenAIProvider(api_key=None), "anthropic": AnthropicProvider(api_key=None)},
        runtime_mode=RuntimeMode.LIVE,
        now_fn=lambda: NOW,
    )
    worker = _worker(tmp_path, gateway=gw)
    result = worker.run_cycle()
    assert worker.research_store.by_symbol("SBSW") == []
    assert gw.budget.status().calls_month == 0
    assert gw.budget.status().spent == Decimal("0")
    _, queue = primary_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    assert queue.all()[0].status is ResearchQueueStatus.QUEUED
    assert queue.all()[0].skipped_reason == "ai_unavailable"
    kinds = [n.kind for n in worker.notify.store.all()]
    assert NotificationKind.RESEARCH_COMPLETED not in kinds
    assert result.status == "DEGRADED"
    assert LIVE_ORDER_PLACEMENT is False


def test_live_missing_api_key_does_not_use_scripted_or_spend(tmp_path):
    _seed(tmp_path, symbol="SBSW")
    gw = build_gateway(tmp_path, runtime_mode=RuntimeMode.LIVE, now_fn=lambda: NOW, environ={})
    assert "scripted" not in gw.providers
    worker = _worker(tmp_path, gateway=gw)
    worker.run_cycle()
    assert worker.research_store.all_reports() == []
    assert gw.budget.status().calls_month == 0


def test_live_gateway_does_not_silently_fallback_to_scripted(tmp_path):
    _seed(tmp_path, symbol="SBSW")
    scripted = ScriptedProvider({"*": _ai("SBSW", conclusion="NEED_MORE_DATA")}, name="scripted")
    gw = build_gateway(
        tmp_path,
        providers={
            "openai": OpenAIProvider(api_key=None),
            "anthropic": AnthropicProvider(api_key=None),
            "scripted": scripted,
        },
        runtime_mode=RuntimeMode.LIVE,
        now_fn=lambda: NOW,
    )
    worker = _worker(tmp_path, gateway=gw)
    worker.run_cycle()
    assert scripted.calls == []
    assert worker.research_store.by_symbol("SBSW") == []
    assert gw.budget.status().calls_month == 0


def test_gateway_research_stamps_ai_provenance_and_charges_ledger(tmp_path):
    _seed(tmp_path, symbol="SBSW")
    scripted = ScriptedProvider(
        {
            "screening:SBSW": {**SCREEN, "ticker": "SBSW"},
            "research_report:SBSW": _ai("SBSW", conclusion="NEED_MORE_DATA"),
        },
        name="openai",
    )
    gw = build_gateway(tmp_path, providers={"openai": scripted}, runtime_mode=RuntimeMode.LIVE, now_fn=lambda: NOW)
    worker = _worker(tmp_path, gateway=gw)
    worker.run_cycle()
    report = worker.research_store.by_symbol("SBSW")[0]
    assert report.research_source == "AI"
    assert report.provider == "openai"
    assert report.model
    assert report.ai_call_id
    assert report.actual_cost is not None
    assert gw.budget.status().calls_month == 2
    assert gw.budget.status().spent > Decimal("0")
    kinds = [n.kind for n in worker.notify.store.all()]
    assert NotificationKind.RESEARCH_COMPLETED in kinds


def test_scripted_reasoner_report_is_labeled_and_does_not_touch_ledger(tmp_path):
    _seed(tmp_path, symbol="SBSW")
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"SBSW": _ai("SBSW", conclusion="NEED_MORE_DATA")}),
    )
    worker.run_cycle()
    report = worker.research_store.by_symbol("SBSW")[0]
    assert report.research_source == "scripted"
    assert report.provider is None
    cfg = load_ai_config()
    status = BudgetManager(UsageLedger(tmp_path, config=cfg), cfg, now_fn=lambda: NOW).status()
    assert status.calls_month == 0
    assert status.spent == Decimal("0")


def test_diagnostics_show_production_queue_even_when_worker_binds_legacy(tmp_path):
    cand, entry = _seed(tmp_path, symbol="SBSW")
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"SBSW": _ai("SBSW", conclusion="NEED_MORE_DATA")}),
    )
    worker.run_cycle()
    legacy = ResearchQueue(tmp_path / "state" / "research_queue.json", runtime_mode=RuntimeMode.LIVE.value)
    for i in range(21):
        legacy.enqueue(
            ResearchQueueEntry(
                queue_id=f"legacy-{i}",
                candidate_id=f"legacy-c-{i}",
                symbol=f"LEG{i:02d}",
                provisional_sleeve=Sleeve.CORE_GROWTH,
                discovery_score=60.0,
                priority=DiscoveryPriority.LOW,
                why_research_warranted="recovered Pi queue",
                required_research_areas=["business_quality"],
                enqueued_at=NOW.isoformat(),
                freshness_deadline=(NOW + timedelta(hours=72)).isoformat(),
                status=ResearchQueueStatus.QUEUED,
            )
        )
    inspected = inspect_research_queues(tmp_path, runtime_mode=RuntimeMode.LIVE)
    assert inspected["worker_uses_legacy_fallback"] is False
    assert any(e.symbol == "SBSW" for e in inspected["production"].all())
    assert inspected["production"].all()[0].status is ResearchQueueStatus.NEED_MORE_DATA
    assert any(e.symbol == "SBSW" for e in inspected["worker"].all())
    assert inspected["legacy"].all() and all(e.status is ResearchQueueStatus.QUEUED for e in inspected["legacy"].all())
    from scripts.diagnose_pipeline import budget_diagnostic, research_report_diagnostics

    rows = research_report_diagnostics(tmp_path, RuntimeMode.LIVE)
    sbsw = next(r for r in rows if r["symbol"] == "SBSW")
    assert sbsw["research_source"] == "scripted"
    assert sbsw["originating_candidate_id"] == cand.candidate_id
    assert sbsw["queue_status"] == ResearchQueueStatus.NEED_MORE_DATA.value
    assert sbsw["queue_source"] == "production"
    budget = budget_diagnostic(tmp_path)
    accounted = budget["spent"] + budget["reserved"]
    assert budget["remaining_reconciles"] is True
    assert abs(budget["remaining"] - (budget["cap"] - accounted)) < 1e-9
    assert budget["calls_month"] == 0
    assert budget["configured_research_provider"] == "openai"
    assert budget["configured_research_model"] == "gpt-5.6-terra"
    assert LIVE_ORDER_PLACEMENT is False
    del entry


def test_budget_remaining_accounts_for_reservations(tmp_path):
    cfg = load_ai_config()
    ledger = UsageLedger(tmp_path, config=cfg)
    now = datetime.now(timezone.utc)
    mgr = BudgetManager(ledger, cfg, now_fn=lambda: now)
    reservation = mgr.authorize(
        Decimal("1.50"),
        purpose="deep_research",
        role="research",
        provider="openai",
        model="gpt-5.6-terra",
        ticker="SBSW",
        runtime_mode=RuntimeMode.LIVE.value,
    )
    status = mgr.status()
    assert status.reserved == Decimal("1.50")
    assert status.remaining == status.cap - status.spent - status.reserved
    assert status.remaining == Decimal("8.50")
    from scripts.diagnose_pipeline import budget_diagnostic

    row = budget_diagnostic(tmp_path)
    assert row["reserved"] == 1.5
    assert row["remaining_reconciles"] is True
    assert row["calls_month"] == 0
    assert row["remaining_formula"] == "cap - spent - reserved"
    del reservation


def test_ai_disabled_once_flag_does_not_create_report(tmp_path):
    _seed(tmp_path, symbol="SBSW")
    now = lambda: NOW
    services = _orch_services(tmp_path, now=now, gateway=build_gateway(tmp_path, runtime_mode=RuntimeMode.LIVE, now_fn=now, environ={}))
    services.ai_allowed = False
    orch = JobOrchestrator(tmp_path, handlers=build_handlers(services), now_fn=now)
    row = orch.run_job("RESEARCH_QUEUE_WORKER", now=NOW)
    assert row["status"] == "BLOCKED"
    assert row["skipped"] == "ai_disabled"
    worker = ResearchQueueWorker(tmp_path, runtime_mode=RuntimeMode.LIVE, now_fn=now)
    assert worker.research_store.all_reports() == []


def test_live_runtime_does_not_inject_scripted_research_reasoner(tmp_path):
    from agentic_portfolio.agent.runtime import AgentRuntime

    runtime = AgentRuntime(tmp_path, runtime_mode=RuntimeMode.LIVE, max_cycles=0, sleep_fn=lambda _s: None)
    assert runtime.services.research_reasoner is None
    assert runtime.services.gateway is not None
    assert "scripted" not in runtime.services.gateway.providers
    assert LIVE_ORDER_PLACEMENT is False


def test_live_never_consumes_paper_queue(tmp_path):
    _seed(tmp_path, symbol="MSFT")
    paper = ResearchQueue(tmp_path / "state" / "research_queue.json", runtime_mode=RuntimeMode.PAPER.value)
    paper.enqueue(
        ResearchQueueEntry(
            queue_id="paper-q",
            candidate_id="paper-c",
            symbol="FAKE",
            provisional_sleeve=Sleeve.CORE_GROWTH,
            discovery_score=99.0,
            priority=DiscoveryPriority.URGENT_RESEARCH,
            why_research_warranted="paper",
            enqueued_at=NOW.isoformat(),
            freshness_deadline=(NOW + timedelta(hours=72)).isoformat(),
            status=ResearchQueueStatus.QUEUED,
        )
    )
    research = ScriptedResearchReasoner({"MSFT": _ai("MSFT", conclusion="KEEP_WATCHING"), "FAKE": _ai("FAKE", conclusion="KEEP_WATCHING")})
    result = _worker(tmp_path, research=research).run_cycle()
    assert "FAKE" not in result.symbols
    _, live_q = primary_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    assert all(e.symbol != "FAKE" for e in live_q.all())
    assert paper.get("paper-q").status is ResearchQueueStatus.QUEUED


def test_duplicate_discovery_does_not_create_duplicate_queue_item(tmp_path):
    from agentic_portfolio.discovery.engine import run_discovery
    from agentic_portfolio.discovery.store import DiscoveryRunStore
    from tests.test_discovery import _quality_core
    from agentic_portfolio.schemas import MarketRegime

    d = discovery_state_dir(tmp_path, mode=RuntimeMode.LIVE)
    cstore = CandidateStore(d / "candidates.json", runtime_mode=RuntimeMode.LIVE.value)
    qstore = ResearchQueue(d / "research_queue.json", runtime_mode=RuntimeMode.LIVE.value)
    rstore = DiscoveryRunStore(d / "discovery_runs.json", runtime_mode=RuntimeMode.LIVE.value)
    kwargs = dict(
        snapshots=[_quality_core("QUAL")],
        context=ctx(500),
        persist=True,
        promote_shortlist=True,
        now=NOW,
        candidate_store=cstore,
        queue_store=qstore,
        run_store=rstore,
        regime=MarketRegime.unknown(observed_at=NOW.isoformat()),
    )
    run_discovery(**kwargs)
    run_discovery(**kwargs)
    queued = [e for e in qstore.all() if e.symbol == "QUAL"]
    assert len(queued) == 1
    assert queued[0].status is ResearchQueueStatus.QUEUED


def test_completed_watch_does_not_requeue_immediately(tmp_path):
    from agentic_portfolio.discovery.engine import run_discovery
    from agentic_portfolio.discovery.store import DiscoveryRunStore
    from tests.test_discovery import _quality_core
    from agentic_portfolio.schemas import MarketRegime

    _seed(tmp_path, symbol="QUAL")
    worker = _worker(tmp_path, research=ScriptedResearchReasoner({"QUAL": _ai("QUAL", conclusion="KEEP_WATCHING")}))
    worker.run_cycle()
    d = discovery_state_dir(tmp_path, mode=RuntimeMode.LIVE)
    cstore = CandidateStore(d / "candidates.json", runtime_mode=RuntimeMode.LIVE.value)
    qstore = ResearchQueue(d / "research_queue.json", runtime_mode=RuntimeMode.LIVE.value)
    rstore = DiscoveryRunStore(d / "discovery_runs.json", runtime_mode=RuntimeMode.LIVE.value)
    before = len(qstore.all())
    run_discovery(
        [_quality_core("QUAL")],
        ctx(500),
        persist=True,
        promote_shortlist=True,
        now=NOW,
        candidate_store=cstore,
        queue_store=qstore,
        run_store=rstore,
        regime=MarketRegime.unknown(observed_at=NOW.isoformat()),
    )
    assert len([e for e in qstore.all() if e.status is ResearchQueueStatus.QUEUED]) == 0
    assert len(qstore.all()) == before
    cand = cstore.active_for_symbol("QUAL")
    assert cand.status is CandidateStatus.WATCHING


def test_need_more_data_retries_only_after_freshness(tmp_path):
    from agentic_portfolio.discovery.engine import run_discovery
    from agentic_portfolio.discovery.store import DiscoveryRunStore
    from tests.test_discovery import _quality_core
    from agentic_portfolio.schemas import MarketRegime

    _seed(tmp_path, symbol="QUAL")
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"QUAL": _ai("QUAL", conclusion="NEED_MORE_DATA", missing=["get_financials"])}),
    )
    worker.payload_fn = lambda candidate: _payload(candidate.symbol, rich=False)
    worker.run_cycle()
    d = discovery_state_dir(tmp_path, mode=RuntimeMode.LIVE)
    cstore = CandidateStore(d / "candidates.json", runtime_mode=RuntimeMode.LIVE.value)
    qstore = ResearchQueue(d / "research_queue.json", runtime_mode=RuntimeMode.LIVE.value)
    rstore = DiscoveryRunStore(d / "discovery_runs.json", runtime_mode=RuntimeMode.LIVE.value)
    run_discovery(
        [_quality_core("QUAL")],
        ctx(500),
        persist=True,
        promote_shortlist=True,
        now=NOW + timedelta(hours=1),
        candidate_store=cstore,
        queue_store=qstore,
        run_store=rstore,
        regime=MarketRegime.unknown(observed_at=NOW.isoformat()),
    )
    assert [e for e in qstore.all() if e.status is ResearchQueueStatus.QUEUED] == []
    later = run_discovery(
        [_quality_core("QUAL")],
        ctx(500),
        persist=True,
        promote_shortlist=True,
        now=NOW + timedelta(hours=25),
        candidate_store=cstore,
        queue_store=qstore,
        run_store=rstore,
        regime=MarketRegime.unknown(observed_at=NOW.isoformat()),
    )
    assert any(e.status is ResearchQueueStatus.QUEUED for e in qstore.all())
    assert later.queue


def test_failed_provider_call_does_not_persist_fake_report(tmp_path):
    _seed(tmp_path, symbol="SBSW")
    gw = build_gateway(
        tmp_path,
        providers={"openai": OpenAIProvider(api_key=None), "anthropic": AnthropicProvider(api_key=None)},
        runtime_mode=RuntimeMode.LIVE,
        now_fn=lambda: NOW,
    )
    worker = _worker(tmp_path, gateway=gw)
    worker.run_cycle()
    assert worker.research_store.by_symbol("SBSW") == []
    kinds = [n.kind for n in worker.notify.store.all()]
    assert NotificationKind.RESEARCH_COMPLETED not in kinds


def test_research_advances_to_thesis_automatically(tmp_path):
    _seed(tmp_path)
    research, decision = _buy_reasoners()
    worker = _worker(tmp_path, research=research, decision=decision)
    result = worker.run_cycle()
    assert result.theses_created >= 1
    assert result.proposals_created == 1
    pending = worker.approvals.store.pending()
    assert len(pending) == 1
    item = pending[0]
    assert item.ticker == "QUAL"
    assert item.status.value == "PENDING"
    assert item.risk_gate_result
    thesis = worker.theses.current_for_symbol("QUAL")
    assert thesis is not None
    raw = json.loads(worker.theses.path.read_text(encoding="utf-8"))
    stored = raw["records"][thesis.thesis_id]
    assert stored["runtime_mode"] == RuntimeMode.LIVE.value
    cand = worker.candidates.current_for_symbol("QUAL")
    assert cand.status is CandidateStatus.RESEARCH_COMPLETE
    batches = worker.decision_store.all_runs()
    assert batches
    assert batches[0].get("runtime_mode") == RuntimeMode.LIVE.value
    assert batches[0].get("gated_actions")


def test_need_more_data_earnings_calendar_does_not_skip_cooldown(tmp_path):
    from agentic_portfolio.discovery.engine import run_discovery
    from agentic_portfolio.discovery.store import DiscoveryRunStore
    from tests.test_discovery import _quality_core
    from agentic_portfolio.schemas import MarketRegime

    snap = _quality_core("QUAL")
    snap.earnings_upcoming_days = 2
    _seed(tmp_path, symbol="QUAL")
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"QUAL": _ai("QUAL", conclusion="NEED_MORE_DATA", missing=["get_financials"])}),
    )
    worker.payload_fn = lambda candidate: _payload(candidate.symbol, rich=False)
    worker.run_cycle()
    d = discovery_state_dir(tmp_path, mode=RuntimeMode.LIVE)
    cstore = CandidateStore(d / "candidates.json", runtime_mode=RuntimeMode.LIVE.value)
    qstore = ResearchQueue(d / "research_queue.json", runtime_mode=RuntimeMode.LIVE.value)
    rstore = DiscoveryRunStore(d / "discovery_runs.json", runtime_mode=RuntimeMode.LIVE.value)
    cand = cstore.current_for_symbol("QUAL")
    cand.event_flags = list(dict.fromkeys(list(cand.event_flags or []) + ["UPCOMING_EARNINGS"]))
    cand.reasons = list(dict.fromkeys(list(cand.reasons or []) + ["upcoming_earnings"]))
    cstore.upsert(cand)
    run_discovery(
        [snap],
        ctx(500),
        persist=True,
        promote_shortlist=True,
        now=NOW + timedelta(hours=1),
        candidate_store=cstore,
        queue_store=qstore,
        run_store=rstore,
        regime=MarketRegime.unknown(observed_at=NOW.isoformat()),
    )
    assert [e for e in qstore.all() if e.status is ResearchQueueStatus.QUEUED] == []


def test_unapproved_packet_cannot_execute(tmp_path, monkeypatch):
    from agentic_portfolio.live_execution import ExecutionStore, FakeBroker, LiveOrderExecutor

    monkeypatch.setenv("AGENTIC_LIVE_ORDER_PLACEMENT", "true")
    _seed(tmp_path)
    research, decision = _buy_reasoners()
    worker = _worker(tmp_path, research=research, decision=decision)
    worker.run_cycle()
    pending = worker.approvals.store.pending()[0]
    broker = FakeBroker()
    executor = LiveOrderExecutor(
        ExecutionStore(tmp_path, runtime_mode=RuntimeMode.LIVE),
        broker,
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        context_fn=lambda: ctx(500),
        regular_hours_fn=lambda: True,
    )
    outcome = executor.execute_approved(pending)
    assert outcome.placed is False
    assert "approval_not_approved" in outcome.reasons
    assert broker.place_calls == []


def test_approved_packet_blocked_when_placement_disabled(tmp_path, monkeypatch):
    from agentic_portfolio.live_approval.types import LiveApprovalStatus
    from agentic_portfolio.live_execution import ExecutionStore, FakeBroker, LiveOrderExecutor

    monkeypatch.delenv("AGENTIC_LIVE_ORDER_PLACEMENT", raising=False)
    _seed(tmp_path)
    research, decision = _buy_reasoners()
    worker = _worker(tmp_path, research=research, decision=decision)
    worker.run_cycle()
    pending = worker.approvals.store.pending()[0]
    worker.approvals.record_decision(pending.approval_id, LiveApprovalStatus.APPROVED, decided_by="human")
    item = worker.approvals.store.get(pending.approval_id)
    broker = FakeBroker()
    executor = LiveOrderExecutor(
        ExecutionStore(tmp_path, runtime_mode=RuntimeMode.LIVE),
        broker,
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        context_fn=lambda: ctx(500),
        regular_hours_fn=lambda: True,
    )
    outcome = executor.execute_approved(item)
    assert outcome.placed is False
    assert "LIVE_ORDER_PLACEMENT_false" in outcome.reasons
    assert broker.place_calls == []


def test_sell_reduce_require_human_approval(tmp_path):
    _seed(tmp_path, symbol="AAPL")
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"AAPL": _ai("AAPL", conclusion="ADVANCE_TO_THESIS")}),
        decision=ScriptedDecisionReasoner(_decision_payload("AAPL", decision="SELL", alloc=0)),
    )
    result = worker.run_cycle()
    pending = worker.approvals.store.pending()
    if result.proposals_created:
        assert pending
        assert pending[0].proposed_action == "SELL"
        assert pending[0].status.value == "PENDING"
        assert pending[0].placed_order is False


def test_incomplete_evidence_does_not_call_terra(tmp_path):
    _seed(tmp_path, symbol="EMPTY")
    scripted = ScriptedProvider({"*": _ai("EMPTY")}, name="openai")
    gw = build_gateway(tmp_path, providers={"openai": scripted}, runtime_mode=RuntimeMode.LIVE, now_fn=lambda: NOW)
    worker = _worker(tmp_path, gateway=gw)
    worker.payload_fn = lambda candidate: ResearchPayload(
        symbol=candidate.symbol,
        observed_at=NOW.isoformat(),
        sources_attempted=["get_equity_quotes"],
        sources_observed=[],
        sources_unavailable=["get_equity_quotes", "get_equity_fundamentals", "get_financials"],
    )
    result = worker.run_cycle()
    assert scripted.calls == []
    reports = worker.research_store.by_symbol("EMPTY")
    assert reports
    assert reports[0].research_source == "deterministic"
    assert reports[0].research_conclusion is ResearchConclusion.NEED_MORE_DATA
    assert result.ai_calls == 0
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    assert queue.all()[0].status is ResearchQueueStatus.NEED_MORE_DATA


def test_need_more_data_does_not_immediately_recall_terra(tmp_path):
    _seed(tmp_path, symbol="EMPTY")
    scripted = ScriptedProvider({"*": _ai("EMPTY")}, name="openai")
    gw = build_gateway(tmp_path, providers={"openai": scripted}, runtime_mode=RuntimeMode.LIVE, now_fn=lambda: NOW)
    worker = _worker(tmp_path, gateway=gw)
    worker.payload_fn = lambda candidate: ResearchPayload(
        symbol=candidate.symbol,
        observed_at=NOW.isoformat(),
        sources_unavailable=["get_equity_quotes", "get_equity_fundamentals"],
    )
    worker.run_cycle()
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    queue.set_status(queue.all()[0].queue_id, ResearchQueueStatus.QUEUED)
    worker.run_cycle()
    assert scripted.calls == []
    assert len(worker.research_store.by_symbol("EMPTY")) == 1


def test_luna_reject_does_not_call_terra(tmp_path):
    _seed(tmp_path)
    scripted = ScriptedProvider(
        {
            "screening:QUAL": {**SCREEN, "ticker": "QUAL", "worth_deep_research": False, "score": 20.0},
            "research_report:QUAL": _ai("QUAL", conclusion="ADVANCE_TO_THESIS"),
        },
        name="openai",
    )
    gw = build_gateway(tmp_path, providers={"openai": scripted}, runtime_mode=RuntimeMode.LIVE, now_fn=lambda: NOW)
    result = _worker(tmp_path, gateway=gw).run_cycle()
    assert [c.schema_name for c in scripted.calls] == ["screening"]
    assert result.rejections == 1
    assert result.proposals_created == 0
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    assert queue.all()[0].status is ResearchQueueStatus.REJECTED


def test_sufficient_evidence_can_produce_non_need_more_data(tmp_path):
    _seed(tmp_path)
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"QUAL": _ai("QUAL", conclusion="KEEP_WATCHING")}),
    )
    result = worker.run_cycle()
    report = worker.research_store.by_symbol("QUAL")[0]
    assert report.research_conclusion is ResearchConclusion.KEEP_WATCHING
    assert report.research_conclusion is not ResearchConclusion.NEED_MORE_DATA
    assert result.proposals_created == 0


def test_unfavorable_research_produces_no_buy(tmp_path):
    _seed(tmp_path)
    worker = _worker(tmp_path, research=ScriptedResearchReasoner({"QUAL": _ai("QUAL", conclusion="REJECT")}))
    result = worker.run_cycle()
    assert result.rejections == 1
    assert worker.approvals.store.pending() == []
    assert result.proposals_created == 0


def _pre_fix_need_more_data_report(symbol: str, candidate_id: str) -> ResearchReport:
    return ResearchReport(
        research_id=f"pre-fix-{symbol.lower()}",
        candidate_id=candidate_id,
        symbol=symbol,
        started_at=(NOW - timedelta(days=1)).isoformat(),
        completed_at=(NOW - timedelta(days=1)).isoformat(),
        provisional_sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
        research_status=ResearchStatus.RESEARCH_INCONCLUSIVE,
        subject_kind=ResearchSubjectKind.NEW_CANDIDATE,
        executive_summary="Paid research skipped: core evidence is missing (fundamentals_or_financials).",
        missing_information=["financials.revenue", "source_unavailable:get_financials"],
        sources_observed=["get_equity_quotes"],
        sources_unavailable=["get_financials", "get_equity_news", "get_sec_filing_index"],
        confidence=ResearchConfidence.LOW,
        research_conclusion=ResearchConclusion.NEED_MORE_DATA,
        recommended_next_step="NEED_MORE_DATA",
        research_source="deterministic",
    )


def test_pre_fix_spy_need_more_data_is_requeued_without_forcing_buy(tmp_path):
    cand, entry = _seed(tmp_path, symbol="SPY", security_class=SecurityClass.BROAD_MARKET_INDEX_ETF)
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    queue.set_status(entry.queue_id, ResearchQueueStatus.NEED_MORE_DATA)
    ResearchStore(tmp_path).save(_pre_fix_need_more_data_report("SPY", cand.candidate_id))

    watch, approvals, notify = _services(tmp_path)
    worker = ResearchQueueWorker(
        tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        research_reasoner=ScriptedResearchReasoner({"SPY": _ai("SPY", conclusion="KEEP_WATCHING")}),
        payload_fn=lambda candidate: _etf_payload(candidate.symbol),
        context_fn=lambda: ctx(500),
        watch=watch,
        approvals=approvals,
        notify=notify,
        now_fn=lambda: NOW,
    )
    result = worker.run_cycle()
    reports = worker.research_store.by_symbol("SPY")
    assert len(reports) >= 2
    latest = sorted(reports, key=lambda r: r.started_at, reverse=True)[0]
    assert latest.research_id != "pre-fix-spy"
    assert latest.research_conclusion is ResearchConclusion.KEEP_WATCHING
    assert latest.research_conclusion is not ResearchConclusion.NEED_MORE_DATA
    assert latest.buy_actions_created == 0
    assert result.proposals_created == 0
    assert worker.approvals.store.pending() == []
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    assert queue.all()[0].status is ResearchQueueStatus.COMPLETED
    assert any(row.get("status") == "COLLECTOR_REPAIR_REQUEUE" for row in result.details)


def test_pre_fix_etf_repair_does_not_requeue_the_same_report_twice(tmp_path):
    cand, entry = _seed(tmp_path, symbol="VTI", security_class=SecurityClass.BROAD_MARKET_INDEX_ETF)
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    queue.set_status(entry.queue_id, ResearchQueueStatus.NEED_MORE_DATA)
    ResearchStore(tmp_path).save(_pre_fix_need_more_data_report("VTI", cand.candidate_id))
    watch, approvals, notify = _services(tmp_path)
    worker = ResearchQueueWorker(
        tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        research_reasoner=ScriptedResearchReasoner({"VTI": _ai("VTI", conclusion="KEEP_WATCHING")}),
        payload_fn=lambda candidate: _etf_payload(candidate.symbol),
        context_fn=lambda: ctx(500),
        watch=watch,
        approvals=approvals,
        notify=notify,
        now_fn=lambda: NOW,
    )
    first = worker.run_cycle()
    second = worker.run_cycle()
    assert first.reports_created == 1
    assert second.reports_created == 0
    assert not any(row.get("status") == "COLLECTOR_REPAIR_REQUEUE" for row in second.details)
    assert worker.research_store.by_symbol("VTI")[-1].research_conclusion is ResearchConclusion.KEEP_WATCHING


def test_requeued_etf_buy_still_goes_through_risk_gate(tmp_path):
    cand, entry = _seed(tmp_path, symbol="VOO", security_class=SecurityClass.BROAD_MARKET_INDEX_ETF)
    _, queue = resolve_queue_stores(tmp_path, runtime_mode=RuntimeMode.LIVE)
    queue.set_status(entry.queue_id, ResearchQueueStatus.NEED_MORE_DATA)
    ResearchStore(tmp_path).save(_pre_fix_need_more_data_report("VOO", cand.candidate_id))
    research, decision = _buy_reasoners("VOO")
    watch, approvals, notify = _services(tmp_path)
    worker = ResearchQueueWorker(
        tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        research_reasoner=research,
        decision_reasoner=decision,
        payload_fn=lambda candidate: _etf_payload(candidate.symbol),
        context_fn=lambda: ctx(500),
        watch=watch,
        approvals=approvals,
        notify=notify,
        now_fn=lambda: NOW,
    )
    result = worker.run_cycle()
    pending = worker.approvals.store.pending()
    if result.proposals_created:
        assert pending
        assert pending[0].risk_gate_result
        assert pending[0].ticker == "VOO"
        assert pending[0].status.value == "PENDING"
        assert pending[0].placed_order is False
    assert LIVE_ORDER_PLACEMENT is False
    latest = sorted(worker.research_store.by_symbol("VOO"), key=lambda r: r.started_at, reverse=True)[0]
    assert latest.research_conclusion is not ResearchConclusion.NEED_MORE_DATA

