"""Proposal-only AI pipeline: deterministic shortlist → cheap screen → research → Risk Gate → proposal.

Stops before broker placement. Continues when AI is blocked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from agentic_portfolio.ai.config import load_ai_config, pipeline_limits
from agentic_portfolio.ai.context import assemble_context
from agentic_portfolio.ai.decision import decide_candidate
from agentic_portfolio.ai.errors import (
    MissingBrokerFacts,
    PaperContaminationError,
    PlacementForbidden,
    StaleSnapshotError,
)
from agentic_portfolio.ai.gateway import AIGateway
from agentic_portfolio.ai.identity import persist_identity, validate_live_candidate
from agentic_portfolio.ai.proposals import create_proposal
from agentic_portfolio.ai.research import research_candidate
from agentic_portfolio.ai.safety import (
    LIVE_ORDER_PLACEMENT,
    assert_broker_facts,
    assert_live_ai_isolated,
    assert_no_forbidden_tools,
    assert_proposal_only,
    assert_snapshot_fresh,
    refuse_placement,
)
from agentic_portfolio.ai.screening import screen_candidate
from agentic_portfolio.ai.store import AIArtifactStore
from agentic_portfolio.ai.types import (
    BudgetMode,
    DeepResearchResult,
    LiveProposal,
    PortfolioDecisionResult,
    ProposalStatus,
    RecommendedAction,
    ScreeningResult,
)
from agentic_portfolio.discovery.engine import run_discovery
from agentic_portfolio.discovery.snapshot import SecuritySnapshot, compute_spread_metrics
from agentic_portfolio.discovery.store import CandidateStore, DiscoveryRunStore, ResearchQueue
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.live.isolation import detect_paper_contamination
from agentic_portfolio.paper_fill.store import PaperFillStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules, load_policy
from agentic_portfolio.runtime import LIVE_SOURCE_OF_TRUTH, RuntimeMode
from agentic_portfolio.schemas import (
    Candidate,
    CandidateStatus,
    Freshness,
    PortfolioContext,
    SecurityClass,
    Sleeve,
    to_dict,
)


@dataclass
class PipelineResult:
    scan_id: str
    runtime_mode: str
    source_of_truth: str
    nav: float
    cash: float
    buying_power: float
    budget_mode: str
    budget_spent: Decimal
    budget_remaining: Decimal
    eligibility_count: int
    ranked: list[str]
    screened: list[ScreeningResult]
    researched: list[DeepResearchResult]
    decisions: list[PortfolioDecisionResult]
    proposals: list[LiveProposal]
    rejected: list[dict[str, Any]]
    ai_calls: int
    estimated_cost: Decimal
    actual_cost: Decimal
    placement_attempted: bool = False
    paper_contamination: bool = False
    ai_blocked: bool = False
    blockers: list[str] = field(default_factory=list)
    snapshot_id: str | None = None
    account: dict[str, Any] = field(default_factory=dict)
    validations: list[dict[str, Any]] = field(default_factory=list)


def _snap_dict(snap: SecuritySnapshot) -> dict[str, Any]:
    metrics = compute_spread_metrics(snap.bid, snap.ask) or {}
    return {
        "current_price": snap.current_price,
        "previous_close": snap.previous_close,
        "bid": snap.bid,
        "ask": snap.ask,
        "name": snap.name,
        "sector": snap.sector,
        "industry": snap.industry,
        "instrument_kind": snap.instrument_kind,
        "market_cap": snap.market_cap,
        "pe_ratio": snap.pe_ratio,
        "pb_ratio": snap.pb_ratio,
        "description": snap.description,
        "rsi": snap.rsi,
        "sma_50": snap.sma_50,
        "sma_200": snap.sma_200,
        "atr": snap.atr,
        "return_5d": snap.return_5d,
        "return_21d": snap.return_21d,
        "return_63d": snap.return_63d,
        "return_252d": snap.return_252d,
        "high_52_week": snap.high_52_week,
        "low_52_week": snap.low_52_week,
        "drawdown_from_52w_high": snap.drawdown_from_52w_high,
        "volume_vs_avg": snap.volume_vs_avg,
        "average_volume": snap.average_volume,
        "news_headlines": list(snap.news_headlines or []),
        "absolute_spread_usd": metrics.get("absolute_spread_usd"),
        "spread_percent": metrics.get("spread_percent"),
        "spread_bps": metrics.get("spread_bps"),
        "spread_pct": snap.spread_pct,
        "dollar_volume": snap.dollar_volume,
        "data_origin": snap.data_origin,
        "broker_instrument_id": snap.broker_instrument_id,
        "exchange": snap.exchange,
        "quote_as_of": snap.quote_as_of,
        "quote_source": snap.quote_source,
    }


def _candidate_from_validated_snapshot(snap: SecuritySnapshot, *, now: datetime) -> Candidate:
    """Turn an already-validated snapshot into a pipeline candidate. Does not run discovery."""
    cls = snap.classification
    sector = None
    if cls is not None and getattr(cls, "sector", None) is not None:
        sector = getattr(cls.sector, "value", None) or None
        if sector == "UNKNOWN":
            sector = snap.sector
    elif snap.sector:
        sector = snap.sector
    return Candidate(
        candidate_id=str(uuid4()),
        symbol=str(snap.symbol).upper(),
        discovered_at=now.isoformat(),
        discovery_source="explicit_ticker",
        discovery_sources=list(snap.sources or []),
        provisional_sleeve=Sleeve.CORE_GROWTH,
        primary_provisional_sleeve=Sleeve.CORE_GROWTH,
        security_class=cls.security_class if cls else None,
        classification_status=cls.status if cls else None,
        current_price=snap.current_price,
        market_cap=snap.market_cap,
        sector=sector,
        industry=snap.industry,
        freshness=Freshness.STALE if snap.data_stale else Freshness.FRESH,
        status=CandidateStatus.SHORTLISTED,
        reasons=["explicit_ticker"],
    )


def run_candidate_pipeline(
    snapshots: list[SecuritySnapshot],
    context: PortfolioContext,
    gateway: AIGateway,
    *,
    runtime_mode: RuntimeMode | str = RuntimeMode.PAPER,
    root: Path | None = None,
    now: datetime | None = None,
    persist: bool = True,
    snapshot: Mapping[str, Any] | None = None,
    snapshot_id: str | None = None,
    config: dict[str, Any] | None = None,
    live_trade_actions_allowed: bool = False,
    auto_execution: bool = False,
    sources_queried: list[str] | None = None,
    skip_ai: bool = False,
    skip_universe_discovery: bool = False,
) -> PipelineResult:
    """Deterministic discovery then optional AI. Never places."""
    stamp = now or datetime.now(timezone.utc)
    mode = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode).upper()
    base = root or project_root()
    cfg = config or load_ai_config()
    limits = pipeline_limits(cfg)
    rules = load_account_rules()
    exe = dict(rules.get("execution") or {})
    assert_proposal_only(
        live_trade_actions_allowed=bool(exe.get("live_trade_actions_allowed") or live_trade_actions_allowed),
        auto_execution=bool(exe.get("auto_execution") or auto_execution),
    )
    assert_no_forbidden_tools(sources_queried or [], root=base)
    if LIVE_ORDER_PLACEMENT:
        refuse_placement("place_equity_order", root=base)

    blockers: list[str] = []
    leaks: list[str] = []
    if mode == RuntimeMode.LIVE.value:
        assert_broker_facts(context)
        paper = PaperFillStore(base).current_book()
        live_snap = dict(snapshot or {})
        if live_snap:
            max_age = int(limits.get("stale_snapshot_seconds") or 3600)
            market = (live_snap.get("market") or {}) if isinstance(live_snap.get("market"), dict) else {}
            if market.get("regular_hours_open"):
                max_age = int(limits.get("stale_snapshot_seconds_market_hours") or 900)
            assert_snapshot_fresh(live_snap, now=stamp, max_age_seconds=max_age)
            leaks = detect_paper_contamination(live_snap, paper, runtime_mode=RuntimeMode.LIVE)
            if leaks:
                raise PaperContaminationError("paper state leaked into LIVE AI pipeline: " + ", ".join(leaks))
            assert_live_ai_isolated(live_snap, runtime_mode=RuntimeMode.LIVE, paper_snapshot=paper, root=base)
        elif limits.get("require_broker_facts"):
            raise StaleSnapshotError("LIVE pipeline requires a confirmed Agentic snapshot")

    store = AIArtifactStore(base, runtime_mode=mode)
    budget = gateway.budget.status()
    ai_blocked = skip_ai or budget.mode is BudgetMode.EXHAUSTED
    if ai_blocked:
        blockers.append("ai_blocked")

    validations: list[dict[str, Any]] = []
    facts_by: dict[str, Any] = {}
    discovery_snaps: list[SecuritySnapshot] = []
    rejected: list[dict[str, Any]] = []
    for snap in snapshots:
        if mode == RuntimeMode.LIVE.value:
            check = validate_live_candidate(snap, now=stamp, runtime_mode=RuntimeMode.LIVE, config=cfg)
            validations.append(check.as_report())
            if persist:
                persist_identity(check, root=base, runtime_mode=mode, now=stamp)
            if not check.eligible_for_ai:
                rejected.append(
                    {
                        "ticker": snap.symbol,
                        "reason": check.status.value,
                        "stage": "identity_validation",
                        "details": list(check.reasons),
                    }
                )
                continue
            facts_by[snap.symbol.upper()] = check.facts
            discovery_snaps.append(snap)
        else:
            discovery_snaps.append(snap)

    if skip_universe_discovery:
        # Explicit diagnostic ticker: use the already-validated snapshot as the
        # screening input. Ordinary universe discovery would drop names like QUAL
        # on discovery score even when identity validation passed.
        eligible = [_candidate_from_validated_snapshot(s, now=stamp) for s in discovery_snaps]
        ranked = list(eligible)
        eligibility_count = len(eligible)
    else:
        discovery_root = store.root
        result_disc = run_discovery(
            discovery_snaps,
            context,
            persist=persist,
            promote_shortlist=True,
            now=stamp,
            sources_queried=sources_queried,
            candidate_store=CandidateStore(discovery_root / "candidates.json", runtime_mode=mode) if persist else None,
            queue_store=ResearchQueue(discovery_root / "research_queue.json", runtime_mode=mode) if persist else None,
            run_store=DiscoveryRunStore(discovery_root / "discovery_runs.json", runtime_mode=mode) if persist else None,
        )

        eligible = list(result_disc.candidates)
        eligibility_count = len(eligible)
        eligible.sort(key=lambda c: float(c.discovery_score or 0), reverse=True)
        max_elig = int(limits.get("max_eligibility_pass") or 20)
        max_rank = int(limits.get("max_quantitative_shortlist") or 8)
        min_score = float(limits.get("min_discovery_score") or 55)
        eligible = [c for c in eligible if float(c.discovery_score or 0) >= min_score][:max_elig]
        ranked = eligible[:max_rank]
        rejected.extend(
            {"ticker": c.symbol, "reason": c.rejection_reason or "below_threshold", "stage": "eligibility"}
            for c in result_disc.rejected
        )
        rejected.extend(
            {"ticker": c.symbol, "reason": "not_shortlisted", "stage": "ranking", "score": c.discovery_score}
            for c in eligible[max_rank:]
        )

    snap_by = {s.symbol.upper(): s for s in snapshots}
    screened: list[ScreeningResult] = []
    researched: list[DeepResearchResult] = []
    decisions: list[PortfolioDecisionResult] = []
    proposals: list[LiveProposal] = []
    policy = load_policy()
    max_screen = int(limits.get("max_ai_screen") or 4)
    max_research = int(limits.get("max_deep_research") or 2)
    max_decisions = int(limits.get("max_portfolio_decisions") or 2)
    min_screen = float(limits.get("min_screen_score") or 60)
    conserving = budget.mode in {BudgetMode.CONSERVING, BudgetMode.CRITICAL}

    screen_pool = ranked[: (1 if conserving else max_screen)]
    if not ai_blocked:
        for cand in screen_pool:
            if store.has_open_proposal(cand.symbol):
                rejected.append({"ticker": cand.symbol, "reason": "duplicate_open_proposal", "stage": "dedupe"})
                continue
            snap = snap_by.get(str(cand.symbol).upper())
            live_facts = facts_by.get(str(cand.symbol).upper())
            if mode == RuntimeMode.LIVE.value:
                if snap is None or live_facts is None:
                    rejected.append({"ticker": cand.symbol, "reason": "INVALID_IDENTITY", "stage": "identity_validation"})
                    continue
                gate = validate_live_candidate(snap, now=stamp, runtime_mode=RuntimeMode.LIVE, config=cfg)
                if not gate.eligible_for_ai:
                    rejected.append(
                        {
                            "ticker": cand.symbol,
                            "reason": gate.status.value,
                            "stage": "identity_validation",
                            "details": list(gate.reasons),
                        }
                    )
                    continue
                live_facts = gate.facts
            ctx = assemble_context(
                cand.symbol,
                context,
                now_iso=stamp.isoformat(),
                runtime_mode=mode,
                snapshot=_snap_dict(snap) if snap else None,
                policy=policy,
                discovery=to_dict(cand),
                prior_research=store.latest_for_ticker("research", cand.symbol),
                instrument_facts=live_facts,
            )
            row = screen_candidate(gateway, ctx, persist=store if persist else None, now=stamp)
            screened.append(row)
            if getattr(row, "operational_failure", False) or (
                row.rejection_reason and row.classification in {"BUDGET_BLOCKED", "AI_UNAVAILABLE"}
            ):
                ai_blocked = True
                blockers.append(row.rejection_reason)
                break
            if (not row.worth_deep_research) or row.score < min_screen:
                rejected.append(
                    {
                        "ticker": cand.symbol,
                        "reason": row.rejection_reason or "screen_rejected",
                        "stage": "screening",
                        "score": row.score,
                    }
                )
    else:
        for cand in screen_pool:
            rejected.append({"ticker": cand.symbol, "reason": "ai_blocked", "stage": "screening"})

    research_pool = [s for s in screened if s.worth_deep_research and s.score >= min_screen][:max_research]
    if budget.mode is BudgetMode.CRITICAL:
        research_pool = []
        blockers.append("critical_mode_skips_new_research")

    for screen in research_pool:
        snap = snap_by.get(str(screen.ticker).upper())
        ctx = assemble_context(
            screen.ticker,
            context,
            now_iso=stamp.isoformat(),
            runtime_mode=mode,
            snapshot=_snap_dict(snap) if snap else None,
            policy=policy,
            discovery={"screening_score": screen.score, "classification": screen.classification},
            prior_research=store.latest_for_ticker("research", screen.ticker),
            instrument_facts=facts_by.get(str(screen.ticker).upper()),
        )
        report = research_candidate(gateway, ctx, context, persist=store if persist else None, now=stamp)
        researched.append(report)
        if getattr(report, "operational_failure", False) or (
            report.rejection_reason and report.recommended_action is RecommendedAction.REJECT and not report.thesis
        ):
            blockers.append(report.rejection_reason or "research_operational_failure")
            continue
        if report.recommended_action in {RecommendedAction.REJECT, RecommendedAction.WATCH}:
            rejected.append(
                {
                    "ticker": screen.ticker,
                    "reason": report.rejection_reason or report.recommended_action.value,
                    "stage": "research",
                }
            )
            if report.recommended_action is RecommendedAction.REJECT:
                continue

        if len(decisions) >= max_decisions:
            rejected.append({"ticker": screen.ticker, "reason": "decision_cap", "stage": "decision"})
            continue
        decision = decide_candidate(gateway, ctx, context, report, persist=store if persist else None, now=stamp)
        if getattr(decision, "operational_failure", False):
            blockers.append(decision.rejection_reason or "decision_operational_failure")
            continue
        decisions.append(decision)
        cand = next((c for c in ranked if c.symbol == screen.ticker), None)
        sleeve = cand.provisional_sleeve if cand else Sleeve.CORE_GROWTH
        price = snap.current_price if snap else None
        proposal = create_proposal(
            decision,
            context,
            store=store,
            runtime_mode=mode,
            snapshot_id=snapshot_id,
            screening_id=screen.screening_id,
            research_id=report.research_id,
            security_class=cand.security_class if cand and cand.security_class else SecurityClass.INDIVIDUAL_EQUITY,
            sleeve=sleeve,
            price=price,
            now=stamp,
            live_trade_actions_allowed=bool(exe.get("live_trade_actions_allowed")),
            auto_execution=bool(exe.get("auto_execution")),
            root=base,
        )
        proposals.append(proposal)

    calls = list(gateway.calls)
    estimated = sum((c.estimated_cost for c in calls), Decimal("0"))
    actual = sum((c.actual_cost for c in calls), Decimal("0"))
    scan_id = str(uuid4())
    account = {"account_number": context.account_number}
    if persist:
        store.save_scan(
            scan_id,
            {
                "scan_id": scan_id,
                "created_at": stamp.isoformat(),
                "runtime_mode": mode,
                "eligibility_count": eligibility_count,
                "ranked": [c.symbol for c in ranked],
                "screened": [s.ticker for s in screened],
                "researched": [r.ticker for r in researched],
                "proposals": [p.proposal_id for p in proposals],
                "rejected": rejected,
                "placement_attempted": False,
                "budget_mode": budget.mode.value,
                "snapshot_id": snapshot_id,
            },
        )
    after = gateway.budget.status()
    append_jsonl(
        {
            "type": "AI_PIPELINE_SCAN",
            "scan_id": scan_id,
            "runtime_mode": mode,
            "placement_attempted": False,
            "ai_calls": len(calls),
            "proposals": len(proposals),
        },
        base / "logs" / "ai_pipeline.jsonl",
    )
    return PipelineResult(
        scan_id=scan_id,
        runtime_mode=mode,
        source_of_truth=LIVE_SOURCE_OF_TRUTH if mode == RuntimeMode.LIVE.value else "isolated_paper_book",
        nav=context.current_nav,
        cash=context.cash,
        buying_power=context.buying_power,
        budget_mode=after.mode.value,
        budget_spent=after.spent,
        budget_remaining=after.remaining,
        eligibility_count=eligibility_count,
        ranked=[c.symbol for c in ranked],
        screened=screened,
        researched=researched,
        decisions=decisions,
        proposals=proposals,
        rejected=rejected,
        ai_calls=len(calls),
        estimated_cost=estimated,
        actual_cost=actual,
        placement_attempted=False,
        paper_contamination=bool(leaks),
        ai_blocked=ai_blocked,
        blockers=blockers,
        snapshot_id=snapshot_id,
        account=account,
        validations=validations,
    )
