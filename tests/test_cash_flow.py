"""External capital flow vs investment return. Fake MCP only. No real broker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_portfolio.cash_flow import (
    BookObservation,
    HoldingLot,
    TradeFill,
    cash_flow_adjusted_total_return,
    observation_from_facts,
    reconcile_external_flow,
)
from agentic_portfolio.context import portfolio_context_from_dict
from agentic_portfolio.dashboard.history import total_return
from agentic_portfolio.dashboard.queries import dashboard_state, dashboard_view
from agentic_portfolio.live.engine import refresh_live_portfolio
from agentic_portfolio.schemas import RiskState
from tests.conftest import ACCOUNT, ctx
from tests.test_live_mode import _accounts, _fetcher, _portfolio, _positions, _quotes

OPEN = datetime(2026, 9, 3, 18, 30, tzinfo=timezone.utc)
AFTER_CLOSE = datetime(2026, 9, 3, 21, 30, tzinfo=timezone.utc)


def _book(nav, cash, holdings=()):
    return observation_from_facts(nav=nav, cash=cash, positions=list(holdings))


def _lot(symbol, qty, price):
    return HoldingLot(symbol=symbol, quantity=qty, market_value=qty * price, current_price=price)


def _refresh(tmp_path: Path, *, nav, cash, positions=None, quotes=None, orders=None, now=OPEN):
    rows = positions or []
    quote_pairs = quotes or [("SPY", 500.0)]
    if rows:
        quote_pairs = list(quote_pairs) + [(str(r["symbol"]), float(r.get("last") or r.get("price") or 100)) for r in rows]
    fetcher = _fetcher(
        accounts=_accounts(),
        portfolio=_portfolio(nav=nav, cash=cash, bp=cash),
        positions=_positions(rows),
        quotes=_quotes(*quote_pairs),
        orders=orders or {"data": {"orders": []}},
    )
    return refresh_live_portfolio(fetcher, now=now, root=tmp_path, persist=True)


def test_01_pure_deposit_cash_only():
    recon = reconcile_external_flow(_book(500, 500), _book(1000, 1000))
    assert recon.external_capital_flow == pytest.approx(500)
    assert recon.investment_pnl == pytest.approx(0)
    assert recon.period_return == pytest.approx(0)
    c = ctx(1000, start_of_day_nav=500, prior_nav=500, prior_hwm=500, external_capital_flow=500, session_external_capital_flow=500)
    assert c.daily_portfolio_return == pytest.approx(0)
    assert c.current_drawdown == pytest.approx(0)
    assert c.risk_state is RiskState.NORMAL


def test_02_pure_withdrawal_cash_only():
    recon = reconcile_external_flow(_book(1000, 1000), _book(600, 600))
    assert recon.external_capital_flow == pytest.approx(-400)
    assert recon.investment_pnl == pytest.approx(0)
    assert recon.period_return == pytest.approx(0)
    c = ctx(600, start_of_day_nav=1000, prior_nav=1000, prior_hwm=1000, external_capital_flow=-400, session_external_capital_flow=-400)
    assert c.daily_portfolio_return == pytest.approx(0)
    assert c.current_drawdown == pytest.approx(0)
    assert c.risk_state is RiskState.NORMAL


def test_03_stock_rise_is_investment_return():
    prior = _book(1000, 500, [_lot("MSFT", 5, 100)])
    current = _book(1050, 500, [_lot("MSFT", 5, 110)])
    recon = reconcile_external_flow(prior, current)
    assert recon.external_capital_flow == pytest.approx(0)
    assert recon.investment_pnl == pytest.approx(50)
    assert recon.period_return == pytest.approx(0.05)


def test_04_stock_fall_is_investment_loss():
    prior = _book(1000, 500, [_lot("MSFT", 5, 100)])
    current = _book(950, 500, [_lot("MSFT", 5, 90)])
    recon = reconcile_external_flow(prior, current)
    assert recon.external_capital_flow == pytest.approx(0)
    assert recon.investment_pnl == pytest.approx(-50)
    assert recon.period_return == pytest.approx(-0.05)


def test_05_deposit_plus_market_gain_only_gain_counts():
    prior = _book(1000, 500, [_lot("MSFT", 5, 100)])
    current = _book(1550, 1000, [_lot("MSFT", 5, 110)])
    recon = reconcile_external_flow(prior, current)
    assert recon.external_capital_flow == pytest.approx(500)
    assert recon.investment_pnl == pytest.approx(50)
    assert recon.period_return == pytest.approx(0.05)


def test_06_withdrawal_plus_market_loss_keeps_loss():
    prior = _book(1000, 500, [_lot("MSFT", 5, 100)])
    current = _book(500, 100, [_lot("MSFT", 5, 80)])
    recon = reconcile_external_flow(prior, current)
    assert recon.external_capital_flow == pytest.approx(-400)
    assert recon.investment_pnl == pytest.approx(-100)
    assert recon.period_return == pytest.approx(-0.10)


def test_07_buy_is_not_external_flow():
    prior = _book(1000, 1000)
    current = _book(1000, 500, [_lot("MSFT", 5, 100)])
    fills = [TradeFill(symbol="MSFT", side="buy", quantity=5, price=100)]
    recon = reconcile_external_flow(prior, current, fills=fills)
    assert recon.external_capital_flow == pytest.approx(0)
    assert recon.investment_pnl == pytest.approx(0)
    no_fills = reconcile_external_flow(prior, current)
    assert no_fills.external_capital_flow == pytest.approx(0)
    assert no_fills.reason == "qty_change_without_fills_no_invented_flow"


def test_08_sell_is_not_external_flow():
    prior = _book(1000, 500, [_lot("MSFT", 5, 100)])
    current = _book(1000, 1000)
    fills = [TradeFill(symbol="MSFT", side="sell", quantity=5, price=100)]
    recon = reconcile_external_flow(prior, current, fills=fills)
    assert recon.external_capital_flow == pytest.approx(0)
    assert recon.investment_pnl == pytest.approx(0)


def test_09_fractional_mark_to_market():
    prior = _book(533.3, 500, [_lot("MSFT", 0.333, 100)])
    current = _book(536.63, 500, [_lot("MSFT", 0.333, 110)])
    recon = reconcile_external_flow(prior, current)
    assert recon.external_capital_flow == pytest.approx(0)
    assert recon.investment_pnl == pytest.approx(3.33, abs=0.02)


def test_10_same_nav_composition_change_no_fake_flow():
    prior = _book(1000, 500, [_lot("AAPL", 5, 100)])
    current = _book(1000, 500, [_lot("MSFT", 5, 100)])
    recon = reconcile_external_flow(prior, current)
    assert recon.external_capital_flow == pytest.approx(0)
    assert recon.reason == "qty_change_without_fills_no_invented_flow"


def test_11_deposit_after_close_still_flow_adjusted(tmp_path):
    first = _refresh(tmp_path, nav=500, cash=500, now=OPEN)
    assert first.context.daily_portfolio_return == pytest.approx(0)
    second = _refresh(tmp_path, nav=1000, cash=1000, now=AFTER_CLOSE)
    assert second.context.external_capital_flow == pytest.approx(500)
    assert second.context.session_external_capital_flow == pytest.approx(500)
    assert second.context.daily_portfolio_return == pytest.approx(0)
    assert second.context.current_drawdown == pytest.approx(0)


def test_12_deposit_between_restarts(tmp_path):
    _refresh(tmp_path, nav=500, cash=500, now=OPEN)
    later = _refresh(tmp_path, nav=1000, cash=1000, now=OPEN + timedelta(minutes=5))
    assert later.context.external_capital_flow == pytest.approx(500)
    assert later.context.daily_portfolio_return == pytest.approx(0)


def test_13_withdrawal_between_restarts(tmp_path):
    _refresh(tmp_path, nav=1000, cash=1000, now=OPEN)
    later = _refresh(tmp_path, nav=600, cash=600, now=OPEN + timedelta(minutes=5))
    assert later.context.external_capital_flow == pytest.approx(-400)
    assert later.context.daily_portfolio_return == pytest.approx(0)
    assert later.context.current_drawdown == pytest.approx(0)
    assert later.context.risk_state is RiskState.NORMAL
    assert later.context.daily_risk_halt is False


def test_14_multiple_deposits_same_session(tmp_path):
    _refresh(tmp_path, nav=500, cash=500, now=OPEN)
    mid = _refresh(tmp_path, nav=800, cash=800, now=OPEN + timedelta(minutes=5))
    last = _refresh(tmp_path, nav=1200, cash=1200, now=OPEN + timedelta(minutes=10))
    assert mid.context.session_external_capital_flow == pytest.approx(300)
    assert last.context.external_capital_flow == pytest.approx(400)
    assert last.context.session_external_capital_flow == pytest.approx(700)
    assert last.context.daily_portfolio_return == pytest.approx(0)


def test_15_hwm_scales_with_deposit_not_as_gain(tmp_path):
    _refresh(tmp_path, nav=500, cash=500, now=OPEN)
    later = _refresh(tmp_path, nav=1000, cash=1000, now=OPEN + timedelta(minutes=5))
    assert later.context.high_water_mark == pytest.approx(1000)
    assert later.context.current_drawdown == pytest.approx(0)
    assert later.context.daily_portfolio_return == pytest.approx(0)


def test_16_withdrawal_does_not_create_drawdown(tmp_path):
    _refresh(tmp_path, nav=1000, cash=1000, now=OPEN)
    later = _refresh(tmp_path, nav=600, cash=600, now=OPEN + timedelta(minutes=5))
    assert later.context.current_drawdown == pytest.approx(0)
    assert later.context.risk_state is RiskState.NORMAL


def test_17_18_dashboard_today_and_total_return(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_RUNTIME_MODE", "LIVE")
    _refresh(tmp_path, nav=500, cash=500, now=OPEN)
    _refresh(tmp_path, nav=1000, cash=1000, now=OPEN + timedelta(minutes=5))
    view = dashboard_view(dashboard_state(tmp_path))
    assert view["kpis"]["today_pnl"]["value"] == pytest.approx(0)
    assert view["kpis"]["today_pnl"]["display"].startswith("$0.00")
    assert view["kpis"]["total_return"]["value"] == pytest.approx(0)
    assert "100" not in (view["kpis"]["total_return"]["display"] or "")


def test_19_risk_state_not_from_withdrawal(tmp_path):
    _refresh(tmp_path, nav=1000, cash=1000, now=OPEN)
    later = _refresh(tmp_path, nav=600, cash=600, now=OPEN + timedelta(minutes=5))
    assert later.context.risk_state is RiskState.NORMAL
    assert later.context.daily_risk_halt is False


def test_20_snapshot_backwards_compatible():
    raw = {
        "timestamp": OPEN.isoformat(),
        "account_number": ACCOUNT,
        "current_nav": 500.0,
        "cash": 500.0,
        "buying_power": 500.0,
        "cash_allocation_pct": 1.0,
        "positions": [],
        "holdings_count": 0,
        "sleeve_market_values": {},
        "sleeve_allocation_pct": {},
        "sector_exposure": {},
        "sector_allocation_pct": {},
        "open_orders": [],
        "start_of_day_nav": 500.0,
        "daily_portfolio_return": 0.0,
        "daily_risk_halt": False,
        "high_water_mark": 500.0,
        "cash_flow_adjusted_hwm": 500.0,
        "external_capital_flow": 0.0,
        "current_drawdown": 0.0,
        "risk_state": "NORMAL",
    }
    parsed = portfolio_context_from_dict(raw)
    assert parsed.session_external_capital_flow == pytest.approx(0.0)
    assert parsed.current_nav == pytest.approx(500)


def test_old_cash_only_history_is_corrected_at_read_time():
    points = [
        {"at": "2026-09-03T14:00:00+00:00", "nav": 500.0, "cash": 500.0, "positions": []},
        {"at": "2026-09-03T15:00:00+00:00", "nav": 1000.0, "cash": 1000.0, "positions": []},
    ]
    assert cash_flow_adjusted_total_return(points) == pytest.approx(0)
    assert total_return(points) == pytest.approx(0)


def test_ambiguous_cash_only_identity_does_not_invent_flow():
    recon = reconcile_external_flow(_book(500, 400), _book(1000, 700))
    assert recon.external_capital_flow == pytest.approx(0)
    assert recon.confident is False
    assert recon.reason == "ambiguous_cash_only_identity"
