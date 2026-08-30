"""Local users for the dashboard. ADMIN and USER only. Passwords are hashed."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from agentic_portfolio.paths import project_root

ROLE_ADMIN = "ADMIN"
ROLE_USER = "USER"
ROLE_FAMILY = "FAMILY"
VIEWER_ROLES = frozenset({ROLE_USER, ROLE_FAMILY})
ROLES = frozenset({ROLE_ADMIN, ROLE_USER, ROLE_FAMILY})


def canonical_role(role: str | None) -> str:
    if role == ROLE_FAMILY:
        return ROLE_USER
    return str(role or "")


def is_viewer_role(role: str | None) -> bool:
    return role in VIEWER_ROLES

DEFAULT_ADMIN_NAME = "Reydel"
DEFAULT_ADMIN_USERNAME = "reydel"
DEFAULT_ADMIN_PASSWORD = "reydel"
MIN_PASSWORD_LENGTH = 4

_EMAILISH = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str) -> str:
    return generate_password_hash(str(password), method="pbkdf2:sha256")


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash or not password:
        return False
    return bool(check_password_hash(str(password_hash), str(password)))


def public_user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if not user:
        return None
    payload = {key: value for key, value in user.items() if key != "password_hash"}
    payload["role"] = canonical_role(payload.get("role"))
    return payload


def normalize_login(value: str | None) -> str:
    return str(value or "").strip().lower()


def looks_like_email(value: str) -> bool:
    return bool(_EMAILISH.match(value.strip()))


def validate_new_password(new_password: str, confirm: str) -> str:
    secret = str(new_password or "")
    if secret != str(confirm or ""):
        raise ValueError("new passwords do not match")
    if len(secret) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return secret


def new_session_nonce() -> str:
    return str(uuid.uuid4())


class AccountStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else project_root()
        self.path = self.root / "state" / "accounts.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()
        self.ensure_default_admin()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("users"), list):
                users = list(raw["users"])
                changed = False
                for user in users:
                    if user.get("role") == ROLE_FAMILY:
                        user["role"] = ROLE_USER
                        changed = True
                payload = {"users": users}
                if changed:
                    self.path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
                return payload
        return {"users": []}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, default=str), encoding="utf-8")

    def all_users(self) -> list[dict[str, Any]]:
        return [dict(user) for user in self._data.get("users") or []]

    def family_users(self) -> list[dict[str, Any]]:
        rows = [user for user in self.all_users() if is_viewer_role(user.get("role"))]
        rows.sort(key=lambda user: str(user.get("name") or user.get("username") or "").lower())
        return rows

    def get(self, user_id: str | None) -> dict[str, Any] | None:
        if not user_id:
            return None
        for user in self.all_users():
            if str(user.get("id")) == str(user_id):
                return user
        return None

    def find_login(self, identifier: str | None) -> dict[str, Any] | None:
        key = normalize_login(identifier)
        if not key:
            return None
        for user in self.all_users():
            if normalize_login(user.get("username")) == key:
                return user
            if normalize_login(user.get("email")) == key:
                return user
        return None

    def authenticate(self, identifier: str, password: str) -> dict[str, Any] | None:
        user = self.find_login(identifier)
        if user is None:
            return None
        if not user.get("enabled", True):
            return None
        if not verify_password(user.get("password_hash"), password):
            return None
        return user

    def username_taken(self, identifier: str, *, exclude_id: str | None = None) -> bool:
        key = normalize_login(identifier)
        for user in self.all_users():
            if exclude_id and str(user.get("id")) == str(exclude_id):
                continue
            if normalize_login(user.get("username")) == key or normalize_login(user.get("email")) == key:
                return True
        return False

    def ensure_default_admin(self) -> dict[str, Any]:
        existing = next((user for user in self.all_users() if user.get("role") == ROLE_ADMIN), None)
        if existing is not None:
            return existing
        named = self.find_login(DEFAULT_ADMIN_USERNAME)
        if named is not None:
            named["role"] = ROLE_ADMIN
            named["enabled"] = True
            named["name"] = named.get("name") or DEFAULT_ADMIN_NAME
            self._replace(named)
            return named
        return self._insert(
            {
                "id": str(uuid.uuid4()),
                "name": DEFAULT_ADMIN_NAME,
                "username": DEFAULT_ADMIN_USERNAME,
                "email": None,
                "role": ROLE_ADMIN,
                "password_hash": hash_password(DEFAULT_ADMIN_PASSWORD),
                "session_nonce": new_session_nonce(),
                "enabled": True,
                "assigned_amount": None,
                "baseline_nav": None,
                "assigned_at": None,
                "created_at": utc_now(),
            }
        )

    def create_family_user(self, *, name: str, login: str, password: str) -> dict[str, Any]:
        display = str(name or "").strip()
        ident = str(login or "").strip()
        secret = str(password or "")
        if not display:
            raise ValueError("name is required")
        if not ident:
            raise ValueError("username/email is required")
        secret = validate_new_password(secret, secret)
        if self.username_taken(ident):
            raise ValueError("username/email already exists")
        email = ident if looks_like_email(ident) else None
        return self._insert(
            {
                "id": str(uuid.uuid4()),
                "name": display,
                "username": normalize_login(ident),
                "email": normalize_login(email) if email else None,
                "role": ROLE_USER,
                "password_hash": hash_password(secret),
                "session_nonce": new_session_nonce(),
                "enabled": True,
                "assigned_amount": None,
                "baseline_nav": None,
                "assigned_at": None,
                "created_at": utc_now(),
            }
        )

    def set_enabled(self, user_id: str, enabled: bool) -> dict[str, Any]:
        user = self._family(user_id)
        user["enabled"] = bool(enabled)
        return self._replace(user)

    def change_own_password(self, user_id: str, current_password: str, new_password: str) -> dict[str, Any]:
        user = self.get(user_id)
        if user is None:
            raise KeyError(user_id)
        if not verify_password(user.get("password_hash"), current_password):
            raise ValueError("current password is incorrect")
        return self._set_password(user, new_password)

    def reset_family_password(self, user_id: str, new_password: str) -> dict[str, Any]:
        user = self._family(user_id)
        return self._set_password(user, new_password)

    def _set_password(self, user: dict[str, Any], new_password: str) -> dict[str, Any]:
        secret = str(new_password or "")
        if len(secret) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        user["password_hash"] = hash_password(secret)
        user["session_nonce"] = new_session_nonce()
        return self._replace(user)

    def assign_amount(self, user_id: str, amount: float, baseline_nav: float) -> dict[str, Any]:
        user = self._family(user_id)
        if amount < 0:
            raise ValueError("assigned amount cannot be negative")
        if baseline_nav <= 0:
            raise ValueError("portfolio NAV baseline is missing or invalid")
        user["assigned_amount"] = float(amount)
        user["baseline_nav"] = float(baseline_nav)
        user["assigned_at"] = utc_now()
        return self._replace(user)

    def _family(self, user_id: str) -> dict[str, Any]:
        user = self.get(user_id)
        if user is None:
            raise KeyError(user_id)
        if not is_viewer_role(user.get("role")):
            raise ValueError("only USER accounts can be changed here")
        return user

    def _insert(self, user: dict[str, Any]) -> dict[str, Any]:
        self._data.setdefault("users", []).append(user)
        self._save()
        return dict(user)

    def _replace(self, user: dict[str, Any]) -> dict[str, Any]:
        rows = self._data.setdefault("users", [])
        for index, existing in enumerate(rows):
            if str(existing.get("id")) == str(user.get("id")):
                rows[index] = user
                self._save()
                return dict(user)
        raise KeyError(user.get("id"))
