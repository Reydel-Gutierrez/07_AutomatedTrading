"""Single-admin authentication for Investment HQ. No default password exists.

Passwords: scrypt (stdlib). Sessions: signed, expiring cookie. CSRF: double
submit (cookie + X-CSRF-Token header) on every state-changing request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from firm.clock import iso, utc_now
from firm.db import Database
from firm.settings import Settings

SESSION_COOKIE = "firm_session"
CSRF_COOKIE = "firm_csrf"
SESSION_TTL = 12 * 3600
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def set_password(db: Database, username: str, password: str) -> None:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)"
            " ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash",
            (username, hash_password(password), "admin", iso(utc_now())),
        )


class Sessions:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        secret = (settings.get("session_secret") or {}).get("value")
        if not secret:
            secret = secrets.token_urlsafe(48)
            settings.put("session_secret", {"value": secret}, by="system")
        self._secret = secret.encode()
        self._failures: dict[str, deque] = defaultdict(deque)

    def has_admin(self) -> bool:
        return self.db.one("SELECT 1 FROM users LIMIT 1") is not None

    def login(self, username: str, password: str, client: str) -> str | None:
        window = self._failures[client]
        now = time.monotonic()
        while window and now - window[0] > 900:
            window.popleft()
        if len(window) >= 8:
            return None  # 8 failures in 15 minutes: locked out for the rest of the window
        row = self.db.one("SELECT password_hash FROM users WHERE username = ?", (username,))
        if row is None or not verify_password(password, row["password_hash"]):
            window.append(now)
            return None
        window.clear()
        return self.issue(username)

    def issue(self, username: str) -> str:
        payload = f"{username}|{int(time.time()) + SESSION_TTL}"
        sig = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()

    def user(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            username, expires, sig = base64.urlsafe_b64decode(token.encode()).decode().split("|")
        except (ValueError, UnicodeDecodeError):
            return None
        expected = hmac.new(self._secret, f"{username}|{expires}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected) or int(expires) < time.time():
            return None
        return username if self.db.one("SELECT 1 FROM users WHERE username = ?", (username,)) else None
