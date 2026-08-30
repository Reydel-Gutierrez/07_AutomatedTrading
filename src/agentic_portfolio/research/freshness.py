"""Sleeve-specific research freshness. Not a single global TTL."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentic_portfolio.policy import load_research_config
from agentic_portfolio.research.types import (
    RefreshTrigger,
    ResearchFreshness,
    ResearchReport,
    ResearchStatus,
)
from agentic_portfolio.schemas import Sleeve


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def freshness_horizon(sleeve: Sleeve, config: dict | None = None) -> timedelta:
    cfg = config or load_research_config()
    hours = float((cfg.get("freshness_hours") or {}).get(sleeve.value, 72))
    return timedelta(hours=hours)


def evaluate_freshness(
    report: ResearchReport,
    *,
    now: datetime | None = None,
    earnings_event: bool = False,
    major_news: bool = False,
    material_filing: bool = False,
    regime_changed: bool = False,
    price_move_pct: float | None = None,
    thesis_concern: bool = False,
    config: dict | None = None,
) -> tuple[ResearchFreshness, list[str]]:
    """Return freshness and trigger names. Tactical expires faster than Core."""
    cfg = config or load_research_config()
    now = now or datetime.now(timezone.utc)
    triggers: list[str] = []
    completed = parse_ts(report.completed_at or report.observed_at or report.started_at)
    horizon = freshness_horizon(report.provisional_sleeve, cfg)
    if completed and now - completed >= horizon:
        triggers.append(RefreshTrigger.ELAPSED_TIME.value)
    if earnings_event:
        triggers.append(RefreshTrigger.EARNINGS_EVENT.value)
    if major_news:
        triggers.append(RefreshTrigger.MAJOR_NEWS.value)
    if material_filing:
        triggers.append(RefreshTrigger.MATERIAL_FILING.value)
    if regime_changed:
        triggers.append(RefreshTrigger.REGIME_CHANGE.value)
    if thesis_concern:
        triggers.append(RefreshTrigger.THESIS_CONCERN.value)
    move_map = (cfg.get("refresh_triggers") or {}).get("price_move_pct") or {}
    thresh = move_map.get(report.provisional_sleeve.value)
    if price_move_pct is not None and thresh is not None and abs(float(price_move_pct)) >= float(thresh):
        triggers.append(RefreshTrigger.PRICE_MOVE.value)
    if triggers:
        return ResearchFreshness.RESEARCH_REFRESH_REQUIRED, triggers
    if completed and now - completed >= horizon / 2:
        return ResearchFreshness.STALE, []
    return ResearchFreshness.FRESH, []


def apply_freshness(report: ResearchReport, freshness: ResearchFreshness, triggers: list[str]) -> ResearchReport:
    report.freshness = freshness
    report.refresh_triggers = list(triggers)
    if freshness == ResearchFreshness.RESEARCH_REFRESH_REQUIRED:
        report.research_status = ResearchStatus.RESEARCH_STALE
    return report
