"""Human Approval Packet must never review, place, cancel, or move money."""

from __future__ import annotations

from pathlib import Path

from agentic_portfolio.adapters.robinhood_read import FORBIDDEN_MCP_TOOLS
from agentic_portfolio.discovery.safety import DISCOVERY_FORBIDDEN_TOOLS
from agentic_portfolio.paths import project_root

APPROVAL_FORBIDDEN_TOOLS = frozenset(DISCOVERY_FORBIDDEN_TOOLS) | frozenset(FORBIDDEN_MCP_TOOLS)


class ApprovalSafetyError(RuntimeError):
    """Raised when approval tries to trade live, invent stops, or move money."""


def assert_no_forbidden_tools(names: list[str] | tuple[str, ...] | set[str]) -> None:
    bad = set(names) & APPROVAL_FORBIDDEN_TOOLS
    if bad:
        raise ApprovalSafetyError(f"Human Approval Packet refused forbidden MCP tools: {sorted(bad)}")


def assert_paper_only(*, live_trade_actions_allowed: bool, auto_execution: bool) -> None:
    if live_trade_actions_allowed:
        raise ApprovalSafetyError("live_trade_actions_allowed must remain false")
    if auto_execution:
        raise ApprovalSafetyError("auto_execution must remain false")


def assert_approval_does_not_place(*, broker_submitted: bool, execution_attempted: bool = False) -> None:
    if broker_submitted:
        raise ApprovalSafetyError("APPROVED must not submit a broker order")
    if execution_attempted:
        raise ApprovalSafetyError("APPROVED must not attempt live execution")


def inspect_approval_module_for_forbidden_tools(root: Path | None = None) -> list[str]:
    base = (root or project_root()) / "src" / "agentic_portfolio" / "approval"
    hits: list[str] = []
    allow = {"APPROVAL_FORBIDDEN_TOOLS", "FORBIDDEN_MCP_TOOLS", "DISCOVERY_FORBIDDEN_TOOLS"}
    for path in base.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in allow) and path.name == "safety.py":
            continue
        for tool in APPROVAL_FORBIDDEN_TOOLS:
            if f'"{tool}"' in text or f"'{tool}'" in text:
                hits.append(f"{path.name}:{tool}")
    return hits
