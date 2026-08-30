"""Review-only bridge may call review_equity_order. Never place, cancel, or move money."""

from __future__ import annotations

from pathlib import Path

from agentic_portfolio.adapters.robinhood_read import FORBIDDEN_MCP_TOOLS
from agentic_portfolio.discovery.safety import DISCOVERY_FORBIDDEN_TOOLS
from agentic_portfolio.paths import project_root

REVIEW_ALLOWED_TOOLS = frozenset({"review_equity_order"})
REVIEW_FORBIDDEN_TOOLS = (frozenset(DISCOVERY_FORBIDDEN_TOOLS) | frozenset(FORBIDDEN_MCP_TOOLS)) - REVIEW_ALLOWED_TOOLS


class ReviewSafetyError(RuntimeError):
    """Raised when review tries to place, cancel, move money, or enable live trading."""


def assert_no_forbidden_tools(names: list[str] | tuple[str, ...] | set[str]) -> None:
    bad = set(names) & REVIEW_FORBIDDEN_TOOLS
    if bad:
        raise ReviewSafetyError(f"Review-only bridge refused forbidden MCP tools: {sorted(bad)}")


def assert_flags_remain_gated(*, live_trade_actions_allowed: bool, auto_execution: bool) -> None:
    if live_trade_actions_allowed:
        raise ReviewSafetyError("live_trade_actions_allowed must remain false")
    if auto_execution:
        raise ReviewSafetyError("auto_execution must remain false")


def assert_review_does_not_place(*, broker_submitted: bool, execution_attempted: bool = False, order_placed: bool = False) -> None:
    if broker_submitted or order_placed:
        raise ReviewSafetyError("review must not submit a live order")
    if execution_attempted:
        raise ReviewSafetyError("review must not attempt live execution")


def inspect_review_module_for_forbidden_tools(root: Path | None = None) -> list[str]:
    base = (root or project_root()) / "src" / "agentic_portfolio" / "review"
    hits: list[str] = []
    allow = {"REVIEW_FORBIDDEN_TOOLS", "FORBIDDEN_MCP_TOOLS", "DISCOVERY_FORBIDDEN_TOOLS", "REVIEW_ALLOWED_TOOLS"}
    for path in base.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in allow) and path.name == "safety.py":
            continue
        for tool in REVIEW_FORBIDDEN_TOOLS:
            if f'"{tool}"' in text or f"'{tool}'" in text:
                hits.append(f"{path.name}:{tool}")
    return hits
