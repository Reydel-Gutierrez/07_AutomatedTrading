"""Deterministic evidence-sufficiency check. Runs before a paid Terra call.

Optional gaps (news, SEC excerpts, technicals) are recorded for the reasoner.
They must not by themselves consume the research budget or force NEED_MORE_DATA.

Broad-market / diversified ETFs use mandate, price, and identity as core
evidence. Company financial statements are not required for funds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from agentic_portfolio.research.types import ResearchEvidencePacket, ResearchReport
from agentic_portfolio.schemas import Candidate, SecurityClass


CORE_SOURCES = frozenset(
    {
        "get_equity_quotes",
        "get_equity_fundamentals",
        "get_equity_tradability",
        "search",
    }
)

OPTIONAL_SOURCES = frozenset(
    {
        "get_equity_news",
        "get_sec_filing_index",
        "get_sec_filing",
        "get_sec_filing_facts",
        "get_sec_filing_facts_catalog",
        "get_equity_technical_indicators",
        "get_earnings_calendar",
        "get_earnings_results",
        "get_equity_historicals",
        "get_equity_price_book",
        "get_index_quotes",
        "get_index_historicals",
        "get_indexes",
        "get_financials",
    }
)

# Company-issuer feeds. Missing these is not a core ETF gap.
ETF_IRRELEVANT_OPTIONAL_SOURCES = frozenset(
    {
        "get_financials",
        "get_sec_filing_index",
        "get_sec_filing",
        "get_sec_filing_facts",
        "get_sec_filing_facts_catalog",
        "get_earnings_results",
        "get_earnings_calendar",
    }
)

COMPANY_EVIDENCE_GAPS = frozenset(
    {
        "financials.revenue",
        "fundamentals_or_financials",
        "valuation.pe_ratio",
        "get_financials",
        "source:get_financials",
        "source_unavailable:get_financials",
        "sec_filings",
        "source_unavailable:get_sec_filing_index",
        "source_unavailable:get_sec_filing",
        "source_unavailable:get_sec_filing_facts",
    }
)

# Collector-repair requeue targets. Seed tickers are not a classification ceiling.
BROAD_MARKET_REPAIR_SYMBOLS = frozenset({"SPY", "VTI", "VOO"})

_ETF_NAME = re.compile(r"\betf\b|\betn\b|exchange[- ]traded", re.I)


@dataclass
class EvidenceSufficiency:
    sufficient: bool
    missing_core: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "sufficient": self.sufficient,
            "missing_core": list(self.missing_core),
            "missing_optional": list(self.missing_optional),
            "reason": self.reason,
        }


def evaluate_evidence_sufficiency(packet: ResearchEvidencePacket) -> EvidenceSufficiency:
    """True when core observed facts exist for a legitimate investment conclusion.

    Equities: market price, identity, and some fundamental/quality substance
    (financials, valuation multiples, description).
    ETFs: market price plus mandate/description (AUM/net assets when present).
    Company financials are not core for funds.
    Optional: news, SEC, technicals, earnings calendar, historicals.
    """
    facts = {item.name: item.value for item in packet.facts}
    derived = {item.name: item.value for item in packet.derived_metrics}
    is_etf = packet_is_etf(packet)

    missing_core: list[str] = []
    price = facts.get("market_price")
    if price is None:
        missing_core.append("market_price")

    identity = facts.get("legal_name")
    tradable = facts.get("tradable")
    if not identity and tradable is None and not (packet.classification and packet.classification.security_class):
        missing_core.append("identity")

    substance = any(
        facts.get(name) not in (None, "", [])
        for name in (
            "revenue_periods",
            "net_income_periods",
            "pe_ratio",
            "pb_ratio",
            "market_cap",
            "description",
            "sector_label_raw",
            "forward_pe",
            "free_cash_flow",
        )
    ) or any(derived.get(name) is not None for name in ("revenue_growth_qoq", "revenue_growth_span", "fcf_yield"))
    if is_etf:
        if not (
            facts.get("description")
            or facts.get("etf_mandate")
            or facts.get("legal_name")
            or facts.get("market_cap")
            or facts.get("net_assets")
        ):
            missing_core.append("etf_mandate_or_description")
    elif not substance:
        missing_core.append("fundamentals_or_financials")

    observed = set(packet.sources_observed or [])
    if "get_equity_quotes" not in observed and price is None:
        if "get_equity_quotes" not in missing_core:
            missing_core.append("source:get_equity_quotes")

    missing_optional: list[str] = []
    for src in packet.sources_unavailable or []:
        if is_etf and src in ETF_IRRELEVANT_OPTIONAL_SOURCES:
            continue
        if src in OPTIONAL_SOURCES or src not in CORE_SOURCES:
            missing_optional.append(src)
    if not facts.get("news_headlines") and not facts.get("news_items"):
        if "news" not in missing_optional:
            missing_optional.append("news")
    if packet.fact_by_name("revenue_periods") is None and not is_etf:
        if "financials.revenue" not in missing_optional:
            missing_optional.append("financials.revenue")

    sufficient = not missing_core
    reason = None if sufficient else "insufficient_core_evidence:" + ",".join(missing_core)
    return EvidenceSufficiency(
        sufficient=sufficient,
        missing_core=missing_core,
        missing_optional=missing_optional,
        reason=reason,
    )


def is_etf_class(value: str | SecurityClass | None) -> bool:
    return bool(value and "ETF" in str(value).upper())


def packet_is_etf(packet: ResearchEvidencePacket) -> bool:
    facts = {item.name: item.value for item in packet.facts}
    return subject_is_etf(
        classification=packet.classification,
        facts=facts,
        symbol=packet.symbol,
    )


def subject_is_etf(
    *,
    classification: Any = None,
    candidate: Candidate | None = None,
    facts: Mapping[str, Any] | None = None,
    instrument_kind: str | None = None,
    name: str | None = None,
    description: str | None = None,
    industry: str | None = None,
    symbol: str | None = None,
) -> bool:
    """True when the subject should use the fund evidence path, not company financials.

    Classification class is authoritative when present. Name / instrument_kind /
    fund industry are fallbacks so a mis-tagged SPY still is not asked for a 10-Q.
    Seed tickers alone do not classify a ceiling.
    """
    cls_value = None
    if classification is not None:
        cls_value = getattr(classification, "security_class", classification)
    if is_etf_class(cls_value):
        return True
    if candidate is not None and is_etf_class(getattr(candidate, "security_class", None)):
        return True
    kind = instrument_kind
    legal = name
    desc = description
    ind = industry
    if facts:
        kind = kind or facts.get("instrument_kind")
        legal = legal or facts.get("legal_name")
        desc = desc or facts.get("etf_mandate") or facts.get("description")
        ind = ind or facts.get("industry_label_raw")
    if kind and str(kind).strip().lower() == "etf":
        return True
    if legal and _ETF_NAME.search(str(legal)):
        return True
    industry_n = str(ind or "").strip().lower()
    if industry_n == "investment trusts or mutual funds" and (legal or desc or (kind and str(kind).lower() == "etf")):
        return True
    return False


def looks_like_pre_fix_need_more_data(report: ResearchReport) -> bool:
    """True when a NEED_MORE_DATA report is not a legitimate post-repair conclusion.

    Matches collector-alias misses and ETF packets that were scored as if they
    were operating companies (missing 10-Q / revenue).
    """
    conclusion = getattr(report.research_conclusion, "value", report.research_conclusion)
    status = getattr(report.research_status, "value", report.research_status)
    if str(conclusion or "") != "NEED_MORE_DATA" and str(status or "") not in {"RESEARCH_INCONCLUSIVE", "RESEARCH_STALE"}:
        return False
    missing = {str(item) for item in (report.missing_information or [])}
    unavailable = {str(item) for item in (report.sources_unavailable or [])}
    etf = is_etf_class(getattr(report, "security_class", None)) or str(report.symbol or "").upper() in BROAD_MARKET_REPAIR_SYMBOLS
    if etf and (missing & COMPANY_EVIDENCE_GAPS):
        return True
    if etf and ("get_financials" in unavailable or "get_equity_news" in unavailable):
        # Pre-fix collector recorded alias feeds as unavailable even when present.
        return True
    return False
