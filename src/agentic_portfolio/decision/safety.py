"""Thesis/decision must never execute, activate theses, or place broker stops."""

from __future__ import annotations

from pathlib import Path

from agentic_portfolio.adapters.robinhood_read import FORBIDDEN_MCP_TOOLS
from agentic_portfolio.discovery.safety import DISCOVERY_FORBIDDEN_TOOLS
from agentic_portfolio.schemas import ThesisStatus

DECISION_FORBIDDEN_TOOLS = frozenset(DISCOVERY_FORBIDDEN_TOOLS) | frozenset(FORBIDDEN_MCP_TOOLS)


class DecisionSafetyError(RuntimeError):
    """Raised when Thesis/Decision tries to execute, activate, or skip Risk Gate."""


def assert_no_forbidden_tools(names: list[str] | tuple[str, ...] | set[str]) -> None:
    bad = set(names) & DECISION_FORBIDDEN_TOOLS
    if bad:
        raise DecisionSafetyError(f"Thesis/Decision refused forbidden MCP tools: {sorted(bad)}")


def assert_draft(status: ThesisStatus | str | None) -> ThesisStatus:
    if status not in {None, ThesisStatus.DRAFT, ThesisStatus.DRAFT.value, "DRAFT"}:
        raise DecisionSafetyError("Thesis must remain DRAFT until a future real execution occurs.")
    return ThesisStatus.DRAFT


def inspect_decision_module_for_forbidden_tools(root: Path | None = None) -> list[str]:
    from agentic_portfolio.paths import project_root as _root

    base = (root or _root()) / "src" / "agentic_portfolio" / "decision"
    hits: list[str] = []
    allow = {"DECISION_FORBIDDEN_TOOLS", "FORBIDDEN_MCP_TOOLS", "DISCOVERY_FORBIDDEN_TOOLS"}
    for path in base.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in allow) and path.name == "safety.py":
            continue
        for tool in DECISION_FORBIDDEN_TOOLS:
            if f'"{tool}"' in text or f"'{tool}'" in text:
                hits.append(f"{path.name}:{tool}")
    return hits
