"""Localhost dashboard: read existing state; write only approve/reject."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_portfolio.approval.store import ApprovalStore
from agentic_portfolio.approval.types import ApprovalStatus, packet_from_dict
from agentic_portfolio.dashboard.accounts import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME
from agentic_portfolio.dashboard.app import create_app
from agentic_portfolio.dashboard.safety import (
    DASHBOARD_FORBIDDEN_TOOLS,
    DashboardSafetyError,
    assert_no_forbidden_tools,
    inspect_dashboard_module_for_forbidden_tools,
)
from agentic_portfolio.dashboard.settings import resolve_bind, resolve_ui_flags
from agentic_portfolio.paths import project_root

PENDING_ID = "55123138-6dbe-4554-a92b-de57b21f1873"
PAGES = ["/", "/approvals", "/research", "/orders", "/journal", "/system", "/ai", "/watchlist", "/notifications"]
API = ["/api/dashboard", "/api/approvals", "/api/research", "/api/orders", "/api/journal", "/api/system", "/api/ai", "/api/watchlist", "/api/notifications", "/api/agent"]


def _pending_raw() -> dict:
    path = project_root() / "state" / "approval_packets" / f"{PENDING_ID}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = ApprovalStatus.PENDING_HUMAN_APPROVAL.value
    payload["decided_at"] = None
    payload["decided_by"] = None
    payload["decision_note"] = None
    return payload


def _csrf(client) -> str:
    client.get("/login")
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    assert token
    return str(token)


def _login(client, username=DEFAULT_ADMIN_USERNAME, password=DEFAULT_ADMIN_PASSWORD):
    token = _csrf(client)
    res = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
    )
    assert res.status_code in (302, 303), res.get_data(as_text=True)
    return res


def _client(root=None):
    client = create_app(root).test_client()
    _login(client)
    return client


def _decide_payload(client, **extra):
    body = {"csrf_token": _csrf(client), "confirm": True}
    body.update(extra)
    return body


def _seed_packet(tmp_path: Path, raw: dict | None = None):
    packet = packet_from_dict(raw or _pending_raw())
    ApprovalStore(tmp_path).save(packet)
    return packet


def test_pages_and_api_ok():
    client = _client()
    for path in PAGES:
        res = client.get(path)
        assert res.status_code == 200, path
        body = res.get_data(as_text=True)
        assert "AUTONOMOUS TRADING DISABLED" in body
        assert "PAPER ENVIRONMENT" in body
        assert "LIVE ORDER PLACEMENT: OFF" in body or "NO LIVE ORDER PLACEMENT ENABLED" in body
        assert "PAPER BOOK" in body
        assert "LIVE ACCOUNT (read-only)" in body
        assert "Agentic Portfolio" in body
    for path in API:
        res = client.get(path)
        assert res.status_code == 200, path
        payload = res.get_json()
        assert isinstance(payload, dict)


def test_dashboard_reads_paper_book_not_invented_nav():
    payload = _client().get("/api/dashboard").get_json()
    assert payload["nav"] == 10000.0
    assert payload["cash_pct"] == 0.93
    assert payload["risk_state"] == "NORMAL"
    assert payload["execution"]["autonomous_trading_disabled"] is True
    assert payload["execution"]["auto_execution"] is False
    assert payload["execution"]["live_trade_actions_allowed"] is False
    symbols = {p["symbol"] for p in payload["positions"]}
    assert "NVDA" in symbols
    assert payload["approved_does_not_place_order"] if "approved_does_not_place_order" in payload else payload["execution"]["approved_does_not_place_order"]
    assert payload["environment"] == "PAPER"
    assert payload["live_order_placement_enabled"] is False
    assert [row["name"] for row in payload["health"]][-1] == "live placement disabled"


def test_approvals_api_lists_pending_and_detail(tmp_path):
    packet = _seed_packet(tmp_path)
    client = _client(tmp_path)
    listing = client.get("/api/approvals").get_json()
    pending_ids = {p["approval_id"] for p in listing["pending"]}
    assert packet.approval_id in pending_ids
    detail = client.get(f"/api/approvals/{packet.approval_id}")
    assert detail.status_code == 200
    body = detail.get_json()
    assert body["symbol"] == "ESTC"
    assert body["action"] == "SELL"
    assert body["can_decide"] is True
    assert body["approved_does_not_place_order"] is True
    assert "Why now" in body["explanation"] or body["why_now"]
    html = client.get(f"/approvals/{packet.approval_id}")
    assert html.status_code == 200
    text = html.get_data(as_text=True)
    assert "APPROVE" in text
    assert "REJECT" in text
    assert "does not place" in text.lower() or "does not place an order" in text


def test_approve_and_reject_do_not_place(tmp_path):
    packet = _seed_packet(tmp_path)
    client = _client(tmp_path)
    res = client.post(f"/api/approvals/{packet.approval_id}/approve", json=_decide_payload(client, note="paper only"))
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["placed_order"] is False
    assert body["packet"]["status"] == "APPROVED"
    assert body["packet"]["broker_submitted"] is False
    stored = ApprovalStore(tmp_path).get_packet(packet.approval_id)
    assert stored.status == ApprovalStatus.APPROVED
    assert stored.broker_submitted is False
    assert stored.approved_does_not_place_order is True
    journal = (tmp_path / "logs" / "approval.jsonl").read_text(encoding="utf-8")
    assert "APPROVAL_APPROVED" in journal
    assert "place_equity_order" not in journal

    raw = _pending_raw()
    raw["approval_id"] = "reject-me-00000000-0000-0000-0000-000000000001"
    raw["symbol"] = "IONQ"
    other = _seed_packet(tmp_path, raw)
    res = client.post(f"/api/approvals/{other.approval_id}/reject", json=_decide_payload(client, note="no"))
    assert res.status_code == 200
    assert res.get_json()["packet"]["status"] == "REJECTED"
    assert ApprovalStore(tmp_path).get_packet(other.approval_id).broker_submitted is False


def test_html_approve_still_does_not_place(tmp_path):
    packet = _seed_packet(tmp_path)
    client = _client(tmp_path)
    token = _csrf(client)
    preview = client.post(f"/approvals/{packet.approval_id}/approve", data={"note": "ok", "csrf_token": token})
    assert preview.status_code == 200
    assert "Confirm" in preview.get_data(as_text=True)
    assert ApprovalStore(tmp_path).get_packet(packet.approval_id).status == ApprovalStatus.PENDING_HUMAN_APPROVAL
    res = client.post(
        f"/approvals/{packet.approval_id}/approve",
        data={"note": "ok", "csrf_token": token, "confirm": "1"},
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert "does not place" in res.get_data(as_text=True).lower()
    stored = ApprovalStore(tmp_path).get_packet(packet.approval_id)
    assert stored.status == ApprovalStatus.APPROVED
    assert stored.broker_submitted is False


def test_expired_and_superseded_cannot_be_approved(tmp_path):
    raw = _pending_raw()
    raw["approval_id"] = "expired-00000000-0000-0000-0000-000000000001"
    raw["status"] = "EXPIRED"
    raw["expiry_reasons"] = ["STALE_QUOTE"]
    expired = _seed_packet(tmp_path, raw)
    client = _client(tmp_path)
    html = client.get(f"/approvals/{expired.approval_id}").get_data(as_text=True)
    assert "EXPIRED" in html
    assert "cannot be approved" in html.lower()
    res = client.post(f"/api/approvals/{expired.approval_id}/approve", json=_decide_payload(client))
    assert res.status_code == 409
    assert res.get_json()["placed_order"] is False
    assert ApprovalStore(tmp_path).get_packet(expired.approval_id).status == ApprovalStatus.EXPIRED

    raw2 = _pending_raw()
    raw2["approval_id"] = "superseded-00000000-0000-0000-0000-000000000002"
    raw2["status"] = "SUPERSEDED"
    raw2["expiry_reasons"] = ["SUPERSEDED_BY_NEWER_DECISION"]
    raw2["superseded_by"] = "newer"
    superseded = _seed_packet(tmp_path, raw2)
    page = client.get(f"/approvals/{superseded.approval_id}").get_data(as_text=True)
    assert "SUPERSEDED" in page
    res = client.post(f"/api/approvals/{superseded.approval_id}/reject", json=_decide_payload(client))
    assert res.status_code == 409


def test_forbidden_writes_are_refused():
    client = create_app().test_client()
    policy = project_root() / "config" / "portfolio_policy.json"
    rules = project_root() / "config" / "account_rules.json"
    before_policy = policy.read_text(encoding="utf-8")
    before_rules = rules.read_text(encoding="utf-8")
    for path in (
        "/api/place_equity_order",
        "/api/cancel_equity_order",
        "/api/review_equity_order",
        "/api/deposit",
        "/api/withdrawal",
    ):
        res = client.post(path)
        assert res.status_code == 403, path
        assert res.get_json()["placed_order"] is False
    assert policy.read_text(encoding="utf-8") == before_policy
    assert rules.read_text(encoding="utf-8") == before_rules
    with pytest.raises(DashboardSafetyError):
        assert_no_forbidden_tools(["place_equity_order"])


def test_bind_localhost_default_and_public_refused(monkeypatch):
    bind = resolve_bind(environ={})
    assert bind["host"] == "127.0.0.1"
    assert bind["port"] == 3100
    assert bind["public_exposure"] is False
    assert bind["cloudflare"]["tunnel_enabled"] is False
    monkeypatch.setenv("DASHBOARD_PORT", "4100")
    bind = resolve_bind(environ={"DASHBOARD_PORT": "4100"})
    assert bind["port"] == 4100
    with pytest.raises(DashboardSafetyError):
        resolve_bind(environ={"DASHBOARD_HOST": "0.0.0.0"})
    allowed = resolve_bind(environ={"DASHBOARD_HOST": "0.0.0.0", "DASHBOARD_ALLOW_PUBLIC_BIND": "true"})
    assert allowed["host"] == "0.0.0.0"
    assert allowed["allow_public_bind"] is True


def test_no_execution_tools_reachable():
    hits = inspect_dashboard_module_for_forbidden_tools()
    assert hits == []
    for tool in ("place_equity_order", "cancel_equity_order", "review_equity_order"):
        assert tool in DASHBOARD_FORBIDDEN_TOOLS


def test_research_orders_journal_system_payloads():
    client = _client()
    research = client.get("/api/research").get_json()
    assert research["candidates"]
    assert research["reports"]
    assert research["theses"]
    orders = client.get("/api/orders").get_json()
    assert orders["plans"]
    assert orders["fills"]
    assert orders["reviews"]
    assert all(r.get("order_placed") is False for r in orders["reviews"])
    journal = client.get("/api/journal").get_json()
    assert journal["entries"]
    system = client.get("/api/system").get_json()
    assert system["execution"]["autonomous_trading_disabled"] is True
    assert system["risk_limits_read_only"] is True
    assert "approve_packet" in system["writes_allowed"]
    health_names = [row["name"] for row in system["health"]]
    assert health_names == [
        "agent runtime",
        "dashboard",
        "monitoring",
        "research",
        "decision",
        "risk gate",
        "review bridge",
        "AI gateway",
        "live placement disabled",
    ]
    live_placement = next(row for row in system["health"] if row["id"] == "live_placement")
    assert live_placement["status"] == "disabled"
    assert system["live_order_placement_enabled"] is False
    assert system["environment"] == "PAPER"
    html = client.get("/research").get_data(as_text=True)
    assert "Candidates" in html
    thesis_id = research["theses"][0]["thesis_id"]
    thesis_page = client.get(f"/research/theses/{thesis_id}")
    assert thesis_page.status_code == 200
    assert "Bull" in thesis_page.get_data(as_text=True)
    orders_html = client.get("/orders").get_data(as_text=True)
    assert "OrderPlan" in orders_html
    assert "ReviewResult" in orders_html or "ReviewResults" in orders_html


def test_csrf_and_confirmation_required_before_persist(tmp_path):
    packet = _seed_packet(tmp_path)
    client = _client(tmp_path)
    missing = client.post(f"/api/approvals/{packet.approval_id}/approve", json={"note": "no csrf", "confirm": True})
    assert missing.status_code == 403
    assert missing.get_json()["error"] == "csrf_rejected"
    assert ApprovalStore(tmp_path).get_packet(packet.approval_id).status == ApprovalStatus.PENDING_HUMAN_APPROVAL
    token = _csrf(client)
    unconfirmed = client.post(
        f"/api/approvals/{packet.approval_id}/approve",
        json={"csrf_token": token, "note": "wait"},
    )
    assert unconfirmed.status_code == 409
    assert unconfirmed.get_json()["needs_confirm"] is True
    assert ApprovalStore(tmp_path).get_packet(packet.approval_id).status == ApprovalStatus.PENDING_HUMAN_APPROVAL


def test_paper_and_demo_packets_blocked_unless_enabled(tmp_path, monkeypatch):
    paper = _seed_packet(tmp_path)
    raw = _pending_raw()
    raw["approval_id"] = "demo-00000000-0000-0000-0000-000000000099"
    raw["symbol"] = "DEMO"
    demo = _seed_packet(tmp_path, raw)
    monkeypatch.setenv("DASHBOARD_ALLOW_PAPER_PACKET_DECISIONS", "false")
    monkeypatch.setenv("DASHBOARD_ALLOW_DEMO_PACKET_DECISIONS", "false")
    client = _client(tmp_path)
    paper_json = client.get(f"/api/approvals/{paper.approval_id}").get_json()
    assert paper_json["packet_kind"] == "paper"
    assert paper_json["can_decide"] is False
    demo_json = client.get(f"/api/approvals/{demo.approval_id}").get_json()
    assert demo_json["packet_kind"] == "demo"
    assert demo_json["can_decide"] is False
    html = client.get(f"/approvals/{paper.approval_id}").get_data(as_text=True)
    assert "paper packet decisions are disabled" in html.lower()
    res = client.post(f"/api/approvals/{paper.approval_id}/approve", json=_decide_payload(client))
    assert res.status_code == 409
    assert "paper packet" in res.get_json()["error"]
    assert ApprovalStore(tmp_path).get_packet(paper.approval_id).status == ApprovalStatus.PENDING_HUMAN_APPROVAL
    res = client.post(f"/api/approvals/{demo.approval_id}/reject", json=_decide_payload(client))
    assert res.status_code == 409
    assert "demo packet" in res.get_json()["error"]


def test_live_fail_closed_survives_template_error(monkeypatch):
    monkeypatch.setenv("AGENTIC_RUNTIME_MODE", "LIVE")
    monkeypatch.setenv("DASHBOARD_ENVIRONMENT", "LIVE")
    monkeypatch.setattr(
        "agentic_portfolio.dashboard.app.dashboard_view",
        lambda _state: (_ for _ in ()).throw(RuntimeError("forced")),
    )
    client = _client()
    monkeypatch.setattr(
        "agentic_portfolio.dashboard.app.render_template",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("template boom")),
    )
    res = client.get("/")
    assert res.status_code == 500
    body = res.get_data(as_text=True)
    assert "LIVE DATA UNAVAILABLE" in body
    assert "Internal Server Error" not in body


def test_ui_flags_keep_localhost_and_paper_default():
    flags = resolve_ui_flags(environ={})
    assert flags["environment"] == "PAPER"
    assert flags["live_order_placement_enabled"] is False
    assert flags["allow_paper_packet_decisions"] is True
    assert flags["allow_demo_packet_decisions"] is False
    assert flags["allow_stale_packet_decisions"] is False
    live = resolve_ui_flags(environ={"DASHBOARD_ENVIRONMENT": "LIVE"})
    assert live["environment"] == "LIVE"
    assert live["live_order_placement_enabled"] is False
