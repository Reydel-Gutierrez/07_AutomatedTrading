"""Assemble a ResearchEvidencePacket from read-only payloads.

Python owns facts and derived metrics. The reasoner does not collect data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol
from uuid import uuid4

from agentic_portfolio.adapters.discovery_source import (
    SymbolPayloads,
    assemble_snapshot,
)
from agentic_portfolio.adapters.robinhood_read import (
    RobinhoodSecurityBundle,
    adapt_classification_evidence,
    adapt_liquidity_evidence,
)
from agentic_portfolio.classification import classify
from agentic_portfolio.policy import load_policy, load_research_config
from agentic_portfolio.research.filings import facts_from_sec, structured_filing_meta
from agentic_portfolio.discovery.snapshot import compute_spread_metrics
from agentic_portfolio.research.metrics import (
    as_float,
    drawdown,
    fcf_yield,
    period_growth,
    ratio,
    share_change,
    sma_alignment,
    trailing_growth,
)
from agentic_portfolio.research.safety import RESEARCH_READ_TOOLS, assert_no_forbidden_tools
from agentic_portfolio.research.types import (
    EvidenceItem,
    EvidenceKind,
    FrozenClassification,
    FrozenPortfolioFacts,
    FrozenRiskLimits,
    ResearchEvidencePacket,
    ResearchSubjectKind,
)
from agentic_portfolio.schemas import (
    Candidate,
    ClassificationResult,
    PortfolioContext,
    Sleeve,
    to_dict,
)


class ResearchFetcher(Protocol):
    """Injected read-only surface. Implementations must not wrap execution tools."""

    source_id: str

    def quotes(self, symbols: list[str]) -> Mapping[str, Any] | None: ...
    def fundamentals(self, symbols: list[str]) -> Mapping[str, Any] | None: ...
    def financials(self, symbols: list[str], *, period: str = "quarterly", limit: int = 8) -> Mapping[str, Any] | None: ...
    def historicals(self, symbols: list[str], *, start_time: str, interval: str = "day") -> Mapping[str, Any] | None: ...
    def technicals(self, symbol: str, *, indicator: str, interval: str, start_time: str, period: int | None = None) -> Mapping[str, Any] | None: ...
    def tradability(self, symbols: list[str]) -> Mapping[str, Any] | None: ...
    def earnings_results(self, symbol: str) -> Mapping[str, Any] | None: ...
    def earnings_calendar(self, *, days: int = 14) -> Mapping[str, Any] | None: ...
    def news(self, symbol: str, *, limit: int = 15) -> Mapping[str, Any] | None: ...
    def sec_index(self, symbol: str, *, form_type: list[str] | None = None) -> Mapping[str, Any] | None: ...
    def sec_filing(self, filing_id: str, *, section: str | None = None) -> Mapping[str, Any] | None: ...
    def sec_facts(self, filing_ids: list[str], concepts: list[str]) -> Mapping[str, Any] | None: ...
    def price_book(self, symbol: str) -> Mapping[str, Any] | None: ...
    def index_quotes(self, instrument_ids: list[str]) -> Mapping[str, Any] | None: ...
    def portfolio(self) -> Mapping[str, Any] | None: ...
    def positions(self) -> Mapping[str, Any] | None: ...
    def search(self, query: str, *, asset_type: str = "instrument", limit: int = 5) -> Mapping[str, Any] | None: ...


@dataclass
class PublicResearchSource:
    """Placeholder for a future non-Robinhood feed. Same packet contract."""

    source_id: str = "public"


@dataclass
class ResearchPayload:
    """Pre-fetched read-only payloads for one research subject."""

    symbol: str
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    sources_attempted: list[str] = field(default_factory=list)
    sources_observed: list[str] = field(default_factory=list)
    sources_unavailable: list[str] = field(default_factory=list)
    tradability: Mapping[str, Any] | None = None
    fundamentals: Mapping[str, Any] | None = None
    search: Mapping[str, Any] | None = None
    quotes: Mapping[str, Any] | None = None
    financials: Mapping[str, Any] | None = None
    historicals: Mapping[str, Any] | None = None
    rsi: Mapping[str, Any] | None = None
    sma_50: Mapping[str, Any] | None = None
    sma_200: Mapping[str, Any] | None = None
    atr: Mapping[str, Any] | None = None
    earnings_results: Mapping[str, Any] | None = None
    earnings_calendar: Mapping[str, Any] | None = None
    news: Mapping[str, Any] | None = None
    sec_index: Mapping[str, Any] | None = None
    sec_sections: list[Mapping[str, Any]] = field(default_factory=list)
    sec_facts: Mapping[str, Any] | None = None
    price_book: Mapping[str, Any] | None = None
    index_quotes: Mapping[str, Any] | None = None
    classification: ClassificationResult | None = None


def freeze_portfolio(context: PortfolioContext) -> FrozenPortfolioFacts:
    return FrozenPortfolioFacts(
        current_nav=context.current_nav,
        cash=context.cash,
        buying_power=context.buying_power,
        cash_allocation_pct=context.cash_allocation_pct,
        holdings_count=context.holdings_count,
        positions=[to_dict(p) for p in context.positions],
        sleeve_allocation_pct=dict(context.sleeve_allocation_pct),
        sector_allocation_pct=dict(context.sector_allocation_pct),
        risk_state=context.risk_state.value if context.risk_state else None,
        high_water_mark=context.high_water_mark,
        current_drawdown=context.current_drawdown,
        daily_risk_halt=bool(context.daily_risk_halt),
        existing_thesis_ids=[p.thesis_id for p in context.positions if p.thesis_id],
    )


def freeze_risk_limits(policy: dict | None = None) -> FrozenRiskLimits:
    pol = policy or load_policy()
    conc = pol.get("concentration") or pol.get("position_limits") or {}
    return FrozenRiskLimits(source="config/portfolio_policy.json", limits=dict(conc) if isinstance(conc, dict) else {"raw": conc})


def freeze_classification(result: ClassificationResult | None, cand: Candidate | None) -> FrozenClassification:
    if result:
        return FrozenClassification(
            security_class=result.security_class.value,
            classification_status=result.status.value,
            sector=result.sector.value if result.sector else None,
            reasons=list(result.reasons),
        )
    if cand:
        return FrozenClassification(
            security_class=cand.security_class.value if cand.security_class else None,
            classification_status=cand.classification_status.value if cand.classification_status else None,
            sector=cand.sector,
            industry=cand.industry,
        )
    return FrozenClassification()


def build_packet(
    payload: ResearchPayload,
    candidate: Candidate,
    context: PortfolioContext,
    *,
    subject_kind: ResearchSubjectKind = ResearchSubjectKind.NEW_CANDIDATE,
    config: dict | None = None,
    policy: dict | None = None,
    comparison_peer_symbols: list[str] | None = None,
    existing_thesis_id: str | None = None,
) -> ResearchEvidencePacket:
    """Normalize observed payloads into facts + derived metrics."""
    cfg = config or load_research_config()
    assert_no_forbidden_tools(payload.sources_attempted)
    ts = payload.observed_at
    facts: list[EvidenceItem] = []
    derived: list[EvidenceItem] = []

    snap_payload = SymbolPayloads(
        symbol=payload.symbol,
        sources=list(payload.sources_observed),
        tradability=payload.tradability,
        fundamentals=payload.fundamentals,
        search=payload.search,
        quotes=payload.quotes,
        financials=payload.financials,
        historicals=payload.historicals,
        rsi=payload.rsi,
        sma_50=payload.sma_50,
        sma_200=payload.sma_200,
        earnings_results=payload.earnings_results,
        news=payload.news,
        sec_index=payload.sec_index,
        observed_at=ts,
    )
    snap = assemble_snapshot(snap_payload)
    cls = payload.classification or snap.classification
    if cls is None and (payload.fundamentals or payload.tradability):
        bundle = RobinhoodSecurityBundle(
            symbol=payload.symbol,
            tradability=payload.tradability,
            fundamentals=payload.fundamentals,
            search=payload.search,
            quotes=payload.quotes,
            observed_at=ts,
        )
        ev = adapt_classification_evidence(bundle, policy)
        liq = adapt_liquidity_evidence(bundle)
        cls = classify(payload.symbol, ev, policy)
        cls.liquidity = liq

    def fact(name: str, value: Any, source: str, data_type: str = "number", raw_ref: str | None = None, notes: list[str] | None = None) -> None:
        if value is None or value == "" or value == []:
            return
        facts.append(
            EvidenceItem(
                evidence_id=f"fact:{name}",
                kind=EvidenceKind.OBSERVED_FACT,
                name=name,
                value=value,
                source=source,
                observed_at=ts,
                data_type=data_type,
                raw_ref=raw_ref or source,
                freshness="FRESH",
                notes=notes or [],
            )
        )

    def deriv(name: str, value: Any, source: str, data_type: str = "number", notes: list[str] | None = None) -> None:
        if value is None or value == "":
            return
        derived.append(
            EvidenceItem(
                evidence_id=f"derived:{name}",
                kind=EvidenceKind.DETERMINISTIC_DERIVED_METRIC,
                name=name,
                value=value,
                source=source,
                observed_at=ts,
                data_type=data_type,
                raw_ref=source,
                derived=True,
                freshness="FRESH",
                notes=notes or [],
            )
        )

    fact("symbol", payload.symbol.upper(), "candidate", "string")
    fact("market_price", snap.current_price, "get_equity_quotes", raw_ref="last_trade_price")
    fact("previous_close", snap.previous_close, "get_equity_quotes")
    fact("bid", snap.bid, "get_equity_quotes")
    fact("ask", snap.ask, "get_equity_quotes")
    fact("bid_price", snap.bid, "get_equity_quotes.bid_price")
    fact("ask_price", snap.ask, "get_equity_quotes.ask_price")
    fact("volume", snap.volume, "get_equity_quotes")
    fact("market_cap", snap.market_cap, "get_equity_fundamentals")
    fact("shares_outstanding", snap.shares_outstanding, "get_equity_fundamentals")
    fact("pe_ratio", snap.pe_ratio, "get_equity_fundamentals")
    fact("pb_ratio", snap.pb_ratio, "get_equity_fundamentals")
    fact("dividend_yield", snap.dividend_yield, "get_equity_fundamentals")
    fact("sector_label_raw", snap.sector, "get_equity_fundamentals", "string")
    fact("industry_label_raw", snap.industry, "get_equity_fundamentals", "string")
    fact("description", snap.description, "get_equity_fundamentals", "text")
    fact("legal_name", snap.name, "get_equity_tradability", "string")
    fact("high_52_week", snap.high_52_week, "get_equity_fundamentals")
    fact("low_52_week", snap.low_52_week, "get_equity_fundamentals")
    fact("average_volume", snap.average_volume, "get_equity_fundamentals")
    fact("tradable", snap.tradable, "get_equity_tradability", "boolean")
    fact("revenue_periods", snap.revenue_periods, "get_financials", "series")
    fact("net_income_periods", snap.net_income_periods, "get_financials", "series")
    fact("net_margin_periods", snap.net_margin_periods, "get_financials", "series")
    fact("gross_profit_periods", snap.gross_profit_periods, "get_financials", "series")
    fact("rsi", snap.rsi, "get_equity_technical_indicators")
    fact("sma_50", snap.sma_50, "get_equity_technical_indicators")
    fact("sma_200", snap.sma_200, "get_equity_technical_indicators")
    if payload.atr:
        fact("atr", _latest_num(payload.atr), "get_equity_technical_indicators")
    fact("earnings_surprise_last", snap.earnings_surprise_last, "get_earnings_results")
    fact("earnings_upcoming_days", snap.earnings_upcoming_days, "get_earnings_results", "integer")
    fact("news_headlines", list(snap.news_headlines), "get_equity_news", "series")
    for i, h in enumerate(snap.news_headlines):
        fact(f"news_headline.{i}", h, "get_equity_news", "string", raw_ref=f"news[{i}]")
    news_items = _news_items(payload.news)
    if news_items:
        fact("news_items", news_items, "get_equity_news", "object")
    earnings_rows = _earnings_rows(payload.earnings_results)
    if earnings_rows:
        fact("earnings_history", earnings_rows, "get_earnings_results", "object")
    if payload.price_book:
        fact("price_book_present", True, "get_equity_price_book", "boolean")
        pb = _first(payload.price_book)
        if pb:
            fact("price_book_bid", as_float(pb.get("bid_price") or pb.get("bid")), "get_equity_price_book")
            fact("price_book_ask", as_float(pb.get("ask_price") or pb.get("ask")), "get_equity_price_book")

    fund = _first(payload.fundamentals) or {}
    fact("forward_pe", as_float(fund.get("forward_pe") or fund.get("forward_pe_ratio") or fund.get("pe_forward")), "get_equity_fundamentals")
    fact("price_to_sales", as_float(fund.get("price_to_sales") or fund.get("ps_ratio") or fund.get("price_sales")), "get_equity_fundamentals")
    fact("enterprise_value", as_float(fund.get("enterprise_value") or fund.get("ev")), "get_equity_fundamentals")
    fact("ebitda", as_float(fund.get("ebitda")), "get_equity_fundamentals")
    fact("free_cash_flow", as_float(fund.get("free_cash_flow") or fund.get("fcf")), "get_equity_fundamentals")
    fact("total_debt", as_float(fund.get("total_debt") or fund.get("debt")), "get_equity_fundamentals")
    fact("total_cash", as_float(fund.get("total_cash") or fund.get("cash") or fund.get("cash_and_equivalents")), "get_equity_fundamentals")
    fact("float", as_float(fund.get("float")), "get_equity_fundamentals")
    fact("shares_float", as_float(fund.get("float") or fund.get("shares_float")), "get_equity_fundamentals")

    facts.extend(structured_filing_meta(payload.sec_index, observed_at=ts, sections=list(payload.sec_sections)))
    facts.extend(facts_from_sec(payload.sec_facts, observed_at=ts))

    deriv("revenue_growth_qoq", period_growth(snap.revenue_periods, periods=1), "derived:get_financials.revenue")
    deriv("revenue_growth_span", trailing_growth(snap.revenue_periods), "derived:get_financials.revenue")
    deriv("earnings_growth_qoq", period_growth(snap.net_income_periods, periods=1), "derived:get_financials.net_income")
    deriv("earnings_growth_span", trailing_growth(snap.net_income_periods), "derived:get_financials.net_income")
    deriv("net_margin_latest", snap.net_margin_periods[0] if snap.net_margin_periods else None, "derived:get_financials.net_margin")
    deriv("gross_margin_latest", ratio(snap.gross_profit_periods[0], snap.revenue_periods[0]) if snap.gross_profit_periods and snap.revenue_periods else None, "derived:get_financials.gross_profit/revenue")
    deriv("return_5d", snap.return_5d, "derived:get_equity_historicals")
    deriv("return_21d", snap.return_21d, "derived:get_equity_historicals")
    deriv("return_63d", snap.return_63d, "derived:get_equity_historicals")
    deriv("return_252d", snap.return_252d, "derived:get_equity_historicals")
    deriv("drawdown_from_52w_high", snap.drawdown_from_52w_high or drawdown(snap.current_price, snap.high_52_week), "derived:price/52w_high")
    deriv("volume_vs_avg", snap.volume_vs_avg, "derived:volume/average_volume")
    deriv("sma_alignment", sma_alignment(snap.current_price, snap.sma_50, snap.sma_200), "derived:price/sma", data_type="string")
    ev = as_float(fund.get("enterprise_value") or fund.get("ev"))
    ebitda = as_float(fund.get("ebitda"))
    rev0 = snap.revenue_periods[0] if snap.revenue_periods else None
    deriv("ev_ebitda", ratio(ev, ebitda), "derived:ev/ebitda")
    deriv("ev_revenue", ratio(ev, rev0), "derived:ev/revenue")
    deriv("fcf_yield", fcf_yield(as_float(fund.get("free_cash_flow") or fund.get("fcf")), snap.market_cap), "derived:fcf/market_cap")
    deriv("spread_pct", snap.spread_pct, "derived:bid_ask", notes=["unit=fraction", "formula=(ask-bid)/midpoint", "internal_eligibility_field"])
    spread = compute_spread_metrics(snap.bid, snap.ask)
    if spread:
        deriv("absolute_spread_usd", spread["absolute_spread_usd"], "derived:ask-bid", notes=["unit=usd", "formula=ask-bid"])
        deriv("spread_percent", spread["spread_percent"], "derived:(ask-bid)/midpoint", notes=["unit=fraction", "formula=(ask-bid)/midpoint", "0.0193_means_1.93_percent_not_1.93_dollars"])
        deriv("spread_bps", spread["spread_bps"], "derived:spread_percent*10000", notes=["unit=bps", "formula=spread_percent*10000"])
    elif snap.spread_pct is not None:
        deriv("spread_percent", snap.spread_pct, "derived:bid_ask", notes=["unit=fraction", "formula=(ask-bid)/midpoint", "0.0193_means_1.93_percent_not_1.93_dollars"])
        deriv("spread_bps", snap.spread_pct * 10000.0, "derived:spread_percent*10000", notes=["unit=bps", "formula=spread_percent*10000"])
    if snap.shares_outstanding and fund.get("shares_outstanding_prior"):
        deriv("share_count_change", share_change(snap.shares_outstanding, as_float(fund.get("shares_outstanding_prior"))), "derived:shares_outstanding")
    debt = as_float(fund.get("total_debt") or fund.get("debt"))
    assets = None
    for item in facts:
        if item.name == "sec_fact.Assets" and as_float(item.value) is not None:
            assets = as_float(item.value)
            break
    deriv("debt_to_assets", ratio(debt, assets), "derived:debt/assets")

    missing: list[str] = []
    optional_gaps: list[str] = []
    cls_value = cls.security_class.value if cls and cls.security_class else None
    is_etf = bool(cls_value and "ETF" in cls_value)

    if snap.current_price is None:
        missing.append("market_price")
    if not (snap.name or snap.tradable is not None):
        missing.append("identity")
    substance = bool(
        snap.revenue_periods
        or snap.pe_ratio is not None
        or snap.market_cap is not None
        or snap.description
        or snap.sector
    )
    if is_etf and not (snap.description or snap.name or snap.market_cap):
        missing.append("etf_mandate_or_description")
    elif not is_etf and not substance:
        missing.append("fundamentals_or_financials")

    if not snap.revenue_periods and not is_etf:
        optional_gaps.append("financials.revenue")
    if snap.pe_ratio is None and not is_etf:
        optional_gaps.append("valuation.pe_ratio")
    if not snap.news_headlines:
        optional_gaps.append("news")
    if payload.sec_index is None:
        optional_gaps.append("sec_filings")
    for src in payload.sources_unavailable:
        label = f"source_unavailable:{src}"
        if src in {"get_equity_quotes", "get_equity_fundamentals", "get_equity_tradability"}:
            missing.append(label)
        else:
            optional_gaps.append(label)

    completeness = "COMPLETE"
    if snap.current_price is None or any(
        item in missing for item in ("market_price", "identity", "fundamentals_or_financials", "etf_mandate_or_description")
    ):
        completeness = "INCOMPLETE"
    elif optional_gaps:
        completeness = "PARTIAL"

    missing_information = list(dict.fromkeys([*missing, *optional_gaps]))

    sleeve = candidate.provisional_sleeve
    questions = list(cfg.get("sleeve_research_questions", {}).get(sleeve.value, []))
    tech_weight = (cfg.get("technical_weight") or {}).get(sleeve.value)
    investment_q = {
        Sleeve.CORE_GROWTH: "Is this opportunity attractive enough as a long-term compounding holding versus cash and broad-market exposure?",
        Sleeve.OPPORTUNISTIC: "Is the market price a temporary dislocation or deserved deterioration, and is the opportunity attractive enough to research further?",
        Sleeve.TACTICAL: "Is there a specific, time-bounded setup with defined confirmation and invalidation?",
        Sleeve.SPECULATIVE: "Is the payoff actually asymmetric after survival, dilution, and catalyst-failure risk?",
    }.get(sleeve, "Is the opportunity attractive enough to justify an investment thesis?")

    sources_obs = list(dict.fromkeys(payload.sources_observed))
    sources_unavail = list(dict.fromkeys(payload.sources_unavailable))
    allowed = set(RESEARCH_READ_TOOLS)
    sources_obs = [s for s in sources_obs if s in allowed or s.startswith("derived") or s in {"candidate", "test"}]
    # Keep attempted read names even if not in the allow-list alias (e.g. test).
    if payload.sources_observed and not sources_obs:
        sources_obs = list(payload.sources_observed)

    return ResearchEvidencePacket(
        packet_id=str(uuid4()),
        candidate_id=candidate.candidate_id,
        symbol=payload.symbol.upper(),
        assembled_at=ts,
        subject_kind=subject_kind,
        provisional_sleeve=sleeve,
        facts=facts,
        derived_metrics=derived,
        sources_observed=list(payload.sources_observed),
        sources_unavailable=sources_unavail,
        classification=freeze_classification(cls, candidate),
        portfolio_facts=freeze_portfolio(context),
        risk_limits=freeze_risk_limits(policy),
        sleeve_research_questions=questions,
        policy_context={
            "research_interprets_evidence": True,
            "portfolio_decision_allocates": True,
            "risk_gate_permits": True,
            "execution_gated_off": True,
            "no_universal_pe_or_growth_rules": True,
            "technical_weight": tech_weight,
            "subject_kind": subject_kind.value,
        },
        missing_information=missing_information,
        comparison_group_id=candidate.comparison_group_id,
        comparison_peer_symbols=list(comparison_peer_symbols or []),
        discovery_score=candidate.discovery_score,
        technical_weight=tech_weight,
        investment_question=investment_q,
        existing_thesis_id=existing_thesis_id,
        completeness=completeness,
    )


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
    return dict(data)


def _latest_num(payload: Mapping[str, Any] | None) -> float | None:
    data = payload.get("data", payload) if isinstance(payload, Mapping) else payload
    if not isinstance(data, dict):
        return as_float(data)
    if "value" in data:
        return as_float(data.get("value"))
    rows = data.get("results") or data.get("series") or data.get("data_points") or []
    if isinstance(rows, list) and rows:
        last = rows[-1]
        if isinstance(last, dict):
            return as_float(last.get("value") or last.get("atr") or last.get("close"))
        return as_float(last)
    return None


def _news_items(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not payload:
        return []
    data = payload.get("data", payload) if isinstance(payload, Mapping) else {}
    if not isinstance(data, dict):
        return []
    rows = data.get("results") or data.get("news") or data.get("articles") or []
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "title": row.get("title") or row.get("headline"),
                "published_at": row.get("published_at") or row.get("date") or row.get("created_at"),
                "source": row.get("source") or row.get("publisher"),
                "url": row.get("url") or row.get("link"),
                "summary": row.get("summary") or row.get("preview"),
            }
        )
    return out[:20]


def _earnings_rows(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not payload:
        return []
    data = payload.get("data", payload) if isinstance(payload, Mapping) else {}
    if not isinstance(data, dict):
        return []
    rows = data.get("results") or data.get("earnings") or []
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        eps = row.get("eps") if isinstance(row.get("eps"), dict) else {}
        report = row.get("report") if isinstance(row.get("report"), dict) else {}
        out.append(
            {
                "report_date": (report or {}).get("date") or row.get("report_date") or row.get("date"),
                "actual_eps": as_float((eps or {}).get("actual") or row.get("actual_eps") or row.get("eps_actual") or row.get("actual")),
                "estimated_eps": as_float((eps or {}).get("estimate") or row.get("estimated_eps") or row.get("eps_estimate") or row.get("estimate")),
                "timing": (report or {}).get("timing") or row.get("timing") or row.get("hour"),
            }
        )
    return out
