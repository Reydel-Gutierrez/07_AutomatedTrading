"""Dashboard must never place, cancel, transfer, or change hard risk limits."""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path

from agentic_portfolio.adapters.robinhood_read import FORBIDDEN_MCP_TOOLS
from agentic_portfolio.discovery.safety import DISCOVERY_FORBIDDEN_TOOLS
from agentic_portfolio.paths import project_root

DASHBOARD_FORBIDDEN_TOOLS = frozenset(DISCOVERY_FORBIDDEN_TOOLS) | frozenset(FORBIDDEN_MCP_TOOLS)

TRANSFER_MARKERS = (
    "deposit",
    "withdrawal",
    "withdraw",
    "transfer_between",
    "inter_account_transfer",
    "initiate_deposits",
    "initiate_withdrawals",
)

POLICY_WRITE_PATHS = (
    "config/portfolio_policy.json",
    "config/account_rules.json",
)


class DashboardSafetyError(RuntimeError):
    """Raised when the dashboard tries to trade, move money, or loosen limits."""


def assert_no_forbidden_tools(names: list[str] | tuple[str, ...] | set[str]) -> None:
    bad = set(names) & DASHBOARD_FORBIDDEN_TOOLS
    if bad:
        raise DashboardSafetyError(f"Dashboard refused forbidden MCP tools: {sorted(bad)}")


def assert_localhost_bind(host: str, *, allow_public_bind: bool) -> None:
    local = {"127.0.0.1", "localhost", "::1"}
    if host in local:
        return
    if allow_public_bind:
        return
    raise DashboardSafetyError(
        f"Dashboard refuses to bind {host!r}. Default is localhost. "
        "Do not expose this origin to the internet; use a Cloudflare Tunnel later."
    )


def is_forbidden_action(name: str) -> bool:
    key = str(name or "").strip()
    if key in DASHBOARD_FORBIDDEN_TOOLS:
        return True
    lowered = key.lower().replace("-", "_")
    if lowered in {t.lower() for t in DASHBOARD_FORBIDDEN_TOOLS}:
        return True
    return any(marker in lowered for marker in TRANSFER_MARKERS)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_tokens_match(expected: str | None, provided: str | None) -> bool:
    if not expected or not provided:
        return False
    left = str(expected)
    right = str(provided)
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def inspect_dashboard_module_for_forbidden_tools(root: Path | None = None) -> list[str]:
    base = (root or project_root()) / "src" / "agentic_portfolio" / "dashboard"
    hits: list[str] = []
    allow = {"DASHBOARD_FORBIDDEN_TOOLS", "FORBIDDEN_MCP_TOOLS", "DISCOVERY_FORBIDDEN_TOOLS"}
    for path in base.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in allow) and path.name == "safety.py":
            continue
        for tool in DASHBOARD_FORBIDDEN_TOOLS:
            if f'"{tool}"' in text or f"'{tool}'" in text:
                hits.append(f"{path.name}:{tool}")
    return hits
