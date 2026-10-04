"""Structural safety for the 24/7 runtime.

Job handlers, watch, approvals and notifications never call broker mutation
tools. The only placement surface is live_execution/executor.py. WHO may
authorize an order is decided by autonomy.OrderAuthority, not by flags here.
"""

from __future__ import annotations

from pathlib import Path

from agentic_portfolio.live.safety import LIVE_FORBIDDEN_TOOLS, LiveSafetyError
from agentic_portfolio.paths import project_root

AGENT_FORBIDDEN_TOOLS = frozenset(LIVE_FORBIDDEN_TOOLS) | {
    "place_equity_order",
    "cancel_equity_order",
    "review_equity_order",
}


class AgentSafetyError(LiveSafetyError):
    """Raised when the 24/7 runtime would trade or move money."""


def inspect_agent_packages_for_forbidden_calls(root: Path | None = None) -> list[str]:
    src = (root or project_root()) / "src" / "agentic_portfolio"
    hits: list[str] = []
    for package in ("agent", "watch", "live_approval", "notify"):
        base = src / package
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for tool in ("place_equity_order", "cancel_equity_order"):
                if f"{tool}(" in text:
                    hits.append(f"{path.relative_to(src)}:{tool}")
    return hits
