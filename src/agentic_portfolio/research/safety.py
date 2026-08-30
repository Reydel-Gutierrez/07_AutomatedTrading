"""Research must never create BUY actions, ACTIVE theses, or call execution tools."""

from __future__ import annotations

from pathlib import Path

from agentic_portfolio.adapters.robinhood_read import FORBIDDEN_MCP_TOOLS
from agentic_portfolio.discovery.safety import DISCOVERY_FORBIDDEN_TOOLS
from agentic_portfolio.research.types import ResearchReport
from agentic_portfolio.schemas import Candidate, Decision, ProposedAction

RESEARCH_FORBIDDEN_TOOLS = frozenset(DISCOVERY_FORBIDDEN_TOOLS) | frozenset(FORBIDDEN_MCP_TOOLS)

RESEARCH_READ_TOOLS = (
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


class ResearchSafetyError(RuntimeError):
    """Raised when Research tries to execute, mutate the book, or skip later stages."""


def assert_no_forbidden_tools(names: list[str] | tuple[str, ...] | set[str]) -> None:
    bad = set(names) & RESEARCH_FORBIDDEN_TOOLS
    if bad:
        raise ResearchSafetyError(f"Research refused forbidden MCP tools: {sorted(bad)}")


def research_cannot_become_buy(report: ResearchReport) -> None:
    raise ResearchSafetyError(
        f"ResearchReport {report.symbol} ({report.research_id}) cannot become a BUY "
        "ProposedAction. Path is Candidate → ResearchReport → InvestmentThesis → "
        "PortfolioDecision → ProposedAction → RiskGate."
    )


def candidate_research_cannot_become_buy(candidate: Candidate) -> None:
    raise ResearchSafetyError(
        f"Candidate {candidate.symbol} cannot become a BUY ProposedAction from Research."
    )


def as_proposed_action(report: ResearchReport) -> ProposedAction:
    research_cannot_become_buy(report)
    raise ResearchSafetyError("unreachable")  # pragma: no cover


def assert_not_a_trade_decision(decision: Decision | str | None) -> None:
    if decision in {Decision.BUY, Decision.ADD, "BUY", "ADD"}:
        raise ResearchSafetyError("Research cannot emit BUY/ADD decisions")


def inspect_research_module_for_forbidden_tools(root: Path | None = None) -> list[str]:
    from agentic_portfolio.paths import project_root as _root

    base = (root or _root()) / "src" / "agentic_portfolio" / "research"
    hits: list[str] = []
    allow = {"RESEARCH_FORBIDDEN_TOOLS", "FORBIDDEN_MCP_TOOLS", "DISCOVERY_FORBIDDEN_TOOLS"}
    for path in base.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in allow) and path.name == "safety.py":
            continue
        for tool in RESEARCH_FORBIDDEN_TOOLS:
            if f'"{tool}"' in text or f"'{tool}'" in text:
                hits.append(f"{path.name}:{tool}")
    return hits
