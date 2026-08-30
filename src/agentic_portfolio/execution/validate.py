"""Deterministic Execution Controller validation. Fail closed. No investment logic."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from agentic_portfolio.liquidity import liquidity_ok
from agentic_portfolio.policy import load_policy
from agentic_portfolio.schemas import (
    Decision,
    GateVerdict,
    OpenOrder,
    PortfolioContext,
    Position,
    ProposedAction,
    RiskGateResult,
)
from agentic_portfolio.execution.types import (
    BUY_ACTIONS,
    EXECUTABLE_ACTIONS,
    NON_EXECUTABLE_ACTIONS,
    SELL_ACTIONS,
    SIDE_FOR_ACTION,
    ExecutionStatus,
    LiquidityCheck,
    OrderPlan,
    OrderSide,
    QuoteSnapshot,
    SlippageCheck,
    TradabilitySnapshot,
)

RISK_OK_VERDICTS = {GateVerdict.PASS}
RISK_REDUCING_VERDICTS = {GateVerdict.RISK_REDUCING_ONLY, GateVerdict.HALTED}


class ExecutionValidationError(ValueError):
    """Malformed or inconsistent order plan. Engine must fail closed."""


def is_executable(decision: Decision) -> bool:
    return decision in EXECUTABLE_ACTIONS


def skip_reason(decision: Decision) -> str | None:
    if decision in NON_EXECUTABLE_ACTIONS:
        return "NON_EXECUTABLE_ACTION"
    if decision not in EXECUTABLE_ACTIONS:
        return "UNKNOWN_ACTION"
    return None


def _close(a: float | None, b: float | None, *, abs_tol: float, rel_tol: float) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b), 1.0))


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def held_position(ctx: PortfolioContext, symbol: str) -> Position | None:
    matches = [p for p in ctx.positions if p.symbol.upper() == symbol.upper()]
    return matches[0] if matches else None


def held_quantity(ctx: PortfolioContext, symbol: str) -> float:
    pos = held_position(ctx, symbol)
    return float(pos.quantity or 0.0) if pos else 0.0


def held_market_value(ctx: PortfolioContext, symbol: str) -> float:
    pos = held_position(ctx, symbol)
    return float(pos.market_value or 0.0) if pos else 0.0


def quote_spread_pct(quote: QuoteSnapshot | None, action: ProposedAction) -> float | None:
    if quote is not None and quote.spread_pct is not None:
        return float(quote.spread_pct)
    if quote is not None and quote.bid is not None and quote.ask is not None and quote.ask > 0 and quote.bid > 0:
        mid = (quote.bid + quote.ask) / 2.0
        if mid > 0:
            return (quote.ask - quote.bid) / mid
    if action.liquidity and action.liquidity.bid_ask_spread_pct is not None:
        return float(action.liquidity.bid_ask_spread_pct)
    return None


def estimated_price(quote: QuoteSnapshot | None) -> float | None:
    if quote is None:
        return None
    if quote.last_price is not None and quote.last_price > 0:
        return float(quote.last_price)
    if quote.bid is not None and quote.ask is not None and quote.bid > 0 and quote.ask > 0:
        return (float(quote.bid) + float(quote.ask)) / 2.0
    return None


def risk_permits(action: ProposedAction, risk: RiskGateResult) -> tuple[bool, str | None]:
    if not risk.recommendation_permitted:
        return False, "RISK_GATE_NOT_PERMITTED"
    if risk.verdict == GateVerdict.FAIL:
        return False, "RISK_GATE_NOT_PERMITTED"
    if risk.verdict in RISK_REDUCING_VERDICTS and action.decision not in SELL_ACTIONS:
        return False, "RISK_REDUCING_ONLY_BLOCKS_BUY"
    if risk.verdict == GateVerdict.REQUIRES_ENHANCED_REVIEW:
        return False, "RISK_GATE_NOT_PERMITTED"
    if risk.verdict in RISK_OK_VERDICTS or risk.verdict in RISK_REDUCING_VERDICTS:
        return True, None
    return False, "RISK_GATE_NOT_PERMITTED"


def quote_is_stale(quote: QuoteSnapshot | None, *, now: datetime, max_age_seconds: float) -> tuple[bool, str | None]:
    if quote is None:
        return True, "MISSING_QUOTE"
    if quote.stale:
        return True, "STALE_QUOTE"
    ts = _parse_ts(quote.observed_at)
    if ts is None:
        return True, "STALE_QUOTE"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (now - ts).total_seconds()
    if age < 0 or age > max_age_seconds:
        return True, "STALE_QUOTE"
    return False, None


def conflicting_open_orders(
    symbol: str,
    side: OrderSide,
    orders: Iterable[OpenOrder],
) -> bool:
    del side
    for order in orders:
        if str(order.symbol).upper() != symbol.upper():
            continue
        state = str(order.state or "").strip().lower()
        if state in {"filled", "cancelled", "canceled", "failed", "rejected", "expired"}:
            continue
        return True
    return False


def live_flags_blocked(live_trade_actions_allowed: bool, auto_execution: bool) -> list[str]:
    codes: list[str] = []
    if live_trade_actions_allowed:
        codes.append("LIVE_TRADE_ACTIONS_MUST_REMAIN_FALSE")
    if auto_execution:
        codes.append("AUTO_EXECUTION_MUST_REMAIN_FALSE")
    return codes


def planned_size(
    action: ProposedAction,
    _ctx: PortfolioContext,
    price: float | None,
    *,
    abs_tol: float,
    rel_tol: float,
) -> tuple[float | None, float | None, list[str]]:
    codes: list[str] = []
    notional = action.proposed_notional
    if notional is None or notional <= 0:
        return None, None, ["MISSING_NOTIONAL"]
    if price is None or price <= 0:
        return None, notional, ["MISSING_PRICE"]
    quantity = notional / price
    if not _close(quantity * price, notional, abs_tol=abs_tol, rel_tol=rel_tol):
        codes.append("QUANTITY_NOTIONAL_MISMATCH")
    if action.proposed_notional is not None and not _close(
        notional, float(action.proposed_notional), abs_tol=abs_tol, rel_tol=rel_tol
    ):
        codes.append("QUANTITY_NOTIONAL_MISMATCH")
    return quantity, notional, codes


def cash_and_quantity_codes(
    action: ProposedAction,
    ctx: PortfolioContext,
    quantity: float | None,
    notional: float | None,
) -> list[str]:
    codes: list[str] = []
    if quantity is None or notional is None:
        return codes
    if action.decision in SELL_ACTIONS:
        held = held_quantity(ctx, action.symbol)
        if quantity > held + 1e-9:
            codes.append("SELL_EXCEEDS_HELD_QUANTITY")
        if held <= 0:
            codes.append("SELL_EXCEEDS_HELD_QUANTITY")
    if action.decision in BUY_ACTIONS:
        if notional > ctx.cash + 1e-9 or notional > ctx.buying_power + 1e-9:
            codes.append("BUY_EXCEEDS_AVAILABLE_CASH")
    return codes


def liquidity_and_slippage(
    action: ProposedAction,
    notional: float | None,
    quote: QuoteSnapshot | None,
    config: dict[str, Any],
) -> tuple[LiquidityCheck, SlippageCheck]:
    policy = load_policy()["liquidity"]
    max_spread = float(config.get("max_spread_pct") or 0.02)
    max_slip = float(config.get("max_slippage_pct") or 0.01)
    spread = quote_spread_pct(quote, action)
    adv = action.liquidity.median_daily_dollar_volume_20d if action.liquidity else None
    frac = None
    if notional is not None and adv and adv > 0:
        frac = notional / adv
    liq_codes: list[str] = []
    slip_codes: list[str] = []
    liq_ok = True
    slip_ok = True

    if action.decision in BUY_ACTIONS:
        ok, fail_codes, _reviews = liquidity_ok(
            sleeve=action.sleeve,
            decision=action.decision,
            proposed_notional=notional,
            liq=action.liquidity,
            speculative_review_complete=action.speculative_liquidity_review_complete,
        )
        if not ok:
            liq_ok = False
            liq_codes.extend(fail_codes)
        sleeve_key = "speculative" if action.sleeve and action.sleeve.value == "SPECULATIVE" else "normal"
        max_frac = float(policy[sleeve_key]["max_order_notional_fraction_of_adv"])
    else:
        max_frac = float(policy["normal"]["max_order_notional_fraction_of_adv"])
        if adv is None or adv <= 0:
            liq_codes.append("LIQUIDITY_UNKNOWN_EXIT")

    if spread is not None and spread > max_spread + 1e-12:
        liq_ok = False
        liq_codes.append("SPREAD_EXCEEDS_MAX")
    elif spread is None and action.decision in BUY_ACTIONS:
        liq_ok = False
        liq_codes.append("LIQUIDITY_INSUFFICIENT_EVIDENCE")

    estimated_slip = (spread / 2.0) if spread is not None else None
    if estimated_slip is not None and estimated_slip > max_slip + 1e-12:
        slip_ok = False
        slip_codes.append("SLIPPAGE_EXCEEDS_MAX")
    elif estimated_slip is None and action.decision in BUY_ACTIONS:
        slip_ok = False
        slip_codes.append("SLIPPAGE_INSUFFICIENT_EVIDENCE")

    if frac is not None and action.decision in BUY_ACTIONS:
        sleeve_key = "speculative" if action.sleeve and action.sleeve.value == "SPECULATIVE" else "normal"
        cap = float(policy[sleeve_key]["max_order_notional_fraction_of_adv"])
        if frac > cap + 1e-12:
            liq_ok = False
            if "ORDER_NOTIONAL_EXCEEDS_ADV_FRACTION" not in liq_codes:
                liq_codes.append("ORDER_NOTIONAL_EXCEEDS_ADV_FRACTION")
        max_frac = cap

    liq = LiquidityCheck(
        ok=liq_ok,
        spread_pct=spread,
        notional=notional,
        adv=adv,
        notional_adv_fraction=frac,
        max_spread_pct=max_spread,
        max_notional_adv_fraction=max_frac,
        codes=liq_codes,
    )
    slip = SlippageCheck(
        ok=slip_ok,
        estimated_slippage_pct=estimated_slip,
        max_slippage_pct=max_slip,
        spread_pct=spread,
        codes=slip_codes,
    )
    return liq, slip


def collect_block_reasons(
    action: ProposedAction,
    risk: RiskGateResult,
    ctx: PortfolioContext,
    quote: QuoteSnapshot | None,
    tradability: TradabilitySnapshot | None,
    open_orders: Iterable[OpenOrder],
    *,
    now: datetime,
    config: dict[str, Any],
    live_trade_actions_allowed: bool,
    auto_execution: bool,
) -> tuple[list[str], float | None, float | None, float | None, LiquidityCheck, SlippageCheck]:
    """Return (codes, quantity, notional, price, liquidity, slippage)."""
    codes: list[str] = []
    codes.extend(live_flags_blocked(live_trade_actions_allowed, auto_execution))
    ok, risk_code = risk_permits(action, risk)
    if not ok and risk_code:
        codes.append(risk_code)

    stale, stale_code = quote_is_stale(
        quote, now=now, max_age_seconds=float(config.get("quote_max_age_seconds") or 300)
    )
    if stale and stale_code:
        codes.append(stale_code)

    price = estimated_price(quote)
    if price is None:
        codes.append("MISSING_PRICE")

    if tradability is None or not tradability.tradable:
        codes.append("SYMBOL_NOT_TRADABLE")

    side = SIDE_FOR_ACTION.get(action.decision)
    if side is None:
        codes.append("UNKNOWN_ACTION")
    elif conflicting_open_orders(action.symbol, side, open_orders):
        codes.append("DUPLICATE_CONFLICTING_ORDER")

    abs_tol = float(config.get("quantity_notional_abs_tolerance") or 0.01)
    rel_tol = float(config.get("quantity_notional_rel_tolerance") or 1e-6)
    quantity, notional, size_codes = planned_size(action, ctx, price, abs_tol=abs_tol, rel_tol=rel_tol)
    codes.extend(size_codes)
    codes.extend(cash_and_quantity_codes(action, ctx, quantity, notional))

    liq, slip = liquidity_and_slippage(action, notional, quote, config)
    if not liq.ok:
        codes.append("LIQUIDITY_CHECK_FAILED")
    if not slip.ok:
        codes.append("SLIPPAGE_CHECK_FAILED")
    return codes, quantity, notional, price, liq, slip


def plan_consistency_codes(
    plan: OrderPlan,
    action: ProposedAction,
    ctx: PortfolioContext,
    *,
    abs_tol: float,
    rel_tol: float,
    pct_tol: float,
) -> list[str]:
    codes: list[str] = []
    if plan.action != action.decision:
        codes.append("INCONSISTENT_ORDER_PLAN")
    expected_side = SIDE_FOR_ACTION.get(action.decision)
    if expected_side is not None and plan.order_side != expected_side:
        codes.append("SIDE_ACTION_MISMATCH")
    if plan.stop_orders_created:
        codes.append("STOP_ORDER_NOT_ALLOWED")
    if plan.broker_submitted:
        codes.append("MALFORMED_ORDER_PLAN")
    if plan.execution_status not in {ExecutionStatus.PAPER_ONLY, ExecutionStatus.BLOCKED_FROM_LIVE}:
        codes.append("MALFORMED_ORDER_PLAN")
    if not plan.live_execution_blocked:
        codes.append("MALFORMED_ORDER_PLAN")
    if plan.live_trade_actions_allowed or plan.auto_execution:
        codes.append("MALFORMED_ORDER_PLAN")
    if plan.quantity is not None and plan.estimated_price is not None and plan.notional is not None:
        if not _close(plan.quantity * plan.estimated_price, plan.notional, abs_tol=abs_tol, rel_tol=rel_tol):
            codes.append("INCONSISTENT_ORDER_PLAN")
    if action.proposed_notional is not None and plan.notional is not None:
        if not _close(plan.notional, float(action.proposed_notional), abs_tol=abs_tol, rel_tol=rel_tol):
            codes.append("QUANTITY_NOTIONAL_MISMATCH")
    if action.expected_resulting_position_pct is not None and plan.estimated_position_pct_after is not None:
        if abs(plan.estimated_position_pct_after - action.expected_resulting_position_pct) > pct_tol:
            codes.append("INCONSISTENT_ORDER_PLAN")
    if plan.execution_status == ExecutionStatus.PAPER_ONLY and plan.blocked_reasons:
        codes.append("MALFORMED_ORDER_PLAN")
    if ctx.current_nav <= 0:
        codes.append("MALFORMED_ORDER_PLAN")
    return codes
