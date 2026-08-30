"""Family accounts: hashed login, ADMIN/FAMILY authorization, NAV-share math."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_portfolio.dashboard.accounts import (
    DEFAULT_ADMIN_NAME,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
    ROLE_ADMIN,
    ROLE_USER,
    AccountStore,
)
from agentic_portfolio.dashboard.app import create_app
from agentic_portfolio.dashboard.family import scaled_share

ADMIN_PATHS = [
    "/",
    "/approvals",
    "/research",
    "/orders",
    "/journal",
    "/system",
    "/family",
    "/users",
    "/api/dashboard",
    "/api/approvals",
    "/api/research",
    "/api/orders",
    "/api/journal",
    "/api/system",
    "/api/family",
    "/api/users",
]
FAMILY_FORBIDDEN = [path for path in ADMIN_PATHS if path not in {"/"}]


def _write_book(root: Path, nav: float, *, stamp: str = "2026-08-30T18:45:00+00:00", spy=None) -> None:
    book_dir = root / "state" / "paper_book"
    book_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": stamp,
        "paper_environment": True,
        "live_book_untouched": True,
        "context": {
            "timestamp": stamp,
            "current_nav": nav,
            "cash": nav * 0.93,
            "cash_allocation_pct": 0.93,
            "positions": [
                {
                    "symbol": "NVDA",
                    "market_value": nav * 0.05,
                    "sleeve": "CORE_GROWTH",
                }
            ],
            "sleeve_allocation_pct": {"CORE_GROWTH": 0.05, "OPPORTUNISTIC": 0.02},
            "spy": spy,
        },
    }
    (book_dir / "current.json").write_text(json.dumps(payload), encoding="utf-8")
    snap_dir = book_dir / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "seed.json").write_text(json.dumps(payload), encoding="utf-8")


def _csrf(client) -> str:
    client.get("/login")
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    assert token
    return str(token)


def _login(client, username: str, password: str):
    token = _csrf(client)
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
    )


def _admin(client):
    res = _login(client, DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
    assert res.status_code in (302, 303), res.get_data(as_text=True)
    return client


def _app(tmp_path, nav: float | None = 10000.0):
    if nav is not None:
        _write_book(tmp_path, nav)
    client = create_app(tmp_path).test_client()
    return client


def test_scaled_share_tracks_nav_from_assignment_baseline():
    first = scaled_share(2000.0, 10000.0, 11000.0)
    assert first["current_value"] == pytest.approx(2200.0)
    assert first["gain_loss"] == pytest.approx(200.0)
    assert first["return_pct"] == pytest.approx(0.10)
    reset = scaled_share(3000.0, 11000.0, 12100.0)
    assert reset["current_value"] == pytest.approx(3300.0)
    assert reset["gain_loss"] == pytest.approx(300.0)
    assert reset["return_pct"] == pytest.approx(0.10)


def test_default_reydel_is_admin_with_hashed_password(tmp_path):
    store = AccountStore(tmp_path)
    user = store.find_login(DEFAULT_ADMIN_USERNAME)
    assert user is not None
    assert user["name"] == DEFAULT_ADMIN_NAME
    assert user["role"] == ROLE_ADMIN
    assert user["enabled"] is True
    assert user["password_hash"]
    assert DEFAULT_ADMIN_PASSWORD not in user["password_hash"]
    raw = json.loads((tmp_path / "state" / "accounts.json").read_text(encoding="utf-8"))
    stored = raw["users"][0]
    assert "password" not in stored
    assert stored["password_hash"] != DEFAULT_ADMIN_PASSWORD
    assert "pbkdf2" in user["password_hash"]
    again = AccountStore(tmp_path).find_login(DEFAULT_ADMIN_USERNAME)
    assert again["password_hash"] == user["password_hash"]


def test_unauthenticated_html_redirects_and_api_is_401(tmp_path):
    client = _app(tmp_path)
    html = client.get("/")
    assert html.status_code == 302
    assert "/login" in html.headers.get("Location", "")
    login = client.get("/login")
    assert login.status_code == 200
    assert "PAPER ENVIRONMENT" in login.get_data(as_text=True)
    assert "NO LIVE ORDER PLACEMENT ENABLED" in login.get_data(as_text=True)
    api = client.get("/api/dashboard")
    assert api.status_code == 401
    assert api.get_json()["error"] == "authentication_required"


def test_admin_family_page_create_assign_disable_enable(tmp_path):
    client = _admin(_app(tmp_path, nav=10000.0))
    token = _csrf(client)
    created = client.post(
        "/family/users",
        json={
            "csrf_token": token,
            "name": "Dad",
            "username": "dad",
            "password": "dadpass",
        },
    )
    assert created.status_code == 200
    dad = created.get_json()["user"]
    assert dad["role"] == ROLE_USER
    assert dad["enabled"] is True
    assert "password_hash" not in dad
    page = client.get("/family")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert "Users" in text
    assert "Create user" in text
    assert "Starting amount" in text
    assert "PAPER BOOK" in text
    listing = client.get("/api/family").get_json()
    assert listing["members"][0]["name"] == "Dad"
    assigned = client.post(
        f"/family/users/{dad['id']}/assign",
        json={"csrf_token": token, "amount": 2000},
    )
    assert assigned.status_code == 200
    body = assigned.get_json()["user"]
    assert body["starting"] == 2000.0
    assert body["current_value"] == 2000.0
    assert body["gain_loss"] == 0.0
    assert "baseline_nav" not in body
    _write_book(tmp_path, 11000.0)
    later = client.get("/api/family").get_json()["members"][0]
    assert later["current_value"] == 2200.0
    assert later["gain_loss"] == 200.0
    assert later["return_pct"] == pytest.approx(0.10)
    disabled = client.post(f"/family/users/{dad['id']}/disable", json={"csrf_token": token})
    assert disabled.status_code == 200
    assert disabled.get_json()["user"]["enabled"] is False
    enabled = client.post(f"/family/users/{dad['id']}/enable", json={"csrf_token": token})
    assert enabled.get_json()["user"]["enabled"] is True
    changed = client.post(
        f"/family/users/{dad['id']}/assign",
        json={"csrf_token": token, "amount": 3000},
    )
    assert changed.get_json()["user"]["starting"] == 3000.0
    assert changed.get_json()["user"]["current_value"] == 3000.0


def test_family_login_personal_dashboard_hides_admin_surfaces(tmp_path):
    client = _admin(_app(tmp_path, nav=10000.0))
    token = _csrf(client)
    dad = client.post(
        "/family/users",
        json={"csrf_token": token, "name": "Dad", "username": "dad", "password": "dadpass"},
    ).get_json()["user"]
    client.post(
        "/family/users",
        json={"csrf_token": token, "name": "Mom", "username": "mom", "password": "mompass"},
    )
    client.post(f"/family/users/{dad['id']}/assign", json={"csrf_token": token, "amount": 2000})
    _write_book(tmp_path, 11000.0, spy={"price": 500.0, "return_pct": 0.04})
    client.get("/logout")
    res = _login(client, "dad", "dadpass")
    assert res.status_code in (302, 303)
    home = client.get("/")
    assert home.status_code == 200
    html = home.get_data(as_text=True)
    assert "Dad" in html
    assert "$2,200.00" in html
    assert "$2,000.00" in html
    assert "+$200.00" in html
    assert "+10.00%" in html
    assert "CORE_GROWTH" in html
    assert "NVDA" in html
    assert "PAPER ENVIRONMENT" in html
    assert "NO LIVE ORDER PLACEMENT ENABLED" in html
    assert "PAPER BOOK" in html
    assert "Mom" not in html
    assert "Pending approvals" not in html
    assert "Live account snapshot" not in html
    assert "Recent AI decisions" not in html
    assert "$10,000.00" not in html
    assert "$11,000.00" not in html
    assert "Candidate discovery" in html
    assert 'href="/discovery"' in html
    for path in ("/approvals", "/research", "/orders", "/journal", "/system", "/family", "/users"):
        assert f'href="{path}"' not in html
    me = client.get("/api/family/me").get_json()
    assert me["name"] == "Dad"
    assert me["current_value"] == 2200.0
    assert "nav" not in me
    assert "members" not in me
    assert me["allocation"]["positions"][0]["symbol"] == "NVDA"
    assert "market_value" not in me["allocation"]["positions"][0]
    assert me["spy"]["observed"] is True
    assert me["chart"] is not None
    for path in FAMILY_FORBIDDEN:
        res = client.get(path)
        assert res.status_code == 403, path
        if path.startswith("/api/"):
            assert res.get_json()["error"] == "forbidden"


def test_disabled_family_user_cannot_login(tmp_path):
    client = _admin(_app(tmp_path))
    token = _csrf(client)
    dad = client.post(
        "/family/users",
        json={"csrf_token": token, "name": "Dad", "username": "dad", "password": "dadpass"},
    ).get_json()["user"]
    client.post(f"/family/users/{dad['id']}/disable", json={"csrf_token": token})
    client.get("/logout")
    res = _login(client, "dad", "dadpass")
    assert res.status_code == 401
    api = client.get("/api/family/me")
    assert api.status_code == 401


def test_family_writes_require_csrf_and_admin(tmp_path):
    client = _admin(_app(tmp_path))
    token = _csrf(client)
    dad = client.post(
        "/family/users",
        json={"csrf_token": token, "name": "Dad", "username": "dad", "password": "dadpass"},
    ).get_json()["user"]
    missing = client.post("/family/users", json={"name": "Kid", "username": "kid", "password": "kidpass"})
    assert missing.status_code == 403
    assert missing.get_json()["error"] == "csrf_rejected"
    client.get("/logout")
    _login(client, "dad", "dadpass")
    stolen = client.post(
        "/family/users",
        json={"csrf_token": _csrf(client), "name": "Kid", "username": "kid", "password": "kidpass"},
    )
    assert stolen.status_code == 403
    assert stolen.get_json()["error"] == "forbidden"


def test_bad_password_rejected_and_login_csrf_required(tmp_path):
    client = _app(tmp_path)
    res = _login(client, DEFAULT_ADMIN_USERNAME, "wrong")
    assert res.status_code == 401
    no_csrf = client.post("/login", data={"username": DEFAULT_ADMIN_USERNAME, "password": DEFAULT_ADMIN_PASSWORD})
    assert no_csrf.status_code == 403
