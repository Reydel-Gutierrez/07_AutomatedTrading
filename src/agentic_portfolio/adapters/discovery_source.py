"""Broker-agnostic discovery source adapters.

Robinhood MCP is one implementation. A public-data source can implement the
same payload assembly later without rewriting Discovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from agentic_portfolio.adapters.robinhood_read import (
    RobinhoodSecurityBundle,
    adapt_classification_evidence,
    adapt_liquidity_evidence,
)
from agentic_portfolio.classification import classify
from agentic_portfolio.discovery.safety import DISCOVERY_FORBIDDEN_TOOLS, DISCOVERY_READ_TOOLS, assert_no_forbidden_tools
from agentic_portfolio.discovery.snapshot import SecuritySnapshot
from agentic_portfolio.schemas import MarketRegime, MarketRegimeStatus

SOURCE_ID_ROBINHOOD = "robinhood"
SOURCE_ID_PUBLIC = "public"


class DiscoveryFetcher(Protocol):
    """Injected read-only surface. Implementations must not wrap execution tools."""

    source_id: str

    def search(self, query: str, *, asset_type: str = "instrument", limit: int = 10) -> Mapping[str, Any] | None: ...
    def quotes(self, symbols: list[str]) -> Mapping[str, Any] | None: ...
    def fundamentals(self, symbols: list[str]) -> Mapping[str, Any] | None: ...
    def financials(self, symbols: list[str], *, period: str = "quarterly", limit: int = 8) -> Mapping[str, Any] | None: ...
    def historicals(self, symbols: list[str], *, start_time: str, interval: str = "day") -> Mapping[str, Any] | None: ...
    def technicals(self, symbol: str, *, indicator: str, interval: str, start_time: str) -> Mapping[str, Any] | None: ...
    def tradability(self, symbols: list[str]) -> Mapping[str, Any] | None: ...
    def earnings_calendar(self, *, days: int = 7) -> Mapping[str, Any] | None: ...
    def earnings_results(self, symbol: str) -> Mapping[str, Any] | None: ...
    def news(self, symbol: str, *, limit: int = 5) -> Mapping[str, Any] | None: ...
    def sec_index(self, symbol: str, *, form_type: list[str] | None = None) -> Mapping[str, Any] | None: ...
    def scans(self) -> Mapping[str, Any] | None: ...
    def run_scan(self, scan_id: str) -> Mapping[str, Any] | None: ...
    def watchlists(self) -> Mapping[str, Any] | None: ...
    def watchlist_items(self, list_id: str) -> Mapping[str, Any] | None: ...
    def popular_watchlists(self) -> Mapping[str, Any] | None: ...
    def indexes(self) -> Mapping[str, Any] | None: ...
    def index_quotes(self, instrument_ids: list[str]) -> Mapping[str, Any] | None: ...
    def portfolio(self) -> Mapping[str, Any] | None: ...
    def positions(self) -> Mapping[str, Any] | None: ...


@dataclass
class PublicDiscoverySource:
    """Placeholder for a future non-Robinhood feed. Same snapshot contract."""

    source_id: str = SOURCE_ID_PUBLIC


@dataclass
class SymbolPayloads:
    symbol: str
    sources: list[str] = field(default_factory=list)
    tradability: Mapping[str, Any] | None = None
    fundamentals: Mapping[str, Any] | None = None
    search: Mapping[str, Any] | None = None
    quotes: Mapping[str, Any] | None = None
    financials: Mapping[str, Any] | None = None
    historicals: Mapping[str, Any] | None = None
    rsi: Mapping[str, Any] | None = None
    sma_50: Mapping[str, Any] | None = None
    sma_200: Mapping[str, Any] | None = None
    earnings_results: Mapping[str, Any] | None = None
    news: Mapping[str, Any] | None = None
    sec_index: Mapping[str, Any] | None = None
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def assemble_snapshot(payload: SymbolPayloads, *, source_id: str = SOURCE_ID_ROBINHOOD) -> SecuritySnapshot:
    """Convert read-only payloads into a SecuritySnapshot. Does not call brokers."""
    ts = payload.observed_at
    bundle = RobinhoodSecurityBundle(
        symbol=payload.symbol,
        tradability=payload.tradability,
        fundamentals=payload.fundamentals,
        search=payload.search,
        quotes=payload.quotes,
        observed_at=ts,
        source_version=source_id,
    )
    evidence = adapt_classification_evidence(bundle)
    liquidity = adapt_liquidity_evidence(bundle)
    classification = classify(payload.symbol, evidence)
    classification.liquidity = liquidity

    fund = _first(payload.fundamentals)
    quote = _first(payload.quotes)
    trad = _first(payload.tradability)
    price = _f((quote or {}).get("last_trade_price") or (quote or {}).get("previous_close") or (fund or {}).get("open"))
    prev = _f((quote or {}).get("previous_close") or (fund or {}).get("previous_close"))
    high52 = _f((fund or {}).get("high_52_weeks") or (fund or {}).get("year_high"))
    low52 = _f((fund or {}).get("low_52_weeks") or (fund or {}).get("year_low"))
    avg_vol = _f(
        (fund or {}).get("average_volume_2_weeks")
        or (fund or {}).get("average_volume_30_days")
        or (fund or {}).get("average_volume")
    )
    volume = _f((fund or {}).get("volume") or (quote or {}).get("volume"))
    bars = _bars(payload.historicals, payload.symbol)
    rets = _returns_from_bars(bars)
    drawdown = None
    if high52 and price:
        drawdown = max(0.0, (high52 - price) / high52)

    fin = _financial_series(payload.financials, payload.symbol)
    surprise, upcoming = _earnings_fields(payload.earnings_results)
    headlines = _headlines(payload.news)
    going, dilution = _sec_flags(payload.sec_index)

    tradable = None
    state = None
    if trad:
        tradable = trad.get("tradeable")
        if tradable is None:
            tradable = str(trad.get("state") or "").lower() in {"active", "tradeable", "unhalted"}
        state = trad.get("state")

    refs = [f"{source_id}:{k}" for k in ("quotes", "fundamentals", "financials", "historicals") if getattr(payload, k)]
    return SecuritySnapshot(
        symbol=payload.symbol.upper(),
        observed_at=ts,
        sources=list(payload.sources) or [source_id],
        name=(trad or {}).get("name") or (fund or {}).get("description"),
        instrument_kind=evidence.instrument_kind,
        tradable=tradable if tradable is None else bool(tradable),
        trade_state=state,
        current_price=price,
        previous_close=prev,
        bid=_f((quote or {}).get("bid_price")),
        ask=_f((quote or {}).get("ask_price")),
        volume=volume,
        market_cap=_f((fund or {}).get("market_cap")),
        shares_outstanding=_f((fund or {}).get("shares_outstanding")),
        pe_ratio=_f((fund or {}).get("pe_ratio") or (fund or {}).get("pe")),
        pb_ratio=_f((fund or {}).get("pb_ratio") or (fund or {}).get("pb")),
        dividend_yield=_f((fund or {}).get("dividend_yield")),
        sector=(fund or {}).get("sector"),
        industry=(fund or {}).get("industry"),
        description=(fund or {}).get("description"),
        average_volume=avg_vol,
        high_52_week=high52,
        low_52_week=low52,
        revenue_periods=fin["revenue"],
        net_income_periods=fin["net_income"],
        net_margin_periods=fin["net_margin"],
        gross_profit_periods=fin["gross_profit"],
        rsi=_latest_indicator(payload.rsi),
        sma_50=_latest_indicator(payload.sma_50),
        sma_200=_latest_indicator(payload.sma_200),
        return_5d=rets.get(5),
        return_21d=rets.get(21),
        return_63d=rets.get(63),
        return_252d=rets.get(252),
        drawdown_from_52w_high=drawdown,
        volume_vs_avg=(volume / avg_vol) if volume and avg_vol else None,
        earnings_surprise_last=surprise,
        earnings_upcoming_days=upcoming,
        news_headlines=headlines,
        sec_going_concern=going,
        sec_dilution_flag=dilution,
        is_leveraged=evidence.is_leveraged,
        is_inverse=evidence.is_inverse,
        classification=classification,
        liquidity=liquidity,
        evidence_refs=refs,
    )


def observe_regime_from_spy_bars(
    bars: list[Mapping[str, Any]] | None,
    *,
    spy_price: float | None = None,
    observed_at: str | None = None,
    source: str = "get_equity_historicals:SPY",
) -> MarketRegime:
    """Minimal regime observation. Returns UNKNOWN unless bars actually exist."""
    ts = observed_at or datetime.now(timezone.utc).isoformat()
    if not bars or len(bars) < 50:
        return MarketRegime.unknown(observed_at=ts, reason="insufficient_spy_bars")
    closes: list[float] = []
    for b in bars:
        c = _f(b.get("close") or b.get("close_price"))
        if c:
            closes.append(c)
    if len(closes) < 50:
        return MarketRegime.unknown(observed_at=ts, reason="insufficient_spy_closes")
    sma50 = sum(closes[-50:]) / 50.0
    sma200 = sum(closes[-200:]) / 200.0 if len(closes) >= 200 else None
    last = spy_price or closes[-1]
    if sma200 is None:
        trend = "up" if last > sma50 else "down"
        return MarketRegime(
            status=MarketRegimeStatus.OBSERVED,
            trend=trend,
            spy_trend=trend,
            observed_at=ts,
            confidence="LOW",
            source=source,
            notes=["sma200_unavailable"],
        )
    trend = "up" if last > sma50 > sma200 else ("down" if last < sma50 < sma200 else "mixed")
    return MarketRegime(
        status=MarketRegimeStatus.OBSERVED,
        trend=trend,
        spy_trend=trend,
        observed_at=ts,
        confidence="LOW",
        source=source,
        notes=["heuristic_sma_alignment_not_a_full_regime_engine"],
    )


def symbols_from_search(payload: Mapping[str, Any] | None) -> list[str]:
    out: list[str] = []
    for item in _results(payload):
        sym = str(item.get("symbol") or "").upper()
        if sym and item.get("asset_type") in (None, "instrument", "equity", "etf"):
            out.append(sym)
    return out


def symbols_from_scan(payload: Mapping[str, Any] | None) -> list[str]:
    out: list[str] = []
    data = _data(payload)
    rows = data.get("results") or data.get("rows") or data.get("instruments") or []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                sym = str(row.get("symbol") or row.get("ticker") or "").upper()
                if sym:
                    out.append(sym)
    return out


def symbols_from_watchlist_items(payload: Mapping[str, Any] | None) -> list[str]:
    out: list[str] = []
    for item in _results(payload):
        otype = str(item.get("object_type") or item.get("type") or "").lower()
        if otype in {"crypto", "currency_pair", "option", "future"}:
            continue
        sym = str(item.get("symbol") or "").upper()
        if sym:
            out.append(sym)
    return out


def symbols_from_earnings_calendar(payload: Mapping[str, Any] | None) -> list[tuple[str, int | None]]:
    out: list[tuple[str, int | None]] = []
    for item in _results(payload):
        sym = str(item.get("symbol") or "").upper()
        if not sym:
            continue
        days = item.get("days_until") or item.get("days")
        try:
            days_i = int(days) if days is not None else None
        except (TypeError, ValueError):
            days_i = None
        out.append((sym, days_i))
    return out


def _first(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return None
    data = payload.get("data", payload) if isinstance(payload, Mapping) else None
    if not isinstance(data, dict):
        return None
    results = data.get("results")
    if isinstance(results, list) and results:
        item = results[0]
        if isinstance(item, dict) and "quote" in item and len(item) <= 3:
            q = item.get("quote")
            return dict(q) if isinstance(q, dict) else dict(item)
        return dict(item) if isinstance(item, dict) else None
    if "symbol" in data or "name" in data or "last_trade_price" in data:
        return dict(data)
    return None


def _data(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    data = payload.get("data", payload)
    return dict(data) if isinstance(data, dict) else {}


def _results(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    data = _data(payload)
    results = data.get("results") or data.get("items") or data.get("events") or data.get("articles") or []
    if isinstance(results, list):
        return [r for r in results if isinstance(r, dict)]
    return []


def _f(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _bars(payload: Mapping[str, Any] | None, symbol: str) -> list[dict[str, Any]]:
    if not payload:
        return []
    data = _data(payload)
    results = data.get("results") or data.get("historicals") or data.get("data_points") or []
    if isinstance(results, dict):
        results = results.get(symbol.upper()) or results.get("data_points") or []
    if not isinstance(results, list):
        return []
    out = []
    for item in results:
        if isinstance(item, dict):
            if "symbol" in item and str(item.get("symbol", "")).upper() != symbol.upper():
                inner = item.get("historicals") or item.get("data_points") or []
                if isinstance(inner, list):
                    out.extend(x for x in inner if isinstance(x, dict))
                    continue
            out.append(item)
    return out


def _returns_from_bars(bars: list[dict[str, Any]]) -> dict[int, float | None]:
    closes: list[float] = []
    for b in bars:
        c = _f(b.get("close") or b.get("close_price") or b.get("close_price_last"))
        if c:
            closes.append(c)
    out: dict[int, float | None] = {5: None, 21: None, 63: None, 252: None}
    if not closes:
        return out
    last = closes[-1]
    for n in (5, 21, 63, 252):
        if len(closes) > n and closes[-1 - n]:
            out[n] = (last / closes[-1 - n]) - 1.0
    return out


def _latest_indicator(payload: Mapping[str, Any] | None) -> float | None:
    if not payload:
        return None
    data = _data(payload)
    if "value" in data:
        return _f(data.get("value"))
    indicators = data.get("indicators")
    if isinstance(indicators, list) and indicators:
        series = indicators[0].get("series") if isinstance(indicators[0], dict) else None
        if isinstance(series, list) and series:
            last = series[-1]
            if isinstance(last, dict):
                return _f(last.get("value") or last.get("sma") or last.get("ema") or last.get("rsi"))
    results = data.get("results") or data.get("series") or data.get("data_points") or []
    if isinstance(results, list) and results:
        last = results[-1]
        if isinstance(last, dict):
            return _f(last.get("value") or last.get("sma") or last.get("ema") or last.get("rsi") or last.get("close"))
        return _f(last)
    return None


def _financial_series(payload: Mapping[str, Any] | None, symbol: str) -> dict[str, list[float]]:
    empty: dict[str, list[float]] = {"revenue": [], "net_income": [], "net_margin": [], "gross_profit": []}
    if not payload:
        return empty
    rows = _results(payload)
    if not rows:
        data = _data(payload)
        maybe = data.get(symbol.upper()) or data.get("financials")
        if isinstance(maybe, list):
            rows = [r for r in maybe if isinstance(r, dict)]
        elif isinstance(maybe, dict):
            rows = [maybe]
    # newest-first if dated
    def _key(r: dict) -> str:
        return str(r.get("period") or r.get("end_date") or r.get("fiscal_period") or "")

    rows = sorted(rows, key=_key, reverse=True) if any(_key(r) for r in rows) else rows
    unwrapped: list[dict[str, Any]] = []
    for r in rows:
        inner = r.get("financials")
        if isinstance(inner, list):
            for item in inner:
                if isinstance(item, dict):
                    row = dict(item)
                    row.setdefault("symbol", r.get("symbol") or symbol)
                    unwrapped.append(row)
        else:
            unwrapped.append(r)
    rows = unwrapped
    rows = sorted(rows, key=_key, reverse=True) if any(_key(r) for r in rows) else rows
    for r in rows:
        if str(r.get("symbol") or symbol).upper() not in {symbol.upper(), ""}:
            if r.get("symbol") and str(r.get("symbol")).upper() != symbol.upper():
                continue
        rev = _f(r.get("revenue") or r.get("total_revenue") or r.get("Revenues"))
        ni = _f(r.get("net_income") or r.get("netIncome") or r.get("NetIncomeLoss"))
        gp = _f(r.get("gross_profit") or r.get("grossProfit"))
        nm = _f(r.get("net_margin") or r.get("netMargin"))
        if nm is not None and abs(nm) > 1.5:
            # MCP get_financials returns net_margin as a percentage.
            nm = nm / 100.0
        if nm is None and rev and ni is not None and rev != 0:
            nm = ni / rev
        if rev is not None:
            empty["revenue"].append(rev)
        if ni is not None:
            empty["net_income"].append(ni)
        if nm is not None:
            empty["net_margin"].append(nm)
        if gp is not None:
            empty["gross_profit"].append(gp)
    return empty


def _earnings_fields(payload: Mapping[str, Any] | None) -> tuple[float | None, int | None]:
    rows = _results(payload)
    surprise = None
    upcoming = None
    now = datetime.now(timezone.utc).date()
    for r in rows:
        eps = r.get("eps") if isinstance(r.get("eps"), dict) else {}
        actual = _f(
            (eps or {}).get("actual")
            or r.get("actual_eps")
            or r.get("eps_actual")
            or r.get("actual")
        )
        est = _f(
            (eps or {}).get("estimate")
            or r.get("estimated_eps")
            or r.get("eps_estimate")
            or r.get("estimate")
        )
        if surprise is None and actual is not None and est not in (None, 0):
            surprise = (actual - est) / abs(est)
        report = r.get("report") if isinstance(r.get("report"), dict) else {}
        date_s = (report or {}).get("date") or r.get("report_date") or r.get("date")
        if date_s and upcoming is None:
            try:
                d = datetime.fromisoformat(str(date_s)[:10]).date()
                delta = (d - now).days
                if delta >= 0:
                    upcoming = delta
            except ValueError:
                pass
    return surprise, upcoming


def _headlines(payload: Mapping[str, Any] | None) -> list[str]:
    out: list[str] = []
    for item in _results(payload):
        t = item.get("title") or item.get("headline") or item.get("summary")
        if t:
            out.append(str(t))
    return out[:8]


def _sec_flags(payload: Mapping[str, Any] | None) -> tuple[bool | None, bool | None]:
    if not payload:
        return None, None
    blob = str(payload).lower()
    going = "going concern" in blob or "substantial doubt" in blob
    dilution = "dilution" in blob or "atm offering" in blob or "s-3" in blob
    if not going and not dilution:
        return None, None
    return (True if going else None), (True if dilution else None)


# Re-export so callers can see the allowed/forbidden split.
ALLOWED_DISCOVERY_TOOLS = DISCOVERY_READ_TOOLS
FORBIDDEN_DISCOVERY_TOOLS = DISCOVERY_FORBIDDEN_TOOLS
