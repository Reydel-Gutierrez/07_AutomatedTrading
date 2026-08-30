"""Portfolio correlation / overlap as an observable — not a hard veto.

A numeric correlation ceiling is intentionally absent. Adding one requires
empirical portfolio testing, not an invented constant. Schema fields
`future_hard_limit` and `reject_on_limit` exist so a later validated rule
can be attached without rewriting portfolio context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CorrelationStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass
class PairwiseCorrelation:
    symbol_a: str
    symbol_b: str
    coefficient: float | None
    window: str | None = None
    status: CorrelationStatus = CorrelationStatus.INSUFFICIENT_DATA


@dataclass
class CorrelationObservation:
    """Informational risk factor. Never used as a sole trade rejection today."""

    status: CorrelationStatus = CorrelationStatus.INSUFFICIENT_DATA
    pairwise: list[PairwiseCorrelation] = field(default_factory=list)
    sleeve_level: dict[str, float] = field(default_factory=dict)
    sector_overlap: dict[str, float] = field(default_factory=dict)
    common_factor_exposure: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # Reserved for a future empirically validated deterministic cap.
    future_hard_limit: float | None = None
    reject_on_limit: bool = False  # must stay False until a human-approved policy sets a limit

    def as_warning_codes(self) -> list[tuple[str, str]]:
        codes: list[tuple[str, str]] = []
        if self.status == CorrelationStatus.INSUFFICIENT_DATA:
            codes.append(("CORRELATION_INSUFFICIENT_DATA", "Correlation/overlap data is insufficient; informational only."))
        elif self.status == CorrelationStatus.PARTIAL:
            codes.append(("CORRELATION_PARTIAL", "Correlation/overlap is partial; informational only."))
        elif self.status == CorrelationStatus.AVAILABLE:
            codes.append(("CORRELATION_AVAILABLE", "Correlation/overlap observed; no hard cap is in force."))
        if self.future_hard_limit is not None and self.reject_on_limit:
            codes.append(("CORRELATION_LIMIT_RESERVED", "A future hard limit is configured; current policy still does not auto-reject."))
        return codes


def observe_correlation(
    *,
    pairwise: list[PairwiseCorrelation] | None = None,
    sleeve_level: dict[str, float] | None = None,
    sector_overlap: dict[str, float] | None = None,
    common_factor_exposure: dict[str, float] | None = None,
) -> CorrelationObservation:
    pairwise = pairwise or []
    usable = [p for p in pairwise if p.coefficient is not None]
    incomplete_pairs = bool(pairwise) and len(usable) < len(pairwise)
    layers = (
        bool(usable),
        bool(sleeve_level),
        bool(sector_overlap),
        bool(common_factor_exposure),
    )
    filled = sum(layers)
    if filled == 0:
        status = CorrelationStatus.INSUFFICIENT_DATA
    elif incomplete_pairs or filled < 3:
        status = CorrelationStatus.PARTIAL
    else:
        status = CorrelationStatus.AVAILABLE
    return CorrelationObservation(
        status=status,
        pairwise=pairwise,
        sleeve_level=dict(sleeve_level or {}),
        sector_overlap=dict(sector_overlap or {}),
        common_factor_exposure=dict(common_factor_exposure or {}),
        future_hard_limit=None,
        reject_on_limit=False,
    )


def empty_correlation() -> CorrelationObservation:
    return CorrelationObservation(status=CorrelationStatus.INSUFFICIENT_DATA)
