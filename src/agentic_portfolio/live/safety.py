"""LIVE runtime is read-only. Never place, cancel, review, or move money from this package."""

from __future__ import annotations

from pathlib import Path

from agentic_portfolio.adapters.robinhood_read import FORBIDDEN_MCP_TOOLS
from agentic_portfolio.discovery.safety import DISCOVERY_FORBIDDEN_TOOLS
from agentic_portfolio.paths import project_root

LIVE_READ_TOOLS = frozenset(
    {
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_equity_quotes",
        "get_equity_orders",
    }
)
LIVE_FORBIDDEN_TOOLS = frozenset(DISCOVERY_FORBIDDEN_TOOLS) | frozenset(FORBIDDEN_MCP_TOOLS)


class LiveSafetyError(RuntimeError):
    """Raised when LIVE runtime would trade, review, move money, or use paper state."""


def assert_no_forbidden_tools(names: list[str] | tuple[str, ...] | set[str]) -> None:
    bad = set(names) & LIVE_FORBIDDEN_TOOLS
    if bad:
        raise LiveSafetyError(f"LIVE runtime refused forbidden MCP tools: {sorted(bad)}")


def assert_placement_disabled(*, live_trade_actions_allowed: bool, auto_execution: bool, live_order_placement_enabled: bool) -> None:
    if live_trade_actions_allowed:
        raise LiveSafetyError("live_trade_actions_allowed must remain false")
    if auto_execution:
        raise LiveSafetyError("auto_execution must remain false")
    if live_order_placement_enabled:
        raise LiveSafetyError("live order placement must remain disabled")


def inspect_live_module_for_forbidden_calls(root: Path | None = None) -> list[str]:
    """Flag place/cancel invocations. Mentioning the names as forbidden strings is allowed."""
    base = (root or project_root()) / "src" / "agentic_portfolio" / "live"
    hits: list[str] = []
    allow = {"LIVE_FORBIDDEN_TOOLS", "FORBIDDEN_MCP_TOOLS", "place_equity_order", "cancel_equity_order", "review_equity_order"}
    for path in base.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for tool in ("place_equity_order", "cancel_equity_order"):
            if f"{tool}(" in text:
                hits.append(f"{path.name}:{tool}")
        if path.name == "safety.py" and any(marker in text for marker in allow):
            continue
    return hits
