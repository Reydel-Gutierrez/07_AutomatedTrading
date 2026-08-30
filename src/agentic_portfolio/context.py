from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from agentic_portfolio.hwm import apply_observation
from agentic_portfolio.policy import load_account_rules, load_policy
from agentic_portfolio.schemas import (
    OpenOrder,
    PortfolioContext,
    Position,
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
