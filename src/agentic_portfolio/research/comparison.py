"""Compare related research reports. No arbitrary sector candidate-count rejection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from agentic_portfolio.policy import load_research_config
from agentic_portfolio.research.types import ComparisonDimension, ResearchComparison, ResearchReport
from agentic_portfolio.schemas import to_dict


class ComparisonReasoner(Protocol):
    def compare(self, reports: list[ResearchReport], *, portfolio_overlap_notes: str | None = None) -> dict[str, Any]: ...


class ScriptedComparisonReasoner:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload

    def compare(self, reports: list[ResearchReport], *, portfolio_overlap_notes: str | None = None) -> dict[str, Any]:
        if self.payload:
            return dict(self.payload)
        symbols = [r.symbol for r in reports]
        dims = []
        for name in (load_research_config().get("comparison_dimensions") or []):
            dims.append({"name": name, "ranking": symbols, "notes": "scripted_passthrough", "uncertainty": "HIGH"})
        return {
            "dimensions": dims,
            "relative_conclusion": "Comparison requires AI judgment; this fallback only groups peers.",
            "evidence_quality_notes": "No comparison reasoner payload supplied.",
            "portfolio_overlap_notes": portfolio_overlap_notes,
        }


def build_comparison(
    reports: list[ResearchReport],
    *,
    reasoner: ComparisonReasoner | None = None,
    now: datetime | None = None,
    portfolio_overlap_notes: str | None = None,
) -> ResearchComparison:
    if not reports:
        raise ValueError("comparison requires at least one ResearchReport")
    reasoner = reasoner or ScriptedComparisonReasoner()
    payload = reasoner.compare(reports, portfolio_overlap_notes=portfolio_overlap_notes)
    dims = []
    for item in payload.get("dimensions") or []:
        dims.append(
            ComparisonDimension(
                name=str(item.get("name")),
                ranking=[str(s) for s in (item.get("ranking") or [])],
                notes=item.get("notes"),
                evidence_refs=[str(r) for r in (item.get("evidence_refs") or [])],
                uncertainty=item.get("uncertainty"),
            )
        )
    group_ids = {r.comparison_group_id for r in reports if r.comparison_group_id}
    return ResearchComparison(
        comparison_id=str(uuid4()),
        comparison_group_id=next(iter(group_ids), None),
        symbols=[r.symbol for r in reports],
        research_ids=[r.research_id for r in reports],
        dimensions=dims,
        relative_conclusion=payload.get("relative_conclusion"),
        created_at=(now or datetime.now(timezone.utc)).isoformat(),
        evidence_quality_notes=payload.get("evidence_quality_notes"),
        portfolio_overlap_notes=payload.get("portfolio_overlap_notes") or portfolio_overlap_notes,
        unsupported_claims=list(payload.get("unsupported_claims") or []),
    )


def comparison_request_payload(reports: list[ResearchReport]) -> list[dict[str, Any]]:
    return [to_dict(r) for r in reports]
