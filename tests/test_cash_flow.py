"""External capital flow vs investment return. Fake MCP only. No real broker."""

from __future__ import annotations

import json
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
    reconstruct_session_external_flow,
    select_session_external_flow,
)
from agentic_portfolio.context import portfolio_context_from_dict
from agentic_portfolio.dashboard.history import total_return
from agentic_portfolio.dashboard.queries import dashboard_state, dashboard_view
from agentic_portfolio.live.engine import refresh_live_portfolio
from agentic_portfolio.live.store import LivePortfolioStore
from agentic_portfolio.schemas import RiskState
from agentic_portfolio.session import SessionNavState, save_session_state
from tests.conftest import ACCOUNT, ctx
from tests.test_live_mode import _accounts, _fetcher, _portfolio, _positions, _quotes

OPEN = datetime(2026, 9, 3, 18, 30, tzinfo=timezone.utc)
AFTER_CLOSE = datetime(2026, 9, 3, 21, 30, tzinfo=timezone.utc)
LEGACY_0930 = datetime(2026, 9, 3, 13, 30, tzinfo=timezone.utc)  # 09:30 ET
LEGACY_1700 = datetime(2026, 9, 3, 21, 0, tzinfo=timezone.utc)  # 17:00 ET
LEGACY_1715 = datetime(2026, 9, 3, 21, 15, tzinfo=timezone.utc)  # 17:15 ET
SESSION_ID = "2026-09-03"


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


def _legacy_live_book(
    tmp_path: Path,
    points: list[tuple[datetime, float, float]],
    *,
    session_id: str = SESSION_ID,
    sod: float = 500.0,
    flow: float = 0.0,
    extra_snapshots: list[tuple[str, datetime, float, float]] | None = None,
):
    """Seed old-code LIVE snapshots (flow=0) plus surviving session state."""
    store = LivePortfolioStore(tmp_path)
    for session, at, nav, cash in extra_snapshots or []:
        _write_legacy_snapshot(store, f"legacy-{session}-{at.strftime('%H%M')}", at, nav, cash, session_id=session, sod=nav, flow=0.0)
    for idx, (at, nav, cash) in enumerate(points):
        _write_legacy_snapshot(
            store,
            f"legacy-{idx}-{at.strftime('%H%M')}",
            at,
            nav,
            cash,
            session_id=session_id,
            sod=sod,
            flow=flow,
        )
    last_at, last_nav, last_cash = points[-1]
    first_at = points[0][0]
    save_session_state(
        SessionNavState(
            session_id=session_id,
            session_date=session_id,
            timezone="America/New_York",
            sod_nav=sod,
            sod_anchored_at=first_at.isoformat(),
            calendar_provider="nyse_builtin_v1",
            calendar_available=True,
            fail_safe=False,
            fail_safe_reason=None,
            last_observed_nav=last_nav,
            last_observed_at=last_at.isoformat(),
            last_observed_cash=last_cash,
            session_external_capital_flow=flow,
        ),
        store.session_path(),
    )
    store.hwm_path().write_text(
        json.dumps(
            {
                "account_number": ACCOUNT,
                "nav": last_nav,
                "cash_flow_adjusted_hwm": last_nav,
                "drawdown": 0.0,
                "risk_state": "NORMAL",
            }
        ),
        encoding="utf-8",
    )
    return store


def _write_legacy_snapshot(store: LivePortfolioStore, snap_id: str, at: datetime, nav: float, cash: float, *, session_id: str, sod: float, flow: float) -> None:
    stamp = at.isoformat()
    store.save_snapshot(
        snap_id,
        {
            "snapshot_id": snap_id,
            "created_at": stamp,
            "runtime_mode": "LIVE",
            "source_of_truth": "robinhood_agentic_account",
            "paper_environment": False,
            "live_order_placement_enabled": False,
            "account": {"account_number": ACCOUNT},
            "mcp_tools_used": ["get_accounts"],
            "mcp_not_called": ["place_equity_order", "cancel_equity_order", "review_equity_order"],
            "context": {
                "timestamp": stamp,
                "account_number": ACCOUNT,
                "current_nav": nav,
                "cash": cash,
                "buying_power": cash,
                "positions": [],
                "holdings_count": 0,
                "start_of_day_nav": sod,
                "daily_portfolio_return": ((nav - sod - flow) / sod) if sod else 0.0,
                "external_capital_flow": 0.0,
                "session_external_capital_flow": flow,
                "trading_session_id": session_id,
                "high_water_mark": nav,
                "cash_flow_adjusted_hwm": nav,
                "current_drawdown": 0.0,
                "risk_state": "NORMAL",
            },
            "session": {
                "session_id": session_id,
                "session_date": session_id,
                "sod_nav": sod,
                "session_external_capital_flow": flow,
                "last_observed_nav": nav,
                "last_observed_cash": cash,
            },
            "market": {"session_id": session_id, "session_date": session_id},
        },
    )


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


def test_reconstruct_session_flow_from_same_session_snapshots():
    snaps = [
        {
            "created_at": LEGACY_0930.isoformat(),
            "context": {
                "timestamp": LEGACY_0930.isoformat(),
                "current_nav": 500.0,
                "cash": 500.0,
                "positions": [],
                "trading_session_id": SESSION_ID,
            },
        },
        {
            "created_at": LEGACY_1700.isoformat(),
            "context": {
                "timestamp": LEGACY_1700.isoformat(),
                "current_nav": 1000.0,
                "cash": 1000.0,
                "positions": [],
                "trading_session_id": SESSION_ID,
            },
        },
        {
            "created_at": "2026-09-02T21:00:00+00:00",
            "context": {
                "timestamp": "2026-09-02T21:00:00+00:00",
                "current_nav": 400.0,
                "cash": 400.0,
                "positions": [],
                "trading_session_id": "2026-09-02",
            },
        },
    ]
    rec = reconstruct_session_external_flow(snaps, session_id=SESSION_ID, current=_book(1000, 1000))
    assert rec.confident is True
    assert rec.session_external_capital_flow == pytest.approx(500)
    assert rec.starting_nav == pytest.approx(500)
    other = reconstruct_session_external_flow(snaps, session_id="2026-09-02")
    assert other.confident is False
    missing = reconstruct_session_external_flow(snaps, session_id=None, current=_book(1000, 1000))
    assert missing.confident is False
    accounted = select_session_external_flow(accounted=500, reconstructed=rec, sod_nav=500)
    assert accounted == pytest.approx(500)
    recovered = select_session_external_flow(accounted=0, reconstructed=rec, sod_nav=500)
    assert recovered == pytest.approx(500)


def test_legacy_deploy_recovers_same_session_deposit(tmp_path):
    store = _legacy_live_book(
        tmp_path,
        [(LEGACY_0930, 500.0, 500.0), (LEGACY_1700, 1000.0, 1000.0)],
        sod=500.0,
        flow=0.0,
    )
    snap_dir = store.root / "snapshots"
    before = {path.name: path.read_text(encoding="utf-8") for path in snap_dir.glob("*.json")}
    later = _refresh(tmp_path, nav=1000, cash=1000, now=LEGACY_1715)
    assert later.context.start_of_day_nav == pytest.approx(500)
    assert later.context.session_external_capital_flow == pytest.approx(500)
    assert later.context.external_capital_flow == pytest.approx(500)
    assert later.context.daily_portfolio_return == pytest.approx(0)
    assert later.context.current_nav == pytest.approx(1000)
    assert later.context.current_drawdown == pytest.approx(0)
    after = {path.name: path.read_text(encoding="utf-8") for path in snap_dir.glob("*.json") if path.name in before}
    assert after == before


def test_restart_after_correct_flow_does_not_double_count(tmp_path):
    _refresh(tmp_path, nav=500, cash=500, now=OPEN)
    accounted = _refresh(tmp_path, nav=1000, cash=1000, now=OPEN + timedelta(minutes=5))
    assert accounted.context.session_external_capital_flow == pytest.approx(500)
    restarted = _refresh(tmp_path, nav=1000, cash=1000, now=OPEN + timedelta(minutes=15))
    assert restarted.context.session_external_capital_flow == pytest.approx(500)
    assert restarted.context.daily_portfolio_return == pytest.approx(0)
    assert restarted.context.external_capital_flow == pytest.approx(0)
    assert restarted.context.current_drawdown == pytest.approx(0)


def test_legacy_same_session_withdrawal_recovered_after_restart(tmp_path):
    _legacy_live_book(
        tmp_path,
        [(LEGACY_0930, 1000.0, 1000.0), (LEGACY_1700, 600.0, 600.0)],
        sod=1000.0,
        flow=0.0,
    )
    later = _refresh(tmp_path, nav=600, cash=600, now=LEGACY_1715)
    assert later.context.start_of_day_nav == pytest.approx(1000)
    assert later.context.session_external_capital_flow == pytest.approx(-400)
    assert later.context.daily_portfolio_return == pytest.approx(0)
    assert later.context.current_drawdown == pytest.approx(0)
    assert later.context.risk_state is RiskState.NORMAL


def test_legacy_multiple_session_flows_recovered(tmp_path):
    _legacy_live_book(
        tmp_path,
        [
            (LEGACY_0930, 500.0, 500.0),
            (LEGACY_0930 + timedelta(hours=2), 800.0, 800.0),
            (LEGACY_1700, 1200.0, 1200.0),
        ],
        sod=500.0,
        flow=0.0,
    )
    later = _refresh(tmp_path, nav=1200, cash=1200, now=LEGACY_1715)
    assert later.context.session_external_capital_flow == pytest.approx(700)
    assert later.context.daily_portfolio_return == pytest.approx(0)


def test_recovery_does_not_cross_session_boundary(tmp_path):
    _legacy_live_book(
        tmp_path,
        [(LEGACY_0930, 500.0, 500.0), (LEGACY_1700, 1000.0, 1000.0)],
        sod=500.0,
        flow=0.0,
        extra_snapshots=[("2026-09-02", datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc), 400.0, 400.0)],
    )
    later = _refresh(tmp_path, nav=1000, cash=1000, now=LEGACY_1715)
    assert later.context.session_external_capital_flow == pytest.approx(500)
    assert later.context.daily_portfolio_return == pytest.approx(0)


def test_recovery_fails_closed_on_ambiguous_snapshot_identity(tmp_path):
    _legacy_live_book(
        tmp_path,
        [(LEGACY_0930, 500.0, 400.0), (LEGACY_1700, 1000.0, 700.0)],
        sod=500.0,
        flow=0.0,
    )
    later = _refresh(tmp_path, nav=1000, cash=700, now=LEGACY_1715)
    assert later.context.session_external_capital_flow == pytest.approx(0)


def test_legacy_deposit_dashboard_today_return_is_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_RUNTIME_MODE", "LIVE")
    _legacy_live_book(
        tmp_path,
        [(LEGACY_0930, 500.0, 500.0), (LEGACY_1700, 1000.0, 1000.0)],
        sod=500.0,
        flow=0.0,
    )
    _refresh(tmp_path, nav=1000, cash=1000, now=LEGACY_1715)
    view = dashboard_view(dashboard_state(tmp_path))
    assert view["kpis"]["today_pnl"]["value"] == pytest.approx(0)
    assert view["kpis"]["total_return"]["value"] == pytest.approx(0)
