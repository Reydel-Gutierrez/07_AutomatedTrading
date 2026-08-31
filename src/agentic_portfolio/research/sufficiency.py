"""Deterministic evidence-sufficiency check. Runs before a paid Terra call.

Optional gaps (news, SEC excerpts, technicals) are recorded for the reasoner.
They must not by themselves consume the research budget or force NEED_MORE_DATA.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentic_portfolio.research.types import ResearchEvidencePacket
from agentic_portfolio.schemas import SecurityClass


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

    Core: market price, identity, and some fundamental/quality substance
    (financials, valuation multiples, description, or ETF mandate).
    Optional: news, SEC, technicals, earnings calendar, historicals.
    """
    facts = {item.name: item.value for item in packet.facts}
    derived = {item.name: item.value for item in packet.derived_metrics}
    cls = packet.classification.security_class if packet.classification else None
    is_etf = bool(cls and "ETF" in str(cls))

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
        if not (facts.get("description") or facts.get("legal_name") or facts.get("market_cap")):
            missing_core.append("etf_mandate_or_description")
    elif not substance:
        missing_core.append("fundamentals_or_financials")

    observed = set(packet.sources_observed or [])
    if "get_equity_quotes" not in observed and price is None:
        if "get_equity_quotes" not in missing_core:
            missing_core.append("source:get_equity_quotes")

    missing_optional: list[str] = []
    for src in packet.sources_unavailable or []:
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
    return bool(value and "ETF" in str(value))
