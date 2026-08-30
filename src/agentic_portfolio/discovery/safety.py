"""Discovery must never create BUY actions, ACTIVE theses, or call execution tools."""

from __future__ import annotations

from agentic_portfolio.adapters.robinhood_read import FORBIDDEN_MCP_TOOLS
from agentic_portfolio.schemas import Candidate, Decision, ProposedAction

# Writes that would mutate the brokerage account or saved screens — never used here.
DISCOVERY_FORBIDDEN_TOOLS = frozenset(FORBIDDEN_MCP_TOOLS) | {
    "create_scan",
    "update_scan_config",
    "update_scan_filters",
    "create_watchlist",
    "add_to_watchlist",
    "add_option_to_watchlist",
    "follow_watchlist",
    "unfollow_watchlist",
    "remove_from_watchlist",
    "remove_option_from_watchlist",
    "update_watchlist",
}

DISCOVERY_READ_TOOLS = (
    "search",
    "get_scans",
    "run_scan",
    "get_scanner_filter_specs",
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
    "get_watchlists",
    "get_watchlist_items",
    "get_popular_watchlists",
    "get_equity_tradability",
    "get_equity_positions",
    "get_portfolio",
    "get_accounts",
    "get_equity_price_book",
    "get_equity_orders",
)


class DiscoverySafetyError(RuntimeError):
    """Raised when a caller tries to turn Discovery into execution or a thesis."""


def assert_no_forbidden_tools(names: list[str] | tuple[str, ...] | set[str]) -> None:
    bad = set(names) & DISCOVERY_FORBIDDEN_TOOLS
    if bad:
        raise DiscoverySafetyError(f"Discovery refused forbidden MCP tools: {sorted(bad)}")


def candidate_cannot_become_buy(candidate: Candidate) -> None:
    """Explicit guard. Research → Thesis → Portfolio Decision is required first."""
    raise DiscoverySafetyError(
        f"Candidate {candidate.symbol} ({candidate.candidate_id}) cannot become a BUY "
        "ProposedAction. Path is Candidate → ResearchReport → InvestmentThesis → "
        "PortfolioDecision → ProposedAction → RiskGate."
    )


def as_proposed_action(candidate: Candidate) -> ProposedAction:
    candidate_cannot_become_buy(candidate)
    raise DiscoverySafetyError("unreachable")  # pragma: no cover


def assert_not_a_trade_decision(decision: Decision | str | None) -> None:
    if decision in {Decision.BUY, Decision.ADD, "BUY", "ADD"}:
        raise DiscoverySafetyError("Discovery cannot emit BUY/ADD decisions")
