"""Password change and admin family reset. Hashed only; sessions invalidated."""

from __future__ import annotations

import json
from pathlib import Path

from agentic_portfolio.dashboard.accounts import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
    AccountStore,
)
from agentic_portfolio.dashboard.app import create_app


def _write_book(root: Path, nav: float = 10000.0) -> None:
    book_dir = root / "state" / "paper_book"
    book_dir.mkdir(parents=True, exist_ok=True)
    payload = {"created_at": "2026-08-30T18:45:00+00:00", "context": {"current_nav": nav}}
    (book_dir / "current.json").write_text(json.dumps(payload), encoding="utf-8")


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


def _app(tmp_path):
    _write_book(tmp_path)
    return create_app(tmp_path)


def test_admin_change_password_requires_current_and_logs_out(tmp_path):
    app = _app(tmp_path)
    client = _admin(app.test_client())
    page = client.get("/password")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Current password" in html
    assert "New password" in html
    assert "Confirm new password" in html
    assert "Change password" in html
    token = _csrf(client)
    mismatch = client.post(
        "/password",
        json={
            "csrf_token": token,
            "current_password": DEFAULT_ADMIN_PASSWORD,
            "new_password": "newpass1",
            "confirm_password": "newpass2",
        },
    )
    assert mismatch.status_code == 409
    assert mismatch.get_json()["error"] == "new passwords do not match"
    wrong = client.post(
        "/password",
        json={
            "csrf_token": token,
            "current_password": "wrong",
            "new_password": "newpass1",
            "confirm_password": "newpass1",
        },
    )
    assert wrong.status_code == 409
    assert wrong.get_json()["error"] == "current password is incorrect"
    still = client.get("/")
    assert still.status_code == 200
    changed = client.post(
        "/password",
        json={
            "csrf_token": token,
            "current_password": DEFAULT_ADMIN_PASSWORD,
            "new_password": "newpass1",
            "confirm_password": "newpass1",
        },
    )
    assert changed.status_code == 200
    body = changed.get_json()
    assert body["ok"] is True
    assert body["logged_out"] is True
    assert "password" not in json.dumps(body)
    assert client.get("/").status_code == 302
    assert _login(client, DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD).status_code == 401
    assert _login(client, DEFAULT_ADMIN_USERNAME, "newpass1").status_code in (302, 303)
    store = AccountStore(tmp_path)
    admin = store.find_login(DEFAULT_ADMIN_USERNAME)
    raw = json.loads((tmp_path / "state" / "accounts.json").read_text(encoding="utf-8"))
    assert "newpass1" not in json.dumps(raw)
    assert admin["password_hash"] != "newpass1"
    assert "pbkdf2" in admin["password_hash"]


def test_family_self_change_password_logs_out(tmp_path):
    app = _app(tmp_path)
    admin = _admin(app.test_client())
    token = _csrf(admin)
    dad = admin.post(
        "/family/users",
        json={"csrf_token": token, "name": "Dad", "username": "dad", "password": "dadpass"},
    ).get_json()["user"]
    family = app.test_client()
    assert _login(family, "dad", "dadpass").status_code in (302, 303)
    page = family.get("/password")
    assert page.status_code == 200
    assert "href=\"/approvals\"" not in page.get_data(as_text=True)
    changed = family.post(
        "/password",
        json={
            "csrf_token": _csrf(family),
            "current_password": "dadpass",
            "new_password": "dadnew1",
            "confirm_password": "dadnew1",
        },
    )
    assert changed.status_code == 200
    assert changed.get_json()["logged_out"] is True
    assert family.get("/").status_code == 302
    assert family.get("/api/family/me").status_code == 401
    assert _login(family, "dad", "dadpass").status_code == 401
    assert _login(family, "dad", "dadnew1").status_code in (302, 303)
    assert AccountStore(tmp_path).get(dad["id"])["password_hash"] != "dadnew1"


def test_admin_reset_family_password_confirms_admin_and_kills_family_session(tmp_path):
    app = _app(tmp_path)
    admin = _admin(app.test_client())
    token = _csrf(admin)
    dad = admin.post(
        "/family/users",
        json={"csrf_token": token, "name": "Dad", "username": "dad", "password": "dadpass"},
    ).get_json()["user"]
    family = app.test_client()
    assert _login(family, "dad", "dadpass").status_code in (302, 303)
    assert family.get("/").status_code == 200
    wrong_admin = admin.post(
        f"/family/users/{dad['id']}/password",
        json={
            "csrf_token": token,
            "admin_password": "nope",
            "new_password": "resetpass",
            "confirm_password": "resetpass",
        },
    )
    assert wrong_admin.status_code == 409
    assert wrong_admin.get_json()["error"] == "admin password is incorrect"
    assert _login(app.test_client(), "dad", "dadpass").status_code in (302, 303)
    reset = admin.post(
        f"/family/users/{dad['id']}/password",
        json={
            "csrf_token": token,
            "admin_password": DEFAULT_ADMIN_PASSWORD,
            "new_password": "resetpass",
            "confirm_password": "resetpass",
        },
    )
    assert reset.status_code == 200
    payload = reset.get_json()
    assert payload["ok"] is True
    assert "password_hash" not in payload["user"]
    assert "resetpass" not in json.dumps(payload)
    family_html = admin.get("/family").get_data(as_text=True)
    assert "Reset password" in family_html
    assert "dadpass" not in family_html
    assert "resetpass" not in family_html
    assert family.get("/").status_code == 302
    assert _login(family, "dad", "dadpass").status_code == 401
    assert _login(family, "dad", "resetpass").status_code in (302, 303)
    missing = admin.post(
        f"/family/users/{dad['id']}/password",
        json={"admin_password": DEFAULT_ADMIN_PASSWORD, "new_password": "x", "confirm_password": "x"},
    )
    assert missing.status_code == 403
    assert missing.get_json()["error"] == "csrf_rejected"


def test_family_cannot_reset_passwords_and_csrf_required(tmp_path):
    app = _app(tmp_path)
    admin = _admin(app.test_client())
    token = _csrf(admin)
    dad = admin.post(
        "/family/users",
        json={"csrf_token": token, "name": "Dad", "username": "dad", "password": "dadpass"},
    ).get_json()["user"]
    family = app.test_client()
    _login(family, "dad", "dadpass")
    stolen = family.post(
        f"/family/users/{dad['id']}/password",
        json={
            "csrf_token": _csrf(family),
            "admin_password": DEFAULT_ADMIN_PASSWORD,
            "new_password": "hacked1",
            "confirm_password": "hacked1",
        },
    )
    assert stolen.status_code == 403
    assert _login(app.test_client(), "dad", "dadpass").status_code in (302, 303)
    no_csrf = admin.post(
        "/password",
        json={
            "current_password": DEFAULT_ADMIN_PASSWORD,
            "new_password": "newpass1",
            "confirm_password": "newpass1",
        },
    )
    assert no_csrf.status_code == 403
    assert no_csrf.get_json()["error"] == "csrf_rejected"
