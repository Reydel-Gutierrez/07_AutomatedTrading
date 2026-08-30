"""Cash-flow-adjusted high-water mark.

External deposits/withdrawals are not investment performance.

Observation update (flow assumed at the end of the interval):

    nav_pre_flow = NAV_after - external_capital_flow
    hwm_after_market = max(prior_HWM, nav_pre_flow)
    hwm_after_flow = hwm_after_market * (NAV_after / nav_pre_flow)   # if nav_pre_flow > 0
    drawdown = (NAV_after / hwm_after_flow) - 1
    performance_since_prior = (nav_pre_flow / prior_NAV) - 1

A deposit scales HWM up with capital; a withdrawal scales it down.
Drawdown from market losses is preserved across a subsequent deposit.

Manual HWM reset after HALTED is human-only. This module has no reset helper
that an agent is allowed to call to wipe history.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentic_portfolio.schemas import RiskState


@dataclass
class HwmUpdate:
    nav: float
    nav_pre_flow: float
    cash_flow_adjusted_hwm: float
    drawdown: float
    performance_since_prior: float | None
    external_capital_flow: float
    risk_state: RiskState


def risk_state_from_drawdown(drawdown: float) -> RiskState:
    """drawdown is (NAV/HWM)-1, typically <= 0.

    Tiny epsilon so 10%/15%/20% constructed as NAV/(NAV/0.90) counts as 'at' the threshold.
    """
    eps = 1e-9
    if drawdown <= -0.20 + eps:
        return RiskState.HALTED
    if drawdown <= -0.15 + eps:
        return RiskState.DEFENSIVE
    if drawdown <= -0.10 + eps:
        return RiskState.RISK_REDUCTION
    if drawdown <= -0.05 + eps:
        return RiskState.WARNING
    return RiskState.NORMAL


def apply_observation(
    *,
    prior_nav: float | None,
    prior_hwm: float | None,
    nav_after: float,
    external_capital_flow: float = 0.0,
) -> HwmUpdate:
    if nav_after <= 0:
        raise ValueError("NAV must be positive")
    flow = float(external_capital_flow)
    nav_pre = nav_after - flow
    if prior_hwm is None:
        prior_hwm = nav_pre if nav_pre > 0 else nav_after
    if prior_nav is None:
        prior_nav = nav_pre if nav_pre > 0 else nav_after

    if nav_pre <= 0:
        hwm = nav_after
        perf = None
    else:
        hwm_market = max(prior_hwm, nav_pre)
        hwm = hwm_market * (nav_after / nav_pre)
        perf = (nav_pre / prior_nav) - 1.0 if prior_nav else None

    dd = (nav_after / hwm) - 1.0 if hwm else 0.0
    return HwmUpdate(
        nav=nav_after,
        nav_pre_flow=nav_pre,
        cash_flow_adjusted_hwm=hwm,
        drawdown=dd,
        performance_since_prior=perf,
        external_capital_flow=flow,
        risk_state=risk_state_from_drawdown(dd),
    )
