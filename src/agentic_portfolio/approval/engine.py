"""Human Approval Packet engine.

Risk-Gate-approved paper OrderPlan → human-readable ApprovalPacket.
APPROVED does not place, review, or cancel a live order.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from agentic_portfolio.approval.safety import (
    assert_approval_does_not_place,
    assert_no_forbidden_tools,
    assert_paper_only,
)
from agentic_portfolio.approval.store import ApprovalStore
from agentic_portfolio.approval.types import (
    ApprovalMarketView,
    ApprovalPacket,
    ApprovalRequest,
    ApprovalResult,
    ApprovalSnapshot,
    ApprovalStatus,
    EvidenceRefs,
    OrderPlanSummary,
    SkippedApproval,
    StatusEvent,
)
from agentic_portfolio.approval.validate import (
    ApprovalValidationError,
    apply_freshness,
    can_record_human_decision,
    current_position_pct,
    skip_reason,
)
from agentic_portfolio.execution.types import BUY_ACTIONS, OrderPlan, QuoteSnapshot
from agentic_portfolio.execution.validate import held_position, held_quantity, quote_is_stale
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.monitoring.types import MonitoredPosition
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules, load_approval_config
from agentic_portfolio.schemas import Decision, ExitPolicy, PortfolioContext, ThesisRecord, to_dict


def journal_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "logs" / "approval.jsonl"


def run_approval(
    items: Iterable[ApprovalRequest],
    context: PortfolioContext,
    *,
    now: datetime | None = None,
    store: ApprovalStore | None = None,
    persist: bool = True,
    config: dict | None = None,
    account_rules: dict | None = None,
    journal: Path | None = None,
    sources_observed: list[str] | None = None,
) -> ApprovalResult:
    """Create approval packets from paper OrderPlans. Never submits live orders."""
    cfg = config or load_approval_config()
    rules = account_rules or load_account_rules()
    now = now or datetime.now(timezone.utc)
    run_id = str(uuid4())
    assert_no_forbidden_tools(sources_observed or [])
    live = bool(rules.get("execution", {}).get("live_trade_actions_allowed")) or bool(cfg.get("live_trade_actions_allowed"))
    auto = bool(rules.get("execution", {}).get("auto_execution")) or bool(cfg.get("auto_execution"))
    assert_paper_only(live_trade_actions_allowed=live, auto_execution=auto)
    if persist and store is None:
        store = ApprovalStore()

    packets: list[ApprovalPacket] = []
    skipped: list[SkippedApproval] = []
    superseded: list[ApprovalPacket] = []
    open_by_symbol: dict[str, list[ApprovalPacket]] = {}

    _journal(
        {
            "type": "APPROVAL_RUN_STARTED",
            "run_id": run_id,
            "live_trade_actions_allowed": False,
            "auto_execution": False,
        },
        journal,
        persist=persist,
    )

    for req in items:
        plan = req.plan
        symbol = plan.symbol.upper()
        reason = skip_reason(plan, req.action, req.risk)
        if reason:
            skipped.append(SkippedApproval(symbol=symbol, action=plan.action, reason=reason, order_plan_id=plan.order_plan_id))
            continue
        if store is not None and store.by_order_plan_id(plan.order_plan_id) is not None:
            skipped.append(
                SkippedApproval(symbol=symbol, action=plan.action, reason="DUPLICATE_ORDER_PLAN", order_plan_id=plan.order_plan_id)
            )
            continue
        priors = list(open_by_symbol.get(symbol, []))
        if store is not None:
            for prior in store.open_for_symbol(symbol):
                if all(prior.approval_id != p.approval_id for p in priors):
                    priors.append(prior)
        packet = create_approval_packet(req, now=now, config=cfg)
        for prior in priors:
            _supersede(prior, superseded_by=packet.approval_id, now=now, store=store, journal=journal, persist=persist)
            superseded.append(prior)
        if persist and store is not None:
            store.save(packet)
        _journal(
            {
                "type": "APPROVAL_CREATED",
                "run_id": run_id,
                "approval_id": packet.approval_id,
                "symbol": packet.symbol,
                "action": packet.action.value,
                "order_plan_id": packet.evidence_refs.order_plan_id,
                "status": packet.status.value,
                "broker_submitted": False,
            },
            journal,
            persist=persist,
        )
        packet = _expire_if_quote_stale(packet, req.quote, now=now, config=cfg, journal=journal, persist=persist)
        if persist and store is not None and packet.status == ApprovalStatus.EXPIRED:
            store.update(packet)
        open_by_symbol[symbol] = [packet] if packet.status == ApprovalStatus.PENDING_HUMAN_APPROVAL else []
        packets.append(packet)

    result = ApprovalResult(run_id=run_id, packets=packets, skipped=skipped, superseded=superseded, context=context)
    if persist and store is not None:
        store.save_run(run_id, _run_record(result, now))
    _journal(
        {
            "type": "APPROVAL_RUN_COMPLETED",
            "run_id": run_id,
            "symbols": [p.symbol for p in packets],
            "pending": len(result.pending),
            "skipped": [s.symbol for s in skipped],
            "execution_attempted": False,
            "broker_orders_submitted": 0,
            "broker_stop_orders_created": 0,
            "live_execution_attempted": False,
        },
        journal,
        persist=persist,
    )
    return result


def create_approval_packet(
    req: ApprovalRequest,
    *,
    now: datetime | None = None,
    config: dict | None = None,
) -> ApprovalPacket:
    """Assemble one packet. Does not persist. Does not place an order."""
    del config
    now = now or datetime.now(timezone.utc)
    plan = req.plan
    action = req.action
    ctx = req.context
    thesis = req.thesis
    decision = req.decision
    report = req.report
    comparison = req.comparison
    ts = now.isoformat()
    current_frac = current_position_pct(ctx, plan.symbol)
    desired = _desired_pct(decision, action, plan)
    price = _price(req.quote, plan, action)
    approval_id = str(uuid4())
    monitoring_state, why_now_monitor = _monitoring_bits(req.monitoring)
    why_now = (decision.rationale if decision else None) or why_now_monitor
    why_cash, why_spy = _why_not_cash_spy(plan.action, decision, comparison)
    bull, base, bear = _cases(thesis, report)
    risks = _risks(thesis, report)
    horizon = (thesis.expected_horizon if thesis else None) or (report.expected_horizon if report else None)
    summary = (thesis.thesis_summary if thesis else None) or (report.executive_summary if report else None)
    packet = ApprovalPacket(
        approval_id=approval_id,
        symbol=plan.symbol.upper(),
        action=plan.action,
        desired_allocation_pct=desired,
        current_allocation_pct=_frac_to_pct(current_frac),
        order_notional=plan.notional,
        order_quantity=plan.quantity,
        current_price=price,
        sleeve=action.sleeve or (thesis.sleeve if thesis else None),
        thesis_summary=summary,
        why_now=why_now,
        why_not_cash=why_cash,
        why_not_spy=why_spy,
        bull_case=bull,
        base_case=base,
        bear_case=bear,
        key_risks=risks,
        invalidation_exit_policy=_invalidation(thesis),
        expected_horizon=horizon,
        portfolio_effect=_portfolio_effect(plan, ctx, current_frac, desired),
        sector_concentration_effect=_sector_effect(action, ctx),
        risk_gate_verdict=req.risk.verdict.value,
        enhanced_review_requirements=list(req.risk.required_reviews or []),
        order_plan_summary=OrderPlanSummary(
            order_plan_id=plan.order_plan_id,
            execution_status=plan.execution_status.value,
            side=plan.order_side.value if plan.order_side else None,
            order_type=plan.order_type.value if plan.order_type else None,
            time_in_force=plan.time_in_force.value if plan.time_in_force else None,
            quantity=plan.quantity,
            notional=plan.notional,
            estimated_price=plan.estimated_price,
            estimated_position_pct_after=plan.estimated_position_pct_after,
            blocked_reasons=list(plan.blocked_reasons),
            live_execution_blocked=True,
            stop_orders_created=0,
            broker_submitted=False,
        ),
        evidence_refs=EvidenceRefs(
            order_plan_id=plan.order_plan_id,
            source_decision_id=plan.source_decision_id,
            thesis_id=(thesis.thesis_id if thesis else None) or plan.thesis_id or action.thesis_id,
            research_id=(report.research_id if report else None) or (thesis.research_id if thesis else None),
            risk_evaluation_id=req.risk.snapshot_id or plan.risk_evaluation_id,
            monitoring_run_id=req.monitoring_run_id,
            supporting_evidence_refs=list((thesis.supporting_evidence_refs if thesis else None) or (report.evidence_refs if report else None) or []),
        ),
        created_at=ts,
        status=ApprovalStatus.PENDING_HUMAN_APPROVAL,
        snapshot=_snapshot(plan, ctx, req.quote, thesis, report, now=ts),
        status_history=[StatusEvent(status=ApprovalStatus.PENDING_HUMAN_APPROVAL, at=ts)],
        monitoring_state=monitoring_state,
        sector=action.sector,
        live_execution_blocked=True,
        broker_submitted=False,
        live_trade_actions_allowed=False,
        auto_execution=False,
        approved_does_not_place_order=True,
        stop_orders_created=0,
    )
    return packet


def record_human_decision(
    packet: ApprovalPacket,
    status: ApprovalStatus,
    *,
    note: str | None = None,
    now: datetime | None = None,
    store: ApprovalStore | None = None,
    persist: bool = True,
    journal: Path | None = None,
) -> ApprovalPacket:
    """Record APPROVED or REJECTED. APPROVED still does not place an order."""
    now = now or datetime.now(timezone.utc)
    err = can_record_human_decision(packet, status)
    if err:
        raise ApprovalValidationError(err)
    assert_approval_does_not_place(broker_submitted=False, execution_attempted=False)
    ts = now.isoformat()
    packet.status = status
    packet.human_note = note
    packet.decided_at = ts
    packet.broker_submitted = False
    packet.live_execution_blocked = True
    packet.live_trade_actions_allowed = False
    packet.auto_execution = False
    packet.approved_does_not_place_order = True
    packet.stop_orders_created = 0
    packet.status_history.append(StatusEvent(status=status, at=ts, note=note))
    event = "APPROVAL_APPROVED" if status == ApprovalStatus.APPROVED else "APPROVAL_REJECTED"
    if persist and store is not None:
        store.update(packet)
    _journal(
        {
            "type": event,
            "approval_id": packet.approval_id,
            "symbol": packet.symbol,
            "action": packet.action.value,
            "status": status.value,
            "note": note,
            "broker_submitted": False,
            "execution_attempted": False,
            "live_execution_attempted": False,
        },
        journal,
        persist=persist,
    )
    return packet


def refresh_approval(
    packet: ApprovalPacket,
    view: ApprovalMarketView,
    *,
    now: datetime | None = None,
    config: dict | None = None,
    store: ApprovalStore | None = None,
    persist: bool = True,
    journal: Path | None = None,
) -> ApprovalPacket:
    """Expire or supersede an open packet when frozen facts no longer hold."""
    cfg = config or load_approval_config()
    now = now or datetime.now(timezone.utc)
    prior = packet.status
    packet = apply_freshness(packet, view, now=now, config=cfg)
    if packet.status == prior:
        return packet
    if persist and store is not None:
        store.update(packet)
    event = "APPROVAL_SUPERSEDED" if packet.status == ApprovalStatus.SUPERSEDED else "APPROVAL_EXPIRED"
    _journal(
        {
            "type": event,
            "approval_id": packet.approval_id,
            "symbol": packet.symbol,
            "status": packet.status.value,
            "reasons": list(packet.expiry_reasons),
            "superseded_by": packet.superseded_by,
            "broker_submitted": False,
        },
        journal,
        persist=persist,
    )
    return packet


def _expire_if_quote_stale(
    packet: ApprovalPacket,
    quote: QuoteSnapshot | None,
    *,
    now: datetime,
    config: dict,
    journal: Path | None,
    persist: bool,
) -> ApprovalPacket:
    max_age = float(config.get("quote_max_age_seconds") or 300)
    stale, _reason = quote_is_stale(quote, now=now, max_age_seconds=max_age)
    if not stale:
        return packet
    packet.status = ApprovalStatus.EXPIRED
    packet.expiry_reasons = ["STALE_QUOTE"]
    packet.status_history.append(StatusEvent(status=ApprovalStatus.EXPIRED, at=now.isoformat(), reason="STALE_QUOTE"))
    _journal(
        {
            "type": "APPROVAL_EXPIRED",
            "approval_id": packet.approval_id,
            "symbol": packet.symbol,
            "status": packet.status.value,
            "reasons": ["STALE_QUOTE"],
            "broker_submitted": False,
        },
        journal,
        persist=persist,
    )
    return packet


def _supersede(
    packet: ApprovalPacket,
    *,
    superseded_by: str,
    now: datetime,
    store: ApprovalStore | None,
    journal: Path | None,
    persist: bool,
) -> ApprovalPacket:
    packet.status = ApprovalStatus.SUPERSEDED
    packet.superseded_by = superseded_by
    packet.expiry_reasons = ["SUPERSEDED_BY_NEWER_DECISION"]
    packet.status_history.append(
        StatusEvent(status=ApprovalStatus.SUPERSEDED, at=now.isoformat(), reason="SUPERSEDED_BY_NEWER_DECISION")
    )
    if persist and store is not None:
        store.update(packet)
    _journal(
        {
            "type": "APPROVAL_SUPERSEDED",
            "approval_id": packet.approval_id,
            "symbol": packet.symbol,
            "status": packet.status.value,
            "superseded_by": superseded_by,
            "broker_submitted": False,
        },
        journal,
        persist=persist,
    )
    return packet


def _frac_to_pct(value: float | None) -> float | None:
    if value is None:
        return None
    return value * 100.0


def _desired_pct(decision, action, plan: OrderPlan) -> float | None:
    if decision is not None and decision.desired_allocation_pct is not None:
        value = float(decision.desired_allocation_pct)
        return value if value > 1.0 + 1e-9 or value == 0.0 else value * 100.0
    if action.expected_resulting_position_pct is not None:
        return _frac_to_pct(action.expected_resulting_position_pct)
    if plan.estimated_position_pct_after is not None:
        return _frac_to_pct(plan.estimated_position_pct_after)
    return None


def _price(quote: QuoteSnapshot | None, plan: OrderPlan, action) -> float | None:
    if quote is not None and quote.last_price:
        return float(quote.last_price)
    if plan.estimated_price:
        return float(plan.estimated_price)
    if action.current_price:
        return float(action.current_price)
    return None


def _monitoring_bits(monitoring: MonitoredPosition | dict | None) -> tuple[str | None, str | None]:
    if monitoring is None:
        return None, None
    if isinstance(monitoring, MonitoredPosition):
        state = monitoring.state.value if monitoring.state else None
        why = monitoring.reassessment.rationale if monitoring.reassessment else None
        return state, why
    state = monitoring.get("state") or monitoring.get("monitoring_state")
    reassessment = monitoring.get("reassessment") or {}
    why = reassessment.get("rationale") if isinstance(reassessment, dict) else None
    return (str(state) if state else None), why


def _why_not_cash_spy(action: Decision, decision, comparison) -> tuple[str | None, str | None]:
    cash = (decision.why_preferable_to_cash if decision else None) or (comparison.vs_cash if comparison else None)
    spy = (decision.why_preferable_to_spy if decision else None) or (comparison.vs_spy if comparison else None)
    if cash is None and action in {Decision.SELL, Decision.REDUCE}:
        cash = "This action moves the book toward cash; cash is the destination."
    if spy is None and action in {Decision.SELL, Decision.REDUCE}:
        spy = "This is a reduction/exit, not a swap into SPY."
    return cash, spy


def _cases(thesis: ThesisRecord | None, report) -> tuple[str | None, str | None, str | None]:
    bull = thesis.bull_case if thesis else None
    base = thesis.base_case if thesis else None
    bear = thesis.bear_case if thesis else None
    generic = {"thesis intact.", "base.", "bear."}

    def _case(value: str | None, attr: str) -> str | None:
        if value and value.strip().lower() not in generic:
            return value
        if report is not None:
            item = getattr(report, attr, None)
            if item is not None:
                return getattr(item, "summary", None) or value
        return value

    return _case(bull, "bull_case"), _case(base, "base_case"), _case(bear, "bear_case")


def _risks(thesis: ThesisRecord | None, report) -> list[str]:
    if thesis and thesis.risks:
        return list(thesis.risks)
    if report is not None and report.key_risks:
        return list(report.key_risks)
    return []


def _invalidation(thesis: ThesisRecord | None) -> str | None:
    if thesis is None:
        return None
    parts = list(thesis.invalidation_conditions or [])
    policy: ExitPolicy | None = thesis.exit_policy
    if policy is not None:
        for label, value in (
            ("price", policy.price_invalidation),
            ("event", policy.event_invalidation),
            ("technical", policy.technical_invalidation),
            ("risk", policy.risk_invalidation),
        ):
            if value:
                parts.append(f"{label}: {value}")
        parts.append("thesis-based exit; not a broker stop order")
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            unique.append(part)
    return "; ".join(unique) if unique else None


def _portfolio_effect(plan: OrderPlan, ctx: PortfolioContext, current_frac: float, desired: float | None) -> str:
    after = plan.estimated_position_pct_after
    after_pct = _frac_to_pct(after) if after is not None else desired
    sign = "+" if plan.action in BUY_ACTIONS else "−"
    notional = f"{sign}${plan.notional:.2f}" if plan.notional is not None else "notional n/a"
    current_pct = _frac_to_pct(current_frac) or 0.0
    after_txt = f"{after_pct:.2f}%" if after_pct is not None else "n/a"
    cash_note = "cash decreases" if plan.action in BUY_ACTIONS else "cash increases"
    return (
        f"{plan.symbol} {current_pct:.2f}% → {after_txt} NAV; {notional} notional; "
        f"{cash_note} (estimated). Book NAV ${ctx.current_nav:.2f}."
    )


def _sector_effect(action, ctx: PortfolioContext) -> str:
    held = held_position(ctx, action.symbol)
    sector = (held.sector if held is not None and held.sector else None) or action.sector or "UNKNOWN"
    current = (ctx.sector_allocation_pct or {}).get(sector)
    if current is None and ctx.current_nav:
        mv = sum(float(p.market_value or 0.0) for p in ctx.positions if (p.sector or "") == sector)
        if mv:
            current = mv / ctx.current_nav
    if current is None and held is not None and ctx.current_nav:
        current = held.market_value / ctx.current_nav
    expected = action.expected_resulting_sector_pct
    cur_txt = f"{current * 100:.2f}%" if current is not None else "n/a"
    exp_txt = f"{expected * 100:.2f}%" if expected is not None else "not projected"
    name_pct = (held.market_value / ctx.current_nav * 100.0) if held is not None and ctx.current_nav else None
    name_txt = f"{name_pct:.2f}%" if name_pct is not None else "n/a"
    return f"Sector {sector}: book {cur_txt} NAV; this name {name_txt} NAV; expected sector after {exp_txt}."


def _snapshot(plan: OrderPlan, ctx: PortfolioContext, quote, thesis, report, *, now: str) -> ApprovalSnapshot:
    freshness = None
    research_id = None
    if report is not None:
        research_id = report.research_id
        freshness = getattr(report.freshness, "value", report.freshness)
    return ApprovalSnapshot(
        nav=ctx.current_nav,
        cash=ctx.cash,
        cash_allocation_pct=ctx.cash_allocation_pct,
        holdings_count=ctx.holdings_count,
        position_pct=current_position_pct(ctx, plan.symbol),
        position_quantity=held_quantity(ctx, plan.symbol),
        risk_state=ctx.risk_state.value,
        daily_risk_halt=bool(ctx.daily_risk_halt),
        quote_observed_at=(quote.observed_at if quote is not None else None) or plan.created_at or now,
        research_id=research_id or (thesis.research_id if thesis else None),
        research_freshness=str(freshness) if freshness else None,
        thesis_id=thesis.thesis_id if thesis else plan.thesis_id,
        thesis_updated_at=thesis.updated_at if thesis else None,
        source_decision_id=plan.source_decision_id,
    )


def _run_record(result: ApprovalResult, now: datetime) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "created_at": now.isoformat(),
        "symbols": [p.symbol for p in result.packets] + [s.symbol for s in result.skipped],
        "packets": [to_dict(p) for p in result.packets],
        "skipped": [to_dict(s) for s in result.skipped],
        "superseded": [p.approval_id for p in result.superseded],
        "execution_attempted": False,
        "broker_orders_submitted": 0,
        "broker_stop_orders_created": 0,
        "live_execution_attempted": False,
        "live_trade_actions_allowed": False,
        "auto_execution": False,
        "approved_does_not_place_order": True,
    }


def _journal(row: dict, path: Path | None, *, persist: bool = True) -> None:
    if path is None and not persist:
        return
    append_jsonl(row, path or journal_path())
