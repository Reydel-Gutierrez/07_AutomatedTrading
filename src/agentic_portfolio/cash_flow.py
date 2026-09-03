"""External capital-flow vs investment-return accounting.

Robinhood's live adapter does not expose transfer/ACH activity. This module
reconcilies consecutive book observations (NAV, cash, holdings, optional fills)
without calling AI and without treating every NAV change as a deposit.

Investment P/L for an interval:

    investment_pnl = ending_nav - beginning_nav - net_external_flow

Return uses beginning NAV as the capital base (flow assumed at interval end,
matching hwm.apply_observation). Dividends/interest have no broker feed here;
unexplained cash with unchanged quantities is classified as external flow.
That understates dividend return and is the documented limitation.

When quantity changes cannot be matched to fills, residual NAV is kept as
investment P/L rather than invented deposits (fail-safe).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

FLOW_EPS = 0.01


@dataclass(frozen=True)
class HoldingLot:
    symbol: str
    quantity: float
    market_value: float
    current_price: float | None = None


@dataclass(frozen=True)
class TradeFill:
    symbol: str
    side: str
    quantity: float
    price: float

    @property
    def notional(self) -> float:
        return abs(self.quantity * self.price)

    @property
    def buy(self) -> bool:
        return str(self.side or "").lower() in {"buy", "b"}


@dataclass
class BookObservation:
    nav: float
    cash: float
    holdings: tuple[HoldingLot, ...] = ()

    @property
    def equity(self) -> float:
        return sum(lot.market_value for lot in self.holdings)

    @property
    def identity_gap(self) -> float:
        return float(self.nav) - float(self.cash) - self.equity

    def identity_ok(self, *, eps: float = FLOW_EPS) -> bool:
        return abs(self.identity_gap) <= _eps(self.nav, eps)


@dataclass
class FlowReconciliation:
    external_capital_flow: float
    investment_pnl: float
    market_pnl: float
    period_return: float | None
    beginning_nav: float | None
    ending_nav: float
    cash_delta: float
    identity_gap: float
    confident: bool
    reason: str
    fills_applied: int = 0
    notes: list[str] = field(default_factory=list)


def _eps(nav: float, floor: float = FLOW_EPS) -> float:
    return max(float(floor), 1e-9 * max(abs(float(nav)), 1.0))


def _round_flow(value: float, *, nav: float) -> float:
    if abs(value) <= _eps(nav):
        return 0.0
    return float(value)


def _price(lot: HoldingLot) -> float | None:
    if lot.current_price is not None and lot.current_price > 0:
        return float(lot.current_price)
    if lot.quantity:
        try:
            return float(lot.market_value) / float(lot.quantity)
        except ZeroDivisionError:
            return None
    return None


def _lot_map(obs: BookObservation) -> dict[str, HoldingLot]:
    return {lot.symbol.upper(): lot for lot in obs.holdings if lot.symbol}


def holding_from_mapping(raw: Mapping[str, Any] | Any) -> HoldingLot | None:
    if raw is None:
        return None
    if isinstance(raw, HoldingLot):
        return raw
    if not isinstance(raw, Mapping):
        symbol = getattr(raw, "symbol", None)
        if not symbol:
            return None
        qty = float(getattr(raw, "quantity", 0.0) or 0.0)
        mv = float(getattr(raw, "market_value", 0.0) or 0.0)
        price = getattr(raw, "current_price", None)
        return HoldingLot(
            symbol=str(symbol).upper(),
            quantity=qty,
            market_value=mv,
            current_price=None if price is None else float(price),
        )
    symbol = str(raw.get("symbol") or "").upper()
    if not symbol:
        return None
    qty = float(raw.get("quantity") or 0.0)
    mv = float(raw.get("market_value") or 0.0)
    price = raw.get("current_price")
    return HoldingLot(
        symbol=symbol,
        quantity=qty,
        market_value=mv,
        current_price=None if price in (None, "") else float(price),
    )


def observation_from_facts(
    *,
    nav: float,
    cash: float,
    positions: Iterable[Any] | None = None,
) -> BookObservation:
    lots: list[HoldingLot] = []
    for item in positions or []:
        lot = holding_from_mapping(item)
        if lot is not None:
            lots.append(lot)
    return BookObservation(nav=float(nav), cash=float(cash), holdings=tuple(lots))


def observation_from_context_dict(raw: Mapping[str, Any] | None) -> BookObservation | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        nav = float(raw.get("current_nav") if raw.get("current_nav") is not None else raw.get("nav"))
        cash = float(raw.get("cash"))
    except (TypeError, ValueError):
        return None
    return observation_from_facts(nav=nav, cash=cash, positions=raw.get("positions") or [])


def continuing_market_pnl(prior: BookObservation, current: BookObservation) -> float:
    """Mark-to-market on the overlapping quantity. New/exited shares are 0 this interval
    unless a fill price is supplied separately.
    """
    pnl = 0.0
    now_map = _lot_map(current)
    for symbol, before in _lot_map(prior).items():
        after = now_map.get(symbol)
        q0 = float(before.quantity or 0.0)
        q1 = float(after.quantity or 0.0) if after is not None else 0.0
        held = min(q0, q1)
        if held <= 0:
            continue
        p0 = _price(before)
        p1 = _price(after) if after is not None else p0
        if p0 is None or p1 is None:
            continue
        pnl += held * (p1 - p0)
    return pnl


def _fill_notionals(fills: Sequence[TradeFill] | None) -> tuple[float, float, int]:
    buys = 0.0
    sells = 0.0
    count = 0
    for fill in fills or []:
        if fill.quantity <= 0 or fill.price <= 0:
            continue
        count += 1
        if fill.buy:
            buys += fill.notional
        else:
            sells += fill.notional
    return buys, sells, count


def reconcile_external_flow(
    prior: BookObservation | None,
    current: BookObservation,
    *,
    fills: Sequence[TradeFill] | None = None,
) -> FlowReconciliation:
    """Deterministic external-flow estimate. Never fabricates flow when ambiguous."""
    if prior is None:
        return FlowReconciliation(
            external_capital_flow=0.0,
            investment_pnl=0.0,
            market_pnl=0.0,
            period_return=None,
            beginning_nav=None,
            ending_nav=float(current.nav),
            cash_delta=0.0,
            identity_gap=current.identity_gap,
            confident=True,
            reason="no_prior_observation",
        )

    cash_delta = float(current.cash) - float(prior.cash)
    nav_delta = float(current.nav) - float(prior.nav)
    prior_qty = {sym: lot.quantity for sym, lot in _lot_map(prior).items()}
    current_qty = {sym: lot.quantity for sym, lot in _lot_map(current).items()}
    symbols = set(prior_qty) | set(current_qty)
    qty_changed = any(abs(float(current_qty.get(s) or 0.0) - float(prior_qty.get(s) or 0.0)) > 1e-9 for s in symbols)
    cash_only = not prior.holdings and not current.holdings
    market = continuing_market_pnl(prior, current)
    buys, sells, fill_count = _fill_notionals(fills)
    notes: list[str] = []

    if cash_only:
        flow = _round_flow(nav_delta, nav=current.nav)
        if abs(cash_delta - nav_delta) > _eps(current.nav):
            notes.append("cash_only_nav_cash_mismatch")
            flow = 0.0
            return FlowReconciliation(
                external_capital_flow=0.0,
                investment_pnl=_round_flow(nav_delta, nav=current.nav),
                market_pnl=_round_flow(nav_delta, nav=current.nav),
                period_return=(nav_delta / prior.nav) if prior.nav else None,
                beginning_nav=float(prior.nav),
                ending_nav=float(current.nav),
                cash_delta=cash_delta,
                identity_gap=current.identity_gap,
                confident=False,
                reason="ambiguous_cash_only_identity",
                notes=notes,
            )
        pnl = nav_delta - flow
        ret = (pnl / prior.nav) if prior.nav else None
        return FlowReconciliation(
            external_capital_flow=flow,
            investment_pnl=_round_flow(pnl, nav=current.nav),
            market_pnl=_round_flow(pnl, nav=current.nav),
            period_return=ret,
            beginning_nav=float(prior.nav),
            ending_nav=float(current.nav),
            cash_delta=cash_delta,
            identity_gap=current.identity_gap,
            confident=True,
            reason="cash_only_nav_delta",
            notes=notes,
        )

    if fill_count:
        # Δcash = -buys + sells + external_flow (+ undocumented dividends)
        flow = _round_flow(cash_delta + buys - sells, nav=current.nav)
        pnl = nav_delta - flow
        ret = (pnl / prior.nav) if prior.nav else None
        notes.append("fills_applied")
        return FlowReconciliation(
            external_capital_flow=flow,
            investment_pnl=_round_flow(pnl, nav=current.nav),
            market_pnl=_round_flow(market if not qty_changed else pnl, nav=current.nav),
            period_return=ret,
            beginning_nav=float(prior.nav),
            ending_nav=float(current.nav),
            cash_delta=cash_delta,
            identity_gap=current.identity_gap,
            confident=True,
            reason="fill_adjusted_cash_residual",
            fills_applied=fill_count,
            notes=notes,
        )

    if not qty_changed:
        # Unchanged share counts: cash residual is contribution/withdrawal (or dividend).
        flow = _round_flow(cash_delta, nav=current.nav)
        pnl = nav_delta - flow
        if abs(pnl - market) > _eps(current.nav) * 10:
            notes.append("mark_to_market_residual")
        ret = (pnl / prior.nav) if prior.nav else None
        return FlowReconciliation(
            external_capital_flow=flow,
            investment_pnl=_round_flow(pnl, nav=current.nav),
            market_pnl=_round_flow(market, nav=current.nav),
            period_return=ret,
            beginning_nav=float(prior.nav),
            ending_nav=float(current.nav),
            cash_delta=cash_delta,
            identity_gap=current.identity_gap,
            confident=prior.identity_ok() and current.identity_ok(),
            reason="unchanged_quantity_cash_residual",
            notes=notes,
        )

    # Quantity changed without fill evidence: do not invent a deposit/withdrawal.
    # BUY/SELL cash swaps stay in the book; residual NAV is investment P/L.
    notes.append("qty_change_without_fills")
    notes.append("dividends_without_transfer_feed_not_separable")
    pnl = nav_delta
    ret = (pnl / prior.nav) if prior.nav else None
    return FlowReconciliation(
        external_capital_flow=0.0,
        investment_pnl=_round_flow(pnl, nav=current.nav),
        market_pnl=_round_flow(market if abs(market) > _eps(current.nav) else pnl, nav=current.nav),
        period_return=ret,
        beginning_nav=float(prior.nav),
        ending_nav=float(current.nav),
        cash_delta=cash_delta,
        identity_gap=current.identity_gap,
        confident=True,
        reason="qty_change_without_fills_no_invented_flow",
        notes=notes,
    )


def interval_return(prior_nav: float | None, investment_pnl: float) -> float | None:
    if prior_nav is None or prior_nav <= 0:
        return None
    return float(investment_pnl) / float(prior_nav)


def chain_time_weighted_return(interval_returns: Sequence[float | None]) -> float | None:
    """Product of (1+r_i) - 1. Skips None. Returns None if nothing usable."""
    acc = 1.0
    used = 0
    for raw in interval_returns:
        if raw is None:
            continue
        acc *= 1.0 + float(raw)
        used += 1
    if used <= 0:
        return None
    return acc - 1.0


def history_interval_returns(points: Sequence[Mapping[str, Any]]) -> list[float | None]:
    """Recompute interval investment returns from snapshot-like history points.

    Does not mutate stored snapshots. Old contribution snapshots with
    external_capital_flow=0 are corrected when cash/holdings identity is usable.
    """
    rows = [p for p in points if isinstance(p, Mapping) and p.get("nav") is not None]
    out: list[float | None] = []
    for prev, cur in zip(rows, rows[1:]):
        prior_obs = _observation_from_history_point(prev)
        current_obs = _observation_from_history_point(cur)
        if prior_obs is None or current_obs is None:
            out.append(_naive_nav_return(prev, cur))
            continue
        recon = reconcile_external_flow(prior_obs, current_obs)
        if recon.period_return is not None:
            out.append(recon.period_return)
        else:
            out.append(_naive_nav_return(prev, cur))
    return out


def cash_flow_adjusted_total_return(points: Sequence[Mapping[str, Any]]) -> float | None:
    returns = history_interval_returns(points)
    if not returns:
        return None
    return chain_time_weighted_return(returns)


def _observation_from_history_point(point: Mapping[str, Any]) -> BookObservation | None:
    nav = point.get("nav")
    cash = point.get("cash")
    if nav is None:
        return None
    try:
        nav_f = float(nav)
    except (TypeError, ValueError):
        return None
    positions = point.get("positions")
    if cash is None and not positions:
        return None
    try:
        cash_f = float(cash) if cash is not None else nav_f
    except (TypeError, ValueError):
        return None
    return observation_from_facts(nav=nav_f, cash=cash_f, positions=positions or [])


def _naive_nav_return(prev: Mapping[str, Any], cur: Mapping[str, Any]) -> float | None:
    try:
        first = float(prev.get("nav"))
        last = float(cur.get("nav"))
    except (TypeError, ValueError):
        return None
    if not first:
        return None
    return (last / first) - 1.0
