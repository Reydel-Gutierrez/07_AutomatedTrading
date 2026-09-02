"""Dashboard AI observability: LIVE vs PAPER isolation."""

from __future__ import annotations

from agentic_portfolio.ai.store import AIArtifactStore
from agentic_portfolio.dashboard.app import create_app
from agentic_portfolio.dashboard.queries import ai_activity_view, ai_view, dashboard_state
from agentic_portfolio.runtime import RuntimeMode
from tests.test_ai_gateway import NOW
from tests.test_dashboard import _client
from tests.test_family import _admin


def test_ai_pages_and_api():
    client = _client()
    for path in ("/ai", "/ai/activity", "/api/ai", "/api/ai/activity"):
        res = client.get(path)
        assert res.status_code == 200, path
    html = client.get("/ai").get_data(as_text=True)
    assert "AUTONOMOUS TRADING DISABLED" in html
    assert "$10" in html or "10.00" in html
    assert "LIVE_ORDER_PLACEMENT" in html
    payload = client.get("/api/ai").get_json()
    assert payload["LIVE_ORDER_PLACEMENT"] is False
    assert payload["budget_mode"] in {"NORMAL", "CONSERVING", "CRITICAL", "EXHAUSTED"}


def test_dashboard_includes_ai_summary():
    client = _client()
    html = client.get("/").get_data(as_text=True)
    assert "AI activity" in html
    payload = client.get("/api/dashboard").get_json()
    assert payload["ai"]["LIVE_ORDER_PLACEMENT"] is False


def test_live_activity_excludes_paper_artifacts(tmp_path, monkeypatch):
    paper = AIArtifactStore(tmp_path, runtime_mode=RuntimeMode.PAPER)
    live = AIArtifactStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    paper.save_screening("p1", {"ticker": "NVDA", "score": 99, "created_at": NOW.isoformat(), "confidence": "HIGH"})
    live.save_screening("l1", {"ticker": "QUAL", "score": 70, "created_at": NOW.isoformat(), "confidence": "MEDIUM"})
    monkeypatch.setenv("AGENTIC_RUNTIME_MODE", "LIVE")
    monkeypatch.setenv("DASHBOARD_ENVIRONMENT", "LIVE")
    state = dashboard_state(tmp_path)
    view = ai_activity_view(state)
    tickers = {row["ticker"] for row in view["rows"]}
    assert "QUAL" in tickers
    assert "NVDA" not in tickers
    budget = ai_view(state)
    assert budget["runtime_mode"] == "LIVE"
    app = create_app(tmp_path)
    client = app.test_client()
    _admin(client)
    html = client.get("/ai/activity").get_data(as_text=True)
    assert "QUAL" in html
    assert "NVDA" not in html
