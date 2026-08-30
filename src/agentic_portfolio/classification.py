from __future__ import annotations

from datetime import datetime, timezone

from agentic_portfolio.policy import load_policy
from agentic_portfolio.schemas import (
    CacheMetadata,
    ClassificationEvidence,
    ClassificationResult,
    ClassificationStatus,
    EmbeddedSectorStatus,
    LiquidityEvidence,
    ProvenanceKind,
    SecurityClass,
)
from agentic_portfolio.sectors import CanonicalSector, SectorStatus, map_sector


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def _matches_any(text: str, patterns: list[str]) -> bool:
    t = _norm(text)
    return any(p in t for p in patterns)


def classify(symbol: str, evidence: ClassificationEvidence, policy: dict | None = None) -> ClassificationResult:
    """Deterministic classification. Seed tickers are never sufficient alone.

    The AI cannot override this result. Embedded ETF sector weights are not
    required for a broad-market call when other diversification evidence exists;
    missing weights are recorded as UNKNOWN/PARTIAL, never invented.
    """
    policy = policy or load_policy()
    cfg = policy["security_classification"]
    broad = cfg["broad_market"]
    seed = {s.upper() for s in broad.get("seed_tickers_supporting_only", [])}
    seed_hit = symbol.upper() in seed or evidence.seed_list_match
    reasons: list[str] = []
    if seed_hit:
        reasons.append("seed_list_supporting_only")

    observed_at = datetime.now(timezone.utc).isoformat()
    sector, sector_status = map_sector(evidence.sector_label_raw, industry=evidence.industry_label_raw)
    if evidence.embedded_sector_exposure_status == EmbeddedSectorStatus.UNKNOWN:
        reasons.append("embedded_sector_exposure_unknown")

    if evidence.conflict_notes or _has_conflicting_provenance(evidence):
        return _result(
            symbol,
            evidence,
            SecurityClass.INDIVIDUAL_EQUITY,
            ClassificationStatus.CONFLICTING_EVIDENCE,
            SecurityClass.INDIVIDUAL_EQUITY,
            "none",
            reasons + ["conflicting_classification_evidence"] + list(evidence.conflict_notes),
            seed_hit,
            sector,
            sector_status,
            observed_at,
        )

    kind = (evidence.instrument_kind or "").lower() or None

    if kind != "etf":
        if kind == "equity":
            return _result(
                symbol,
                evidence,
                SecurityClass.INDIVIDUAL_EQUITY,
                ClassificationStatus.VALIDATED,
                SecurityClass.INDIVIDUAL_EQUITY,
                "high",
                reasons + ["instrument_kind=equity"],
                seed_hit,
                sector,
                sector_status,
                observed_at,
            )
        return _result(
            symbol,
            evidence,
            SecurityClass.INDIVIDUAL_EQUITY,
            ClassificationStatus.INSUFFICIENT_EVIDENCE,
            SecurityClass.INDIVIDUAL_EQUITY,
            "none",
            reasons + ["instrument_kind_unknown_fail_closed"],
            seed_hit,
            sector,
            sector_status,
            observed_at,
        )

    disqualifiers = {
        "is_leveraged": evidence.is_leveraged,
        "is_inverse": evidence.is_inverse,
        "is_thematic": evidence.is_thematic,
        "is_sector_or_industry_fund": evidence.is_sector_or_industry_fund,
        "is_narrow_factor": evidence.is_narrow_factor,
        "is_single_stock_fund": evidence.is_single_stock_fund,
    }
    if any(v is True for v in disqualifiers.values()):
        hit = [k for k, v in disqualifiers.items() if v is True]
        if evidence.is_leveraged is True or evidence.is_inverse is True:
            return _result(
                symbol,
                evidence,
                SecurityClass.INDIVIDUAL_EQUITY,
                ClassificationStatus.VALIDATED,
                SecurityClass.INDIVIDUAL_EQUITY,
                "high",
                reasons + ["prohibited_or_non_broad"] + hit,
                seed_hit,
                CanonicalSector.UNKNOWN,
                SectorStatus.UNKNOWN,
                observed_at,
            )
        if evidence.is_single_stock_fund is True:
            return _result(
                symbol,
                evidence,
                SecurityClass.INDIVIDUAL_EQUITY,
                ClassificationStatus.VALIDATED,
                SecurityClass.INDIVIDUAL_EQUITY,
                "high",
                reasons + ["single_stock_fund"] + hit,
                seed_hit,
                CanonicalSector.UNKNOWN,
                SectorStatus.UNKNOWN,
                observed_at,
            )
        return _result(
            symbol,
            evidence,
            SecurityClass.OTHER_DIVERSIFIED_ETF,
            ClassificationStatus.VALIDATED,
            SecurityClass.OTHER_DIVERSIFIED_ETF,
            "high",
            reasons + ["etf_not_broad_market"] + hit,
            seed_hit,
            CanonicalSector.UNKNOWN if sector_status != SectorStatus.MAPPED else sector,
            SectorStatus.UNKNOWN if sector == CanonicalSector.UNKNOWN else sector_status,
            observed_at,
        )

    required_false = broad.get("required_known_false", [])
    unknown_flags = [name for name in required_false if getattr(evidence, name) is None]
    if unknown_flags:
        return _result(
            symbol,
            evidence,
            SecurityClass.OTHER_DIVERSIFIED_ETF,
            ClassificationStatus.INSUFFICIENT_EVIDENCE,
            SecurityClass.OTHER_DIVERSIFIED_ETF,
            "none",
            reasons + ["unknown_disqualifier_flags_fail_closed"] + unknown_flags,
            seed_hit,
            CanonicalSector.UNKNOWN,
            SectorStatus.UNKNOWN,
            observed_at,
        )

    patterns = broad.get("broad_index_name_patterns", [])
    index_ok = _matches_any(evidence.underlying_index, patterns)
    mandate_ok = _matches_any(evidence.fund_mandate, patterns)
    if not (index_ok or mandate_ok):
        known_text = bool(evidence.underlying_index or evidence.fund_mandate)
        return _result(
            symbol,
            evidence,
            SecurityClass.OTHER_DIVERSIFIED_ETF,
            ClassificationStatus.VALIDATED if known_text else ClassificationStatus.INSUFFICIENT_EVIDENCE,
            SecurityClass.OTHER_DIVERSIFIED_ETF,
            "medium" if known_text else "none",
            reasons + ["index_or_mandate_not_broad_or_missing"],
            seed_hit,
            CanonicalSector.UNKNOWN,
            SectorStatus.UNKNOWN,
            observed_at,
        )

    min_n = int(broad.get("min_constituent_count", 100))
    max_sw = float(broad.get("max_sector_weight_for_broad", 0.30))
    div_ok = False
    if evidence.constituent_count is not None and evidence.constituent_count >= min_n:
        div_ok = True
        reasons.append("constituent_count_ok")
    if evidence.max_sector_weight is not None and evidence.max_sector_weight <= max_sw:
        div_ok = True
        reasons.append("max_sector_weight_ok")
    if evidence.underlying_index_definitionally_broad is True:
        div_ok = True
        reasons.append("definitional_broad_index_diversification")
    if not div_ok:
        return _result(
            symbol,
            evidence,
            SecurityClass.OTHER_DIVERSIFIED_ETF,
            ClassificationStatus.INSUFFICIENT_EVIDENCE,
            SecurityClass.OTHER_DIVERSIFIED_ETF,
            "none",
            reasons + ["diversification_metric_missing_fail_closed_no_40pct"],
            seed_hit,
            CanonicalSector.UNKNOWN,
            SectorStatus.UNKNOWN,
            observed_at,
        )

    if evidence.embedded_sector_weights is None:
        reasons.append("embedded_sector_weights_not_fabricated")

    return _result(
        symbol,
        evidence,
        SecurityClass.BROAD_MARKET_INDEX_ETF,
        ClassificationStatus.VALIDATED,
        SecurityClass.BROAD_MARKET_INDEX_ETF,
        "high",
        reasons + ["broad_market_criteria_met"],
        seed_hit,
        CanonicalSector.UNKNOWN,
        SectorStatus.UNKNOWN,
        observed_at,
        instrument_type="etf",
    )


def _has_conflicting_provenance(evidence: ClassificationEvidence) -> bool:
    return any(v.provenance == ProvenanceKind.CONFLICTING for v in evidence.provenance.values())


def _result(
    symbol: str,
    evidence: ClassificationEvidence,
    security_class: SecurityClass,
    status: ClassificationStatus,
    effective: SecurityClass,
    confidence: str,
    reasons: list[str],
    seed_hit: bool,
    sector: CanonicalSector,
    sector_status: SectorStatus,
    observed_at: str,
    instrument_type: str | None = None,
) -> ClassificationResult:
    kind = (evidence.instrument_kind or "").lower() or None
    return ClassificationResult(
        security_class=security_class,
        status=status,
        effective_class_for_ceiling=effective,
        confidence=confidence,
        reasons=reasons,
        seed_list_used=seed_hit,
        symbol=symbol.upper(),
        instrument_type=instrument_type or kind,
        evidence=evidence,
        sector=sector,
        sector_status=sector_status,
        liquidity=None,
        observed_at=observed_at,
        cache=CacheMetadata(refreshed_at=observed_at),
    )
