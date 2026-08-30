"""Position monitoring + thesis reassessment engine.

Existing positions → facts/triggers → optional Research refresh → thesis
reassessment → Portfolio Decision → ProposedAction → Risk Gate.

Python owns facts, triggers, state, persistence, and hard safety.
AI interprets new evidence. No broker stops. No execution tools.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from agentic_portfolio.decision.engine import run_portfolio_decision, to_proposed_action
from agentic_portfolio.decision.reasoner import DecisionReasoner
from agentic_portfolio.decision.types import GatedAction, NameDecision
from agentic_portfolio.journal import append_jsonl, append_risk_decision
from agentic_portfolio.monitoring.facts import assemble_facts, build_packet
from agentic_portfolio.monitoring.reasoner import REASONER_INSTRUCTIONS, MonitoringReasoner
from agentic_portfolio.monitoring.safety import assert_no_forbidden_tools
from agentic_portfolio.monitoring.store import MonitoringStore
from agentic_portfolio.monitoring.triggers import detect_triggers, is_price_move_alone, preliminary_state
from agentic_portfolio.monitoring.types import (
    MonitoredPosition,
    MonitoringFacts,
    MonitoringReasoningRequest,
    MonitoringResult,
    MonitoringState,
    PositionObservation,
    ThesisReassessment,
    Trigger,
)
from agentic_portfolio.monitoring.validate import MonitoringValidationError, to_reassessment, validate_payload
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_monitoring_config
from agentic_portfolio.research.engine import request_refresh, run_research
from agentic_portfolio.research.packet import ResearchPayload
from agentic_portfolio.research.reasoner import ResearchReasoner
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.types import ResearchReport, ResearchSubjectKind
from agentic_portfolio.risk_gate import evaluate
from agentic_portfolio.schemas import (
    Candidate,
    Decision,
    PortfolioContext,
    Position,
    Sleeve,
    ThesisRecord,
    ThesisStatus,
    to_dict,
)
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry

DECISION_ACTIONS = {Decision.HOLD, Decision.ADD, Decision.REDUCE, Decision.SELL, Decision.NO_ACTION}


def journal_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "logs" / "position_monitor.jsonl"


def run_position_monitor(
    context: PortfolioContext,
    observations: Mapping[str, PositionObservation] | list[PositionObservation] | None = None,
    *,
    reasoner: MonitoringReasoner | None = None,
    decision_reasoner: DecisionReasoner | None = None,
    research_reasoner: ResearchReasoner | None = None,
    research_payloads: Mapping[str, ResearchPayload] | None = None,
    reports: Mapping[str, ResearchReport] | list[ResearchReport] | None = None,
    theses: ThesisRegistry | None = None,
    sleeves: SleeveRegistry | None = None,
    research_store: ResearchStore | None = None,
    store: MonitoringStore | None = None,
    persist: bool = True,
    now: datetime | None = None,
    config: dict | None = None,
    journal: Path | None = None,
) -> MonitoringResult:
    """Monitor holdings. Meaningful triggers reassess thesis and may hit Risk Gate."""
    cfg = config or load_monitoring_config()
    now = now or datetime.now(timezone.utc)
    run_id = str(uuid4())
    obs_map = _obs_map(observations)
    used: list[str] = []
    for obs in obs_map.values():
        used.extend(obs.sources_observed)
    assert_no_forbidden_tools(used)
    report_map = _report_map(reports)
    theses = theses or (ThesisRegistry() if persist else None)
    sleeves = sleeves or (SleeveRegistry() if persist else None)
    rstore = research_store if research_store is not None else (ResearchStore() if persist else None)

    _journal(
        {"type": "POSITION_MONITOR_STARTED", "run_id": run_id, "symbols": [p.symbol for p in context.positions]},
        journal,
        persist=persist,
    )

    rows: list[MonitoredPosition] = []
    packet_ids: list[str] = []
    unsupported: list[str] = []
    errors: list[str] = []
    for position in context.positions:
        row = _monitor_one(
            position,
            context,
            obs_map.get(position.symbol.upper()),
            reasoner=reasoner,
            decision_reasoner=decision_reasoner,
            research_reasoner=research_reasoner,
            research_payloads=research_payloads or {},
            report_map=report_map,
            theses=theses,
            sleeves=sleeves,
            research_store=rstore,
            persist=persist,
            now=now,
            config=cfg,
            journal=journal,
        )
        packet_ids.append(row.facts.symbol)
        rows.append(row)
        if row.reassessment:
            unsupported.extend(row.reassessment.unsupported_claims)
        report_map[position.symbol.upper()] = row.research or report_map.get(position.symbol.upper())

    result = MonitoringResult(
        run_id=run_id,
        packet_ids=packet_ids,
        positions=rows,
        context=context,
        execution_attempted=False,
        broker_stop_orders_created=0,
        theses_activated=0,
        unsupported_claims=unsupported,
        validation_errors=errors,
    )
    if persist:
        (store or MonitoringStore()).save(run_id, _run_record(result, now))
    _journal(
        {
            "type": "POSITION_MONITOR_COMPLETED",
            "run_id": run_id,
            "symbols": [r.symbol for r in rows],
            "states": {r.symbol: r.state.value for r in rows},
            "actions": {r.symbol: r.recommended_action.value for r in rows},
            "proposed_actions": sum(len(r.gated_actions) for r in rows),
            "execution_attempted": False,
            "broker_stop_orders_created": 0,
        },
        journal,
        persist=persist,
    )
    return result


def apply_sleeve_guardrails(
    reassessment: ThesisReassessment,
    facts: MonitoringFacts,
    triggers: list[Trigger],
) -> ThesisReassessment:
    """Hard safety. Not a stock-picking overlay."""
    reassessment.broker_stop_orders_created = False
    if facts.sleeve == Sleeve.CORE_GROWTH and is_price_move_alone(triggers):
        reassessment.core_price_not_used_as_invalidation = True
        if reassessment.new_status == ThesisStatus.INVALIDATED:
            reassessment.new_status = ThesisStatus.UNCHANGED
            reassessment.unsupported_claims.append("core_price_move_cannot_invalidate")
        if reassessment.recommended_action == Decision.SELL:
            reassessment.recommended_action = Decision.HOLD
            reassessment.unsupported_claims.append("core_price_move_cannot_sell")
        if reassessment.monitoring_state in {
            MonitoringState.THESIS_INVALIDATED,
            MonitoringState.EXIT_CONDITION_TRIGGERED,
        }:
            if any(t.kind.value == "RESEARCH_REFRESH_REQUIRED" for t in triggers):
                reassessment.monitoring_state = MonitoringState.RESEARCH_REFRESH_REQUIRED
            else:
                reassessment.monitoring_state = MonitoringState.REVIEW_REQUIRED
    return reassessment


def _monitor_one(
    position: Position,
    context: PortfolioContext,
    observation: PositionObservation | None,
    *,
    reasoner: MonitoringReasoner | None,
    decision_reasoner: DecisionReasoner | None,
    research_reasoner: ResearchReasoner | None,
    research_payloads: Mapping[str, ResearchPayload],
    report_map: dict[str, ResearchReport],
    theses: ThesisRegistry | None,
    sleeves: SleeveRegistry | None,
    research_store: ResearchStore | None,
    persist: bool,
    now: datetime,
    config: dict,
    journal: Path | None,
) -> MonitoredPosition:
    symbol = position.symbol.upper()
    thesis = theses.current_for_symbol(symbol) if theses is not None else None
    report = report_map.get(symbol) or (research_store.latest_for_symbol(symbol) if research_store else None)
    facts = assemble_facts(position, context, thesis=thesis, report=report, observation=observation, now=now, config=config)
    triggers = detect_triggers(facts, observation)
    pre = preliminary_state(facts, triggers)
    refresh_requested = False
    if report is not None and (
        facts.earnings_event
        or facts.major_news
        or facts.material_filing
        or facts.research_freshness == "RESEARCH_REFRESH_REQUIRED"
        or any(t.kind.value == "PRICE_MOVE" for t in triggers)
    ):
        report = request_refresh(
            report,
            earnings_event=facts.earnings_event,
            major_news=facts.major_news,
            material_filing=facts.material_filing,
            price_move_pct=facts.price_move_pct,
            thesis_concern=any(t.kind.value == "THESIS_INVALIDATION_CANDIDATE" for t in triggers),
            now=now,
            persist=persist,
            journal=journal,
        )
        refresh_requested = report.freshness.value == "RESEARCH_REFRESH_REQUIRED"
        facts.research_freshness = report.freshness.value
        facts.refresh_triggers = list(report.refresh_triggers)
    payload = research_payloads.get(symbol)
    if research_reasoner is not None and payload is not None and (refresh_requested or report is None):
        cand = Candidate(
            candidate_id=report.candidate_id if report else f"monitor-{symbol}",
            symbol=symbol,
            discovered_at=now.isoformat(),
            discovery_source="position_monitor",
            provisional_sleeve=facts.sleeve or Sleeve.CORE_GROWTH,
            current_price=facts.current_price,
        )
        refreshed = run_research(
            cand,
            payload,
            context,
            research_reasoner,
            subject_kind=ResearchSubjectKind.EXISTING_POSITION_REVIEW,
            existing_thesis_id=facts.thesis_id,
            persist=persist,
            now=now,
            journal=journal,
        )
        report = refreshed.report
        refresh_requested = True
    packet = build_packet(facts, triggers, pre, context, thesis=thesis, report=report, now=now, config=config)

    row = MonitoredPosition(
        symbol=symbol,
        facts=facts,
        triggers=triggers,
        preliminary_state=pre,
        state=pre,
        research=report,
        thesis=thesis,
        recommended_action=Decision.NO_ACTION,
        research_refresh_requested=refresh_requested,
    )
    if pre == MonitoringState.HEALTHY or reasoner is None:
        if theses is not None and thesis is not None and facts.current_price is not None:
            theses.record_price_observation(thesis.thesis_id, facts.current_price, observed_at=now.isoformat())
        return row

    try:
        raw = reasoner.reason(
            MonitoringReasoningRequest(
                packet=to_dict(packet),
                facts=to_dict(facts),
                triggers=[to_dict(t) for t in triggers],
                thesis=to_dict(thesis) if thesis else None,
                research_brief=packet.research_brief,
                portfolio_context=to_dict(packet.portfolio_facts) if packet.portfolio_facts else {},
                policy_context=dict(packet.policy_context),
                instructions=REASONER_INSTRUCTIONS,
            )
        )
        normalized, claims = validate_payload(raw, symbol)
        reassessment = to_reassessment(
            normalized,
            thesis_id=facts.thesis_id,
            prior_status=facts.thesis_status,
            unsupported=claims,
        )
        reassessment = apply_sleeve_guardrails(reassessment, facts, triggers)
    except MonitoringValidationError as exc:
        _journal({"type": "POSITION_MONITOR_INCONCLUSIVE", "symbol": symbol, "reason": str(exc)}, journal, persist=persist)
        row.state = MonitoringState.REVIEW_REQUIRED
        row.recommended_action = Decision.NO_ACTION
        return row

    row.reassessment = reassessment
    row.state = reassessment.monitoring_state
    row.recommended_action = reassessment.recommended_action
    if theses is not None and thesis is not None:
        _persist_reassessment(theses, thesis, reassessment, facts, now)
        row.thesis = theses.get(thesis.thesis_id) or thesis

    row.gated_actions = _route_action(
        reassessment,
        context,
        report,
        row.thesis,
        sleeves,
        theses,
        decision_reasoner,
        persist,
        now,
        journal,
    )
    if row.gated_actions:
        row.recommended_action = row.gated_actions[0].proposed_action.decision
        row.decision_result = None
    return row


def _route_action(
    reassessment: ThesisReassessment,
    context: PortfolioContext,
    report: ResearchReport | None,
    thesis: ThesisRecord | None,
    sleeves: SleeveRegistry | None,
    theses: ThesisRegistry | None,
    decision_reasoner: DecisionReasoner | None,
    persist: bool,
    now: datetime,
    journal: Path | None,
) -> list[GatedAction]:
    action = reassessment.recommended_action
    if action not in DECISION_ACTIONS:
        return []
    if action == Decision.ADD and decision_reasoner is None:
        return []
    if decision_reasoner is not None and report is not None:
        result = run_portfolio_decision(
            [report],
            context,
            decision_reasoner,
            theses=theses,
            sleeves=sleeves,
            persist=persist,
            now=now,
            journal=journal,
        )
        if result.gated_actions or action == Decision.ADD:
            return list(result.gated_actions)
    if report is None:
        return []
    nd = NameDecision(
        symbol=reassessment.symbol,
        decision=action,
        desired_allocation_pct=reassessment.desired_allocation_pct,
        rationale=reassessment.rationale,
        thesis_id=reassessment.thesis_id,
        research_id=thesis.research_id if thesis else report.research_id,
    )
    proposed = to_proposed_action(nd, context, report, thesis)
    if proposed is None:
        return []
    if action == Decision.ADD:
        proposed.investment_thesis_review_complete = True
        proposed.risk_review_complete = True
    risk = evaluate(context, proposed, sleeves=sleeves, theses=theses)
    if persist:
        append_risk_decision(risk, journal)
    return [GatedAction(proposed_action=proposed, risk=risk, thesis_id=reassessment.thesis_id)]


def _persist_reassessment(
    theses: ThesisRegistry,
    thesis: ThesisRecord,
    reassessment: ThesisReassessment,
    facts: MonitoringFacts,
    now: datetime,
) -> None:
    if facts.current_price is not None:
        theses.record_price_observation(thesis.thesis_id, facts.current_price, observed_at=now.isoformat())
    new_status = reassessment.new_status
    if thesis.status == ThesisStatus.DRAFT and new_status in {ThesisStatus.UNCHANGED, ThesisStatus.STRENGTHENED}:
        new_status = ThesisStatus.DRAFT
    if new_status != thesis.status and not (thesis.status == ThesisStatus.DRAFT and new_status == ThesisStatus.DRAFT):
        theses.set_status(thesis.thesis_id, new_status, reason=reassessment.rationale)
    theses.add_review(
        thesis.thesis_id,
        review_type="POSITION_MONITOR_REVIEW",
        notes=reassessment.rationale,
        reviewed_at=now.isoformat(),
    )


def _obs_map(observations: Mapping[str, PositionObservation] | list[PositionObservation] | None) -> dict[str, PositionObservation]:
    if observations is None:
        return {}
    if isinstance(observations, Mapping):
        return {str(k).upper(): v for k, v in observations.items()}
    return {o.symbol.upper(): o for o in observations}


def _report_map(reports: Mapping[str, ResearchReport] | list[ResearchReport] | None) -> dict[str, ResearchReport]:
    if reports is None:
        return {}
    if isinstance(reports, Mapping):
        return {str(k).upper(): v for k, v in reports.items()}
    return {r.symbol.upper(): r for r in reports}


def _run_record(result: MonitoringResult, now: datetime) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "created_at": now.isoformat(),
        "symbols": [p.symbol for p in result.positions],
        "states": {p.symbol: p.state.value for p in result.positions},
        "actions": {p.symbol: p.recommended_action.value for p in result.positions},
        "positions": [
            {
                "symbol": p.symbol,
                "state": p.state.value,
                "preliminary_state": p.preliminary_state.value,
                "recommended_action": p.recommended_action.value,
                "triggers": [to_dict(t) for t in p.triggers],
                "reassessment": to_dict(p.reassessment) if p.reassessment else None,
                "gated_actions": [
                    {
                        "proposed_action": to_dict(g.proposed_action),
                        "risk": {
                            "verdict": g.risk.verdict.value,
                            "execution_permitted": g.risk.execution_permitted,
                            "recommendation_permitted": g.risk.recommendation_permitted,
                            "reasons": to_dict(g.risk.reasons),
                        },
                        "thesis_id": g.thesis_id,
                    }
                    for g in p.gated_actions
                ],
                "research_refresh_requested": p.research_refresh_requested,
                "broker_stop_orders_created": 0,
                "execution_attempted": False,
            }
            for p in result.positions
        ],
        "execution_attempted": False,
        "broker_stop_orders_created": 0,
        "theses_activated": 0,
        "unsupported_claims": list(result.unsupported_claims),
        "nav": result.context.current_nav if result.context else None,
    }


def _journal(row: dict, path: Path | None, *, persist: bool = True) -> None:
    if path is None and not persist:
        return
    append_jsonl(row, path or journal_path())
