"""Read-only reconciliation: Robinhood positions vs sleeve vs thesis registries.

Does not automatically repair ambiguous state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentic_portfolio.evidence_cache import field_is_stale, get_cached
from agentic_portfolio.schemas import (
    Position,
    PositionRegistryStatus,
    ReconciliationFinding,
    SleeveAssignmentStatus,
    ThesisStatus,
)
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry


ACTIVE_THESIS = {
    ThesisStatus.ACTIVE,
    ThesisStatus.STRENGTHENED,
    ThesisStatus.UNCHANGED,
    ThesisStatus.WEAKENED,
}

LIVE_SLEEVE = {
    SleeveAssignmentStatus.ACTIVE,
    SleeveAssignmentStatus.REDUCING,
    SleeveAssignmentStatus.PROPOSED,
    SleeveAssignmentStatus.WATCH,
}


@dataclass
class ReconciliationReport:
    findings: list[ReconciliationFinding] = field(default_factory=list)
    unregistered_symbols: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


def reconcile(
    *,
    robinhood_positions: list[Position],
    sleeves: SleeveRegistry,
    theses: ThesisRegistry,
    classification_cache_path=None,
    now=None,
    quantity_tolerance: float = 1e-8,
    value_tolerance: float = 0.5,
) -> ReconciliationReport:
    findings: list[ReconciliationFinding] = []
    rh = {p.symbol.upper(): p for p in robinhood_positions}

    local_symbols = set(sleeves._data.get("records", {}))
    for rec in sleeves.all_records():
        if rec and rec.status in LIVE_SLEEVE:
            local_symbols.add(rec.symbol)

    for symbol, pos in rh.items():
        sleeve = sleeves.get(symbol)
        if sleeve is None:
            findings.append(
                ReconciliationFinding(
                    code="ROBINHOOD_POSITION_MISSING_LOCALLY",
                    symbol=symbol,
                    message="Robinhood holds a position that has no sleeve registry entry.",
                    details={"quantity": pos.quantity, "market_value": pos.market_value},
                )
            )
            findings.append(
                ReconciliationFinding(
                    code="UNREGISTERED_POSITION",
                    symbol=symbol,
                    message="Position is UNREGISTERED_POSITION. Do not invent a sleeve. No risk-increasing ADD until sleeve and thesis are explicit.",
                    details={"registry_status": PositionRegistryStatus.UNREGISTERED_POSITION.value},
                )
            )
            findings.append(
                ReconciliationFinding(
                    code="MISSING_SLEEVE",
                    symbol=symbol,
                    message="No sleeve assignment for a live Robinhood position.",
                )
            )
        else:
            if sleeve.quantity is not None and abs(sleeve.quantity - pos.quantity) > quantity_tolerance:
                findings.append(
                    ReconciliationFinding(
                        code="QUANTITY_MISMATCH",
                        symbol=symbol,
                        message="Registry quantity differs from Robinhood quantity.",
                        details={"local": sleeve.quantity, "robinhood": pos.quantity},
                    )
                )
            if sleeve.market_value is not None and abs(sleeve.market_value - pos.market_value) > value_tolerance:
                findings.append(
                    ReconciliationFinding(
                        code="MARKET_VALUE_DIFFERENCE",
                        symbol=symbol,
                        message="Registry market value differs from Robinhood market value.",
                        details={"local": sleeve.market_value, "robinhood": pos.market_value},
                    )
                )

        thesis = theses.active_for_symbol(symbol)
        all_th = theses.all_for_symbol(symbol)
        if thesis is None:
            findings.append(
                ReconciliationFinding(
                    code="MISSING_THESIS",
                    symbol=symbol,
                    message="Live Robinhood position has no ACTIVE thesis.",
                )
            )
        closed_live = [t for t in all_th if t.status in {ThesisStatus.CLOSED, ThesisStatus.INVALIDATED, ThesisStatus.REJECTED}]
        if closed_live and thesis is None:
            findings.append(
                ReconciliationFinding(
                    code="CLOSED_THESIS_LIVE_POSITION",
                    symbol=symbol,
                    message="Thesis is closed/invalidated/rejected but Robinhood still holds the name.",
                    details={"thesis_ids": [t.thesis_id for t in closed_live]},
                )
            )

        cached = get_cached(symbol, classification_cache_path)
        if cached is None:
            findings.append(
                ReconciliationFinding(
                    code="STALE_CLASSIFICATION",
                    symbol=symbol,
                    message="No cached classification for a live position.",
                )
            )
        elif now is not None and field_is_stale("classification", cached.get("refreshed_at"), now):
            findings.append(
                ReconciliationFinding(
                    code="STALE_CLASSIFICATION",
                    symbol=symbol,
                    message="Cached classification is stale versus field TTL.",
                    details={"refreshed_at": cached.get("refreshed_at")},
                )
            )

    for rec in sleeves.all_records():
        if rec is None:
            continue
        if rec.status in {SleeveAssignmentStatus.ACTIVE, SleeveAssignmentStatus.REDUCING} and rec.symbol not in rh:
            findings.append(
                ReconciliationFinding(
                    code="LOCAL_ACTIVE_NOT_HELD",
                    symbol=rec.symbol,
                    message="Local ACTIVE/REDUCING sleeve has no matching Robinhood position.",
                    details={"sleeve": rec.sleeve.value, "status": rec.status.value},
                )
            )

    for raw in theses._data.get("records", {}).values():
        status = ThesisStatus(raw["status"])
        symbol = str(raw.get("symbol", "")).upper()
        if status in ACTIVE_THESIS and symbol not in rh:
            findings.append(
                ReconciliationFinding(
                    code="ACTIVE_THESIS_NO_LIVE_POSITION",
                    symbol=symbol,
                    message="ACTIVE thesis has no live Robinhood position.",
                    details={"thesis_id": raw.get("thesis_id")},
                )
            )

    unknown = []
    for p in robinhood_positions:
        if not p.symbol or not str(p.symbol).strip():
            findings.append(
                ReconciliationFinding(
                    code="UNKNOWN_SYMBOL",
                    symbol=None,
                    message="Robinhood position is missing a symbol.",
                )
            )
            unknown.append("")

    unregistered = sorted({f.symbol for f in findings if f.code == "UNREGISTERED_POSITION" and f.symbol})
    return ReconciliationReport(findings=findings, unregistered_symbols=unregistered)


def registry_status_for(symbol: str, sleeves: SleeveRegistry) -> PositionRegistryStatus:
    rec = sleeves.get(symbol)
    if rec is None:
        return PositionRegistryStatus.UNREGISTERED_POSITION
    return PositionRegistryStatus.REGISTERED
