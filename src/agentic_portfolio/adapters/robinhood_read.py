"""Read-only Robinhood MCP → ClassificationEvidence.

This adapter collects facts. It is not a stock picker and does not recommend
trades. It never calls order review/place/cancel or capital-transfer tools.

Observed MCP shapes (2026-08-29):
  get_equity_tradability — name, simple_name, state, tradeable (no explicit ETF flag)
  get_equity_fundamentals — description, sector (FactSet), industry, volume averages
  search — name, simple_name, instrument_id
  get_equity_quotes — last trade, bid/ask, official close

Holdings / constituent counts / GICS ETF sector weights are NOT exposed.
Those fields stay MISSING. Embedded sector status is UNKNOWN or PARTIAL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from agentic_portfolio.policy import load_policy
from agentic_portfolio.schemas import (
    ClassificationEvidence,
    EmbeddedSectorStatus,
    EvidenceValue,
    LiquidityEvidence,
    ProvenanceKind,
)
from agentic_portfolio.sectors import CanonicalSector, map_sector


# Execution / mutation tools — never invoked from this module.
FORBIDDEN_MCP_TOOLS = frozenset(
    {
        "review_equity_order",
        "place_equity_order",
        "cancel_equity_order",
        "place_option_order",
        "review_option_order",
        "cancel_option_order",
        "exercise_option",
        "cancel_option_exercise",
        "place_crypto_order",
        "preview_crypto_order",
        "cancel_crypto_order",
    }
)

CLASSIFICATION_READ_TOOLS = (
    "get_equity_tradability",
    "get_equity_fundamentals",
    "search",
    "get_equity_quotes",
)

RECONCILIATION_READ_TOOLS = (
    "get_equity_positions",
    "get_portfolio",
    "get_accounts",
)

_ETF_NAME = re.compile(r"\betf\b|\betn\b|exchange[- ]traded", re.I)
_COMMON_STOCK = re.compile(r"common stock|ordinary shares|class [a-z] common", re.I)
_LEVERAGE = re.compile(
    r"\b(\d+\s*x|2x|3x)\b|\bleveraged\b|ultrapro|ultrashort|\bultra\s+(pro|short)\b",
    re.I,
)
_INVERSE = re.compile(r"\binverse\b|ultrapro short|\bshort\s+(qqq|s&p|dow|russell)\b", re.I)
_SECTOR_FUND = re.compile(
    r"select sector|\bsector\s+(spdr|etf)\b|\btechnology stocks\b|\benergy stocks\b|"
    r"\bfinancial (select|select sector)\b|\butilities\s+select\b|\bhealthcare\s+select\b|"
    r"industry etf|sector index",
    re.I,
)
_THEMATIC = re.compile(r"\bthematic\b|\bnext gen\b|\bdisruptive\b|\bcannabis\b|\bspace exploration\b", re.I)
_SINGLE_STOCK = re.compile(r"single[- ]stock|2x long\s+\w+|single name", re.I)
_NARROW_FACTOR = re.compile(r"\b(momentum|quality|value|low vol|min vol|dividend aristocrat)s?\b.*etf", re.I)

# Indexes that are definitionally diversified. Used only as DERIVED evidence
# of breadth — never as invented constituent counts or sector weights.
_DEFINITIONAL_BROAD_INDEX = (
    "s&p 500",
    "s&p500",
    "s&p 1500",
    "s&p total market",
    "crsp us total",
    "crsp total",
    "wilshire 5000",
    "russell 3000",
    "russell 1000",
    "dow jones u.s. total stock market",
    "total stock market",
    "total us stock",
)


class ReadOnlyFetcher(Protocol):
    """Injected callable surface. Implementations must not wrap execution tools."""

    def get_equity_tradability(self, symbol: str) -> Mapping[str, Any] | None: ...

    def get_equity_fundamentals(self, symbol: str) -> Mapping[str, Any] | None: ...

    def search_instrument(self, symbol: str) -> Mapping[str, Any] | None: ...

    def get_equity_quotes(self, symbol: str) -> Mapping[str, Any] | None: ...


@dataclass
class RobinhoodSecurityBundle:
    """Pre-fetched read-only MCP payloads for one symbol."""

    symbol: str
    tradability: Mapping[str, Any] | None = None
    fundamentals: Mapping[str, Any] | None = None
    search: Mapping[str, Any] | None = None
    quotes: Mapping[str, Any] | None = None
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_version: str | None = None


def _ev(
    value: Any,
    *,
    source: str | None,
    observed_at: str,
    provenance: ProvenanceKind,
    confidence: str | None = None,
    status: str | None = None,
) -> EvidenceValue:
    if value is None and provenance == ProvenanceKind.MCP_OBSERVED_FACT:
        provenance = ProvenanceKind.MISSING
    return EvidenceValue(
        value=value,
        source=source,
        observed_at=observed_at,
        provenance=provenance if value is not None else (provenance if provenance == ProvenanceKind.CONFLICTING else ProvenanceKind.MISSING),
        confidence=confidence,
        status=status or provenance.value,
    )


def _first_result(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return None
    data = payload.get("data", payload)
    if isinstance(data, dict):
        results = data.get("results")
        if isinstance(results, list) and results:
            item = results[0]
            if isinstance(item, dict) and "quote" in item and len(item) <= 3:
                return dict(item.get("quote") or item)
            return dict(item) if isinstance(item, dict) else None
        if "symbol" in data or "name" in data:
            return dict(data)
    return None


def _text(*parts: str | None) -> str:
    return " ".join(p for p in parts if p)


def adapt_classification_evidence(bundle: RobinhoodSecurityBundle, policy: dict | None = None) -> ClassificationEvidence:
    """Convert observed MCP payloads into ClassificationEvidence + provenance."""
    policy = policy or load_policy()
    ts = bundle.observed_at
    trad = _first_result(bundle.tradability)
    fund = _first_result(bundle.fundamentals)
    search = _match_search(bundle.symbol, bundle.search)
    quote = _first_result(bundle.quotes)

    name = (trad or {}).get("name") or (search or {}).get("name")
    simple = (trad or {}).get("simple_name") or (search or {}).get("simple_name")
    description = (fund or {}).get("description")
    sector_raw = (fund or {}).get("sector")
    industry_raw = (fund or {}).get("industry")
    blob = _text(name, simple, description)

    provenance: dict[str, EvidenceValue] = {}
    conflicts: list[str] = []

    if name:
        provenance["legal_name"] = _ev(name, source="get_equity_tradability.name", observed_at=ts, provenance=ProvenanceKind.MCP_OBSERVED_FACT)
    else:
        provenance["legal_name"] = _ev(None, source="get_equity_tradability.name", observed_at=ts, provenance=ProvenanceKind.MISSING)

    if description:
        provenance["description"] = _ev(description, source="get_equity_fundamentals.description", observed_at=ts, provenance=ProvenanceKind.MCP_OBSERVED_FACT)
    else:
        provenance["description"] = _ev(None, source="get_equity_fundamentals.description", observed_at=ts, provenance=ProvenanceKind.MISSING)

    if sector_raw:
        provenance["sector_label_raw"] = _ev(sector_raw, source="get_equity_fundamentals.sector", observed_at=ts, provenance=ProvenanceKind.MCP_OBSERVED_FACT)
    else:
        provenance["sector_label_raw"] = _ev(None, source="get_equity_fundamentals.sector", observed_at=ts, provenance=ProvenanceKind.MISSING)

    instrument_kind, kind_prov, kind_src = _derive_instrument_kind(name, industry_raw, description)
    provenance["instrument_kind"] = _ev(instrument_kind, source=kind_src, observed_at=ts, provenance=kind_prov)

    is_lev = _flag(_LEVERAGE, blob)
    is_inv = _flag(_INVERSE, blob)
    is_sector = _flag(_SECTOR_FUND, blob)
    is_them = _flag(_THEMATIC, blob)
    is_single = _flag(_SINGLE_STOCK, blob)
    is_factor = _flag(_NARROW_FACTOR, blob)
    if instrument_kind == "etf":
        # Known-false only when the ETF blob has no matching language.
        if is_lev is None:
            is_lev = False
        if is_inv is None:
            is_inv = False
        if is_sector is None:
            is_sector = False
        if is_them is None:
            is_them = False
        if is_single is None:
            is_single = False
        if is_factor is None:
            is_factor = False

    provenance["is_leveraged"] = _ev(is_lev, source="derived:name+description", observed_at=ts, provenance=ProvenanceKind.DERIVED_DETERMINISTIC_VALUE if is_lev is not None else ProvenanceKind.MISSING)
    provenance["is_inverse"] = _ev(is_inv, source="derived:name+description", observed_at=ts, provenance=ProvenanceKind.DERIVED_DETERMINISTIC_VALUE if is_inv is not None else ProvenanceKind.MISSING)
    provenance["is_sector_or_industry_fund"] = _ev(is_sector, source="derived:name+description", observed_at=ts, provenance=ProvenanceKind.DERIVED_DETERMINISTIC_VALUE if is_sector is not None else ProvenanceKind.MISSING)
    provenance["is_thematic"] = _ev(is_them, source="derived:name+description", observed_at=ts, provenance=ProvenanceKind.DERIVED_DETERMINISTIC_VALUE if is_them is not None else ProvenanceKind.MISSING)
    provenance["is_single_stock_fund"] = _ev(is_single, source="derived:name+description", observed_at=ts, provenance=ProvenanceKind.DERIVED_DETERMINISTIC_VALUE if is_single is not None else ProvenanceKind.MISSING)
    provenance["is_narrow_factor"] = _ev(is_factor, source="derived:name+description", observed_at=ts, provenance=ProvenanceKind.DERIVED_DETERMINISTIC_VALUE if is_factor is not None else ProvenanceKind.MISSING)

    index, mandate = _extract_index_and_mandate(name, description, policy)
    definitional = None
    if index:
        provenance["underlying_index"] = _ev(index, source="derived:name+description", observed_at=ts, provenance=ProvenanceKind.DERIVED_DETERMINISTIC_VALUE)
        definitional = any(p in _norm(index) or p in _norm(blob) for p in _DEFINITIONAL_BROAD_INDEX)
        if is_sector:
            definitional = False
    else:
        provenance["underlying_index"] = _ev(None, source="name+description", observed_at=ts, provenance=ProvenanceKind.MISSING)
    if mandate:
        provenance["fund_mandate"] = _ev(mandate, source="get_equity_fundamentals.description", observed_at=ts, provenance=ProvenanceKind.MCP_OBSERVED_FACT)
    else:
        provenance["fund_mandate"] = _ev(None, source="get_equity_fundamentals.description", observed_at=ts, provenance=ProvenanceKind.MISSING)
    if definitional is True:
        provenance["underlying_index_definitionally_broad"] = _ev(
            True,
            source="derived:canonical_broad_index_list",
            observed_at=ts,
            provenance=ProvenanceKind.DERIVED_DETERMINISTIC_VALUE,
        )
    else:
        provenance["underlying_index_definitionally_broad"] = _ev(
            definitional,
            source="derived:canonical_broad_index_list",
            observed_at=ts,
            provenance=ProvenanceKind.MISSING if definitional is None else ProvenanceKind.DERIVED_DETERMINISTIC_VALUE,
        )

    # Holdings are not in MCP. Do not invent them.
    provenance["constituent_count"] = _ev(None, source="holdings", observed_at=ts, provenance=ProvenanceKind.MISSING)
    provenance["max_sector_weight"] = _ev(None, source="holdings", observed_at=ts, provenance=ProvenanceKind.MISSING)
    provenance["embedded_sector_weights"] = _ev(None, source="holdings", observed_at=ts, provenance=ProvenanceKind.MISSING)

    seed = {s.upper() for s in policy["security_classification"]["broad_market"].get("seed_tickers_supporting_only", [])}
    seed_hit = bundle.symbol.upper() in seed
    provenance["seed_list_match"] = _ev(seed_hit, source="policy.seed_tickers_supporting_only", observed_at=ts, provenance=ProvenanceKind.DERIVED_DETERMINISTIC_VALUE)

    if instrument_kind == "etf" and instrument_kind == "equity":
        conflicts.append("instrument_kind_etf_and_equity")
    if is_lev and definitional:
        conflicts.append("leveraged_and_definitional_broad")

    _, sector_status = map_sector(sector_raw, industry=industry_raw)
    if sector_status.value == "CONFLICTING":
        conflicts.append("sector_label_conflict")
        provenance["sector"] = _ev(CanonicalSector.UNKNOWN.value, source="map_sector", observed_at=ts, provenance=ProvenanceKind.CONFLICTING)

    return ClassificationEvidence(
        instrument_kind=instrument_kind,
        is_leveraged=is_lev,
        is_inverse=is_inv,
        is_thematic=is_them,
        is_sector_or_industry_fund=is_sector,
        is_narrow_factor=is_factor,
        is_single_stock_fund=is_single,
        underlying_index=index,
        fund_mandate=mandate,
        constituent_count=None,
        max_sector_weight=None,
        top10_weight=None,
        seed_list_match=seed_hit,
        embedded_sector_weights=None,
        embedded_sector_exposure_status=EmbeddedSectorStatus.UNKNOWN,
        underlying_index_definitionally_broad=definitional if definitional else None,
        sector_label_raw=sector_raw,
        industry_label_raw=industry_raw,
        legal_name=name,
        description=description,
        conflict_notes=conflicts,
        provenance=provenance,
    )


def adapt_liquidity_evidence(bundle: RobinhoodSecurityBundle) -> LiquidityEvidence:
    """Liquidity from MCP volume averages is a proxy, not 20-session median ADV$.

    Policy still requires 20-session median for a hard pass; this is PARTIAL.
    """
    ts = bundle.observed_at
    fund = _first_result(bundle.fundamentals)
    quote = _first_result(bundle.quotes)
    notes: list[str] = []
    adv_shares = None
    if fund:
        for key in ("average_volume_2_weeks", "average_volume_30_days", "average_volume"):
            raw = fund.get(key)
            if raw not in (None, ""):
                try:
                    adv_shares = float(raw)
                    notes.append(f"volume_proxy_field={key}")
                    break
                except (TypeError, ValueError):
                    pass
    price = None
    if quote:
        raw_px = quote.get("last_trade_price") or quote.get("previous_close")
        if raw_px not in (None, ""):
            try:
                price = float(raw_px)
            except (TypeError, ValueError):
                pass
    dollar = None
    if adv_shares and price:
        dollar = adv_shares * price
        notes.append("dollar_volume_is_mean_proxy_not_20d_median")
    spread = None
    if quote:
        bid, ask = quote.get("bid_price"), quote.get("ask_price")
        try:
            b, a = float(bid), float(ask)
            mid = (a + b) / 2.0
            if mid > 0 and a > 0 and b > 0:
                spread = (a - b) / mid
        except (TypeError, ValueError):
            pass
    status = "PARTIAL" if dollar else "UNKNOWN"
    prov = ProvenanceKind.DERIVED_DETERMINISTIC_VALUE if dollar else ProvenanceKind.MISSING
    return LiquidityEvidence(
        median_daily_dollar_volume_20d=None,
        recent_dollar_volume=dollar,
        bid_ask_spread_pct=spread,
        average_volume_proxy=dollar,
        status=status,
        provenance=prov,
        source="get_equity_fundamentals.average_volume+get_equity_quotes",
        observed_at=ts,
        notes=notes,
    )


def collect_from_fetcher(symbol: str, fetcher: ReadOnlyFetcher) -> tuple[ClassificationEvidence, LiquidityEvidence, RobinhoodSecurityBundle]:
    bundle = RobinhoodSecurityBundle(
        symbol=symbol.upper(),
        tradability=fetcher.get_equity_tradability(symbol),
        fundamentals=fetcher.get_equity_fundamentals(symbol),
        search=fetcher.search_instrument(symbol),
        quotes=fetcher.get_equity_quotes(symbol),
    )
    return adapt_classification_evidence(bundle), adapt_liquidity_evidence(bundle), bundle


def _match_search(symbol: str, payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return None
    data = payload.get("data", payload)
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return _first_result(payload)
    want = symbol.upper()
    for item in results:
        if isinstance(item, dict) and str(item.get("symbol", "")).upper() == want:
            return dict(item)
    return dict(results[0]) if results and isinstance(results[0], dict) else None


def _derive_instrument_kind(name: str | None, industry: str | None, description: str | None) -> tuple[str | None, ProvenanceKind, str]:
    industry_n = (industry or "").strip().lower()
    etf_industry = industry_n == "investment trusts or mutual funds"
    name_etf = bool(name and _ETF_NAME.search(name))
    name_stock = bool(name and _COMMON_STOCK.search(name))
    if name_etf or etf_industry:
        if name_stock:
            return None, ProvenanceKind.CONFLICTING, "name+industry"
        return "etf", ProvenanceKind.DERIVED_DETERMINISTIC_VALUE, "get_equity_tradability.name+get_equity_fundamentals.industry"
    if name_stock:
        return "equity", ProvenanceKind.DERIVED_DETERMINISTIC_VALUE, "get_equity_tradability.name"
    if industry_n and industry_n != "investment trusts or mutual funds" and not etf_industry:
        # Operating-company sector labels imply equity, not a fund.
        if (industry or "") and sector_looks_like_operating_company(industry_n):
            return "equity", ProvenanceKind.DERIVED_DETERMINISTIC_VALUE, "get_equity_fundamentals.industry"
    return None, ProvenanceKind.MISSING, "instrument_type"


def sector_looks_like_operating_company(industry_n: str) -> bool:
    return industry_n not in {"investment trusts or mutual funds", "miscellaneous", ""}


def _flag(pattern: re.Pattern[str], blob: str) -> bool | None:
    if not blob.strip():
        return None
    return bool(pattern.search(blob))


def _extract_index_and_mandate(name: str | None, description: str | None, policy: dict) -> tuple[str | None, str | None]:
    blob = _text(name, description)
    patterns: list[str] = policy["security_classification"]["broad_market"].get("broad_index_name_patterns", [])
    found = None
    n = _norm(blob)
    for p in patterns:
        if p in n:
            found = p
            break
    # Prefer a readable index label from the legal name when present.
    if name and re.search(r"s&p\s*500", name, re.I):
        found = "S&P 500"
    mandate = description
    return found, mandate


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()
