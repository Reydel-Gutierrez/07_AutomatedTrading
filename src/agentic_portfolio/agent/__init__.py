"""24/7 autonomous portfolio-management runtime."""

from __future__ import annotations

from typing import Any

__all__ = [
    "AgentRuntime",
    "MarketPhase",
    "classify_market_phase",
    "run_forever",
    "status",
    "stop",
]


def __getattr__(name: str) -> Any:
    if name in {"AgentRuntime", "run_forever"}:
        from agentic_portfolio.agent.runtime import AgentRuntime, run_forever

        return AgentRuntime if name == "AgentRuntime" else run_forever
    if name in {"MarketPhase", "classify_market_phase"}:
        from agentic_portfolio.agent.session import MarketPhase, classify_market_phase

        return MarketPhase if name == "MarketPhase" else classify_market_phase
    if name in {"status", "stop"}:
        from agentic_portfolio.agent.lifecycle import status, stop

        return status if name == "status" else stop
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
