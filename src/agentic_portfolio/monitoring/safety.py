"""Position monitoring must never execute, place broker stops, or move money."""

from __future__ import annotations

from pathlib import Path

from agentic_portfolio.adapters.robinhood_read import FORBIDDEN_MCP_TOOLS
from agentic_portfolio.discovery.safety import DISCOVERY_FORBIDDEN_TOOLS

MONITORING_FORBIDDEN_TOOLS = frozenset(DISCOVERY_FORBIDDEN_TOOLS) | frozenset(FORBIDDEN_MCP_TOOLS)

MONITORING_READ_TOOLS = (
    "search",
    "get_equity_quotes",
    "get_equity_historicals",
    "get_equity_technical_indicators",
    "get_equity_fundamentals",
    "get_financials",
    "get_earnings_calendar",
    "get_earnings_results",
    "get_equity_news",
    "get_sec_filing",
    "get_sec_filing_facts",
    "get_sec_filing_facts_catalog",
    "get_sec_filing_index",
    "get_index_quotes",
    "get_index_historicals",
    "get_indexes",
    "get_equity_tradability",
    "get_equity_price_book",
    "get_equity_positions",
    "get_portfolio",
    "get_accounts",
    "get_equity_orders",
)


class MonitoringSafetyError(RuntimeError):
    """Raised when monitoring tries to execute, place stops, or skip Risk Gate."""


def assert_no_forbidden_tools(names: list[str] | tuple[str, ...] | set[str]) -> None:
    bad = set(names) & MONITORING_FORBIDDEN_TOOLS
    if bad:
        raise MonitoringSafetyError(f"Position monitoring refused forbidden MCP tools: {sorted(bad)}")


def inspect_monitoring_module_for_forbidden_tools(root: Path | None = None) -> list[str]:
    from agentic_portfolio.paths import project_root as _root

    base = (root or _root()) / "src" / "agentic_portfolio" / "monitoring"
    hits: list[str] = []
    allow = {"MONITORING_FORBIDDEN_TOOLS", "FORBIDDEN_MCP_TOOLS", "DISCOVERY_FORBIDDEN_TOOLS"}
    for path in base.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in allow) and path.name == "safety.py":
            continue
        for tool in MONITORING_FORBIDDEN_TOOLS:
            if f'"{tool}"' in text or f"'{tool}'" in text:
                hits.append(f"{path.name}:{tool}")
    return hits
