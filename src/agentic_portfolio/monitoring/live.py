"""LIVE POSITION_MONITOR wiring.

Uses the existing monitoring engine, thesis registry, CORE committee, Risk Gate,
and LiveApproval path. Does not place orders. Does not call AI every cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from agentic_portfolio.agent.activity import log_activity
from agentic_portfolio.monitoring.engine import run_position_monitor
from agentic_portfolio.monitoring.facts import assemble_facts
from agentic_portfolio.monitoring.triggers import detect_triggers, is_price_move_alone, preliminary_state
from agentic_portfolio.monitoring.types import MonitoringState, PositionObservation, TriggerKind
from agentic_portfolio.research.freshness import freshness_horizon
from agentic_portfolio.runtime import LIVE_SOURCE_OF_TRUTH, RuntimeMode, live_placement_enabled
from agentic_portfolio.schemas import Decision, GateVerdict, PortfolioContext, Sleeve
from agentic_portfolio.notify import NotificationKind
from agentic_portfolio.thesis_registry import ThesisRegistry

ACTIONABLE = {Decision.ADD, Decision.REDUCE, Decision.SELL}
RISK_PERMIT = {GateVerdict.PASS, GateVerdict.REQUIRES_ENHANCED_REVIEW}

MATERIAL_KINDS = {
    TriggerKind.EARNINGS_EVENT,
    TriggerKind.MAJOR_NEWS,
    TriggerKind.MATERIAL_FILING,
    TriggerKind.THESIS_REVIEW_TRIGGER,
    TriggerKind.THESIS_INVALIDATION_CANDIDATE,
    TriggerKind.EXIT_POLICY_CONDITION,
    TriggerKind.TACTICAL_PRICE_OR_TECHNICAL,
    TriggerKind.SPECULATIVE_RISK_OR_CATALYST,
    TriggerKind.OPPORTUNISTIC_DISLOCATION_REVIEW,
    TriggerKind.RESEARCH_REFRESH_REQUIRED,
    TriggerKind.PORTFOLIO_RISK_STATE,
}


def observations_from_quotes(context: PortfolioContext, quotes: Mapping[str, Mapping[str, Any]] | None) -> dict[str, PositionObservation]:
    out: dict[str, PositionObservation] = {}
    quote_map = dict(quotes or {})
    for pos in context.positions:
        symbol = pos.symbol.upper()
        quote = quote_map.get(symbol) or {}
        price = quote.get("price") or quote.get("last") or pos.current_price
        prev = quote.get("previous_close") or quote.get("adjusted_previous_close")
        out[symbol] = PositionObservation(
            symbol=symbol,
            current_price=float(price) if price is not None else pos.current_price,
            previous_close=float(prev) if prev is not None else None,
            earnings_event=bool(quote.get("earnings_event") or quote.get("earnings_update")),
            major_news=bool(quote.get("major_news") or quote.get("adverse_catalyst")),
            material_filing=bool(quote.get("material_filing")),
            sources_observed=["live_quotes"] if quote else ["live_positions"],
        )
    return out


def holding_due_for_reassessment(
    position,
    context: PortfolioContext,
    *,
    thesis,
    observation: PositionObservation | None,
    now: datetime,
    theses: ThesisRegistry | None,
) -> tuple[bool, str]:
    """Deterministic: due review or existing material trigger. Never an AI call."""
    facts = assemble_facts(position, context, thesis=thesis, report=None, observation=observation, now=now)
    triggers = detect_triggers(facts, observation)
    kinds = {t.kind for t in triggers}
    if MATERIAL_KINDS & kinds:
        return True, "material_trigger"
    if TriggerKind.PRICE_MOVE in kinds and position.sleeve != Sleeve.CORE_GROWTH:
        return True, "material_price_move"
    if thesis is None:
        return False, "missing_thesis"
    sleeve = thesis.sleeve or position.sleeve
    if sleeve is None:
        sleeve = Sleeve.OPPORTUNISTIC
    horizon = freshness_horizon(sleeve)
    if theses is not None and theses.has_fresh_review(
        thesis.thesis_id,
        "POSITION_MONITOR_REVIEW",
        max_age_hours=horizon.total_seconds() / 3600.0,
        now=now,
    ):
        return False, "recent_review"
    if not any(r.review_type == "POSITION_MONITOR_REVIEW" for r in (thesis.review_history or [])):
        updated = thesis.updated_at or thesis.created_at
        if updated:
            try:
                started = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if now - started < horizon:
                    return False, "not_yet_due"
            except ValueError:
                return False, "not_yet_due"
        else:
            return False, "not_yet_due"
    pre = preliminary_state(facts, triggers)
    if pre != MonitoringState.HEALTHY:
        return True, "review_due"
    if theses is not None and not theses.has_fresh_review(
        thesis.thesis_id,
        "POSITION_MONITOR_REVIEW",
        max_age_hours=horizon.total_seconds() / 3600.0,
        now=now,
    ):
        return True, "review_due"
    return False, "healthy"


def run_live_position_monitor(services, ctx: dict[str, Any]) -> dict[str, Any]:
    """Refresh LIVE account, then reassess holdings only when due or material."""
    from agentic_portfolio.agent.handlers import _ok, _refresh_account, _ai_blocked, _quotes_for, _pipeline_worker

    refresh = _refresh_account(services, ctx, job="POSITION_MONITOR")
    if refresh.get("status") in {"FAIL_CLOSED", "BLOCKED"}:
        return refresh
    context = services.last_context
    if context is None:
        refresh["monitored"] = 0
        refresh["ai_calls"] = 0
        refresh["skipped"] = refresh.get("skipped") or "missing_live_context"
        return refresh
    if not getattr(context, "positions", None):
        refresh.update({"monitored": 0, "ai_calls": 0, "reassessed": 0, "approvals_created": 0, "holdings_detected": 0})
        return refresh

    worker = _pipeline_worker(services)
    quotes = _quotes_for(services, [p.symbol for p in context.positions])
    observations = observations_from_quotes(context, quotes)
    theses = worker.theses
    now = services.now_fn() if hasattr(services, "now_fn") else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    core_positions = [p for p in context.positions if (p.sleeve or (theses.current_for_symbol(p.symbol).sleeve if theses.current_for_symbol(p.symbol) else None)) == Sleeve.CORE_GROWTH]
    other_positions = [p for p in context.positions if p not in core_positions]

    due_symbols: list[str] = []
    due_reasons: dict[str, str] = {}
    core_material = False
    for pos in other_positions:
        thesis = theses.current_for_symbol(pos.symbol)
        due, reason = holding_due_for_reassessment(
            pos, context, thesis=thesis, observation=observations.get(pos.symbol.upper()), now=now, theses=theses
        )
        if due:
            due_symbols.append(pos.symbol.upper())
            due_reasons[pos.symbol.upper()] = reason
    for pos in core_positions:
        thesis = theses.current_for_symbol(pos.symbol)
        facts = assemble_facts(pos, context, thesis=thesis, report=None, observation=observations.get(pos.symbol.upper()), now=now)
        triggers = detect_triggers(facts, observations.get(pos.symbol.upper()))
        kinds = {t.kind for t in triggers}
        if (MATERIAL_KINDS & kinds) and not is_price_move_alone(triggers):
            core_material = True

    blocked, budget_reason = _ai_blocked(services)
    ai_calls = 0
    approvals_created = 0
    approvals_reused = 0
    risk_blocked = 0
    committee_status = None
    monitor_ai = 0

    if core_positions:
        trigger = "material_thesis_change" if core_material else "position_monitor"
        if blocked:
            log_activity(services.root, "AI_SKIPPED", job="POSITION_MONITOR", reason=budget_reason, path="core_committee")
            committee_status = "BLOCKED"
        else:
            try:
                committee = worker.run_core_committee(context, trigger=trigger)
                committee_status = committee.get("status")
                ai_calls += int(committee.get("ai_calls") or 0)
                approvals_created += int(committee.get("proposals_created") or committee.get("approvals_created") or 0)
                risk_blocked += int(committee.get("risk_blocked") or 0)
            except Exception as exc:  # noqa: BLE001 — budget/reasoner failures must not crash the job
                from agentic_portfolio.ai.errors import BudgetDenied, BudgetExhausted

                if isinstance(exc, (BudgetDenied, BudgetExhausted)):
                    log_activity(services.root, "AI_SKIPPED", job="POSITION_MONITOR", reason="budget_denied", path="core_committee")
                    committee_status = "BLOCKED"
                else:
                    raise

    reasoner = None
    decision_reasoner = None
    if due_symbols and not blocked:
        reasoner = _monitoring_reasoner(services, worker)
        if worker.decision_reasoner is not None:
            decision_reasoner = worker.decision_reasoner
        elif worker.gateway is not None:
            decision_reasoner = worker._decision()
    elif due_symbols and blocked:
        log_activity(services.root, "AI_SKIPPED", job="POSITION_MONITOR", reason=budget_reason, symbols=due_symbols)

    try:
        result = run_position_monitor(
            context,
            observations,
            reasoner=reasoner,
            decision_reasoner=decision_reasoner,
            theses=theses,
            sleeves=worker.sleeves,
            research_store=worker.research_store,
            persist=True,
            now=now,
            journal=services.root / "logs" / "position_monitor.jsonl",
            runtime_mode=str(services.runtime_mode.value if isinstance(services.runtime_mode, RuntimeMode) else services.runtime_mode),
            reassess_symbols=set(due_symbols) if due_symbols else set(),
        )
    except Exception as exc:  # noqa: BLE001
        from agentic_portfolio.ai.errors import BudgetDenied, BudgetExhausted

        if isinstance(exc, (BudgetDenied, BudgetExhausted)):
            log_activity(services.root, "AI_SKIPPED", job="POSITION_MONITOR", reason="budget_denied", symbols=due_symbols)
            reasoner = None
            from agentic_portfolio.monitoring.types import MonitoringResult

            result = MonitoringResult(run_id="budget_denied", packet_ids=[], positions=[], context=context, execution_attempted=False)
        else:
            raise
    if reasoner is not None:
        monitor_ai = sum(1 for row in result.positions if row.reassessment is not None)
        ai_calls += monitor_ai

    applied = _apply_monitor_actions(services, worker, context, result)
    approvals_created += int(applied.get("created") or 0)
    approvals_reused += int(applied.get("reused") or 0)
    risk_blocked += int(applied.get("risk_blocked") or 0)

    refresh.update(
        {
            "status": "OK",
            "job": "POSITION_MONITOR",
            "holdings_detected": len(context.positions),
            "monitored": len(result.positions),
            "reassessed": len(due_symbols) + (1 if core_material or (core_positions and committee_status not in {None, "SKIPPED", "SKIPPED_UNCHANGED", "BLOCKED"}) else 0),
            "due_symbols": due_symbols,
            "due_reasons": due_reasons,
            "ai_calls": ai_calls,
            "approvals_created": approvals_created,
            "approvals_reused": approvals_reused,
            "risk_blocked": risk_blocked,
            "core_committee": committee_status,
            "core_holdings": [p.symbol for p in core_positions],
            "placement_attempted": False,
            "auto_execution": False,
            "LIVE_ORDER_PLACEMENT": live_placement_enabled(),
            "execution_attempted": False,
        }
    )
    return refresh


def _monitoring_reasoner(services, worker):
    existing = getattr(services, "monitoring_reasoner", None)
    if existing is not None:
        return existing
    if worker.gateway is not None:
        from agentic_portfolio.ai.reasoners import GatewayMonitoringReasoner

        return GatewayMonitoringReasoner(worker.gateway)
    return None


def _apply_monitor_actions(services, worker, context: PortfolioContext, result) -> dict[str, int]:
    created = 0
    reused = 0
    risk_blocked = 0
    if worker.approvals is None:
        return {"created": 0, "reused": 0, "risk_blocked": 0}
    for row in result.positions:
        for gated in row.gated_actions:
            action = gated.proposed_action.decision
            if action not in ACTIONABLE:
                continue
            verdict = gated.risk.verdict
            if verdict not in RISK_PERMIT:
                risk_blocked += 1
                worker._notify(
                    NotificationKind.RISK_GATE_BLOCKED,
                    title=f"Risk Gate blocked {row.symbol}",
                    body=f"{row.symbol} {action.value} blocked: {verdict.value if hasattr(verdict, 'value') else verdict}",
                    payload={"symbol": row.symbol, "verdict": str(verdict)},
                )
                continue
            existing = worker.approvals.canonical_pending(ticker=row.symbol, proposed_action=action.value)
            if existing is not None:
                reused += 1
                continue
            item, is_new = _create_monitor_approval(worker, context, row, gated)
            if item is None:
                continue
            if is_new:
                created += 1
                worker._notify(
                    NotificationKind.APPROVAL_REQUIRED,
                    title=f"TRADE APPROVAL REQUIRED — {row.symbol}",
                    body=f"{row.symbol} {action.value}. Approving does not place an order.",
                    payload={
                        "ticker": row.symbol,
                        "approval_id": item.approval_id,
                        "action": item.proposed_action,
                        "proposed_dollar_amount": item.proposed_dollar_amount,
                        "proposed_allocation_pct": item.proposed_allocation_pct,
                        "sleeve": item.sleeve,
                        "reason": item.reason,
                        "expires_at": item.expires_at,
                    },
                )
                log_activity(services.root, "APPROVAL_CREATED", ticker=row.symbol, approval_id=item.approval_id, job="POSITION_MONITOR")
            else:
                reused += 1
    return {"created": created, "reused": reused, "risk_blocked": risk_blocked}


def _create_monitor_approval(worker, context: PortfolioContext, row, gated):
    action = gated.proposed_action
    nav = float(context.current_nav or 0)
    dollars = float(action.proposed_notional or 0)
    pct = None
    if row.reassessment is not None and row.reassessment.desired_allocation_pct is not None:
        pct = float(row.reassessment.desired_allocation_pct)
    elif action.expected_resulting_position_pct is not None:
        raw = float(action.expected_resulting_position_pct)
        pct = raw * 100.0 if 0.0 <= raw <= 1.0 else raw
    if pct is not None and not dollars and nav:
        current_mv = sum(p.market_value for p in context.positions if p.symbol.upper() == row.symbol.upper())
        dollars = max(0.0, current_mv - (pct / 100.0) * nav)
    impact = {
        "nav": nav,
        "cash": context.cash,
        "buying_power": context.buying_power,
        "source_of_truth": LIVE_SOURCE_OF_TRUTH if worker.runtime_mode is RuntimeMode.LIVE else "isolated_paper_book",
        "proposed_notional": dollars,
        "desired_allocation_pct": pct,
        "expected_resulting_position_pct": action.expected_resulting_position_pct,
        "LIVE_ORDER_PLACEMENT": live_placement_enabled(),
        "auto_execution": False,
    }
    item, is_new = worker.approvals.get_or_create(
        ticker=row.symbol,
        proposed_action=action.decision.value,
        proposed_dollar_amount=dollars or None,
        proposed_allocation_pct=None if pct is None else float(pct),
        reason=(row.reassessment.rationale if row.reassessment else None) or f"{row.symbol} position monitor {action.decision.value}",
        supporting_thesis=(row.thesis.thesis_summary if row.thesis else None),
        current_quote=action.current_price or (row.facts.current_price if row.facts else None),
        risk_gate_result={
            "verdict": gated.risk.verdict.value if hasattr(gated.risk.verdict, "value") else str(gated.risk.verdict),
            "reasons": [str(r) for r in (gated.risk.reasons or [])],
        },
        portfolio_impact=impact,
        expected_order_type="market",
        metadata={
            "sleeve": action.sleeve.value if action.sleeve else None,
            "thesis_id": gated.thesis_id or (row.thesis.thesis_id if row.thesis else None),
            "research_id": row.research.research_id if row.research else None,
            "nav_at_proposal": nav,
            "quote_at_proposal": action.current_price,
        },
    )
    return item, is_new
