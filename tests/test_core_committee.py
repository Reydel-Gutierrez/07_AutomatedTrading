"""CORE Portfolio Investment Committee.

Does not force BUYs. Does not bypass Risk Gate, human approval, or live execution.
"""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_portfolio.agent.activity import read_activity
from agentic_portfolio.ai.budget import BudgetManager
from agentic_portfolio.ai.config import load_ai_config, monthly_cap
from agentic_portfolio.ai.gateway import AIGateway
from agentic_portfolio.ai.ledger import UsageLedger
from agentic_portfolio.ai.types import BudgetMode
from agentic_portfolio.decision.committee import (
    collect_committee_input,
    committee_fingerprint,
    reevaluate_live_core_committee,
    should_run_committee,
)
from agentic_portfolio.decision.reasoner import ScriptedDecisionReasoner
from agentic_portfolio.decision.validate import DecisionValidationError, validate_payload
from agentic_portfolio.discovery.store import CandidateStore
from agentic_portfolio.journal import read_jsonl
from agentic_portfolio.live_approval import LiveApprovalStatus
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.types import ResearchConclusion, ResearchConfidence, ResearchReport, ResearchStatus
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, RuntimeMode, discovery_state_dir
from agentic_portfolio.schemas import CandidateStatus, ResearchQueueStatus, SecurityClass, Sleeve, SpyBenchmark
from agentic_portfolio.watch.store import WatchStore
from agentic_portfolio.watch.types import WatchItem, WatchStatus
from tests.conftest import ctx, pos
from tests.test_candidate_lifecycle import _put_candidate, _put_queue, _stores
from tests.test_decision import _payload, _report, _thesis, _fact
from tests.test_production_pipeline import _seed, _services, _worker
from tests.test_research import _ai
from agentic_portfolio.research.reasoner import ScriptedResearchReasoner


NOW = datetime(2026, 9, 2, 17, 23, tzinfo=timezone.utc)
TS = NOW.isoformat()


def _fresh_report(symbol: str, candidate_id: str, **kwargs):
    report = _report(symbol, rid=kwargs.pop("rid", f"res-{symbol}"), **kwargs)
    report.candidate_id = candidate_id
    report.stale_after = (NOW + timedelta(days=10)).isoformat()
    return report


def _stale_report(symbol: str, candidate_id: str):
    report = _fresh_report(symbol, candidate_id)
    report.stale_after = (NOW - timedelta(days=1)).isoformat()
    return report


def _watch(symbol: str, candidate_id: str, *, status=WatchStatus.WATCH, paper: bool = False, **kwargs) -> WatchItem:
    return WatchItem(
        watch_id=f"w-{symbol.lower()}-{'paper' if paper else 'live'}",
        ticker=symbol,
        created_at=TS,
        last_updated=TS,
        status=status,
        candidate_id=candidate_id,
        research_id=kwargs.get("research_id", f"res-{symbol}"),
        runtime_mode="PAPER" if paper else "LIVE",
        paper_environment=paper,
        sleeve=kwargs.get("sleeve", Sleeve.CORE_GROWTH.value),
        reason_for_watch=kwargs.get("reason_for_watch", "decision_watch"),
        proposed_notional=kwargs.get("proposed_notional"),
        desired_allocation_pct=kwargs.get("desired_allocation_pct"),
    )


def _watch_row(symbol: str, *, lost_to=None):
    return {
        "symbol": symbol,
        "decision": "WATCH",
        "desired_allocation_pct": 0,
        "rationale": f"{symbol} is qualified but is not the residual allocation today.",
        "reconsideration": {
            "why_lost": "Another residual (or cash) ranked higher.",
            "lost_to": list(lost_to or ["CASH"]),
            "valuation_condition": "Reconsider if price/valuation improves versus alternatives.",
            "thesis_condition": "Reconsider if durability evidence strengthens.",
            "required_evidence_improvement": "Updated quality/valuation packet.",
            "next_review_reason": "committee_residual",
            "next_review_at": (NOW + timedelta(days=7)).isoformat(),
        },
    }


def _committee_payload(symbols: list[str], *, buy: str | None = None, alloc: float = 5.0, etf: bool = False):
    names = [s.upper() for s in symbols]
    ranking = ([buy] if buy else []) + ["CASH", "SPY"] + [s for s in names if s not in {buy, "CASH", "SPY"}]
    theses = []
    if buy:
        thesis = _thesis(buy)
        if etf:
            thesis["catalysts"] = []
            thesis["thesis_drivers"] = [
                "diversified market exposure",
                "long-term earnings participation",
                "residual CORE vehicle versus excess cash",
            ]
        theses.append(thesis)
    decisions = []
    if buy:
        spy_why = "" if buy == "SPY" else "Concentrated quality versus generic beta at starter size."
        decisions.append(
            {
                "symbol": buy,
                "decision": "BUY",
                "desired_allocation_pct": alloc,
                "starter_position": True,
                "rationale": f"Starter CORE allocation to {buy}; residual cash retained.",
                "why_preferable_to_cash": "Expected compounding exceeds cash opportunity cost at a starter size.",
                "why_preferable_to_spy": spy_why,
                "why_preferable_to_alternatives": f"{buy} is the best residual among the qualified set.",
            }
        )
    for symbol in names:
        if symbol == buy:
            continue
        decisions.append(_watch_row(symbol, lost_to=[buy or "CASH", "CASH"]))
    cash_pct = 100.0 - (alloc if buy else 0.0)
    cash_row = {
        "symbol": "CASH",
        "decision": "HOLD",
        "desired_allocation_pct": cash_pct,
        "rationale": (
            f"Cash residual after starter CORE allocation to {buy}."
            if buy
            else "Affirmative cash allocation: diversified CORE exposure is not the better expected outcome on current evidence."
        ),
        "why_preferable_to_spy": (
            None
            if buy
            else "Current risk/valuation context favors cash over diversified market exposure until the reconsideration trigger."
        ),
        "reconsideration": {
            "why_lost": None if buy else "Broad-market CORE currently ranks below cash.",
            "lost_to": [buy] if buy else ["SPY"],
            "next_review_reason": "scheduled_core_review",
            "next_review_at": (NOW + timedelta(days=1)).isoformat(),
        },
    }
    decisions.append({k: v for k, v in cash_row.items() if v is not None})
    vs_spy = (
        "SPY is the residual benchmark; comparison is versus cash and individual names, not versus itself."
        if buy == "SPY"
        else (
            "Selected residual versus generic beta, or cash if no residual is justified."
            if buy
            else "Diversified CORE exposure is currently ranked below cash on the stated comparative evidence."
        )
    )
    return {
        "theses": theses,
        "comparison": {
            "ranking": ranking,
            "vs_cash": (
                "Deploy only if a residual improves the book versus retaining cash."
                if buy
                else "Cash currently has the better expected risk-adjusted outcome versus deploying CORE capital."
            ),
            "vs_spy": vs_spy,
            "notes": "One coherent committee allocation. Unused sleeve capacity is not a mandate.",
        },
        "decisions": decisions,
        "portfolio_action": "ALLOCATE" if buy else "HOLD_CASH",
        "cash": {
            "target_weight": cash_pct,
            "rationale": cash_row["rationale"],
            "action": "HOLD",
            "why_cash_beats_broad_market": cash_row.get("why_preferable_to_spy"),
            "reconsideration_trigger": "scheduled_core_review",
        },
    }


def _seed_core_universe(root: Path, symbols: list[str], *, stale: tuple[str, ...] = ()):
    cstore, qstore, _ = _stores(root)
    watches = WatchStore(root, runtime_mode=RuntimeMode.LIVE)
    research = ResearchStore(root)
    cands = {}
    for symbol in symbols:
        cand = _put_candidate(cstore, symbol, CandidateStatus.WATCHING)
        _put_queue(qstore, cand, ResearchQueueStatus.COMPLETED)
        if symbol in stale:
            research.save(_stale_report(symbol, cand.candidate_id))
        else:
            research.save(_fresh_report(symbol, cand.candidate_id))
        watches.save(_watch(symbol, cand.candidate_id))
        cands[symbol] = cand
    return cands


def test_a_100_percent_cash_does_not_force_deployment(tmp_path):
    _seed(tmp_path, symbol="MSFT")
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"MSFT": _ai("MSFT", conclusion="ADVANCE_TO_THESIS")}),
        decision=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy=None)),
        now=NOW,
        nav=500,
    )
    result = worker.run_cycle()
    assert result.proposals_created == 0
    assert worker.approvals.store.pending() == []
    item = worker.watch.store.by_ticker("MSFT")
    assert item is not None
    assert item.status is WatchStatus.WATCH
    assert item.reason_for_watch == "decision_watch"
    kinds = {row.get("type") for row in read_jsonl(tmp_path / "logs" / "core_committee.jsonl")}
    assert "CORE_COMMITTEE_NO_ACTION" in kinds


def test_b_empty_core_book_may_take_starter_position(tmp_path):
    _seed(tmp_path, symbol="MSFT")
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"MSFT": _ai("MSFT", conclusion="ADVANCE_TO_THESIS")}),
        decision=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy="MSFT", alloc=5.0)),
        now=NOW,
        nav=500,
    )
    result = worker.run_cycle()
    assert result.proposals_created == 1
    pending = worker.approvals.store.pending()
    assert pending
    assert pending[0].proposed_action == "BUY"
    assert pending[0].proposed_allocation_pct == pytest.approx(5.0)
    assert pending[0].status is LiveApprovalStatus.PENDING
    assert pending[0].placed_order is False
    assert LIVE_ORDER_PLACEMENT is False


def test_c_production_committee_sends_multiple_alternatives(tmp_path):
    _seed_core_universe(tmp_path, ["MA", "SPGI"])
    _seed(tmp_path, symbol="MSFT")
    captured: dict = {}

    def responder(request):
        captured["n"] = len(request.reports)
        captured["symbols"] = [row["symbol"] for row in request.reports]
        captured["committee"] = (request.packet or {}).get("committee") or (request.policy_context or {}).get("committee")
        assert "CASH" in request.alternatives
        assert request.policy_context.get("consider_cash_yield_and_opportunity_cost") is True
        cash_alt = request.policy_context.get("cash_alternative") or {}
        assert cash_alt.get("yield_known") is True
        assert cash_alt.get("current_yield") == pytest.approx(0.0)
        assert cash_alt.get("yield_source") == "configured_actual_account_cash_yield"
        assert cash_alt.get("yield_as_of") == "2026-09-02"
        return _committee_payload(captured["symbols"], buy="MSFT")

    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"MSFT": _ai("MSFT", conclusion="ADVANCE_TO_THESIS")}),
        decision=ScriptedDecisionReasoner(responder),
        now=NOW,
    )
    worker.run_cycle()
    assert captured["n"] > 1
    assert set(captured["symbols"]) >= {"MSFT", "MA", "SPGI"}
    assert captured["committee"] is True


def test_d_best_residual_wins_others_watch(tmp_path):
    _seed_core_universe(tmp_path, ["MA", "SPGI", "SYK"])
    _seed(tmp_path, symbol="MSFT")
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"MSFT": _ai("MSFT", conclusion="ADVANCE_TO_THESIS")}),
        decision=ScriptedDecisionReasoner(_committee_payload(["MSFT", "MA", "SPGI", "SYK"], buy="MSFT")),
        now=NOW,
    )
    worker.run_cycle()
    pending = worker.approvals.store.pending()
    assert len(pending) == 1
    assert pending[0].ticker == "MSFT"
    for symbol in ("MA", "SPGI", "SYK"):
        item = worker.watch.store.by_ticker(symbol)
        assert item is not None
        assert item.status is WatchStatus.WATCH
        assert item.approval_id is None


def test_e_cash_can_win_all_candidates_declined(tmp_path):
    _seed_core_universe(tmp_path, ["MSFT", "MA", "LLY"])
    watch, approvals, notify = _services(tmp_path, now=NOW)
    worker = _worker(tmp_path, decision=ScriptedDecisionReasoner(_committee_payload(["MSFT", "MA", "LLY"], buy=None)), now=NOW)
    worker.watch = watch
    worker.approvals = approvals
    worker.notify = notify
    result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["MSFT", "MA", "LLY"], buy=None)),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert result.proposals_created == 0
    assert result.status in {"NO_ACTION", "OK"}
    assert approvals.store.pending() == []
    assert result.forced_buy is False


def test_f_etf_buy_validates_without_company_catalyst(tmp_path):
    cstore, _, _ = _stores(tmp_path)
    cand = _put_candidate(cstore, "SPY", CandidateStatus.WATCHING)
    ResearchStore(tmp_path).save(
        _fresh_report("SPY", cand.candidate_id, sc=SecurityClass.BROAD_MARKET_INDEX_ETF, sector="UNKNOWN")
    )
    WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE).save(_watch("SPY", cand.candidate_id))
    watch, approvals, notify = _services(tmp_path, now=NOW)
    result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["SPY"], buy="SPY", alloc=8.0, etf=True)),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert result.proposals_created == 1
    assert approvals.store.pending()[0].ticker == "SPY"
    assert approvals.store.pending()[0].placed_order is False


def test_g_spy_circular_comparison_is_not_required(tmp_path):
    test_f_etf_buy_validates_without_company_catalyst(tmp_path)


def test_h_weak_research_cannot_buy_via_starter_semantics(tmp_path):
    _seed(tmp_path, symbol="QUAL")
    payload = _committee_payload(["QUAL"], buy="QUAL")
    payload["theses"][0]["thesis_summary"] = ""
    payload["theses"][0]["why_position_should_exist"] = ""
    payload["theses"][0]["bull_case"] = ""
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"QUAL": _ai("QUAL", conclusion="ADVANCE_TO_THESIS")}),
        decision=ScriptedDecisionReasoner(payload),
        now=NOW,
    )
    result = worker.run_cycle()
    assert result.proposals_created == 0
    assert worker.approvals.store.pending() == []


def test_i_risk_gate_still_blocks_committee_buy(tmp_path):
    _seed(tmp_path, symbol="MSFT")
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"MSFT": _ai("MSFT", conclusion="ADVANCE_TO_THESIS")}),
        decision=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy="MSFT")),
        now=NOW,
        halted=True,
    )
    result = worker.run_cycle()
    assert result.proposals_created == 0
    assert worker.approvals.store.pending() == []
    item = worker.watch.store.by_ticker("MSFT")
    assert item is not None
    assert item.reason_for_watch == "risk_gate_blocked"
    kinds = {row.get("type") for row in read_jsonl(tmp_path / "logs" / "core_committee.jsonl")}
    assert "CORE_COMMITTEE_BLOCKED_BY_RISK" in kinds


def test_j_human_approval_still_required(tmp_path):
    _seed(tmp_path, symbol="MSFT")
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"MSFT": _ai("MSFT", conclusion="ADVANCE_TO_THESIS")}),
        decision=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy="MSFT")),
        now=NOW,
    )
    worker.run_cycle()
    pending = worker.approvals.store.pending()[0]
    assert pending.status is LiveApprovalStatus.PENDING
    assert pending.placed_order is False


def test_k_no_auto_execution(tmp_path):
    _seed(tmp_path, symbol="MSFT")
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"MSFT": _ai("MSFT", conclusion="ADVANCE_TO_THESIS")}),
        decision=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy="MSFT")),
        now=NOW,
    )
    result = worker.run_cycle()
    assert result.placement_attempted is False
    assert result.LIVE_ORDER_PLACEMENT is False
    committee = next(row for row in result.details if row.get("status") == "CORE_COMMITTEE")
    assert committee["auto_execution"] is False
    assert LIVE_ORDER_PLACEMENT is False


def test_l_fresh_watch_artifacts_enter_reevaluation_without_research(tmp_path):
    _seed_core_universe(tmp_path, ["CRM", "LLY", "MA", "MSFT"])
    watch, approvals, notify = _services(tmp_path, now=NOW)
    result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["CRM", "LLY", "MA", "MSFT"], buy=None)),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
        payload_fn=None,
        research_reasoner=None,
        fetcher=None,
    )
    assert result.research_called is False
    assert result.terra_called is False
    assert set(result.eligible_symbols) >= {"CRM", "LLY", "MA", "MSFT"}
    assert result.ai_stages_called == ["portfolio_decision"]
    assert "research" not in result.ai_stages_called


def test_m_stale_reports_excluded(tmp_path):
    _seed_core_universe(tmp_path, ["MSFT", "ANET"], stale=("ANET",))
    watch = WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    inp = collect_committee_input(
        research_store=ResearchStore(tmp_path),
        watch_store=watch,
        candidates=CandidateStore(discovery_state_dir(tmp_path, mode=RuntimeMode.LIVE) / "candidates.json", runtime_mode=RuntimeMode.LIVE.value),
        now=NOW,
    )
    assert "MSFT" in inp.symbols
    assert "ANET" not in inp.symbols
    assert any(row.get("symbol") == "ANET" and row.get("reason") == "stale_research" for row in inp.skipped) or any(
        row.get("symbol") == "ANET" and "stale" in str(row.get("reason") or "") for row in inp.skipped
    )


def test_n_duplicate_committee_run_is_idempotent(tmp_path):
    _seed_core_universe(tmp_path, ["MSFT", "MA"])
    watch, approvals, notify = _services(tmp_path, now=NOW)
    first = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["MSFT", "MA"], buy="MSFT")),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert first.proposals_created == 1
    second = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["MSFT", "MA"], buy="MSFT")),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert second.skipped_reason == "unchanged_fingerprint"
    assert second.proposals_created == 0
    assert len(approvals.store.pending()) == 1


def test_o_ai_budget_enforcement_fail_closed(tmp_path):
    _seed_core_universe(tmp_path, ["MSFT"])
    watch, approvals, notify = _services(tmp_path, now=NOW)

    class Denied:
        def reason(self, request):
            raise DecisionValidationError("AI budget blocked decision: monthly cap")

    result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=Denied(),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert result.status == "DEGRADED"
    assert "budget" in (result.reason or "").lower()
    assert result.proposals_created == 0
    assert approvals.store.pending() == []


def test_p_paper_state_untouched_by_live_repair(tmp_path):
    _seed_core_universe(tmp_path, ["MSFT"])
    paper = WatchStore(tmp_path, runtime_mode=RuntimeMode.PAPER)
    paper.save(_watch("MSFT", "cand-msft", paper=True, reason_for_watch="paper_only"))
    watch, approvals, notify = _services(tmp_path, now=NOW)
    paper_result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.PAPER,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy="MSFT")),
        context_fn=lambda: ctx(500),
        now=NOW,
    )
    assert paper_result.skipped_reason == "not_live"
    live = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy=None)),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert live.paper_state_touched is False
    assert paper.by_ticker("MSFT").reason_for_watch == "paper_only"
    assert paper.by_ticker("MSFT").runtime_mode == "PAPER"


def test_q_watch_reconsideration_metadata_persists(tmp_path):
    _seed_core_universe(tmp_path, ["MA", "MSFT"])
    watch, approvals, notify = _services(tmp_path, now=NOW)
    reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["MA", "MSFT"], buy="MSFT")),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    item = watch.store.by_ticker("MA")
    assert item is not None
    assert item.reconsideration
    assert item.reconsideration["not_an_auto_execution_condition"] is True
    assert "MSFT" in item.reconsideration["lost_to"] or "CASH" in item.reconsideration["lost_to"]
    assert item.reconsideration.get("valuation_condition")
    assert item.conditional_plan is None


def test_r_waiting_for_liquidity_unchanged(tmp_path):
    cstore, qstore, _ = _stores(tmp_path)
    cand = _put_candidate(cstore, "HD", CandidateStatus.RESEARCH_COMPLETE)
    _put_queue(qstore, cand, ResearchQueueStatus.COMPLETED)
    ResearchStore(tmp_path).save(_fresh_report("HD", cand.candidate_id))
    watches = WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    item = _watch("HD", cand.candidate_id, status=WatchStatus.WAITING_FOR_LIQUIDITY, proposed_notional=25.0, desired_allocation_pct=5.0)
    watches.save(item)
    _seed_core_universe(tmp_path, ["MSFT"])
    watch, approvals, notify = _services(tmp_path, now=NOW)
    watch.store.save(item)
    result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy=None)),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert "HD" not in result.eligible_symbols
    hd = watch.store.by_ticker("HD")
    assert hd is not None
    assert hd.status is WatchStatus.WAITING_FOR_LIQUIDITY
    assert hd.proposed_notional == 25.0


def test_reevaluation_refuses_research_kwargs(tmp_path):
    _seed_core_universe(tmp_path, ["MSFT"])
    with pytest.raises(RuntimeError, match="must not collect research"):
        reevaluate_live_core_committee(
            root=tmp_path,
            runtime_mode=RuntimeMode.LIVE,
            decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy=None)),
            context_fn=lambda: ctx(500),
            now=NOW,
            payload_fn=lambda candidate: None,
        )


def test_tactical_advance_still_uses_singleton_decision(tmp_path):
    _seed(tmp_path, symbol="ESTC", sleeve=Sleeve.TACTICAL)
    payload = _payload("ESTC")
    payload["theses"] = [_thesis("ESTC", "TACTICAL")]
    payload["theses"][0]["exit_policy"]["price_invalidation"] = "lose 8% from entry"
    payload["decisions"][0]["symbol"] = "ESTC"
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"ESTC": _ai("ESTC", conclusion="ADVANCE_TO_THESIS")}),
        decision=ScriptedDecisionReasoner(payload),
        now=NOW,
    )
    result = worker.run_cycle()
    assert not any(row.get("pending_committee") for row in result.details)
    assert not any(row.get("status") == "CORE_COMMITTEE" for row in result.details)


def test_committee_observability_events(tmp_path):
    _seed(tmp_path, symbol="MSFT")
    worker = _worker(
        tmp_path,
        research=ScriptedResearchReasoner({"MSFT": _ai("MSFT", conclusion="ADVANCE_TO_THESIS")}),
        decision=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy=None)),
        now=NOW,
    )
    worker.run_cycle()
    kinds = {row.get("type") for row in read_jsonl(tmp_path / "logs" / "core_committee.jsonl")}
    assert "CORE_COMMITTEE_STARTED" in kinds
    assert "CORE_COMMITTEE_ELIGIBLE_SET" in kinds
    assert "CORE_COMMITTEE_NO_ACTION" in kinds
    activity = {row.get("type") for row in read_activity(tmp_path, limit=50)}
    assert "CORE_COMMITTEE_STARTED" in activity


def test_committee_instructions_stay_multi_name_and_compact():
    from agentic_portfolio.decision.reasoner import COMMITTEE_REASONER_INSTRUCTIONS, REASONER_INSTRUCTIONS

    text = COMMITTEE_REASONER_INSTRUCTIONS
    assert "qualified researched individual names" in text or "several qualified CORE alternatives" in text
    assert "selected_allocations" in text
    assert "rankings" in text
    assert "Do not independently mint several correlated starter BUYs" in text
    assert "yield_known" in text
    assert "not a Treasury" in text
    assert "broad_market_residual" in text
    assert "strategic residual path" in text
    assert "Do not buy SPY from the snapshot alone" not in text
    assert "NO_ACTION / HOLD_CASH is allowed only when supported" in text
    assert REASONER_INSTRUCTIONS not in text or "Return JSON only:" in text
    assert "Every ADVANCE_TO_THESIS researched symbol in this packet still needs exactly one decisions[] row" not in text


def _spy_ctx(nav: float = 500):
    return ctx(nav, spy=SpyBenchmark(price=640.12, period_return=0.0042))


def _operational_spy_report(candidate_id: str, *, with_extras: bool = True):
    facts = [_fact("market_price", 640.12)]
    derived = []
    if with_extras:
        facts.extend(
            [
                _fact("pe_ratio", 26.2),
                _fact("fund_pe_ratio", 26.2),
            ]
        )
        derived.extend(
            [
                _fact("return_21d", 0.021),
                _fact("return_63d", 0.055),
                _fact("return_252d", 0.18),
                _fact("drawdown_from_52w_high", 0.03),
                _fact("sma_alignment", "up"),
            ]
        )
    return ResearchReport(
        research_id="pre-fix-spy",
        candidate_id=candidate_id,
        symbol="SPY",
        started_at=(NOW - timedelta(days=1)).isoformat(),
        completed_at=(NOW - timedelta(days=1)).isoformat(),
        provisional_sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
        market_price=640.12,
        research_status=ResearchStatus.RESEARCH_INCONCLUSIVE,
        subject_kind=_report("MSFT").subject_kind,
        executive_summary="Paid research skipped: core evidence is missing (fundamentals_or_financials).",
        missing_information=["financials.revenue", "source_unavailable:get_financials"],
        sources_observed=["get_equity_quotes"],
        sources_unavailable=["get_financials", "get_equity_news", "get_sec_filing_index"],
        confidence=ResearchConfidence.LOW,
        research_conclusion=ResearchConclusion.NEED_MORE_DATA,
        recommended_next_step="NEED_MORE_DATA",
        research_source="deterministic",
        facts=facts,
        derived_metrics=derived,
    )


def test_spy_snapshot_reaches_committee_when_research_is_operational_failure(tmp_path):
    _seed_core_universe(tmp_path, ["MSFT"])
    cstore, _, _ = _stores(tmp_path)
    cand = _put_candidate(cstore, "SPY", CandidateStatus.RESEARCH_INCONCLUSIVE)
    ResearchStore(tmp_path).save(_operational_spy_report(cand.candidate_id))
    captured: dict = {}

    def responder(request):
        captured["report_symbols"] = [row["symbol"] for row in request.reports]
        captured["alternatives"] = list(request.alternatives)
        captured["spy_included"] = request.policy_context.get("spy_included")
        captured["residual"] = request.policy_context.get("broad_market_residual")
        captured["cash"] = request.policy_context.get("cash_alternative")
        return _committee_payload(["MSFT"], buy=None)

    watch, approvals, notify = _services(tmp_path, now=NOW)
    result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(responder),
        context_fn=_spy_ctx,
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
        payload_fn=None,
        research_reasoner=None,
        fetcher=None,
    )
    assert "SPY" not in captured["report_symbols"]
    assert "MSFT" in captured["report_symbols"]
    assert "SPY" in captured["alternatives"]
    assert captured["spy_included"] is True
    residual = captured["residual"]
    assert residual["current_price"] == pytest.approx(640.12)
    assert residual["return_1d"] == pytest.approx(0.0042)
    assert residual["return_21d"] == pytest.approx(0.021)
    assert residual["return_63d"] == pytest.approx(0.055)
    assert residual["return_252d"] == pytest.approx(0.18)
    assert residual["trend"] == "up"
    assert residual["drawdown_from_52w_high"] == pytest.approx(0.03)
    assert residual["fund_pe"] == pytest.approx(26.2)
    assert residual["usable_for_comparison"] is True
    assert residual["strategic_residual_eligible"] is True
    assert residual["does_not_authorize_individual_stock_buy"] is True
    assert residual["does_not_authorize_buy"] is False
    assert residual["comparison_snapshot"] is True
    assert captured["cash"]["yield_known"] is True
    assert captured["cash"]["current_yield"] == pytest.approx(0.0)
    assert captured["cash"]["yield_source"] == "configured_actual_account_cash_yield"
    assert captured["cash"]["yield_as_of"] == "2026-09-02"
    assert captured["cash"]["yield_unit"] == "annualized_decimal"
    assert result.spy_included is True
    assert result.status == "NO_ACTION"
    assert result.proposals_created == 0
    assert result.forced_buy is False
    assert result.research_called is False
    assert result.terra_called is False
    assert result.paper_state_touched is False
    assert result.as_dict()["placement_attempted"] is False
    assert LIVE_ORDER_PLACEMENT is False
    assert monthly_cap() == Decimal("10")
    cash_pct = result.target_allocations.get("CASH")
    assert cash_pct == pytest.approx(100.0)
    assert result.target_allocations.get("SPY") in {None, 0, 0.0} or result.decisions.get("SPY") in {"NO_ACTION", "WATCH", None}


def test_spy_ranked_from_snapshot_without_company_research(tmp_path):
    _seed_core_universe(tmp_path, ["MSFT"])
    captured: dict = {}

    def responder(request):
        captured["spy_included"] = request.policy_context.get("spy_included")
        residual = request.policy_context.get("broad_market_residual") or {}
        captured["usable"] = residual.get("usable_for_comparison")
        assert "SPY" not in {row["symbol"] for row in request.reports}
        payload = _committee_payload(["MSFT"], buy=None)
        payload["comparison"]["ranking"] = ["CASH", "SPY", "MSFT"]
        payload["comparison"]["vs_spy"] = "Cash beats generic beta at unknown opportunity cost no longer; yield is known."
        return payload

    watch, approvals, notify = _services(tmp_path, now=NOW)
    result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(responder),
        context_fn=_spy_ctx,
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert captured["spy_included"] is True
    assert captured["usable"] is True
    assert result.status == "NO_ACTION"
    assert result.proposals_created == 0
    assert result.forced_buy is False
    assert result.target_allocations.get("CASH") == pytest.approx(100.0)


def test_committee_spy_buy_from_snapshot_uses_strategic_residual_path(tmp_path):
    _seed_core_universe(tmp_path, ["MSFT"])
    watch, approvals, notify = _services(tmp_path, now=NOW)
    result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy="SPY", alloc=8.0, etf=True)),
        context_fn=_spy_ctx,
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert result.proposals_created == 1
    pending = approvals.store.pending()
    assert pending
    assert pending[0].ticker == "SPY"
    assert pending[0].status is LiveApprovalStatus.PENDING
    assert pending[0].placed_order is False
    assert result.status == "OK"
    assert result.as_dict()["placement_attempted"] is False
    assert result.as_dict()["auto_execution"] is False
    assert LIVE_ORDER_PLACEMENT is False


def test_spy_included_is_false_without_snapshot_evidence(tmp_path):
    _seed_core_universe(tmp_path, ["MSFT"])
    captured: dict = {}

    def responder(request):
        captured["spy_included"] = request.policy_context.get("spy_included")
        captured["usable"] = (request.policy_context.get("broad_market_residual") or {}).get("usable_for_comparison")
        captured["alternatives"] = list(request.alternatives)
        return _committee_payload(["MSFT"], buy=None)

    watch, approvals, notify = _services(tmp_path, now=NOW)
    result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(responder),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert "SPY" in captured["alternatives"]
    assert captured["spy_included"] is False
    assert captured["usable"] is False
    assert result.spy_included is False
    assert result.status == "NO_ACTION"
    assert result.proposals_created == 0


def _empty_spy_ctx(nav: float = 1010):
    return ctx(nav, spy=SpyBenchmark(price=640.12, period_return=0.0042))


def test_a_empty_portfolio_can_select_broad_market_without_spy_thesis(tmp_path):
    watch, approvals, notify = _services(tmp_path, now=NOW)
    result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload([], buy="SPY", alloc=8.0, etf=True)),
        context_fn=_empty_spy_ctx,
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert result.status == "OK"
    assert result.proposals_created == 1
    pending = approvals.store.pending()
    assert pending
    assert pending[0].ticker == "SPY"
    assert pending[0].proposed_action == "BUY"
    assert pending[0].status is LiveApprovalStatus.PENDING
    assert pending[0].placed_order is False
    assert result.forced_buy is False
    assert result.auto_execution is False
    assert result.as_dict()["placement_attempted"] is False
    assert LIVE_ORDER_PLACEMENT is False
    gated_ok = any(row.get("risk_verdict") in {None, "PASS", "REQUIRES_ENHANCED_REVIEW"} or row.get("proposals_created") for row in result.symbol_rows)
    assert gated_ok
    kinds = {row.get("type") for row in read_jsonl(tmp_path / "logs" / "core_committee.jsonl")}
    assert "CORE_COMMITTEE_PROPOSAL_CREATED" in kinds
    assert "CORE_COMMITTEE_DECIDED" in kinds


def test_b_empty_portfolio_defensive_cash_still_valid(tmp_path):
    watch, approvals, notify = _services(tmp_path, now=NOW)
    result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload([], buy=None)),
        context_fn=_empty_spy_ctx,
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert result.status == "NO_ACTION"
    assert result.portfolio_action == "HOLD_CASH"
    assert result.proposals_created == 0
    assert approvals.store.pending() == []
    assert result.forced_buy is False
    assert result.target_allocations.get("CASH") == pytest.approx(100.0)


def test_c_individual_stock_without_advance_to_thesis_cannot_buy(tmp_path):
    cstore, _, _ = _stores(tmp_path)
    cand = _put_candidate(cstore, "MSFT", CandidateStatus.WATCHING)
    report = _fresh_report("MSFT", cand.candidate_id)
    report.research_conclusion = ResearchConclusion.KEEP_WATCHING
    ResearchStore(tmp_path).save(report)
    WatchStore(tmp_path, runtime_mode=RuntimeMode.LIVE).save(_watch("MSFT", cand.candidate_id))
    watch, approvals, notify = _services(tmp_path, now=NOW)
    result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy="MSFT")),
        context_fn=_empty_spy_ctx,
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert result.proposals_created == 0
    assert approvals.store.pending() == []
    assert result.status in {"DEGRADED", "SKIPPED", "NO_ACTION"}
    if result.status == "DEGRADED":
        assert "buy_add_requires" in (result.reason or "") or "no_research_report:MSFT" in (result.reason or "")


def test_d_individual_stock_with_advance_to_thesis_can_compete(tmp_path):
    _seed_core_universe(tmp_path, ["MSFT"])
    watch, approvals, notify = _services(tmp_path, now=NOW)
    result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy="MSFT", alloc=8.0)),
        context_fn=_empty_spy_ctx,
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert result.proposals_created == 1
    assert approvals.store.pending()[0].ticker == "MSFT"
    assert approvals.store.pending()[0].placed_order is False
    assert result.selected_symbols == ["MSFT"]


def test_e_same_day_no_action_may_skip_next_day_runs(tmp_path):
    _seed_core_universe(tmp_path, ["MSFT"])
    watch, approvals, notify = _services(tmp_path, now=NOW)
    payload = _committee_payload(["MSFT"], buy=None)
    first = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(payload),
        context_fn=_empty_spy_ctx,
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert first.status == "NO_ACTION"
    same_day = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(payload),
        context_fn=_empty_spy_ctx,
        now=NOW + timedelta(hours=2),
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert same_day.skipped_reason == "unchanged_fingerprint"
    nxt = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(payload),
        context_fn=_empty_spy_ctx,
        now=NOW + timedelta(hours=25),
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert nxt.skipped_reason != "unchanged_fingerprint"
    assert nxt.status == "NO_ACTION"
    kinds = {row.get("type") for row in read_jsonl(tmp_path / "logs" / "core_committee.jsonl")}
    assert "CORE_COMMITTEE_NO_ACTION_EXPIRED_REVIEW" in kinds


def test_e_unit_scheduled_expiry_checked_before_fingerprint(tmp_path):
    _seed_core_universe(tmp_path, ["MSFT"])
    inp = collect_committee_input(research_store=ResearchStore(tmp_path), now=NOW)
    context = _empty_spy_ctx()
    ledger = {
        "last_fingerprint": committee_fingerprint(inp, context),
        "last_run_at": NOW.isoformat(),
        "last_research_ids": [r.research_id for r in inp.reports],
        "last_holdings_symbols": [],
        "last_cash_allocation_pct": context.cash_allocation_pct,
        "last_status": "NO_ACTION",
        "last_portfolio_action": "HOLD_CASH",
        "last_risk_state": context.risk_state.value if context.risk_state else None,
    }
    run, why = should_run_committee(
        inp, context, ledger, now=NOW + timedelta(hours=2), trigger="scheduled_review", residual_eligible=True
    )
    assert run is False
    assert why == "unchanged_fingerprint"
    run, why = should_run_committee(
        inp, context, ledger, now=NOW + timedelta(hours=25), trigger="scheduled_review", residual_eligible=True
    )
    assert run is True
    assert why == "no_action_expired_review"


def test_f_new_research_id_reevaluates_immediately(tmp_path):
    cands = _seed_core_universe(tmp_path, ["MSFT"])
    watch, approvals, notify = _services(tmp_path, now=NOW)
    first = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy=None)),
        context_fn=_empty_spy_ctx,
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert first.status == "NO_ACTION"
    newer = _fresh_report("MSFT", cands["MSFT"].candidate_id, rid="res-MSFT-new")
    newer.started_at = (NOW + timedelta(minutes=5)).isoformat()
    ResearchStore(tmp_path).save(newer)
    second = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy=None)),
        context_fn=_empty_spy_ctx,
        now=NOW + timedelta(minutes=6),
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert second.skipped_reason != "unchanged_fingerprint"
    assert second.status == "NO_ACTION"


def test_g_holdings_or_cash_change_reevaluates_immediately(tmp_path):
    _seed_core_universe(tmp_path, ["MSFT"])
    watch, approvals, notify = _services(tmp_path, now=NOW)
    first = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy=None)),
        context_fn=_empty_spy_ctx,
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert first.status == "NO_ACTION"
    changed = ctx(
        1010,
        positions=[pos("CASHPROXY", 0.20, 1010, Sleeve.CORE_GROWTH, SecurityClass.INDIVIDUAL_EQUITY, "INFORMATION_TECHNOLOGY")],
        spy=SpyBenchmark(price=640.12, period_return=0.0042),
    )
    second = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy=None)),
        context_fn=lambda: changed,
        now=NOW + timedelta(minutes=10),
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    assert second.skipped_reason != "unchanged_fingerprint"
    assert second.status == "NO_ACTION"


def test_h_ai_budget_blocked_does_not_bypass(tmp_path):
    _seed_core_universe(tmp_path, ["MSFT"])
    cfg = load_ai_config()
    ledger = UsageLedger(tmp_path, config=cfg)
    data = ledger.load_month(now=NOW)
    data["spent"] = "10.00"
    ledger.save_month(data)
    gw = AIGateway(
        budget=BudgetManager(ledger, cfg, now_fn=lambda: NOW),
        providers={},
        config=cfg,
        runtime_mode=RuntimeMode.LIVE,
    )
    assert gw.budget.status().mode is BudgetMode.EXHAUSTED
    worker = _worker(
        tmp_path,
        gateway=gw,
        decision=ScriptedDecisionReasoner(_committee_payload(["MSFT"], buy="MSFT")),
        now=NOW,
        nav=1010,
    )
    result = worker.run_core_committee(_empty_spy_ctx(), trigger="scheduled_review")
    assert result["status"] == "BLOCKED"
    assert result["skipped_reason"] == "budget_exhausted"
    assert result["proposals_created"] == 0
    assert worker.approvals.store.pending() == []


def test_i_broad_market_proposal_requires_human_approval_not_execution(tmp_path):
    test_a_empty_portfolio_can_select_broad_market_without_spy_thesis(tmp_path)


def test_j_allocation_validation_rejects_inconsistent_or_over_ceiling_weights():
    snap = {
        "symbol": "SPY",
        "current_price": 640.12,
        "allows_strategic_allocation": True,
        "usable_for_comparison": True,
    }
    over_ceiling = _committee_payload(["MSFT"], buy="SPY", alloc=50.0, etf=True)
    with pytest.raises(DecisionValidationError, match="allocation_exceeds_ceiling:SPY"):
        validate_payload(
            over_ceiling,
            [_report("MSFT")],
            current_nav=1010,
            committee=True,
            residual_snapshot=snap,
        )
    over_total = _committee_payload(["MSFT"], buy="MSFT", alloc=8.0)
    over_total["decisions"][-1]["desired_allocation_pct"] = 100.0
    with pytest.raises(DecisionValidationError, match="allocation_weights_exceed_100"):
        validate_payload(
            over_total,
            [_report("MSFT")],
            current_nav=1010,
            committee=True,
            residual_snapshot=snap,
        )

