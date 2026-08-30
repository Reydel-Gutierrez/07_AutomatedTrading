"""Execution Controller engine.

Risk-Gate-approved ProposedAction → paper OrderPlan.
No investment logic. No broker review/place/cancel. No stop orders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from agentic_portfolio.decision.types import GatedAction
from agentic_portfolio.execution.safety import assert_no_forbidden_tools
from agentic_portfolio.execution.store import OrderPlanStore
from agentic_portfolio.execution.types import (
    BUY_ACTIONS,
    EXECUTABLE_ACTIONS,
    SIDE_FOR_ACTION,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    OrderPlan,
    OrderType,
    QuoteSnapshot,
    SkippedAction,
    TimeInForce,
    TradabilitySnapshot,
)
from agentic_portfolio.execution.validate import (
    collect_block_reasons,
    held_market_value,
    held_quantity,
    is_executable,
    live_flags_blocked,
    plan_consistency_codes,
    skip_reason,
)
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules, load_execution_config
from agentic_portfolio.schemas import OpenOrder, PortfolioContext, ProposedAction, RiskGateResult, to_dict


def journal_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "logs" / "order_plan.jsonl"


def run_execution(
    items: Iterable[GatedAction | ExecutionRequest | tuple[ProposedAction, RiskGateResult]],
    context: PortfolioContext,
    quotes: Mapping[str, QuoteSnapshot] | None = None,
    tradability: Mapping[str, TradabilitySnapshot] | None = None,
    *,
    open_orders: list[OpenOrder] | None = None,
    source_decision_id: str | None = None,
    store: OrderPlanStore | None = None,
    persist: bool = True,
    now: datetime | None = None,
    config: dict | None = None,
    account_rules: dict | None = None,
    journal: Path | None = None,
    sources_observed: list[str] | None = None,
) -> ExecutionResult:
    """Convert approved actions into paper OrderPlans. Never submits live orders."""
    cfg = config or load_execution_config()
    rules = account_rules or load_account_rules()
    now = now or datetime.now(timezone.utc)
    run_id = str(uuid4())
    assert_no_forbidden_tools(sources_observed or [])

    live = bool(rules.get("execution", {}).get("live_trade_actions_allowed")) or bool(cfg.get("live_trade_actions_allowed"))
    auto = bool(rules.get("execution", {}).get("auto_execution")) or bool(cfg.get("auto_execution"))
    flag_codes = live_flags_blocked(live, auto)

    quote_map = {str(k).upper(): v for k, v in (quotes or {}).items()}
    trad_map = {str(k).upper(): v for k, v in (tradability or {}).items()}
    orders = list(open_orders if open_orders is not None else context.open_orders)

    _journal(
        {
            "type": "ORDER_PLAN_STARTED",
            "run_id": run_id,
            "source_decision_id": source_decision_id,
            "live_trade_actions_allowed": live,
            "auto_execution": auto,
        },
        journal,
        persist=persist,
    )

    plans: list[OrderPlan] = []
    skipped: list[SkippedAction] = []
    errors: list[str] = list(flag_codes)

    for raw in items:
        req = _as_request(raw, source_decision_id=source_decision_id)
        action = req.action
        symbol = action.symbol.upper()
        reason = skip_reason(action.decision)
        if reason:
            skipped.append(SkippedAction(symbol=symbol, action=action.decision, reason=reason))
            _journal(
                {"type": "ORDER_PLAN_SKIPPED", "run_id": run_id, "symbol": symbol, "action": action.decision.value, "reason": reason},
                journal,
                persist=persist,
            )
            continue
        if action.decision not in EXECUTABLE_ACTIONS:
            skipped.append(SkippedAction(symbol=symbol, action=action.decision, reason="UNKNOWN_ACTION"))
            continue

        quote = req.quote or quote_map.get(symbol)
        trad = req.tradability or trad_map.get(symbol)
        merged_orders = list(orders) + list(req.open_orders)
        plan = plan_order(
            action,
            req.risk,
            context,
            quote=quote,
            tradability=trad,
            open_orders=merged_orders,
            source_decision_id=req.source_decision_id or source_decision_id,
            thesis_id=req.thesis_id or action.thesis_id,
            now=now,
            config=cfg,
            live_trade_actions_allowed=live,
            auto_execution=auto,
        )
        plans.append(plan)
        _journal(
            {
                "type": "ORDER_PLAN_CREATED" if plan.execution_status == ExecutionStatus.PAPER_ONLY else "ORDER_PLAN_BLOCKED",
                "run_id": run_id,
                "order_plan_id": plan.order_plan_id,
                "symbol": plan.symbol,
                "action": plan.action.value,
                "execution_status": plan.execution_status.value,
                "blocked_reasons": list(plan.blocked_reasons),
                "notional": plan.notional,
                "quantity": plan.quantity,
                "source_decision_id": plan.source_decision_id,
                "thesis_id": plan.thesis_id,
                "risk_evaluation_id": plan.risk_evaluation_id,
                "stop_orders_created": 0,
                "broker_submitted": False,
            },
            journal,
            persist=persist,
        )

    result = ExecutionResult(
        run_id=run_id,
        plans=plans,
        skipped=skipped,
        context=context,
        validation_errors=errors,
    )
    if persist:
        (store or OrderPlanStore()).save(run_id, _run_record(result, now, source_decision_id))
    _journal(
        {
            "type": "ORDER_PLAN_COMPLETED",
            "run_id": run_id,
            "symbols": [p.symbol for p in plans],
            "paper_plans": len(result.paper_plans),
            "blocked_plans": len(result.blocked_plans),
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


def plan_order(
    action: ProposedAction,
    risk: RiskGateResult,
    context: PortfolioContext,
    *,
    quote: QuoteSnapshot | None = None,
    tradability: TradabilitySnapshot | None = None,
    open_orders: list[OpenOrder] | None = None,
    source_decision_id: str | None = None,
    thesis_id: str | None = None,
    now: datetime | None = None,
    config: dict | None = None,
    live_trade_actions_allowed: bool = False,
    auto_execution: bool = False,
) -> OrderPlan:
    """Build one paper OrderPlan. Fail closed. Never invents stop orders."""
    cfg = config or load_execution_config()
    now = now or datetime.now(timezone.utc)
    if not is_executable(action.decision):
        raise ValueError("plan_order requires BUY/ADD/REDUCE/SELL")

    codes, quantity, notional, price, liq, slip = collect_block_reasons(
        action,
        risk,
        context,
        quote,
        tradability,
        open_orders or [],
        now=now,
        config=cfg,
        live_trade_actions_allowed=live_trade_actions_allowed,
        auto_execution=auto_execution,
    )
    after_qty, after_mv, after_pct = _position_after(action, context, quantity, notional)
    status = ExecutionStatus.PAPER_ONLY if not codes else ExecutionStatus.BLOCKED_FROM_LIVE
    plan = OrderPlan(
        order_plan_id=str(uuid4()),
        symbol=action.symbol.upper(),
        action=action.decision,
        quantity=quantity,
        notional=notional,
        estimated_price=price,
        estimated_position_quantity_after=after_qty,
        estimated_position_notional_after=after_mv,
        estimated_position_pct_after=after_pct,
        order_side=SIDE_FOR_ACTION.get(action.decision),
        order_type=OrderType(str(cfg.get("default_order_type") or "market")),
        time_in_force=TimeInForce(str(cfg.get("default_time_in_force") or "gfd")),
        slippage_check=slip if slip is not None else SlippageCheck(ok=False, codes=["SLIPPAGE_INSUFFICIENT_EVIDENCE"]),
        liquidity_check=liq if liq is not None else LiquidityCheck(ok=False, codes=["LIQUIDITY_INSUFFICIENT_EVIDENCE"]),
        source_decision_id=source_decision_id,
        thesis_id=thesis_id or action.thesis_id,
        risk_evaluation_id=risk.snapshot_id,
        execution_status=status,
        live_execution_blocked=True,
        blocked_reasons=list(codes),
        created_at=now.isoformat(),
        stop_orders_created=0,
        broker_submitted=False,
        live_trade_actions_allowed=False,
        auto_execution=False,
    )
    extra = plan_consistency_codes(
        plan,
        action,
        context,
        abs_tol=float(cfg.get("quantity_notional_abs_tolerance") or 0.01),
        rel_tol=float(cfg.get("quantity_notional_rel_tolerance") or 1e-6),
        pct_tol=float(cfg.get("position_pct_tolerance") or 0.001),
    )
    if extra:
        plan.blocked_reasons.extend(extra)
        plan.execution_status = ExecutionStatus.BLOCKED_FROM_LIVE
    if plan.execution_status == ExecutionStatus.PAPER_ONLY:
        plan.blocked_reasons = []
    else:
        plan.execution_status = ExecutionStatus.BLOCKED_FROM_LIVE
    return plan


def _position_after(
    action: ProposedAction,
    ctx: PortfolioContext,
    quantity: float | None,
    notional: float | None,
) -> tuple[float | None, float | None, float | None]:
    if quantity is None or notional is None:
        return None, None, None
    held_qty = held_quantity(ctx, action.symbol)
    held_mv = held_market_value(ctx, action.symbol)
    if action.decision in BUY_ACTIONS:
        after_qty = held_qty + quantity
        after_mv = held_mv + notional
    else:
        after_qty = max(0.0, held_qty - quantity)
        after_mv = max(0.0, held_mv - notional)
    after_pct = (after_mv / ctx.current_nav) if ctx.current_nav else None
    return after_qty, after_mv, after_pct


def _as_request(
    raw: GatedAction | ExecutionRequest | tuple[ProposedAction, RiskGateResult],
    *,
    source_decision_id: str | None,
) -> ExecutionRequest:
    if isinstance(raw, ExecutionRequest):
        return raw
    if isinstance(raw, GatedAction):
        return ExecutionRequest(
            action=raw.proposed_action,
            risk=raw.risk,
            source_decision_id=source_decision_id,
            thesis_id=raw.thesis_id,
        )
    action, risk = raw
    return ExecutionRequest(action=action, risk=risk, source_decision_id=source_decision_id)


def _run_record(result: ExecutionResult, now: datetime, source_decision_id: str | None) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "created_at": now.isoformat(),
        "source_decision_id": source_decision_id,
        "symbols": [p.symbol for p in result.plans] + [s.symbol for s in result.skipped],
        "plans": [to_dict(p) for p in result.plans],
        "skipped": [to_dict(s) for s in result.skipped],
        "execution_attempted": False,
        "broker_orders_submitted": 0,
        "broker_stop_orders_created": 0,
        "live_execution_attempted": False,
        "live_trade_actions_allowed": False,
        "auto_execution": False,
        "nav": result.context.current_nav if result.context else None,
    }


def _journal(row: dict, path: Path | None, *, persist: bool = True) -> None:
    if path is None and not persist:
        return
    append_jsonl(row, path or journal_path())
