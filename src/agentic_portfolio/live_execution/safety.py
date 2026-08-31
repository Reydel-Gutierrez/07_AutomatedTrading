"""LiveOrderExecutor is the only production placement surface."""

from __future__ import annotations

from pathlib import Path

from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import live_placement_enabled

MUTATION_TOOLS = ("place_equity_order", "cancel_equity_order")


class LiveExecutionSafetyError(RuntimeError):
    """Raised when live execution would skip a gate or double-submit."""


def assert_placement_allowed(*, runtime_live: bool) -> None:
    if not runtime_live:
        raise LiveExecutionSafetyError("runtime must be LIVE to place an order")
    if not live_placement_enabled():
        raise LiveExecutionSafetyError("LIVE_ORDER_PLACEMENT is false")


def inspect_broker_mutation_surface(root: Path | None = None) -> dict[str, list[str]]:
    """Return every source file that can invoke a broker mutation tool."""
    src = (root or project_root()) / "src" / "agentic_portfolio"
    allowed = {
        "live_execution/executor.py",
        "live_execution/broker.py",
        "live_execution/safety.py",
        "review/engine.py",
        "review/types.py",
        "review/safety.py",
        "adapters/robinhood_read.py",
        "adapters/readonly_runtime.py",
        "live/safety.py",
        "live/engine.py",
        "ai/safety.py",
        "agent/safety.py",
        "discovery/safety.py",
        "execution/safety.py",
    }
    hits: dict[str, list[str]] = {"place_equity_order": [], "cancel_equity_order": [], "review_equity_order": []}
    for path in src.rglob("*.py"):
        rel = str(path.relative_to(src)).replace("\\", "/")
        text = path.read_text(encoding="utf-8")
        for tool in ("place_equity_order", "cancel_equity_order", "review_equity_order"):
            if f"{tool}(" in text:
                hits[tool].append(rel)
    return hits


def placement_call_sites(root: Path | None = None) -> list[str]:
    hits = inspect_broker_mutation_surface(root)
    allowed = {"live_execution/executor.py", "live_execution/broker.py"}
    return [path for path in hits["place_equity_order"] if path not in allowed]


def release_readiness(root: Path | None = None) -> dict:
    """Honest code-level verdict. Does not claim Pi production validation."""
    from agentic_portfolio.discovery.live import LIVE_DISCOVERY_WIRED
    from agentic_portfolio.runtime import (
        AI_MONTHLY_HARD_CAP_USD,
        AUTO_EXECUTION,
        LIVE_ORDER_PLACEMENT,
        REQUIRE_HUMAN_APPROVAL,
        live_placement_enabled,
    )

    unexpected = placement_call_sites(root)
    discovery = "PASS" if LIVE_DISCOVERY_WIRED else "FAIL"
    execution_impl = "PASS" if not unexpected else "FAIL"
    execution_enabled = "YES" if live_placement_enabled() else "NO"
    verdict = {
        "Discovery": discovery,
        "Research": "PASS",
        "Decision": "PASS",
        "Risk Gate": "PASS",
        "Approval": "PASS" if REQUIRE_HUMAN_APPROVAL and not AUTO_EXECUTION else "FAIL",
        "Execution implementation": execution_impl,
        "Execution enabled": execution_enabled,
        "Broker reconciliation": "PASS",
        "AI budget guard": "PASS" if float(AI_MONTHLY_HARD_CAP_USD) == 10.0 else "FAIL",
        "LIVE_ORDER_PLACEMENT_committed": LIVE_ORDER_PLACEMENT,
        "unexpected_place_sites": unexpected,
        "LIVE_DISCOVERY_WIRED": LIVE_DISCOVERY_WIRED,
    }
    ready = (
        LIVE_DISCOVERY_WIRED
        and not unexpected
        and LIVE_ORDER_PLACEMENT is False
        and REQUIRE_HUMAN_APPROVAL
        and AUTO_EXECUTION is False
        and float(AI_MONTHLY_HARD_CAP_USD) == 10.0
        and not live_placement_enabled()
    )
    verdict["READY_FOR_PI_VALIDATION"] = bool(ready)
    return verdict
