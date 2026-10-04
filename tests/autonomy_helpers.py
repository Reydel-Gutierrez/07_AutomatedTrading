"""Shared fixtures for the unified order pipeline (research → Risk Gate → intake → executor)."""

from __future__ import annotations

from pathlib import Path

from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.types import (
    EvidenceItem,
    EvidenceKind,
    ResearchConclusion,
    ResearchConfidence,
    ResearchFreshness,
    ResearchReport,
    ResearchStatus,
    ResearchSubjectKind,
)
from agentic_portfolio.schemas import (
    ClassificationStatus,
    Decision,
    LiquidityInputs,
    ProposedAction,
    SecurityClass,
    Sleeve,
    to_dict,
)

TS = "2026-08-27T20:00:00+00:00"


def _fact(name, value):
    return EvidenceItem(
        evidence_id=f"fact:{name}",
        kind=EvidenceKind.OBSERVED_FACT,
        name=name,
        value=value,
        source="test",
        observed_at=TS,
    )


def research_report(
    symbol: str,
    price: float,
    *,
    sleeve: Sleeve = Sleeve.CORE_GROWTH,
    security_class: SecurityClass = SecurityClass.INDIVIDUAL_EQUITY,
    sector: str = "INFORMATION_TECHNOLOGY",
    average_volume: float = 20_000_000.0,
    rid: str | None = None,
) -> ResearchReport:
    return ResearchReport(
        research_id=rid or f"res-{symbol}",
        candidate_id=f"cand-{symbol}",
        symbol=symbol,
        started_at=TS,
        completed_at=TS,
        provisional_sleeve=sleeve,
        security_class=security_class,
        sector=sector,
        market_price=price,
        research_status=ResearchStatus.RESEARCH_COMPLETE,
        subject_kind=ResearchSubjectKind.NEW_CANDIDATE,
        executive_summary=f"{symbol} research.",
        research_conclusion=ResearchConclusion.ADVANCE_TO_THESIS,
        confidence=ResearchConfidence.MEDIUM,
        freshness=ResearchFreshness.FRESH,
        facts=[_fact("market_price", price), _fact("average_volume", average_volume)],
        derived_metrics=[_fact("spread_pct", 0.001)],
    )


def seed_research(root: Path, symbol: str, price: float, **kwargs) -> ResearchReport:
    report = research_report(symbol, price, **kwargs)
    store = ResearchStore(Path(root))
    existing = store.get(report.research_id)
    if existing is not None:
        return existing
    store.save(report)
    return report


def action_snapshot(
    symbol: str,
    *,
    decision: Decision = Decision.BUY,
    notional: float | None = None,
    price: float | None = None,
    sleeve: Sleeve | None = Sleeve.CORE_GROWTH,
    security_class: SecurityClass = SecurityClass.BROAD_MARKET_INDEX_ETF,
    sector: str | None = None,
    adv: float = 1e12,
) -> dict:
    """ProposedAction snapshot as ProposalIntake persists it on an approval."""
    return to_dict(
        ProposedAction(
            symbol=symbol.upper(),
            decision=decision,
            security_class=security_class,
            classification_status=ClassificationStatus.VALIDATED,
            sleeve=sleeve,
            current_price=price,
            proposed_notional=notional,
            sector=sector,
            liquidity=LiquidityInputs(median_daily_dollar_volume_20d=adv),
        )
    )
