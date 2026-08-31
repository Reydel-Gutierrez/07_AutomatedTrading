"""Collect a ResearchPayload from an injected read-only fetcher.

Uses only methods the fetcher actually exposes. Missing tools are recorded as
unavailable — they are never fabricated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from agentic_portfolio.adapters.robinhood_read import collect_from_fetcher
from agentic_portfolio.classification import classify
from agentic_portfolio.research.packet import ResearchPayload
from agentic_portfolio.research.safety import RESEARCH_READ_TOOLS, assert_no_forbidden_tools


def collect_research_payload(
    symbol: str,
    fetcher: Any,
    *,
    now: datetime | None = None,
) -> ResearchPayload:
    """Best-effort read-only collection. Never calls execution tools."""
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    ticker = str(symbol).upper()
    attempted: list[str] = []
    observed: list[str] = []
    unavailable: list[str] = []
    payload = ResearchPayload(symbol=ticker, observed_at=stamp.isoformat())

    def _try(tool: str, attr: str, *args: Any, **kwargs: Any) -> Mapping[str, Any] | None:
        attempted.append(tool)
        method = getattr(fetcher, attr, None)
        if method is None:
            unavailable.append(tool)
            return None
        try:
            result = method(*args, **kwargs)
        except Exception:  # noqa: BLE001 — one missing feed must not abort research
            unavailable.append(tool)
            return None
        if result is None:
            unavailable.append(tool)
            return None
        observed.append(tool)
        return result if isinstance(result, Mapping) else {"data": result}

    assert_no_forbidden_tools(list(RESEARCH_READ_TOOLS))
    payload.quotes = _try("get_equity_quotes", "get_equity_quotes", [ticker])
    payload.fundamentals = _try("get_equity_fundamentals", "get_equity_fundamentals", ticker)
    payload.tradability = _try("get_equity_tradability", "get_equity_tradability", ticker)
    payload.search = _try("search", "search_instrument", ticker)
    payload.financials = _try("get_financials", "get_financials", [ticker]) if hasattr(fetcher, "get_financials") else _mark_unavailable(
        attempted, unavailable, "get_financials"
    )
    payload.historicals = _try("get_equity_historicals", "get_equity_historicals", [ticker]) if hasattr(fetcher, "get_equity_historicals") else _mark_unavailable(
        attempted, unavailable, "get_equity_historicals"
    )
    payload.news = _try("get_equity_news", "get_equity_news", ticker) if hasattr(fetcher, "get_equity_news") else _mark_unavailable(
        attempted, unavailable, "get_equity_news"
    )
    payload.earnings_results = _try("get_earnings_results", "get_earnings_results", ticker) if hasattr(fetcher, "get_earnings_results") else _mark_unavailable(
        attempted, unavailable, "get_earnings_results"
    )
    payload.earnings_calendar = _try("get_earnings_calendar", "get_earnings_calendar") if hasattr(fetcher, "get_earnings_calendar") else _mark_unavailable(
        attempted, unavailable, "get_earnings_calendar"
    )
    payload.sec_index = _try("get_sec_filing_index", "get_sec_filing_index", ticker) if hasattr(fetcher, "get_sec_filing_index") else _mark_unavailable(
        attempted, unavailable, "get_sec_filing_index"
    )

    if hasattr(fetcher, "get_equity_tradability") or hasattr(fetcher, "get_equity_fundamentals"):
        try:
            _, _, bundle = collect_from_fetcher(ticker, fetcher)
            if bundle is not None:
                from agentic_portfolio.adapters.robinhood_read import adapt_classification_evidence

                ev = adapt_classification_evidence(bundle)
                payload.classification = classify(ticker, ev)
        except Exception:  # noqa: BLE001
            pass

    payload.sources_attempted = list(dict.fromkeys(attempted))
    payload.sources_observed = list(dict.fromkeys(observed))
    payload.sources_unavailable = list(dict.fromkeys(unavailable))
    return payload


def _mark_unavailable(attempted: list[str], unavailable: list[str], tool: str) -> None:
    attempted.append(tool)
    unavailable.append(tool)
    return None
