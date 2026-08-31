"""24/7 agent runtime must never place, cancel, or review an order."""

from __future__ import annotations

from pathlib import Path

from agentic_portfolio.live.safety import LIVE_FORBIDDEN_TOOLS, LiveSafetyError, assert_placement_disabled
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT

AGENT_FORBIDDEN_TOOLS = frozenset(LIVE_FORBIDDEN_TOOLS) | {
    "place_equity_order",
    "cancel_equity_order",
    "review_equity_order",
}


class AgentSafetyError(LiveSafetyError):
    """Raised when the 24/7 runtime would trade or move money."""


def assert_execution_disabled(*, live_trade_actions_allowed: bool = False, auto_execution: bool = False) -> None:
    if LIVE_ORDER_PLACEMENT:
        raise AgentSafetyError("LIVE_ORDER_PLACEMENT must remain false")
    assert_placement_disabled(
        live_trade_actions_allowed=live_trade_actions_allowed,
        auto_execution=auto_execution,
        live_order_placement_enabled=False,
    )


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
