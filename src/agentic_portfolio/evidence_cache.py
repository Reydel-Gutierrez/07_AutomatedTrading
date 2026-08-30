"""Persisted classification evidence/results with per-field staleness.

Force-refresh reasons include high-concentration capacity, corporate actions,
conflicts, material fund changes, session start, and explicit human request.
TTL is not assumed to be uniform across fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agentic_portfolio.paths import project_root
from agentic_portfolio.schemas import (
    CacheMetadata,
    ClassificationEvidence,
    ClassificationResult,
    ClassificationStatus,
    EmbeddedSectorStatus,
    EvidenceValue,
    ProvenanceKind,
    RefreshReason,
    SecurityClass,
    to_dict,
)
from agentic_portfolio.sectors import CanonicalSector, SectorStatus


# Field-level TTLs. Liquidity/volume moves faster than legal name.
DEFAULT_FIELD_TTLS = {
    "instrument_kind": timedelta(days=30),
    "legal_name": timedelta(days=30),
    "is_leveraged": timedelta(days=7),
    "is_inverse": timedelta(days=7),
    "is_thematic": timedelta(days=7),
    "is_sector_or_industry_fund": timedelta(days=7),
    "is_narrow_factor": timedelta(days=7),
    "is_single_stock_fund": timedelta(days=7),
    "underlying_index": timedelta(days=7),
    "fund_mandate": timedelta(days=7),
    "constituent_count": timedelta(days=7),
    "max_sector_weight": timedelta(days=7),
    "embedded_sector_weights": timedelta(days=7),
    "sector_label_raw": timedelta(days=7),
    "classification": timedelta(hours=20),  # ~one session
    "liquidity": timedelta(hours=6),
}


def classification_cache_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "state" / "classification_cache.json"


@dataclass
class CachedClassification:
    symbol: str
    result: dict[str, Any]
    evidence: dict[str, Any]
    created_at: str
    refreshed_at: str
    source_version: str | None
    refresh_reason: str
    field_refreshed_at: dict[str, str]


def load_cache(path: Path | None = None) -> dict[str, Any]:
    p = path or classification_cache_path()
    if not p.exists():
        return {"entries": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def save_cache(data: dict[str, Any], path: Path | None = None) -> Path:
    p = path or classification_cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return p


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def field_is_stale(field_name: str, refreshed_at: str | None, now: datetime, ttls: dict[str, timedelta] | None = None) -> bool:
    ttls = ttls or DEFAULT_FIELD_TTLS
    ts = _parse_ts(refreshed_at)
    if ts is None:
        return True
    ttl = ttls.get(field_name, timedelta(days=7))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return now - ts > ttl


def needs_refresh(
    entry: dict[str, Any] | None,
    *,
    now: datetime,
    force: RefreshReason | None = None,
    using_high_concentration_capacity: bool = False,
    human_request: bool = False,
    corporate_action: bool = False,
    material_fund_change: bool = False,
    conflicting: bool = False,
    session_start: bool = False,
) -> tuple[bool, RefreshReason | None]:
    if human_request:
        return True, RefreshReason.HUMAN_REQUEST
    if corporate_action:
        return True, RefreshReason.CORPORATE_ACTION
    if material_fund_change:
        return True, RefreshReason.MATERIAL_FUND_CHANGE
    if conflicting:
        return True, RefreshReason.CONFLICTING_OBSERVATIONS
    if using_high_concentration_capacity:
        return True, RefreshReason.HIGH_CONCENTRATION_CAPACITY
    if session_start:
        return True, RefreshReason.SESSION_START
    if force is not None:
        return True, force
    if entry is None:
        return True, RefreshReason.MISSING
    refreshed = entry.get("refreshed_at")
    if field_is_stale("classification", refreshed, now):
        return True, RefreshReason.STALE
    status = (entry.get("result") or {}).get("status")
    if status in {ClassificationStatus.INSUFFICIENT_EVIDENCE.value, ClassificationStatus.CONFLICTING_EVIDENCE.value}:
        return True, RefreshReason.STALE
    return False, None


def put_classification(
    symbol: str,
    result: ClassificationResult,
    *,
    now: datetime | None = None,
    reason: RefreshReason = RefreshReason.INITIAL,
    source_version: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    ts = now.isoformat()
    data = load_cache(path)
    entries = data.setdefault("entries", {})
    prior = entries.get(symbol.upper())
    created = prior.get("created_at") if prior else ts
    entries[symbol.upper()] = {
        "symbol": symbol.upper(),
        "result": to_dict(result),
        "evidence": to_dict(result.evidence) if result.evidence else None,
        "created_at": created,
        "refreshed_at": ts,
        "source_version": source_version or (result.cache.source_version if result.cache else None),
        "refresh_reason": reason.value,
        "field_refreshed_at": {k: ts for k in DEFAULT_FIELD_TTLS},
    }
    save_cache(data, path)
    return entries[symbol.upper()]


def get_cached(symbol: str, path: Path | None = None) -> dict[str, Any] | None:
    return load_cache(path).get("entries", {}).get(symbol.upper())


def result_from_cache_entry(entry: dict[str, Any]) -> ClassificationResult:
    from agentic_portfolio.schemas import SecurityClass as SC

    r = entry["result"]
    ev_raw = entry.get("evidence") or {}
    evidence = None
    if ev_raw:
        prov = {}
        for k, v in (ev_raw.get("provenance") or {}).items():
            if isinstance(v, dict):
                prov[k] = EvidenceValue(
                    value=v.get("value"),
                    source=v.get("source"),
                    observed_at=v.get("observed_at"),
                    provenance=ProvenanceKind(v["provenance"]) if v.get("provenance") else ProvenanceKind.MISSING,
                    confidence=v.get("confidence"),
                    status=v.get("status"),
                )
        weights = ev_raw.get("embedded_sector_weights")
        emb = ev_raw.get("embedded_sector_exposure_status") or EmbeddedSectorStatus.UNKNOWN.value
        evidence = ClassificationEvidence(
            instrument_kind=ev_raw.get("instrument_kind"),
            is_leveraged=ev_raw.get("is_leveraged"),
            is_inverse=ev_raw.get("is_inverse"),
            is_thematic=ev_raw.get("is_thematic"),
            is_sector_or_industry_fund=ev_raw.get("is_sector_or_industry_fund"),
            is_narrow_factor=ev_raw.get("is_narrow_factor"),
            is_single_stock_fund=ev_raw.get("is_single_stock_fund"),
            underlying_index=ev_raw.get("underlying_index"),
            fund_mandate=ev_raw.get("fund_mandate"),
            constituent_count=ev_raw.get("constituent_count"),
            max_sector_weight=ev_raw.get("max_sector_weight"),
            top10_weight=ev_raw.get("top10_weight"),
            seed_list_match=bool(ev_raw.get("seed_list_match")),
            embedded_sector_weights=weights,
            embedded_sector_exposure_status=EmbeddedSectorStatus(emb),
            underlying_index_definitionally_broad=ev_raw.get("underlying_index_definitionally_broad"),
            sector_label_raw=ev_raw.get("sector_label_raw"),
            industry_label_raw=ev_raw.get("industry_label_raw"),
            legal_name=ev_raw.get("legal_name"),
            description=ev_raw.get("description"),
            conflict_notes=list(ev_raw.get("conflict_notes") or []),
            provenance=prov,
        )
    sector = r.get("sector") or CanonicalSector.UNKNOWN.value
    try:
        sector_e = CanonicalSector(sector)
    except ValueError:
        sector_e = CanonicalSector.UNKNOWN
    try:
        ss = SectorStatus(r.get("sector_status") or SectorStatus.UNKNOWN.value)
    except ValueError:
        ss = SectorStatus.UNKNOWN
    status_raw = r.get("status") or ClassificationStatus.INSUFFICIENT_EVIDENCE.value
    if status_raw == "VERIFIED":
        status_raw = ClassificationStatus.VALIDATED.value
    cache_meta = CacheMetadata(
        created_at=entry.get("created_at"),
        refreshed_at=entry.get("refreshed_at"),
        source_version=entry.get("source_version"),
        refresh_reason=entry.get("refresh_reason"),
        stale=False,
    )
    return ClassificationResult(
        security_class=SC(r["security_class"]),
        status=ClassificationStatus(status_raw),
        effective_class_for_ceiling=SC(r.get("effective_class_for_ceiling") or r["security_class"]),
        confidence=r.get("confidence") or "none",
        reasons=list(r.get("reasons") or []),
        seed_list_used=bool(r.get("seed_list_used")),
        symbol=entry.get("symbol") or r.get("symbol") or "",
        instrument_type=r.get("instrument_type"),
        evidence=evidence,
        sector=sector_e,
        sector_status=ss,
        observed_at=r.get("observed_at"),
        cache=cache_meta,
    )
