"""Deterministic paper book updates. Average cost + FIFO lots. No broker behavior."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from agentic_portfolio.context import build_context
from agentic_portfolio.execution.types import BUY_ACTIONS, SELL_ACTIONS, OrderPlan
from agentic_portfolio.execution.validate import held_position, held_quantity
from agentic_portfolio.paper_fill.types import BookDelta, PaperLot
from agentic_portfolio.schemas import PortfolioContext, Position, Sleeve

QTY_EPS = 1e-9


class PaperAccountingError(ValueError):
    """Illegal paper book transition. Engine must fail closed."""


def is_zero_qty(qty: float, *, eps: float = QTY_EPS) -> bool:
    return abs(qty) <= eps


def lots_from_context(ctx: PortfolioContext, *, opened_at: str) -> list[PaperLot]:
    lots: list[PaperLot] = []
    for pos in ctx.positions:
        qty = float(pos.quantity or 0.0)
        if is_zero_qty(qty):
            continue
        cost = pos.average_cost
        if cost is None or cost <= 0:
            if pos.current_price is not None and pos.current_price > 0:
                cost = float(pos.current_price)
            elif qty:
                cost = float(pos.market_value) / qty
            else:
                continue
        lots.append(
            PaperLot(
                lot_id=str(uuid4()),
                symbol=pos.symbol.upper(),
                quantity=qty,
                cost_price=float(cost),
                opened_at=opened_at,
                thesis_id=pos.thesis_id,
            )
        )
    return lots


def average_cost_for(lots: list[PaperLot], symbol: str) -> tuple[float, float | None]:
    held = [lot for lot in lots if lot.symbol == symbol.upper() and not is_zero_qty(lot.quantity)]
    qty = sum(lot.quantity for lot in held)
    if is_zero_qty(qty):
        return 0.0, None
    cost = sum(lot.quantity * lot.cost_price for lot in held) / qty
    return qty, cost


def rebuild_context(
    ctx: PortfolioContext,
    *,
    cash: float,
    positions: list[Position],
    realized_pnl: float | None,
    timestamp: str,
) -> PortfolioContext:
    invested = sum(p.market_value for p in positions)
    nav = float(cash) + invested
    return build_context(
        account_number=ctx.account_number,
        current_nav=nav,
        cash=float(cash),
        buying_power=float(cash),
        positions=positions,
        open_orders=[],
        realized_pnl=realized_pnl,
        start_of_day_nav=ctx.start_of_day_nav,
        prior_nav=ctx.current_nav,
        prior_hwm=ctx.cash_flow_adjusted_hwm,
        external_capital_flow=0.0,
        spy=ctx.spy,
        timestamp=timestamp,
        trading_session_id=ctx.trading_session_id,
        session_fail_safe=ctx.session_fail_safe,
        correlation=ctx.correlation,
    )


def apply_to_book(
    plan: OrderPlan,
    ctx: PortfolioContext,
    lots: list[PaperLot],
    *,
    fill_price: float,
    quantity: float,
    timestamp: str,
    eps: float = QTY_EPS,
    sleeve: Sleeve | None = None,
) -> tuple[PortfolioContext, BookDelta]:
    if fill_price <= 0 or quantity <= 0:
        raise PaperAccountingError("MISSING_PRICE" if fill_price <= 0 else "MISSING_QUANTITY")

    symbol = plan.symbol.upper()
    notional = quantity * fill_price
    held = held_position(ctx, symbol)
    qty_before = held_quantity(ctx, symbol)
    avg_before = held.average_cost if held is not None else None
    if avg_before is None and held is not None:
        _, avg_before = average_cost_for(lots, symbol)
    cash_before = float(ctx.cash)
    sleeve_before = held.sleeve.value if held is not None and held.sleeve else None
    thesis_id = plan.thesis_id or (held.thesis_id if held is not None else None)

    working = list(lots)
    realized = 0.0
    closed = False

    if plan.action in BUY_ACTIONS:
        if notional > cash_before + eps:
            raise PaperAccountingError("BUY_EXCEEDS_AVAILABLE_CASH")
        cash_after = cash_before - notional
        working.append(
            PaperLot(
                lot_id=str(uuid4()),
                symbol=symbol,
                quantity=quantity,
                cost_price=fill_price,
                opened_at=timestamp,
                thesis_id=thesis_id,
            )
        )
        qty_after, avg_after = average_cost_for(working, symbol)
        positions = _upsert_position(
            ctx.positions,
            held,
            symbol=symbol,
            quantity=qty_after,
            fill_price=fill_price,
            average_cost=avg_after,
            thesis_id=thesis_id,
            sleeve=sleeve or (held.sleeve if held is not None else None),
        )
    elif plan.action in SELL_ACTIONS:
        if quantity > qty_before + eps or qty_before <= 0:
            raise PaperAccountingError("SELL_CREATES_NEGATIVE_POSITION")
        cash_after = cash_before + notional
        working, realized = _consume_lots(working, symbol, quantity, fill_price, eps=eps)
        qty_after, avg_after = average_cost_for(working, symbol)
        closed = is_zero_qty(qty_after, eps=eps)
        if closed:
            positions = [p for p in ctx.positions if p.symbol.upper() != symbol]
            avg_after = None
            qty_after = 0.0
        else:
            positions = _upsert_position(
                ctx.positions,
                held,
                symbol=symbol,
                quantity=qty_after,
                fill_price=fill_price,
                average_cost=avg_after,
                thesis_id=thesis_id,
                sleeve=held.sleeve if held is not None else sleeve,
            )
    else:
        raise PaperAccountingError("UNKNOWN_ACTION")

    prior_realized = float(ctx.realized_pnl or 0.0)
    new_ctx = rebuild_context(
        ctx,
        cash=cash_after,
        positions=positions,
        realized_pnl=prior_realized + realized,
        timestamp=timestamp,
    )
    held_after = held_position(new_ctx, symbol)
    delta = BookDelta(
        symbol=symbol,
        action=plan.action,
        side=plan.order_side,
        quantity=quantity,
        fill_price=fill_price,
        filled_notional=notional,
        cash_before=cash_before,
        cash_after=cash_after,
        quantity_before=qty_before,
        quantity_after=qty_after,
        average_cost_before=avg_before,
        average_cost_after=avg_after,
        realized_pnl=realized,
        position_closed=closed,
        nav_before=float(ctx.current_nav),
        nav_after=float(new_ctx.current_nav),
        thesis_id=thesis_id,
        sleeve_before=sleeve_before,
        sleeve_after=held_after.sleeve.value if held_after is not None and held_after.sleeve else None,
        lots_after=working,
    )
    return new_ctx, delta


def _consume_lots(
    lots: list[PaperLot],
    symbol: str,
    quantity: float,
    fill_price: float,
    *,
    eps: float,
) -> tuple[list[PaperLot], float]:
    remaining = quantity
    realized = 0.0
    out: list[PaperLot] = []
    want = symbol.upper()
    for lot in lots:
        if lot.symbol != want or remaining <= eps:
            out.append(lot)
            continue
        take = min(lot.quantity, remaining)
        realized += (fill_price - lot.cost_price) * take
        leftover = lot.quantity - take
        remaining -= take
        if leftover > eps:
            out.append(replace(lot, quantity=leftover))
    if remaining > eps:
        raise PaperAccountingError("SELL_CREATES_NEGATIVE_POSITION")
    return out, realized


def _upsert_position(
    positions: list[Position],
    held: Position | None,
    *,
    symbol: str,
    quantity: float,
    fill_price: float,
    average_cost: float | None,
    thesis_id: str | None,
    sleeve: Sleeve | None = None,
) -> list[Position]:
    mv = quantity * fill_price
    unreal = None
    if average_cost is not None:
        unreal = (fill_price - average_cost) * quantity
    if held is None:
        new_pos = Position(
            symbol=symbol,
            market_value=mv,
            quantity=quantity,
            average_cost=average_cost,
            current_price=fill_price,
            sleeve=sleeve,
            unrealized_pnl=unreal,
            thesis_id=thesis_id,
        )
        return list(positions) + [new_pos]
    updated = replace(
        held,
        market_value=mv,
        quantity=quantity,
        average_cost=average_cost,
        current_price=fill_price,
        unrealized_pnl=unreal,
        thesis_id=held.thesis_id or thesis_id,
        sleeve=held.sleeve or sleeve,
    )
    return [updated if p.symbol.upper() == symbol else p for p in positions]
