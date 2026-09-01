"""Candidate Discovery engine.

Sits after read-only market adapters and before deep Research / Thesis.
Does not create ACTIVE theses, BUY actions, or call execution tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from agentic_portfolio.discovery.channels import ChannelNomination, run_channels
from agentic_portfolio.discovery.eligibility import hard_reject, liquidity_status
from agentic_portfolio.discovery.freshness import freshness_deadline_at
from agentic_portfolio.discovery.safety import DISCOVERY_FORBIDDEN_TOOLS, assert_no_forbidden_tools
from agentic_portfolio.discovery.scoring import score_signals
from agentic_portfolio.discovery.signals import merge_signals
from agentic_portfolio.discovery.snapshot import SecuritySnapshot
from agentic_portfolio.discovery.store import CandidateStore, DiscoveryRunStore, ResearchQueue
from agentic_portfolio.policy import load_discovery_config
from agentic_portfolio.schemas import (
    Candidate,
    CandidateStatus,
    DiscoveryPriority,
    DiscoveryRun,
    DiscoverySignal,
    Freshness,
    MarketRegime,
    MarketRegimeStatus,
    PortfolioContext,
    ResearchQueueEntry,
    ResearchQueueStatus,
    RiskState,
    Sleeve,
    SleeveHypothesis,
)


PRIORITY_ORDER = [
    DiscoveryPriority.LOW,
    DiscoveryPriority.MEDIUM,
    DiscoveryPriority.HIGH,
    DiscoveryPriority.URGENT_RESEARCH,
]

# Discrete new events that may reopen NEED_MORE_DATA before the cooldown.
# Membership on the earnings calendar (UPCOMING_EARNINGS / "earnings" in reasons)
# is not a material change — that is how the name was found in the first place.
MATERIAL_RETRY_FLAGS = {
    "MAJOR_NEWS",
    "MATERIAL_FILING",
    "REGIME_CHANGE",
    "HUMAN_REQUEST",
    "EARNINGS_EVENT",
    "POST_EARNINGS_MOVE",
}


@dataclass
class DiscoveryResult:
    run: DiscoveryRun
    candidates: list[Candidate]
    rejected: list[Candidate]
    queue: list[ResearchQueueEntry]
    conclusion: str
    theses_created: int = 0
    buy_actions_created: int = 0
    execution_attempted: bool = False


def run_discovery(
    snapshots: list[SecuritySnapshot],
    context: PortfolioContext,
    *,
    regime: MarketRegime | None = None,
    config: dict | None = None,
    candidate_store: CandidateStore | None = None,
    queue_store: ResearchQueue | None = None,
    run_store: DiscoveryRunStore | None = None,
    now: datetime | None = None,
    sources_queried: list[str] | None = None,
    session_context: dict | None = None,
    persist: bool = True,
    promote_shortlist: bool = True,
) -> DiscoveryResult:
    """Evaluate snapshots, persist candidates, and optionally enqueue research.

    NAV is used only for portfolio-aware priority (allocation %). It does not
    change the opportunity discovery_score.
    """
    cfg = config or load_discovery_config()
    assert_no_forbidden_tools(sources_queried or [])
    now = now or datetime.now(timezone.utc)
    regime = regime or MarketRegime.unknown(observed_at=now.isoformat())
    run_id = str(uuid4())
    started = now.isoformat()

    created: list[Candidate] = []
    rejected: list[Candidate] = []
    errors: list[str] = []
    symbols = sorted({s.symbol.upper() for s in snapshots})

    grouped = _group_snapshots(snapshots)
    for symbol, snaps in grouped.items():
        try:
            cand, was_reject = _evaluate_symbol(symbol, snaps, context, regime, cfg, now)
            if was_reject:
                rejected.append(cand)
            else:
                created.append(cand)
        except Exception as exc:  # noqa: BLE001 — record and continue the run
            errors.append(f"{symbol}: {type(exc).__name__}: {exc}")

    created = _apply_overlap_priority(created, cfg)

    if persist:
        cstore = candidate_store or CandidateStore()
        for c in created + rejected:
            existing = cstore.current_for_symbol(c.symbol)
            if existing is not None:
                c.candidate_id = existing.candidate_id
                if (
                    existing.status
                    in {
                        CandidateStatus.PROMOTED_TO_RESEARCH,
                        CandidateStatus.WATCHING,
                        CandidateStatus.RESEARCH_COMPLETE,
                    }
                    and c.status != CandidateStatus.REJECTED
                ):
                    # One current row per symbol. Do not reset a finished
                    # research outcome just because discovery re-scored it.
                    # RESEARCH_INCONCLUSIVE stays as this cycle's score so
                    # NEED_MORE_DATA can re-promote after cooldown.
                    c.status = existing.status
            cstore.upsert(c)

    queue_entries: list[ResearchQueueEntry] = []
    promoted_ids: list[str] = []
    if promote_shortlist:
        qstore = queue_store if persist else None
        for c in created:
            if c.status != CandidateStatus.SHORTLISTED:
                continue
            # Overlap may notch priority to LOW; that defers research order,
            # it does not discard a shortlisted candidate from the queue.
            if c.priority in {DiscoveryPriority.LOW} and not c.deferred_due_to_overlap:
                continue
            prior = qstore.latest_for_symbol(c.symbol, candidate_id=c.candidate_id) if persist and qstore is not None else None
            if persist and qstore is not None:
                blocked = qstore.active_entry(symbol=c.symbol, candidate_id=c.candidate_id)
                if blocked is not None:
                    continue
                allowed, _why = may_reopen_research(c, prior, now=now, config=cfg)
                if not allowed:
                    c.status = _stable_status_for(prior)
                    cstore.upsert(c)
                    continue
            entry = _queue_entry(c, cfg, now=now)
            if prior is not None:
                entry.research_generation = int(prior.research_generation or 1) + 1
            c.status = CandidateStatus.PROMOTED_TO_RESEARCH
            promoted_ids.append(c.candidate_id)
            queue_entries.append(entry)
            if persist:
                (qstore or ResearchQueue()).enqueue(entry)
                (candidate_store or CandidateStore()).upsert(c)

    conclusion = "NO_HIGH_QUALITY_CANDIDATES"
    if any(c.status in {CandidateStatus.SHORTLISTED, CandidateStatus.PROMOTED_TO_RESEARCH} for c in created):
        conclusion = "CANDIDATES_READY_FOR_RESEARCH"
    elif created:
        conclusion = "CANDIDATES_DISCOVERED_BELOW_SHORTLIST"
    if not snapshots:
        conclusion = "NO_HIGH_QUALITY_CANDIDATES"

    run = DiscoveryRun(
        run_id=run_id,
        started_at=started,
        completed_at=datetime.now(timezone.utc).isoformat() if persist else now.isoformat(),
        market_session_context=session_context or {
            "trading_session_id": context.trading_session_id,
            "session_fail_safe": context.session_fail_safe,
        },
        risk_state=context.risk_state.value,
        sources_queried=list(sources_queried or []),
        symbols_evaluated=symbols,
        candidates_created=[c.candidate_id for c in created],
        candidates_rejected=[c.candidate_id for c in rejected],
        candidates_promoted=promoted_ids,
        errors=errors,
        data_freshness="FRESH" if snapshots and not any(s.data_stale for s in snapshots) else ("STALE" if snapshots else "EMPTY"),
        conclusion=conclusion,
        regime_status=regime.status.value,
        theses_created=0,
        buy_actions_created=0,
        execution_attempted=False,
    )
    if persist:
        (run_store or DiscoveryRunStore()).save_run(run)

    return DiscoveryResult(
        run=run,
        candidates=created,
        rejected=rejected,
        queue=queue_entries,
        conclusion=conclusion,
    )


def expire_candidates(
    store: CandidateStore,
    *,
    now: datetime | None = None,
    queue: ResearchQueue | None = None,
) -> list[Candidate]:
    now = now or datetime.now(timezone.utc)
    expired: list[Candidate] = []
    for rec in store.all():
        if rec.status in {CandidateStatus.REJECTED, CandidateStatus.EXPIRED}:
            continue
        if rec.expires_at and _parse(rec.expires_at) <= now:
            rec.status = CandidateStatus.EXPIRED
            rec.freshness = Freshness.EXPIRED
            store.upsert(rec)
            expired.append(rec)
            if queue:
                for q in queue.all():
                    if q.candidate_id == rec.candidate_id and q.status in {
                        ResearchQueueStatus.QUEUED,
                        ResearchQueueStatus.IN_PROGRESS,
                        ResearchQueueStatus.RESEARCHING,
                    }:
                        queue.set_status(q.queue_id, ResearchQueueStatus.EXPIRED)
    return expired


def _group_snapshots(snapshots: list[SecuritySnapshot]) -> dict[str, list[SecuritySnapshot]]:
    grouped: dict[str, list[SecuritySnapshot]] = {}
    for s in snapshots:
        grouped.setdefault(s.symbol.upper(), []).append(s)
    return grouped


def _merge_snaps(snaps: list[SecuritySnapshot]) -> SecuritySnapshot:
    """Duplicate discovery sources merge into one evaluation snapshot."""
    from dataclasses import replace

    first = snaps[0]
    base = replace(
        first,
        sources=list(first.sources),
        revenue_periods=list(first.revenue_periods),
        net_income_periods=list(first.net_income_periods),
        net_margin_periods=list(first.net_margin_periods),
        gross_profit_periods=list(first.gross_profit_periods),
        news_headlines=list(first.news_headlines),
        evidence_refs=list(first.evidence_refs),
    )
    sources: list[str] = []
    for s in snaps:
        for src in s.sources:
            if src not in sources:
                sources.append(src)
    base.sources = sources
    for other in snaps[1:]:
        for field in (
            "name",
            "instrument_kind",
            "tradable",
            "current_price",
            "market_cap",
            "sector",
            "industry",
            "description",
            "pe_ratio",
            "classification",
            "liquidity",
            "rsi",
            "sma_50",
            "sma_200",
            "return_21d",
            "drawdown_from_52w_high",
        ):
            if getattr(base, field) is None and getattr(other, field) is not None:
                setattr(base, field, getattr(other, field))
        for seq in ("revenue_periods", "net_income_periods", "net_margin_periods", "news_headlines", "evidence_refs"):
            if not getattr(base, seq) and getattr(other, seq):
                setattr(base, seq, getattr(other, seq))
        if other.earnings_upcoming_days is not None:
            base.earnings_upcoming_days = other.earnings_upcoming_days
    return base


def _evaluate_symbol(
    symbol: str,
    snaps: list[SecuritySnapshot],
    context: PortfolioContext,
    regime: MarketRegime,
    cfg: dict,
    now: datetime,
) -> tuple[Candidate, bool]:
    snap = _merge_snaps(snaps)
    reason, evidence, extra_signals = hard_reject(snap, cfg)
    cid = str(uuid4())
    src = ",".join(snap.sources) if snap.sources else "unknown"
    if reason:
        cand = _blank_candidate(cid, snap, src, now, cfg, Sleeve.CORE_GROWTH)
        cand.status = CandidateStatus.REJECTED
        cand.rejection_reason = reason
        cand.rejection_evidence = evidence
        cand.signals = extra_signals
        cand.discovery_score = 0.0
        cand.reasons = [reason]
        return cand, True

    nominations = run_channels(snap, regime, cfg)
    if not nominations:
        cand = _blank_candidate(cid, snap, src, now, cfg, Sleeve.CORE_GROWTH)
        cand.status = CandidateStatus.REJECTED
        cand.rejection_reason = "no_channel_nomination"
        cand.rejection_evidence = ["no_core_quality_dislocation_tactical_or_speculative_setup"]
        cand.reasons = ["NO_HIGH_QUALITY_SETUP"]
        return cand, True

    scored: list[tuple[ChannelNomination, float, dict]] = []
    for nom in nominations:
        sc, breakdown = score_signals(nom.signals, nom.sleeve, cfg)
        scored.append((nom, sc, breakdown))
    primary_nom, primary_score, breakdown = _select_primary_nomination(scored)
    alternatives = [
        SleeveHypothesis(sleeve=n.sleeve, reason=n.sleeve_reason, confidence=n.sleeve_confidence)
        for n, s, _ in sorted(scored, key=lambda x: x[1], reverse=True)
        if n.sleeve != primary_nom.sleeve
    ]

    all_signals: list[DiscoverySignal] = []
    for nom, _, _ in scored:
        all_signals = merge_signals(all_signals, nom.signals)
    all_signals = merge_signals(all_signals, extra_signals)

    cand = _blank_candidate(cid, snap, src, now, cfg, primary_nom.sleeve)
    cand.discovery_score = primary_score
    cand.score_breakdown = breakdown
    cand.signals = all_signals
    cand.reasons = list(dict.fromkeys(r for n, _, _ in scored for r in n.reasons))
    cand.research_questions = list(dict.fromkeys(q for n, _, _ in scored for q in n.research_questions))
    cand.initial_observations = list(dict.fromkeys(o for n, _, _ in scored for o in n.observations))
    cand.event_flags = list(dict.fromkeys(f for n, _, _ in scored for f in n.event_flags))
    cand.known_risks = list(dict.fromkeys(k for n, _, _ in scored for k in n.known_risks))
    cand.channels = [n.channel.value for n, _, _ in scored]
    cand.sleeve_reason = primary_nom.sleeve_reason
    cand.sleeve_confidence = primary_nom.sleeve_confidence
    cand.primary_provisional_sleeve = primary_nom.sleeve
    cand.alternative_sleeves = alternatives
    cand.thesis_type = primary_nom.thesis_type
    cand.required_research_areas = list(cfg["research_areas"].get(primary_nom.sleeve.value, []))
    cand.supporting_evidence_refs = list(dict.fromkeys(snap.evidence_refs + [s.evidence_ref for s in all_signals if s.evidence_ref]))

    sleeve_cfg = cfg["scoring"][primary_nom.sleeve.value]
    if primary_score < float(sleeve_cfg["reject_below"]):
        cand.status = CandidateStatus.REJECTED
        cand.rejection_reason = "score_below_reject_threshold"
        cand.rejection_evidence = [f"score={primary_score}", f"threshold={sleeve_cfg['reject_below']}"]
        return cand, True

    if primary_nom.sleeve == Sleeve.SPECULATIVE:
        spec_min = float(cfg["rejection"]["speculative_min_dollar_volume"])
        spec_spread = float(cfg["rejection"]["speculative_max_spread_pct"])
        dv = snap.dollar_volume
        if dv is not None and dv < spec_min:
            cand.status = CandidateStatus.REJECTED
            cand.rejection_reason = "severe_speculative_liquidity"
            cand.rejection_evidence = [f"dollar_volume={dv}", f"min={spec_min}"]
            cand.known_risks.append("severe_speculative_liquidity")
            return cand, True
        if snap.spread_pct is not None and snap.spread_pct >= spec_spread:
            cand.status = CandidateStatus.REJECTED
            cand.rejection_reason = "severe_speculative_liquidity"
            cand.rejection_evidence = [f"spread_pct={snap.spread_pct}"]
            return cand, True

    cand.status = CandidateStatus.DISCOVERED
    if primary_score >= float(sleeve_cfg["shortlist_at"]):
        cand.status = CandidateStatus.SHORTLISTED

    _apply_portfolio_priority(cand, context, cfg, primary_score)
    return cand, False


def _select_primary_nomination(
    scored: list[tuple[ChannelNomination, float, dict]],
) -> tuple[ChannelNomination, float, dict]:
    """Pick the sleeve whose distinctive evidence actually applies.

    Highest raw score is not enough: opportunistic drawdown-from-high must not
    swallow a core, tactical, or speculative nomination that has its own setup.
    """
    if len(scored) == 1:
        return scored[0]

    def distinctive(nom: ChannelNomination) -> bool:
        names = {s.name for s in nom.signals}
        if nom.sleeve == Sleeve.OPPORTUNISTIC:
            return "selloff" in names or "post_earnings_overreaction" in names
        if nom.sleeve == Sleeve.TACTICAL:
            return bool(names & {"sma_alignment", "trend", "breakout", "pullback", "expansion", "medium_term"})
        if nom.sleeve == Sleeve.SPECULATIVE:
            return bool(names & {"asymmetric_upside", "optionality", "binary_or_pipeline", "high_growth_uncertainty"})
        if nom.sleeve == Sleeve.CORE_GROWTH:
            return bool(names & {"profitability", "revenue_growth", "diversified_fund", "competitive_position"})
        return True

    eligible = [row for row in scored if distinctive(row[0])] or list(scored)
    # A genuine selloff / post-earnings overreaction is the live setup.
    # Core quality may still exist as an alternative sleeve, but must not win
    # the primary route just because compounding scores higher than dislocation.
    if any(row[0].sleeve == Sleeve.OPPORTUNISTIC for row in eligible):
        without_core = [row for row in eligible if row[0].sleeve != Sleeve.CORE_GROWTH]
        if without_core:
            eligible = without_core
    eligible.sort(key=lambda row: row[1], reverse=True)
    return eligible[0]


def _blank_candidate(
    cid: str,
    snap: SecuritySnapshot,
    src: str,
    now: datetime,
    cfg: dict,
    sleeve: Sleeve,
) -> Candidate:
    ttl_h = float(cfg["ttl_hours"][sleeve.value])
    expires = (now + timedelta(hours=ttl_h)).isoformat()
    cls = snap.classification
    liq = liquidity_status(snap, cfg)
    return Candidate(
        candidate_id=cid,
        symbol=snap.symbol.upper(),
        discovered_at=now.isoformat(),
        discovery_source=src,
        discovery_sources=list(snap.sources),
        provisional_sleeve=sleeve,
        primary_provisional_sleeve=sleeve,
        security_class=cls.security_class if cls else None,
        classification_status=cls.status if cls else None,
        current_price=snap.current_price,
        market_cap=snap.market_cap,
        sector=cls.sector.value if cls and cls.sector.value != "UNKNOWN" else snap.sector,
        industry=snap.industry,
        liquidity_status=liq,
        expires_at=expires,
        freshness=Freshness.STALE if snap.data_stale else Freshness.FRESH,
    )


def _apply_portfolio_priority(cand: Candidate, context: PortfolioContext, cfg: dict, score: float) -> None:
    pcfg = cfg["priority"]
    priority = DiscoveryPriority.LOW
    if score >= float(pcfg["urgent_research_score"]) or (
        score >= float(pcfg["high_score"]) and "UPCOMING_EARNINGS" in cand.event_flags and cand.provisional_sleeve != Sleeve.TACTICAL
    ):
        priority = DiscoveryPriority.URGENT_RESEARCH
    elif score >= float(pcfg["high_score"]):
        priority = DiscoveryPriority.HIGH
    elif score >= float(pcfg["medium_score"]):
        priority = DiscoveryPriority.MEDIUM

    overlap = 0.0
    if cand.sector:
        overlap = float(context.sector_allocation_pct.get(cand.sector, 0.0) or 0.0)
        thresh = float(pcfg["sector_overlap_penalty_above_pct"])
        if overlap >= thresh:
            cand.overlap_penalty = overlap
            cand.known_risks.append(f"portfolio_sector_overlap={cand.sector}:{overlap:.2%}")
            priority = _notch_down(priority, int(pcfg["sector_overlap_priority_notch"]))

    core_pct = float(context.sleeve_allocation_pct.get(Sleeve.CORE_GROWTH.value, 0.0) or 0.0)
    if cand.provisional_sleeve == Sleeve.CORE_GROWTH and core_pct < float(pcfg["core_underweight_boost_if_below_pct"]):
        priority = _notch_up(priority, 1)
        cand.reasons.append("core_sleeve_underrepresented")

    risk = context.risk_state
    suppress = {Sleeve(s) for s in pcfg.get("risk_reduction_suppress_sleeves") or []}
    if risk == RiskState.RISK_REDUCTION and cand.provisional_sleeve in suppress:
        cand.status = CandidateStatus.DISCOVERED
        cand.priority = DiscoveryPriority.LOW
        cand.reasons.append("RISK_REDUCTION_SUPPRESSES_TACTICAL_SPECULATIVE_PRIORITY")
        cand.known_risks.append("cannot_currently_be_acted_upon_risk_reduction")
        return
    if risk == RiskState.DEFENSIVE:
        defensive = set(pcfg.get("defensive_sectors") or [])
        if cand.sector in defensive or (
            cand.security_class and cand.security_class.value == "BROAD_MARKET_INDEX_ETF"
        ):
            priority = _notch_up(priority, 1)
            cand.reasons.append("defensive_regime_research_priority")
        elif cand.provisional_sleeve in {Sleeve.TACTICAL, Sleeve.SPECULATIVE}:
            priority = _notch_down(priority, 1)
            cand.reasons.append("defensive_regime_deprioritize_risk_adding")
    if risk == RiskState.HALTED:
        cand.action_blocked_reason = pcfg["halted_action_flag"]
        cand.reasons.append("HALTED_research_may_continue_action_blocked")

    cand.priority = priority


def _notch_down(p: DiscoveryPriority, n: int) -> DiscoveryPriority:
    idx = max(0, PRIORITY_ORDER.index(p) - n)
    return PRIORITY_ORDER[idx]


def _notch_up(p: DiscoveryPriority, n: int) -> DiscoveryPriority:
    idx = min(len(PRIORITY_ORDER) - 1, PRIORITY_ORDER.index(p) + n)
    return PRIORITY_ORDER[idx]


def _apply_overlap_priority(candidates: list[Candidate], cfg: dict) -> list[Candidate]:
    """Penalize/defer crowded sector-sleeve groups. Never hard-reject on count.

    A later, higher-quality name must remain researchable so Research can
    compare peers (AAPL vs MSFT vs NVDA vs AVGO). Portfolio Decision later
    chooses capital. Do not reintroduce a max-N candidate cap.
    """
    pcfg = cfg.get("priority") or {}
    ocfg = pcfg.get("research_queue_overlap") or cfg.get("research_queue_overlap") or {}
    if pcfg.get("overlap_priority_penalty") is False or ocfg.get("apply_priority_penalty") is False:
        return candidates
    defer_non_leaders = ocfg.get("defer_non_leaders", True)
    flag = str(ocfg.get("flag") or "DEFERRED_DUE_TO_RESEARCH_QUEUE_OVERLAP")
    groups: dict[tuple[str, str], list[Candidate]] = {}
    for c in candidates:
        key = (c.provisional_sleeve.value, c.sector or "UNKNOWN")
        groups.setdefault(key, []).append(c)
    for key, members in groups.items():
        if len(members) < 2:
            continue
        sleeve, sector = key
        gid = f"cmp:{sleeve}:{sector}"
        ranked = sorted(members, key=lambda c: (-c.discovery_score, c.symbol))
        warning = f"OVERLAP_PRIORITY_PENALTY:{sleeve}:{sector}:n={len(members)}"
        for i, c in enumerate(ranked):
            c.comparison_group_id = gid
            if warning not in c.overlap_warnings:
                c.overlap_warnings.append(warning)
            risk = f"research_queue_sector_sleeve_overlap={sector}:{sleeve}:n={len(members)}"
            if risk not in c.known_risks:
                c.known_risks.append(risk)
            if i == 0:
                if "COMPARISON_GROUP_MEMBER" not in c.reasons:
                    c.reasons.append("COMPARISON_GROUP_MEMBER")
                continue
            if not defer_non_leaders:
                c.priority = _notch_down(c.priority, 1)
                if "OVERLAP_PRIORITY_PENALTY" not in c.reasons:
                    c.reasons.append("OVERLAP_PRIORITY_PENALTY")
                continue
            c.deferred_due_to_overlap = True
            c.priority = _notch_down(c.priority, 1)
            if flag not in c.reasons:
                c.reasons.append(flag)
            if "OVERLAP_PRIORITY_PENALTY" not in c.reasons:
                c.reasons.append("OVERLAP_PRIORITY_PENALTY")
    return candidates


def _stable_status_for(prior: ResearchQueueEntry | None) -> CandidateStatus:
    if prior is None:
        return CandidateStatus.DISCOVERED
    if prior.status is ResearchQueueStatus.REJECTED:
        return CandidateStatus.REJECTED
    if prior.status in {ResearchQueueStatus.NEED_MORE_DATA, ResearchQueueStatus.INCONCLUSIVE}:
        return CandidateStatus.RESEARCH_INCONCLUSIVE
    if prior.status is ResearchQueueStatus.COMPLETED:
        return CandidateStatus.WATCHING
    return CandidateStatus.DISCOVERED


def may_reopen_research(
    candidate: Candidate,
    prior: ResearchQueueEntry | None,
    *,
    now: datetime,
    config: dict | None = None,
) -> tuple[bool, str]:
    """Fresh discovery may reopen research only after a legitimate trigger."""
    if prior is None:
        return True, "no_prior"
    if prior.status in {ResearchQueueStatus.QUEUED, ResearchQueueStatus.RESEARCHING, ResearchQueueStatus.IN_PROGRESS}:
        return False, "active_queue"
    cfg = config or load_discovery_config()
    retry = dict(cfg.get("reassessment") or {})
    sleeve = candidate.provisional_sleeve.value if candidate.provisional_sleeve else "CORE_GROWTH"
    flags = {str(f).upper() for f in (candidate.event_flags or [])}
    material = bool(flags & MATERIAL_RETRY_FLAGS)
    last = _parse(prior.last_attempt_at or prior.enqueued_at) if (prior.last_attempt_at or prior.enqueued_at) else None
    elapsed_h = ((now - last).total_seconds() / 3600.0) if last else 10**9
    if prior.status in {ResearchQueueStatus.NEED_MORE_DATA, ResearchQueueStatus.INCONCLUSIVE}:
        hours = float(retry.get("need_more_data_min_retry_hours") or 24)
        if material or elapsed_h >= hours:
            return True, "need_more_data_retry"
        return False, "need_more_data_cooldown"
    if prior.status is ResearchQueueStatus.REJECTED:
        hours = float(retry.get("reject_min_retry_hours") or 168)
        if material or elapsed_h >= hours:
            return True, "reject_retry"
        return False, "reject_stable"
    if prior.status is ResearchQueueStatus.COMPLETED:
        watch_hours = dict(retry.get("watch_min_retry_hours") or {})
        hours = float(watch_hours.get(sleeve) or retry.get("completed_min_retry_hours") or 72)
        expired = False
        if prior.freshness_deadline:
            try:
                expired = _parse(prior.freshness_deadline) <= now
            except ValueError:
                expired = False
        if material or expired or elapsed_h >= hours:
            return True, "completed_stale_or_trigger"
        return False, "completed_waiting"
    if prior.status in {ResearchQueueStatus.EXPIRED, ResearchQueueStatus.DROPPED}:
        return True, "prior_terminal_expired"
    return False, "stable"


def _queue_entry(cand: Candidate, cfg: dict, *, now: datetime) -> ResearchQueueEntry:
    why = "; ".join(cand.reasons[:4]) or "shortlisted_for_research"
    stamp = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    enqueued_at = stamp.isoformat()
    return ResearchQueueEntry(
        queue_id=str(uuid4()),
        candidate_id=cand.candidate_id,
        symbol=cand.symbol,
        provisional_sleeve=cand.provisional_sleeve,
        discovery_score=cand.discovery_score,
        priority=cand.priority,
        why_research_warranted=why,
        required_research_areas=list(cand.required_research_areas or cfg["research_areas"].get(cand.provisional_sleeve.value, [])),
        freshness_deadline=freshness_deadline_at(cand.provisional_sleeve, stamp, cfg),
        status=ResearchQueueStatus.QUEUED,
        enqueued_at=enqueued_at,
        notes="Research priority is not trade urgency. Do not buy from this queue.",
        comparison_group_id=cand.comparison_group_id,
        overlap_warnings=list(cand.overlap_warnings),
        deferred_due_to_research_queue_overlap=cand.deferred_due_to_overlap,
    )


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def inspect_discovery_module_for_forbidden_tools(root: Path | None = None) -> list[str]:
    """Static check used by tests: Discovery source must not invoke execution tools."""
    from agentic_portfolio.paths import project_root as _root

    base = (root or _root()) / "src" / "agentic_portfolio" / "discovery"
    hits: list[str] = []
    for path in base.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for tool in DISCOVERY_FORBIDDEN_TOOLS:
            # Allow appearance only as a forbidden-set literal.
            if tool in text and "DISCOVERY_FORBIDDEN_TOOLS" not in text and "FORBIDDEN_MCP_TOOLS" not in text:
                if f'"{tool}"' in text or f"'{tool}'" in text:
                    hits.append(f"{path.name}:{tool}")
    return hits
