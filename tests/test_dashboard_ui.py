"""Dashboard presentation: NAV history, discovery page, and identity chrome."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_portfolio.dashboard.app import create_app
from agentic_portfolio.dashboard.history import (
    chart_ready,
    load_nav_history,
    record_nav_snapshot,
    spy_return,
    total_return,
)
from agentic_portfolio.dashboard.labels import HISTORY_COLLECTING, UNAVAILABLE
from agentic_portfolio.dashboard.queries import dashboard_state, dashboard_view

from tests.test_dashboard import _client
from tests.test_family import _admin, _app, _csrf, _login


def _write_book(root: Path, nav: float, *, stamp: str, spy=None) -> None:
    book_dir = root / "state" / "paper_book"
    (book_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": stamp,
        "context": {"timestamp": stamp, "current_nav": nav, "cash": nav * 0.5, "cash_allocation_pct": 0.5, "spy": spy, "sleeve_allocation_pct": {"CORE_GROWTH": 0.3, "OPPORTUNISTIC": 0.2, "TACTICAL": 0.0, "SPECULATIVE": 0.0}, "positions": [], "daily_portfolio_return": 0.0, "start_of_day_nav": nav, "current_drawdown": 0.0, "risk_state": "NORMAL"},
    }
    (book_dir / "current.json").write_text(json.dumps(payload), encoding="utf-8")
    (book_dir / "snapshots" / f"{stamp[:10]}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_nav_history_records_observed_values_and_does_not_invent_spy(tmp_path):
    _write_book(tmp_path, 10000.0, stamp="2026-08-01T12:00:00+00:00")
    first = record_nav_snapshot(tmp_path, nav=10000.0, spy=None, at="2026-08-01T12:00:00+00:00")
    assert first
    assert all(point.get("nav") == 10000.0 for point in first)
    assert all(point.get("spy") is None for point in first)
    later = record_nav_snapshot(tmp_path, nav=11000.0, spy=None, at="2026-08-30T12:00:00+00:00")
    assert chart_ready(later)
    assert total_return(later) == pytest.approx(0.1)
    assert spy_return(later) is None
    stored = json.loads((tmp_path / "state" / "dashboard_nav_history.json").read_text(encoding="utf-8"))
    assert "points" in stored
    assert spy_return(load_nav_history(tmp_path)) is None


def test_performance_collecting_without_enough_history(tmp_path):
    state = dashboard_state(tmp_path)
    view = dashboard_view(state)
    assert view["performance"]["ready"] is False
    assert view["performance"]["message"] == HISTORY_COLLECTING
    assert view["kpis"]["spy_return"]["available"] is False
    assert view["kpis"]["spy_return"]["display"] == UNAVAILABLE
    assert view["kpis"]["excess_return"]["display"] == UNAVAILABLE
    html = create_app(tmp_path).test_client()
    client = _admin(html)
    page = client.get("/").get_data(as_text=True)
    assert HISTORY_COLLECTING in page
    assert UNAVAILABLE in page


def test_admin_dashboard_identity_kpis_and_discovery_nav():
    client = _client()
    html = client.get("/").get_data(as_text=True)
    assert "Reydel (ADMIN)" in html
    assert "Sign Out" in html
    assert ">PAPER<" in html or "PAPER</span>" in html
    assert "Portfolio Value" in html
    assert "Today's P/L" in html
    assert "Watchlist" in html
    assert "Eastern Time (ET)" in html
    assert "View watchlist" in html
    assert 'id="allocation-chart"' in html
    assert 'href="/discovery"' in html
    payload = client.get("/api/dashboard").get_json()
    assert payload["nav"] == 10000.0
    assert "kpis" in payload
    assert "allocation_chart" in payload
    assert payload["allocation_chart"]["keys"] == [
        "CORE_GROWTH",
        "OPPORTUNISTIC",
        "TACTICAL",
        "SPECULATIVE",
        "CASH",
    ]
    assert payload["live_order_placement_enabled"] is False


def test_admin_dashboard_hides_live_refresh_in_paper():
    html = _client().get("/").get_data(as_text=True)
    assert "Portfolio Value" in html
    assert "Refresh Live State" not in html
    assert 'action="/live/refresh"' not in html


def test_discovery_page_reads_store_and_allows_users(tmp_path):
    client = _admin(_app(tmp_path, nav=10000.0))
    empty = client.get("/discovery")
    assert empty.status_code == 200
    assert "does not run discovery" in empty.get_data(as_text=True).lower() or "does not run discovery" in client.get("/api/discovery").get_json()["note"].lower()
    live = _client()
    listing = live.get("/api/discovery").get_json()
    assert listing["live_order_placement_enabled"] is False
    assert listing["candidates"]
    nvda = next(row for row in listing["candidates"] if row["symbol"] == "NVDA")
    assert "discovery_score" in nvda
    assert nvda["sleeve_label"] == "Core Growth"
    page = live.get("/discovery")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert "NVDA" in text
    assert "Discovery" in text
    assert "data-page-size" in text
    assert "Eastern Time (ET)" in text
    user_app = _app(tmp_path, nav=10000.0)
    admin = _admin(user_app)
    token = _csrf(admin)
    admin.post(
        "/family/users",
        json={"csrf_token": token, "name": "Dad", "username": "dad", "password": "dadpass"},
    )
    admin.get("/logout")
    user_client = user_app
    assert _login(user_client, "dad", "dadpass").status_code in (302, 303)
    listing_page = user_client.get("/discovery")
    assert listing_page.status_code == 200
    assert user_client.get("/api/discovery").status_code == 200
    home = user_client.get("/").get_data(as_text=True)
    assert "Dad (USER)" in home
    assert "Sign Out" in home
    assert 'href="/discovery"' in home
    assert "Candidate discovery" in home or "Discovery" in home
