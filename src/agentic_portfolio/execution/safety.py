"""Execution Controller must never review, place, cancel, or move money."""

from __future__ import annotations

from pathlib import Path

from agentic_portfolio.adapters.robinhood_read import FORBIDDEN_MCP_TOOLS
from agentic_portfolio.discovery.safety import DISCOVERY_FORBIDDEN_TOOLS

EXECUTION_FORBIDDEN_TOOLS = frozenset(DISCOVERY_FORBIDDEN_TOOLS) | frozenset(FORBIDDEN_MCP_TOOLS)

EXECUTION_READ_TOOLS = (
    "get_equity_quotes",
    "get_equity_tradability",
    "get_equity_orders",
    "get_equity_positions",
    "get_portfolio",
    "get_accounts",
    "get_equity_price_book",
)


class ExecutionSafetyError(RuntimeError):
    """Raised when Execution Controller tries to trade live, invent stops, or move money."""


def assert_no_forbidden_tools(names: list[str] | tuple[str, ...] | set[str]) -> None:
    bad = set(names) & EXECUTION_FORBIDDEN_TOOLS
    if bad:
        raise ExecutionSafetyError(f"Execution Controller refused forbidden MCP tools: {sorted(bad)}")


def assert_paper_only(*, live_trade_actions_allowed: bool, auto_execution: bool) -> None:
    if live_trade_actions_allowed:
        raise ExecutionSafetyError("live_trade_actions_allowed must remain false")
    if auto_execution:
        raise ExecutionSafetyError("auto_execution must remain false")


def inspect_execution_module_for_forbidden_tools(root: Path | None = None) -> list[str]:
    from agentic_portfolio.paths import project_root as _root

    base = (root or _root()) / "src" / "agentic_portfolio" / "execution"
    hits: list[str] = []
    allow = {"EXECUTION_FORBIDDEN_TOOLS", "FORBIDDEN_MCP_TOOLS", "DISCOVERY_FORBIDDEN_TOOLS"}
    for path in base.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in allow) and path.name == "safety.py":
            continue
        for tool in EXECUTION_FORBIDDEN_TOOLS:
            if f'"{tool}"' in text or f"'{tool}'" in text:
                hits.append(f"{path.name}:{tool}")
    return hits
