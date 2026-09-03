"""Deterministic live order sizing. Fail closed. No broker calls.

SELL = full liquidation of the current live holding.
REDUCE = partial rebalance to the approved target post-trade allocation.

`proposed_allocation_pct` is the target remaining portfolio allocation in percent
of current NAV after the trade — not the trade size and not a delta.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

from agentic_portfolio.review.validate import quantity_str
from agentic_portfolio.schemas import PortfolioContext


BUY_ACTIONS = {"BUY", "ADD"}
SELL_ACTIONS = {"SELL", "REDUCE"}
NON_EXECUTABLE_ACTIONS = {"HOLD", "WATCH", "REJECT", "NO_ACTION"}

NO_LIVE_POSITION = "no_live_position"
MISSING_QUOTE = "missing_quote"
MISSING_NAV = "missing_nav"
MISSING_TARGET_ALLOCATION = "missing_target_allocation"
REDUCE_TARGET_NOT_BELOW_CURRENT = "reduce_target_not_below_current"
REDUCE_BELOW_MINIMUM = "reduce_below_minimum"
UNSIZED_SELL = "unsized_sell"
ZERO_QUANTITY = "zero_quantity"
OVERSELL_BLOCKED = "oversell_blocked"
NON_EXECUTABLE_ACTION = "non_executable_action"
UNKNOWN_ACTION = "unknown_action"
ORDER_TOO_SMALL = "order_too_small_or_unsized"


@dataclass(frozen=True)
class SizedOrder:
    """Resolved live size. `reason` is set iff the order must not be submitted."""

    action: str
    side: str
    quantity: float | None = None
    notional: float | None = None
    use_dollar_amount: bool = False
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.reason is None


def as_float(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return value


def held_quantity_from_context(
    context: PortfolioContext | None,
    symbol: str,
    quote: float | None = None,
) -> float:
    """Current live held quantity. Current holdings win over approval-time size."""
    if context is None:
        return 0.0
    want = str(symbol).upper()
    for pos in context.positions or []:
        if str(getattr(pos, "symbol", "")).upper() != want:
            continue
        qty = as_float(getattr(pos, "quantity", None))
        if qty is not None and qty > 0:
            return qty
        mv = as_float(getattr(pos, "market_value", None)) or 0.0
        price = (
            quote
            or as_float(getattr(pos, "current_price", None))
            or as_float(getattr(pos, "last_price", None))
            or as_float(getattr(pos, "price", None))
        )
        if mv > 0 and price and price > 0:
            return mv / float(price)
        return float(qty or 0.0)
    return 0.0


def _to_decimal(value: float) -> Decimal:
    return Decimal(format(float(value), ".15g"))


def cap_quantity(quantity: float, *, held: float, decimals: int) -> float | None:
    """Round to configured precision without exceeding held quantity.

    If normal rounding would oversell, round down. Zero after rounding is rejected.
    """
    if quantity <= 0 or held <= 0:
        return None
    places = max(0, int(decimals))
    quantize_exp = Decimal("1").scaleb(-places)
    qty = _to_decimal(quantity)
    held_d = _to_decimal(held)
    rounded = qty.quantize(quantize_exp)
    if rounded > held_d:
        rounded = min(qty, held_d).quantize(quantize_exp, rounding=ROUND_DOWN)
    if rounded <= 0:
        return None
    if rounded > held_d:
        return None
    return float(rounded)


def format_quantity(quantity: float, *, decimals: int) -> str:
    return quantity_str(quantity, decimals=decimals)


def resolve_live_sizing(
    *,
    action: str,
    symbol: str,
    context: PortfolioContext,
    quote: float | None,
    proposed_dollar_amount: float | None,
    proposed_allocation_pct: float | None,
    prefer_dollar_amount_for_buy: bool = True,
    order_type: str = "market",
    min_order_notional_usd: float = 1.0,
    quantity_decimal_places: int = 6,
) -> SizedOrder:
    """Size one live order from current holdings/NAV/quote. Never invent a size."""
    action_u = str(action or "").upper()
    decimals = int(quantity_decimal_places or 6)
    min_notional = float(min_order_notional_usd or 0.0)

    if action_u in NON_EXECUTABLE_ACTIONS:
        return SizedOrder(action=action_u, side="buy", reason=NON_EXECUTABLE_ACTION)
    if action_u not in BUY_ACTIONS | SELL_ACTIONS:
        return SizedOrder(action=action_u or "UNKNOWN", side="buy", reason=UNKNOWN_ACTION)
    if action_u in BUY_ACTIONS:
        return _resolve_buy(
            action=action_u,
            context=context,
            quote=quote,
            proposed_dollar_amount=proposed_dollar_amount,
            proposed_allocation_pct=proposed_allocation_pct,
            prefer_dollar_amount=prefer_dollar_amount_for_buy,
            order_type=order_type,
            min_notional=min_notional,
            decimals=decimals,
        )
    if action_u == "SELL":
        return resolve_sell_quantity(
            symbol=symbol,
            context=context,
            quote=quote,
            decimals=decimals,
            min_notional=min_notional,
        )
    return resolve_reduce_sizing(
        symbol=symbol,
        context=context,
        quote=quote,
        proposed_allocation_pct=proposed_allocation_pct,
        decimals=decimals,
        min_notional=min_notional,
    )


def _resolve_buy(
    *,
    action: str,
    context: PortfolioContext,
    quote: float | None,
    proposed_dollar_amount: float | None,
    proposed_allocation_pct: float | None,
    prefer_dollar_amount: bool,
    order_type: str,
    min_notional: float,
    decimals: int,
) -> SizedOrder:
    nav = as_float(getattr(context, "current_nav", None)) or 0.0
    notional = as_float(proposed_dollar_amount) or 0.0
    pct = as_float(proposed_allocation_pct)
    if pct and nav:
        from_pct = nav * (pct / 100.0)
        notional = min(notional, from_pct) if notional else from_pct
    notional = min(
        notional,
        float(getattr(context, "cash", None) or 0.0),
        float(getattr(context, "buying_power", None) or 0.0),
    )
    if notional < min_notional:
        return SizedOrder(action=action, side="buy", reason=ORDER_TOO_SMALL)
    if prefer_dollar_amount and str(order_type or "market") == "market":
        return SizedOrder(action=action, side="buy", notional=notional, use_dollar_amount=True)
    price = as_float(quote)
    if not price or price <= 0:
        return SizedOrder(action=action, side="buy", reason=MISSING_QUOTE)
    qty = cap_quantity(notional / price, held=notional / price, decimals=decimals)
    if qty is None or qty <= 0:
        return SizedOrder(action=action, side="buy", reason=ZERO_QUANTITY)
    return SizedOrder(action=action, side="buy", quantity=qty, notional=qty * price)


def resolve_sell_quantity(
    *,
    symbol: str,
    context: PortfolioContext,
    quote: float | None,
    decimals: int,
    min_notional: float,
) -> SizedOrder:
    """SELL = full liquidation of the current live holding, capped to holdings.

    Ignores proposed_dollar_amount and proposed_allocation_pct. Current live
    quantity is authoritative. Stale approval quantity is never used.
    """
    held = held_quantity_from_context(context, symbol, quote)
    if held <= 0:
        return SizedOrder(action="SELL", side="sell", reason=NO_LIVE_POSITION)
    qty = cap_quantity(held, held=held, decimals=decimals)
    if qty is None or qty <= 0:
        return SizedOrder(action="SELL", side="sell", reason=ZERO_QUANTITY)
    if qty > held + 1e-15:
        return SizedOrder(action="SELL", side="sell", reason=OVERSELL_BLOCKED)
    price = as_float(quote)
    notional = (qty * price) if price and price > 0 else None
    if notional is not None and min_notional > 0 and notional < min_notional:
        return SizedOrder(action="SELL", side="sell", reason=ORDER_TOO_SMALL)
    return SizedOrder(action="SELL", side="sell", quantity=qty, notional=notional)


def resolve_reduce_sizing(
    *,
    symbol: str,
    context: PortfolioContext,
    quote: float | None,
    proposed_allocation_pct: float | None,
    decimals: int,
    min_notional: float,
) -> SizedOrder:
    """REDUCE sells down to the approved target post-trade allocation.

    Never falls back to selling the whole position. Formula:

        current_market_value = held_quantity * current_quote
        target_market_value = current_nav * proposed_allocation_pct / 100
        sell_notional = current_market_value - target_market_value
        quantity_to_sell = sell_notional / current_quote
    """
    price = as_float(quote)
    if price is None or price <= 0:
        return SizedOrder(action="REDUCE", side="sell", reason=MISSING_QUOTE)
    nav = as_float(getattr(context, "current_nav", None))
    if nav is None or nav <= 0:
        return SizedOrder(action="REDUCE", side="sell", reason=MISSING_NAV)
    target_pct = as_float(proposed_allocation_pct)
    if target_pct is None or target_pct < 0:
        return SizedOrder(action="REDUCE", side="sell", reason=MISSING_TARGET_ALLOCATION)

    held = held_quantity_from_context(context, symbol, price)
    if held <= 0:
        return SizedOrder(action="REDUCE", side="sell", reason=NO_LIVE_POSITION)

    current_market_value = held * price
    target_market_value = nav * (target_pct / 100.0)
    sell_notional = current_market_value - target_market_value
    if sell_notional <= 1e-12:
        return SizedOrder(action="REDUCE", side="sell", reason=REDUCE_TARGET_NOT_BELOW_CURRENT)
    if min_notional > 0 and sell_notional < min_notional:
        return SizedOrder(action="REDUCE", side="sell", reason=REDUCE_BELOW_MINIMUM)

    raw_qty = sell_notional / price
    if raw_qty <= 0:
        return SizedOrder(action="REDUCE", side="sell", reason=ZERO_QUANTITY)
    if raw_qty > held:
        raw_qty = held
    qty = cap_quantity(raw_qty, held=held, decimals=decimals)
    if qty is None or qty <= 0:
        return SizedOrder(action="REDUCE", side="sell", reason=ZERO_QUANTITY)
    if qty > held + 1e-15:
        return SizedOrder(action="REDUCE", side="sell", reason=OVERSELL_BLOCKED)

    final_notional = qty * price
    if min_notional > 0 and final_notional < min_notional:
        return SizedOrder(action="REDUCE", side="sell", reason=REDUCE_BELOW_MINIMUM)
    return SizedOrder(action="REDUCE", side="sell", quantity=qty, notional=final_notional)
