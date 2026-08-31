from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping

from agentic_portfolio.hwm import apply_observation
from agentic_portfolio.policy import load_account_rules, load_policy
from agentic_portfolio.schemas import (
    ClassificationStatus,
    OpenOrder,
    PortfolioContext,
    Position,
    PositionRegistryStatus,
    RiskState,
    SecurityClass,
    Sleeve,
    SpyBenchmark,
)
from agentic_portfolio.sectors import CanonicalSector, map_sector


def _pct(part: float, nav: float) -> float:
    if nav <= 0:
        return 0.0
    return part / nav


def build_context(
    *,
    account_number: str,
    current_nav: float,
    cash: float,
    buying_power: float,
    positions: list[Position],
    open_orders: list[OpenOrder] | None = None,
    realized_pnl: float | None = None,
    start_of_day_nav: float | None = None,
    prior_nav: float | None = None,
    prior_hwm: float | None = None,
    external_capital_flow: float = 0.0,
    spy: SpyBenchmark | None = None,
    timestamp: str | None = None,
    trading_session_id: str | None = None,
    session_fail_safe: bool = False,
    correlation=None,
) -> PortfolioContext:
    """Build a canonical snapshot from observed facts. Does not call brokers."""
    policy = load_policy()
    rules = load_account_rules()
    expected = rules["account"]["account_number"]
    if account_number != expected:
        raise ValueError("account_number is not the configured Agentic account")

    nav = float(current_nav)
    sleeves: dict[str, float] = {s.value: 0.0 for s in Sleeve}
    sectors: dict[str, float] = defaultdict(float)
    unreal = 0.0
    for p in positions:
        if p.sleeve:
            sleeves[p.sleeve.value] += p.market_value
        if p.sector:
            mapped, status = map_sector(p.sector)
            key = mapped.value if mapped != CanonicalSector.UNKNOWN else CanonicalSector.UNKNOWN.value
            if status.value != "CONFLICTING":
                p.sector = key
            else:
                p.sector = CanonicalSector.UNKNOWN.value
            sectors[p.sector] += p.market_value
        if p.unrealized_pnl is not None:
            unreal += p.unrealized_pnl

    hwm = apply_observation(
        prior_nav=prior_nav,
        prior_hwm=prior_hwm,
        nav_after=nav,
        external_capital_flow=external_capital_flow,
    )

    daily_ret = None
    daily_halt = False
    thr = float(policy["daily_risk_halt"]["threshold_fraction_of_start_of_day_nav"])
    if start_of_day_nav and start_of_day_nav > 0:
        daily_ret = (nav - start_of_day_nav) / start_of_day_nav
        daily_halt = daily_ret <= -thr

    ts = timestamp or datetime.now(timezone.utc).isoformat()
    return PortfolioContext(
        timestamp=ts,
        account_number=account_number,
        current_nav=nav,
        cash=float(cash),
        buying_power=float(buying_power),
        cash_allocation_pct=_pct(cash, nav),
        positions=list(positions),
        holdings_count=len(positions),
        sleeve_market_values=dict(sleeves),
        sleeve_allocation_pct={k: _pct(v, nav) for k, v in sleeves.items()},
        sector_exposure=dict(sectors),
        sector_allocation_pct={k: _pct(v, nav) for k, v in sectors.items()},
        open_orders=list(open_orders or []),
        realized_pnl=realized_pnl,
        unrealized_pnl=unreal if positions else realized_pnl,
        start_of_day_nav=start_of_day_nav,
        daily_portfolio_return=daily_ret,
        daily_risk_halt=daily_halt,
        high_water_mark=hwm.cash_flow_adjusted_hwm,
        cash_flow_adjusted_hwm=hwm.cash_flow_adjusted_hwm,
        external_capital_flow=hwm.external_capital_flow,
        current_drawdown=hwm.drawdown,
        risk_state=hwm.risk_state,
        spy=spy,
        trading_session_id=trading_session_id,
        session_fail_safe=session_fail_safe,
        correlation=correlation,
    )


def _enum(cls, value, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, cls):
        return value
    return cls(value)


def position_from_dict(raw: Mapping[str, Any] | None) -> Position:
    data = dict(raw or {})
    sleeve = data.get("sleeve")
    cls = data.get("security_class")
    status = data.get("classification_status")
    registry = data.get("registry_status")
    return Position(
        symbol=str(data.get("symbol") or "").upper(),
        market_value=float(data.get("market_value") or 0.0),
        quantity=float(data.get("quantity") or 0.0),
        average_cost=None if data.get("average_cost") is None else float(data.get("average_cost")),
        current_price=None if data.get("current_price") is None else float(data.get("current_price")),
        sleeve=_enum(Sleeve, sleeve),
        security_class=_enum(SecurityClass, cls),
        classification_status=_enum(ClassificationStatus, status),
        sector=data.get("sector"),
        unrealized_pnl=None if data.get("unrealized_pnl") is None else float(data.get("unrealized_pnl")),
        registry_status=_enum(PositionRegistryStatus, registry, PositionRegistryStatus.REGISTERED),
        thesis_id=data.get("thesis_id"),
    )


def portfolio_context_from_dict(raw: Mapping[str, Any] | None) -> PortfolioContext:
    data = dict(raw or {})
    positions = [position_from_dict(p) for p in (data.get("positions") or []) if isinstance(p, dict)]
    spy_raw = data.get("spy")
    spy = None
    if isinstance(spy_raw, dict) and spy_raw:
        spy = SpyBenchmark(
            price=None if spy_raw.get("price") is None else float(spy_raw.get("price")),
            period_return=spy_raw.get("period_return") if spy_raw.get("period_return") is not None else spy_raw.get("return_pct"),
            portfolio_return=spy_raw.get("portfolio_return"),
            excess_return=spy_raw.get("excess_return"),
        )
    orders = []
    for item in data.get("open_orders") or []:
        if not isinstance(item, dict):
            continue
        orders.append(
            OpenOrder(
                order_id=str(item.get("order_id") or ""),
                symbol=str(item.get("symbol") or ""),
                side=str(item.get("side") or ""),
                state=str(item.get("state") or ""),
                notional=item.get("notional"),
            )
        )
    nav = float(data.get("current_nav") or 0.0)
    cash = float(data.get("cash") or 0.0)
    risk = data.get("risk_state") or RiskState.NORMAL
    return PortfolioContext(
        timestamp=str(data.get("timestamp") or datetime.now(timezone.utc).isoformat()),
        account_number=str(data.get("account_number") or load_account_rules()["account"]["account_number"]),
        current_nav=nav,
        cash=cash,
        buying_power=float(data.get("buying_power") if data.get("buying_power") is not None else cash),
        cash_allocation_pct=float(data.get("cash_allocation_pct") if data.get("cash_allocation_pct") is not None else (cash / nav if nav else 0.0)),
        positions=positions,
        holdings_count=int(data.get("holdings_count") if data.get("holdings_count") is not None else len(positions)),
        sleeve_market_values=dict(data.get("sleeve_market_values") or {}),
        sleeve_allocation_pct=dict(data.get("sleeve_allocation_pct") or {}),
        sector_exposure=dict(data.get("sector_exposure") or {}),
        sector_allocation_pct=dict(data.get("sector_allocation_pct") or {}),
        open_orders=orders,
        realized_pnl=data.get("realized_pnl"),
        unrealized_pnl=data.get("unrealized_pnl"),
        start_of_day_nav=data.get("start_of_day_nav"),
        daily_portfolio_return=data.get("daily_portfolio_return"),
        daily_risk_halt=bool(data.get("daily_risk_halt")),
        high_water_mark=float(data.get("high_water_mark") or nav or 1.0),
        cash_flow_adjusted_hwm=float(data.get("cash_flow_adjusted_hwm") or data.get("high_water_mark") or nav or 1.0),
        external_capital_flow=float(data.get("external_capital_flow") or 0.0),
        current_drawdown=float(data.get("current_drawdown") or 0.0),
        risk_state=_enum(RiskState, risk, RiskState.NORMAL),
        spy=spy,
        trading_session_id=data.get("trading_session_id"),
        session_fail_safe=bool(data.get("session_fail_safe")),
    )
