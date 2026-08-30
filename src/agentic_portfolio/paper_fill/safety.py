"""Paper fill must never review, place, cancel, or move money."""

from __future__ import annotations

from pathlib import Path

from agentic_portfolio.adapters.robinhood_read import FORBIDDEN_MCP_TOOLS
from agentic_portfolio.discovery.safety import DISCOVERY_FORBIDDEN_TOOLS
from agentic_portfolio.paths import project_root

PAPER_FILL_FORBIDDEN_TOOLS = frozenset(DISCOVERY_FORBIDDEN_TOOLS) | frozenset(FORBIDDEN_MCP_TOOLS)

LIVE_STATE_RELATIVE = (
    "state/thesis_registry.json",
    "state/sleeve_registry.json",
)


class PaperFillSafetyError(RuntimeError):
    """Raised when paper fill tries to trade live, invent stops, or move money."""


def assert_no_forbidden_tools(names: list[str] | tuple[str, ...] | set[str]) -> None:
    bad = set(names) & PAPER_FILL_FORBIDDEN_TOOLS
    if bad:
        raise PaperFillSafetyError(f"Paper fill refused forbidden MCP tools: {sorted(bad)}")


def assert_paper_only(*, live_trade_actions_allowed: bool, auto_execution: bool) -> None:
    if live_trade_actions_allowed:
        raise PaperFillSafetyError("live_trade_actions_allowed must remain false")
    if auto_execution:
        raise PaperFillSafetyError("auto_execution must remain false")


def live_state_paths(root: Path | None = None) -> tuple[Path, ...]:
    base = root or project_root()
    return tuple(base / rel for rel in LIVE_STATE_RELATIVE)


def inspect_paper_fill_module_for_forbidden_tools(root: Path | None = None) -> list[str]:
    base = (root or project_root()) / "src" / "agentic_portfolio" / "paper_fill"
    hits: list[str] = []
    allow = {"PAPER_FILL_FORBIDDEN_TOOLS", "FORBIDDEN_MCP_TOOLS", "DISCOVERY_FORBIDDEN_TOOLS"}
    for path in base.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in allow) and path.name == "safety.py":
            continue
        for tool in PAPER_FILL_FORBIDDEN_TOOLS:
            if f'"{tool}"' in text or f"'{tool}'" in text:
                hits.append(f"{path.name}:{tool}")
    return hits
