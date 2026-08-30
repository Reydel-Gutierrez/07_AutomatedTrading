from agentic_portfolio.hwm import apply_observation, risk_state_from_drawdown
from agentic_portfolio.schemas import RiskState


def test_deposit_does_not_create_fake_return_or_erase_need_for_hwm_scale():
    # Start 100, HWM 100. Deposit 50 → NAV 150. Drawdown must stay 0.
    u = apply_observation(prior_nav=100, prior_hwm=100, nav_after=150, external_capital_flow=50)
    assert abs(u.drawdown) < 1e-9
    assert abs(u.performance_since_prior or 0) < 1e-9
    assert abs(u.cash_flow_adjusted_hwm - 150) < 1e-9


def test_withdrawal_does_not_create_fake_drawdown():
    u = apply_observation(prior_nav=100, prior_hwm=100, nav_after=80, external_capital_flow=-20)
    assert abs(u.drawdown) < 1e-9
    assert abs(u.performance_since_prior or 0) < 1e-9


def test_market_loss_then_deposit_preserves_drawdown():
    # 100 → 90 market, then +50 deposit → NAV 140. DD should remain -10%.
    mid = apply_observation(prior_nav=100, prior_hwm=100, nav_after=90, external_capital_flow=0)
    assert abs(mid.drawdown - (-0.10)) < 1e-9
    after = apply_observation(
        prior_nav=90,
        prior_hwm=mid.cash_flow_adjusted_hwm,
        nav_after=140,
        external_capital_flow=50,
    )
    assert abs(after.drawdown - (-0.10)) < 1e-6


def test_risk_states():
    assert risk_state_from_drawdown(-0.04) == RiskState.NORMAL
    assert risk_state_from_drawdown(-0.05) == RiskState.WARNING
    assert risk_state_from_drawdown(-0.10) == RiskState.RISK_REDUCTION
    assert risk_state_from_drawdown(-0.15) == RiskState.DEFENSIVE
    assert risk_state_from_drawdown(-0.20) == RiskState.HALTED
