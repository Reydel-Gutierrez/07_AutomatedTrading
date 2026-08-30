"""Paper fill engine.

PAPER_ONLY OrderPlan → simulated fill → paper book + blotter + reconciliation.
No investment logic. No broker review/place/cancel. No stop orders. No money movement.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from agentic_portfolio.execution.types import (
    SELL_ACTIONS,
    ExecutionResult,
    OrderPlan,
    QuoteSnapshot,
    SkippedAction,
)
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.execution.validate import held_position
from agentic_portfolio.paper_fill.accounting import (
    PaperAccountingError,
    apply_to_book,
    lots_from_context,
)
from agentic_portfolio.paper_fill.safety import assert_no_forbidden_tools, assert_paper_only
from agentic_portfolio.paper_fill.store import PaperFillStore
from agentic_portfolio.paper_fill.types import (
    BlotterEntry,
    FillStatus,
    PaperFill,
    PaperFillResult,
    PaperLot,
    ReconciliationResult,
    SkippedFill,
)
from agentic_portfolio.paper_fill.validate import (
    merge_reconciliation,
    pretrade_codes,
    reconcile_step,
    resolve_fill_price,
    skip_fill_reason,
)
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules, load_paper_fill_config
from agentic_portfolio.schemas import (
    Decision,
    PortfolioContext,
    SleeveAssignmentStatus,
    ThesisStatus,
    to_dict,
)
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry


def journal_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "logs" / "paper_fill.jsonl"


def run_paper_fill(
    plans: Iterable[OrderPlan] | ExecutionResult,
    context: PortfolioContext,
    quotes: Mapping[str, QuoteSnapshot] | None = None,
    *,
    skipped: Iterable[SkippedAction] | None = None,
    now: datetime | None = None,
    store: PaperFillStore | None = None,
    persist: bool = True,
    theses: ThesisRegistry | None = None,
    sleeves: SleeveRegistry | None = None,
    config: dict | None = None,
    account_rules: dict | None = None,
    journal: Path | None = None,
    sources_observed: list[str] | None = None,
    lots: list[PaperLot] | None = None,
) -> PaperFillResult:
    """Simulate fills for PAPER_ONLY plans. Never submits live orders. Never touches live registries unless passed in."""
    cfg = config or load_paper_fill_config()
    rules = account_rules or load_account_rules()
    now = now or datetime.now(timezone.utc)
    run_id = str(uuid4())
    assert_no_forbidden_tools(sources_observed or [])
    live = bool(rules.get("execution", {}).get("live_trade_actions_allowed")) or bool(cfg.get("live_trade_actions_allowed"))
    auto = bool(rules.get("execution", {}).get("auto_execution")) or bool(cfg.get("auto_execution"))
    assert_paper_only(live_trade_actions_allowed=live, auto_execution=auto)

    if isinstance(plans, ExecutionResult):
        skipped = list(skipped) if skipped is not None else list(plans.skipped)
        context = context or plans.context
        plan_list = list(plans.plans)
    else:
        plan_list = list(plans)
        skipped = list(skipped or [])

    quote_map = {str(k).upper(): v for k, v in (quotes or {}).items()}
    if persist and store is None:
        store = PaperFillStore()
    filled_ids = set(store.filled_order_plan_ids()) if store is not None else set()
    working_lots = list(lots) if lots is not None else lots_from_context(context, opened_at=now.isoformat())
    ctx = context

    _journal(
        {
            "type": "PAPER_FILL_STARTED",
            "run_id": run_id,
            "symbols": [p.symbol for p in plan_list],
            "paper_environment": True,
            "live_execution_attempted": False,
        },
        journal,
        persist=persist,
    )

    fills: list[PaperFill] = []
    blotter: list[BlotterEntry] = []
    skipped_out: list[SkippedFill] = [
        SkippedFill(symbol=s.symbol, action=s.action, reason=s.reason) for s in skipped
    ]
    parts: list[ReconciliationResult] = []
    errors: list[str] = []

    for item in skipped:
        _journal(
            {
                "type": "PAPER_FILL_SKIPPED",
                "run_id": run_id,
                "symbol": item.symbol,
                "action": item.action.value,
                "reason": item.reason,
            },
            journal,
            persist=persist,
        )

    for plan in plan_list:
        reason = skip_fill_reason(plan)
        if reason:
            skipped_out.append(
                SkippedFill(symbol=plan.symbol, action=plan.action, reason=reason, order_plan_id=plan.order_plan_id)
            )
            _journal(
                {
                    "type": "PAPER_FILL_SKIPPED",
                    "run_id": run_id,
                    "order_plan_id": plan.order_plan_id,
                    "symbol": plan.symbol,
                    "action": plan.action.value,
                    "reason": reason,
                },
                journal,
                persist=persist,
            )
            continue

        quote = quote_map.get(plan.symbol.upper())
        price, price_codes = resolve_fill_price(plan, quote, now=now, config=cfg)
        qty = plan.quantity
        codes = list(price_codes)
        codes.extend(
            pretrade_codes(plan, ctx, filled_ids=filled_ids, quantity=qty, fill_price=price, config=cfg)
        )
        ts = now.isoformat()
        if codes or price is None or qty is None:
            fill = _fill_record(plan, FillStatus.REJECTED, ts, price, qty, codes)
            fills.append(fill)
            parts.append(
                reconcile_step(plan, fill, ctx, ctx, None, filled_ids=filled_ids, config=cfg)
            )
            errors.extend(codes)
            _journal(
                {
                    "type": "PAPER_FILL_REJECTED",
                    "run_id": run_id,
                    "order_plan_id": plan.order_plan_id,
                    "symbol": plan.symbol,
                    "reasons": codes,
                },
                journal,
                persist=persist,
            )
            continue

        sleeve = None
        held = held_position(ctx, plan.symbol)
        if held is not None:
            sleeve = held.sleeve
        elif sleeves is not None:
            rec = sleeves.get(plan.symbol)
            sleeve = rec.sleeve if rec is not None else None
        try:
            new_ctx, delta = apply_to_book(
                plan,
                ctx,
                working_lots,
                fill_price=price,
                quantity=qty,
                timestamp=ts,
                sleeve=sleeve,
            )
        except PaperAccountingError as exc:
            code = str(exc)
            fill = _fill_record(plan, FillStatus.REJECTED, ts, price, qty, [code])
            fills.append(fill)
            parts.append(reconcile_step(plan, fill, ctx, ctx, None, filled_ids=filled_ids, config=cfg))
            errors.append(code)
            _journal(
                {
                    "type": "PAPER_FILL_REJECTED",
                    "run_id": run_id,
                    "order_plan_id": plan.order_plan_id,
                    "symbol": plan.symbol,
                    "reasons": [code],
                },
                journal,
                persist=persist,
            )
            continue

        fill = _fill_record(plan, FillStatus.FILLED, ts, price, qty, [])
        recon = reconcile_step(plan, fill, ctx, new_ctx, delta, filled_ids=filled_ids, config=cfg)
        if not recon.ok:
            fill.status = FillStatus.REJECTED
            fill.reject_reasons = list(recon.codes)
            fills.append(fill)
            parts.append(recon)
            errors.extend(recon.codes)
            _journal(
                {
                    "type": "PAPER_FILL_REJECTED",
                    "run_id": run_id,
                    "order_plan_id": plan.order_plan_id,
                    "symbol": plan.symbol,
                    "reasons": list(recon.codes),
                },
                journal,
                persist=persist,
            )
            continue

        entry = BlotterEntry(
            blotter_id=str(uuid4()),
            fill_id=fill.fill_id,
            order_plan_id=plan.order_plan_id,
            symbol=plan.symbol,
            action=plan.action,
            side=plan.order_side,
            quantity=qty,
            fill_price=price,
            filled_notional=delta.filled_notional,
            timestamp=ts,
            cash_before=delta.cash_before,
            cash_after=delta.cash_after,
            quantity_before=delta.quantity_before,
            quantity_after=delta.quantity_after,
            average_cost_before=delta.average_cost_before,
            average_cost_after=delta.average_cost_after,
            realized_pnl=delta.realized_pnl,
            position_closed=delta.position_closed,
            estimated_slippage_pct=plan.slippage_check.estimated_slippage_pct if plan.slippage_check else None,
            source_decision_id=plan.source_decision_id,
            thesis_id=plan.thesis_id,
            sleeve=delta.sleeve_after or delta.sleeve_before,
            status=FillStatus.FILLED,
        )
        fills.append(fill)
        blotter.append(entry)
        parts.append(recon)
        ctx = new_ctx
        working_lots = list(delta.lots_after)
        filled_ids.add(plan.order_plan_id)
        _update_paper_registries(plan, delta.position_closed, theses=theses, sleeves=sleeves)
        _journal(
            {
                "type": "PAPER_FILL_CREATED",
                "run_id": run_id,
                "fill_id": fill.fill_id,
                "order_plan_id": plan.order_plan_id,
                "symbol": plan.symbol,
                "action": plan.action.value,
                "quantity": qty,
                "fill_price": price,
                "filled_notional": delta.filled_notional,
                "realized_pnl": delta.realized_pnl,
                "position_closed": delta.position_closed,
                "thesis_id": plan.thesis_id,
            },
            journal,
            persist=persist,
        )

    recon_all = merge_reconciliation(parts) if parts else ReconciliationResult(ok=True)
    result = PaperFillResult(
        run_id=run_id,
        fills=fills,
        blotter=blotter,
        skipped=skipped_out,
        reconciliation=recon_all,
        context_before=context,
        context_after=ctx,
        lots=working_lots,
        validation_errors=errors,
    )
    if persist:
        (store or PaperFillStore()).save(run_id, _run_record(result, now))
        _journal(
            {
                "type": "PAPER_BOOK_UPDATED",
                "run_id": run_id,
                "nav": ctx.current_nav,
                "cash": ctx.cash,
                "holdings": [p.symbol for p in ctx.positions],
                "paper_environment": True,
            },
            journal,
            persist=persist,
        )
    _journal(
        {
            "type": "PAPER_FILL_COMPLETED",
            "run_id": run_id,
            "filled": [f.symbol for f in result.filled],
            "rejected": [f.symbol for f in result.rejected],
            "skipped": [s.symbol for s in skipped_out],
            "execution_attempted": False,
            "broker_orders_submitted": 0,
            "broker_stop_orders_created": 0,
            "live_execution_attempted": False,
        },
        journal,
        persist=persist,
    )
    return result


def _fill_record(
    plan: OrderPlan,
    status: FillStatus,
    timestamp: str,
    price: float | None,
    quantity: float | None,
    reasons: list[str],
) -> PaperFill:
    notional = None
    if price is not None and quantity is not None:
        notional = quantity * price
    return PaperFill(
        fill_id=str(uuid4()),
        order_plan_id=plan.order_plan_id,
        symbol=plan.symbol,
        side=plan.order_side,
        quantity=quantity,
        fill_price=price,
        filled_notional=notional,
        timestamp=timestamp,
        source_decision_id=plan.source_decision_id,
        thesis_id=plan.thesis_id,
        status=status,
        reject_reasons=list(reasons),
    )


def _update_paper_registries(
    plan: OrderPlan,
    position_closed: bool,
    *,
    theses: ThesisRegistry | None,
    sleeves: SleeveRegistry | None,
) -> None:
    """Paper environment only. Callers must pass isolated registries, never live ones."""
    if theses is not None and plan.thesis_id:
        rec = theses.get(plan.thesis_id)
        if rec is not None:
            if plan.action == Decision.BUY and rec.status == ThesisStatus.DRAFT:
                theses.set_status(plan.thesis_id, ThesisStatus.ACTIVE)
            elif position_closed:
                theses.set_status(plan.thesis_id, ThesisStatus.CLOSED)
    elif theses is not None and position_closed:
        rec = theses.current_for_symbol(plan.symbol)
        if rec is not None:
            theses.set_status(rec.thesis_id, ThesisStatus.CLOSED)

    if sleeves is None:
        return
    existing = sleeves.get(plan.symbol)
    if existing is None:
        return
    if plan.action == Decision.BUY:
        sleeves.set_status(plan.symbol, SleeveAssignmentStatus.ACTIVE)
    elif position_closed:
        sleeves.set_status(plan.symbol, SleeveAssignmentStatus.CLOSED)
    elif plan.action in SELL_ACTIONS or plan.action == Decision.REDUCE:
        sleeves.set_status(plan.symbol, SleeveAssignmentStatus.REDUCING)


def _run_record(result: PaperFillResult, now: datetime) -> dict[str, Any]:
    ctx_after = result.context_after
    return {
        "run_id": result.run_id,
        "created_at": now.isoformat(),
        "paper_environment": True,
        "live_book_untouched": True,
        "symbols": [f.symbol for f in result.fills] + [s.symbol for s in result.skipped],
        "fills": [to_dict(f) for f in result.fills],
        "blotter": [to_dict(b) for b in result.blotter],
        "skipped": [to_dict(s) for s in result.skipped],
        "reconciliation": to_dict(result.reconciliation),
        "lots": [to_dict(lot) for lot in result.lots],
        "context_before": to_dict(result.context_before) if result.context_before else None,
        "context_after": to_dict(ctx_after) if ctx_after else None,
        "filled_order_plan_ids": [f.order_plan_id for f in result.filled],
        "filled_count": len(result.filled),
        "execution_attempted": False,
        "broker_orders_submitted": 0,
        "broker_stop_orders_created": 0,
        "live_execution_attempted": False,
        "live_trade_actions_allowed": False,
        "auto_execution": False,
        "nav": ctx_after.current_nav if ctx_after else None,
        "cash": ctx_after.cash if ctx_after else None,
    }


def _journal(row: dict, path: Path | None, *, persist: bool = True) -> None:
    if path is None and not persist:
        return
    append_jsonl(row, path or journal_path())
