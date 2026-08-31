"""Collect a ResearchPayload from an injected read-only fetcher.

Uses only methods the fetcher actually exposes. Missing tools are recorded as
unavailable — they are never fabricated.

Production Robinhood adapters expose short aliases (`financials`, `news`,
`historicals`, `sec_index`, `technicals`) in addition to MCP tool names.
This collector must resolve both. Calling only MCP names against the live
adapter previously marked get_financials / news / filings unavailable even
though Robinhood had the data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from agentic_portfolio.adapters.robinhood_read import collect_from_fetcher
from agentic_portfolio.classification import classify
from agentic_portfolio.research.packet import ResearchPayload
from agentic_portfolio.research.safety import RESEARCH_READ_TOOLS, assert_no_forbidden_tools

# MCP tool name → adapter method aliases (first match wins).
_ALIASES: dict[str, tuple[str, ...]] = {
    "get_equity_quotes": ("get_equity_quotes", "quotes"),
    "get_equity_fundamentals": ("get_equity_fundamentals", "fundamentals"),
    "get_equity_tradability": ("get_equity_tradability", "tradability"),
    "search": ("search_instrument", "search"),
    "get_financials": ("get_financials", "financials"),
    "get_equity_historicals": ("get_equity_historicals", "historicals"),
    "get_equity_news": ("get_equity_news", "news"),
    "get_earnings_results": ("get_earnings_results", "earnings_results"),
    "get_earnings_calendar": ("get_earnings_calendar", "earnings_calendar"),
    "get_sec_filing_index": ("get_sec_filing_index", "sec_index"),
    "get_sec_filing_facts": ("get_sec_filing_facts", "sec_facts"),
    "get_sec_filing": ("get_sec_filing", "sec_filing"),
    "get_equity_technical_indicators": ("get_equity_technical_indicators", "technicals"),
    "get_equity_price_book": ("get_equity_price_book", "price_book"),
}

_HISTORICAL_LOOKBACK_DAYS = 400


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
    start_day = (stamp - timedelta(days=_HISTORICAL_LOOKBACK_DAYS)).date().isoformat()
    symbols = [ticker]

    def _try(tool: str, shapes: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> Mapping[str, Any] | None:
        attempted.append(tool)
        _name, method = _resolve(fetcher, tool)
        if method is None:
            unavailable.append(tool)
            return None
        try:
            result = _invoke(method, shapes)
        except Exception:  # noqa: BLE001 — one missing feed must not abort research
            unavailable.append(tool)
            return None
        if result is None:
            unavailable.append(tool)
            return None
        observed.append(tool)
        return result if isinstance(result, Mapping) else {"data": result}

    assert_no_forbidden_tools(list(RESEARCH_READ_TOOLS))
    payload.quotes = _try("get_equity_quotes", [((symbols,), {}), ((), {"symbols": symbols}), ((ticker,), {})])
    payload.fundamentals = _try(
        "get_equity_fundamentals",
        [((ticker,), {}), ((symbols,), {}), ((), {"symbols": symbols})],
    )
    payload.tradability = _try(
        "get_equity_tradability",
        [((ticker,), {}), ((symbols,), {}), ((), {"symbols": symbols, "account_number": None})],
    )
    payload.search = _try(
        "search",
        [((ticker,), {}), ((), {"query": ticker, "asset_type": "instrument", "limit": 10}), ((ticker,), {"asset_type": "instrument", "limit": 10})],
    )
    payload.financials = _try(
        "get_financials",
        [
            ((), {"symbols": symbols, "period": "quarterly", "limit": 8}),
            ((symbols,), {"period": "quarterly", "limit": 8}),
            ((symbols,), {}),
        ],
    )
    payload.historicals = _try(
        "get_equity_historicals",
        [
            ((), {"symbols": symbols, "start_time": start_day, "interval": "day"}),
            ((symbols,), {"start_time": start_day, "interval": "day"}),
            ((symbols,), {"start_time": start_day}),
        ],
    )
    payload.news = _try(
        "get_equity_news",
        [((ticker,), {"limit": 15}), ((), {"symbol": ticker, "limit": 15}), ((ticker,), {})],
    )
    payload.earnings_results = _try(
        "get_earnings_results",
        [((ticker,), {}), ((), {"symbol": ticker})],
    )
    payload.earnings_calendar = _try(
        "get_earnings_calendar",
        [((), {"days": 14}), ((), {}), ((14,), {})],
    )
    payload.sec_index = _try(
        "get_sec_filing_index",
        [
            ((ticker,), {"form_type": ["10-K", "10-Q", "8-K"]}),
            ((), {"symbol": ticker, "form_type": ["10-K", "10-Q", "8-K"]}),
            ((ticker,), {}),
        ],
    )
    payload.price_book = _try(
        "get_equity_price_book",
        [((ticker,), {}), ((), {"symbol": ticker})],
    )
    payload.rsi = _technical(fetcher, ticker, start_day, "rsi", 14, attempted, observed, unavailable)
    if "get_equity_technical_indicators" in observed or _resolve(fetcher, "get_equity_technical_indicators")[1]:
        payload.sma_50 = _technical_followup(fetcher, ticker, start_day, "sma", 50)
        payload.sma_200 = _technical_followup(fetcher, ticker, start_day, "sma", 200)
        payload.atr = _technical_followup(fetcher, ticker, start_day, "atr", 14)

    if any(hasattr(fetcher, name) for name in ("get_equity_tradability", "get_equity_fundamentals", "tradability", "fundamentals")):
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


def _resolve(fetcher: Any, tool: str) -> tuple[str | None, Any]:
    for name in _ALIASES.get(tool, (tool,)):
        method = getattr(fetcher, name, None)
        if callable(method):
            return name, method
    return None, None


def _invoke(method: Any, shapes: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> Any:
    last: Exception | None = None
    for args, kwargs in shapes:
        try:
            return method(*args, **kwargs)
        except TypeError as exc:
            last = exc
            continue
    if last is not None:
        raise last
    return method()


def _technical(
    fetcher: Any,
    ticker: str,
    start_day: str,
    indicator: str,
    period: int,
    attempted: list[str],
    observed: list[str],
    unavailable: list[str],
) -> Mapping[str, Any] | None:
    tool = "get_equity_technical_indicators"
    attempted.append(tool)
    _name, method = _resolve(fetcher, tool)
    if method is None:
        unavailable.append(tool)
        return None
    try:
        result = _technical_call(method, ticker, start_day, indicator, period)
    except Exception:  # noqa: BLE001
        unavailable.append(tool)
        return None
    if result is None:
        unavailable.append(tool)
        return None
    observed.append(tool)
    return result if isinstance(result, Mapping) else {"data": result}


def _technical_followup(fetcher: Any, ticker: str, start_day: str, indicator: str, period: int) -> Mapping[str, Any] | None:
    _name, method = _resolve(fetcher, "get_equity_technical_indicators")
    if method is None:
        return None
    try:
        result = _technical_call(method, ticker, start_day, indicator, period)
    except Exception:  # noqa: BLE001
        return None
    if result is None:
        return None
    return result if isinstance(result, Mapping) else {"data": result}


def _technical_call(method: Any, ticker: str, start_day: str, indicator: str, period: int) -> Any:
    return _invoke(
        method,
        [
            ((), {"symbol": ticker, "indicator": indicator, "interval": "day", "start_time": start_day, "period": period}),
            ((ticker,), {"indicator": indicator, "interval": "day", "start_time": start_day}),
            ((ticker,), {"type": indicator, "interval": "day", "start_time": start_day, "period": period}),
            ((), {"symbol": ticker, "type": indicator, "interval": "day", "start_time": start_day, "period": period}),
        ],
    )
