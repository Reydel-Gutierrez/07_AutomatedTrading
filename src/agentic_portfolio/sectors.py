"""Canonical GICS-style 11-sector taxonomy and deterministic external-label mapping.

The AI must not freely rename sectors to pass risk rules. If a label cannot be
mapped with this table, the result is UNKNOWN — never a fabricated sector.

Robinhood MCP `get_equity_fundamentals` currently returns FactSet-style sector
names (e.g. "Electronic Technology") and ETF catch-alls ("Miscellaneous").
Those are mapped here; they are not used as-is in portfolio sector math.
"""

from __future__ import annotations

import re
from enum import Enum


class CanonicalSector(str, Enum):
    COMMUNICATION_SERVICES = "COMMUNICATION_SERVICES"
    CONSUMER_DISCRETIONARY = "CONSUMER_DISCRETIONARY"
    CONSUMER_STAPLES = "CONSUMER_STAPLES"
    ENERGY = "ENERGY"
    FINANCIALS = "FINANCIALS"
    HEALTH_CARE = "HEALTH_CARE"
    INDUSTRIALS = "INDUSTRIALS"
    INFORMATION_TECHNOLOGY = "INFORMATION_TECHNOLOGY"
    MATERIALS = "MATERIALS"
    REAL_ESTATE = "REAL_ESTATE"
    UTILITIES = "UTILITIES"
    UNKNOWN = "UNKNOWN"


class SectorStatus(str, Enum):
    VERIFIED = "VERIFIED"
    MAPPED = "MAPPED"
    UNKNOWN = "UNKNOWN"
    CONFLICTING = "CONFLICTING"


# Exact (normalized) industry labels that override the parent sector.
# REIT issuers are reported by Robinhood as sector=Finance.
_INDUSTRY_OVERRIDES: dict[str, CanonicalSector] = {
    "real estate investment trusts": CanonicalSector.REAL_ESTATE,
    "real estate development": CanonicalSector.REAL_ESTATE,
    "real estate operators": CanonicalSector.REAL_ESTATE,
    "real estate investment trusts/services": CanonicalSector.REAL_ESTATE,
}

# Exact (normalized) sector labels from GICS, ICB, FactSet, and common prose.
_SECTOR_MAP: dict[str, CanonicalSector] = {
    # GICS 11
    "communication services": CanonicalSector.COMMUNICATION_SERVICES,
    "communications": CanonicalSector.COMMUNICATION_SERVICES,
    "communication": CanonicalSector.COMMUNICATION_SERVICES,
    "consumer discretionary": CanonicalSector.CONSUMER_DISCRETIONARY,
    "consumer staples": CanonicalSector.CONSUMER_STAPLES,
    "energy": CanonicalSector.ENERGY,
    "financials": CanonicalSector.FINANCIALS,
    "financial": CanonicalSector.FINANCIALS,
    "finance": CanonicalSector.FINANCIALS,
    "health care": CanonicalSector.HEALTH_CARE,
    "healthcare": CanonicalSector.HEALTH_CARE,
    "health": CanonicalSector.HEALTH_CARE,
    "industrials": CanonicalSector.INDUSTRIALS,
    "industrial": CanonicalSector.INDUSTRIALS,
    "information technology": CanonicalSector.INFORMATION_TECHNOLOGY,
    "information_technology": CanonicalSector.INFORMATION_TECHNOLOGY,
    "technology": CanonicalSector.INFORMATION_TECHNOLOGY,
    "materials": CanonicalSector.MATERIALS,
    "real estate": CanonicalSector.REAL_ESTATE,
    "utilities": CanonicalSector.UTILITIES,
    # FactSet / Robinhood fundamentals.sector
    "electronic technology": CanonicalSector.INFORMATION_TECHNOLOGY,
    "technology services": CanonicalSector.INFORMATION_TECHNOLOGY,
    "health technology": CanonicalSector.HEALTH_CARE,
    "health services": CanonicalSector.HEALTH_CARE,
    "energy minerals": CanonicalSector.ENERGY,
    "non-energy minerals": CanonicalSector.MATERIALS,
    "process industries": CanonicalSector.MATERIALS,
    "producer manufacturing": CanonicalSector.INDUSTRIALS,
    "industrial services": CanonicalSector.INDUSTRIALS,
    "distribution services": CanonicalSector.INDUSTRIALS,
    "transportation": CanonicalSector.INDUSTRIALS,
    "retail trade": CanonicalSector.CONSUMER_DISCRETIONARY,
    "consumer durables": CanonicalSector.CONSUMER_DISCRETIONARY,
    "consumer services": CanonicalSector.CONSUMER_DISCRETIONARY,
    "consumer non-durables": CanonicalSector.CONSUMER_STAPLES,
    "consumer non durables": CanonicalSector.CONSUMER_STAPLES,
    "commercial services": CanonicalSector.INDUSTRIALS,
    # Common aliases
    "telecom": CanonicalSector.COMMUNICATION_SERVICES,
    "telecommunication services": CanonicalSector.COMMUNICATION_SERVICES,
    "telecommunications": CanonicalSector.COMMUNICATION_SERVICES,
    "media": CanonicalSector.COMMUNICATION_SERVICES,
    "banks": CanonicalSector.FINANCIALS,
    "insurance": CanonicalSector.FINANCIALS,
    "oil & gas": CanonicalSector.ENERGY,
    "oil and gas": CanonicalSector.ENERGY,
}

# ETF/fund catch-alls are not an economic sector of the portfolio.
_NON_SECTOR_LABELS = {
    "miscellaneous",
    "investment trusts or mutual funds",
    "investment trusts",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "other",
}


def _norm(label: str | None) -> str:
    t = (label or "").strip().lower()
    t = t.replace("_", " ")
    t = re.sub(r"[&/]+", " ", t)
    t = re.sub(r"[^a-z0-9 +]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def map_sector(
    label: str | None,
    *,
    industry: str | None = None,
) -> tuple[CanonicalSector, SectorStatus]:
    """Map an external sector/industry label into the canonical 11-sector set.

    Returns (UNKNOWN, UNKNOWN) when the label is missing or unmapped.
    Returns CONFLICTING only when two provided labels map to different
    canonical sectors and no industry override applies.
    """
    ind_n = _norm(industry)
    if ind_n and ind_n in _INDUSTRY_OVERRIDES:
        return _INDUSTRY_OVERRIDES[ind_n], SectorStatus.MAPPED

    sec_n = _norm(label)
    if not sec_n:
        if ind_n and ind_n in _SECTOR_MAP:
            return _SECTOR_MAP[ind_n], SectorStatus.MAPPED
        return CanonicalSector.UNKNOWN, SectorStatus.UNKNOWN

    if sec_n in _NON_SECTOR_LABELS:
        if ind_n and ind_n in _INDUSTRY_OVERRIDES:
            return _INDUSTRY_OVERRIDES[ind_n], SectorStatus.MAPPED
        if ind_n and ind_n in _SECTOR_MAP and ind_n not in _NON_SECTOR_LABELS:
            return _SECTOR_MAP[ind_n], SectorStatus.MAPPED
        return CanonicalSector.UNKNOWN, SectorStatus.UNKNOWN

    mapped = _SECTOR_MAP.get(sec_n)
    if mapped is None and sec_n in {c.value.lower().replace("_", " ") for c in CanonicalSector if c != CanonicalSector.UNKNOWN}:
        mapped = CanonicalSector(sec_n.upper().replace(" ", "_")) if sec_n.upper().replace(" ", "_") in CanonicalSector._value2member_map_ else None

    if mapped is None:
        # Allow already-canonical enum values / GICS-style identifiers.
        raw = (label or "").strip().upper().replace(" ", "_")
        if raw in CanonicalSector._value2member_map_ and raw != CanonicalSector.UNKNOWN.value:
            return CanonicalSector(raw), SectorStatus.MAPPED
        return CanonicalSector.UNKNOWN, SectorStatus.UNKNOWN

    if ind_n and ind_n in _SECTOR_MAP:
        ind_mapped = _SECTOR_MAP[ind_n]
        if ind_mapped != mapped and ind_n not in _NON_SECTOR_LABELS:
            # Industry disagreed with sector and was not an override.
            if ind_n not in _INDUSTRY_OVERRIDES:
                return CanonicalSector.UNKNOWN, SectorStatus.CONFLICTING

    return mapped, SectorStatus.MAPPED


def canonical_sector_value(label: str | None, *, industry: str | None = None) -> str | None:
    """Return the canonical enum value, or None if missing/unknown."""
    sector, status = map_sector(label, industry=industry)
    if sector == CanonicalSector.UNKNOWN or status == SectorStatus.CONFLICTING:
        return None if not label else CanonicalSector.UNKNOWN.value
    return sector.value
