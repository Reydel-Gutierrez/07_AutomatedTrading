"""Assemble monitoring facts from holdings, thesis, research, and observations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from agentic_portfolio.monitoring.types import MonitoringFacts, MonitoringPacket, PositionObservation
from agentic_portfolio.policy import load_monitoring_config, load_policy
from agentic_portfolio.research.freshness import evaluate_freshness
from agentic_portfolio.research.packet import freeze_portfolio
from agentic_portfolio.research.types import ResearchFreshness, ResearchReport
from agentic_portfolio.schemas import ExitPolicy, PortfolioContext, Position, Sleeve, ThesisRecord, to_dict

MATERIAL_FORMS = ("8-K", "10-Q", "10-K", "6-K", "20-F")


def price_move_pct(
    current: float | None,
    reference: float | None,
    observed: float | None = None,
) -> float | None:
    if observed is not None:
        return float(observed)
    if current is None or reference is None or reference == 0:
        return None
    return (float(current) - float(reference)) / float(reference)


def reference_price(position: Position, thesis: ThesisRecord | None, report: ResearchReport | None, obs: PositionObservation | None) -> float | None:
    if obs and obs.reference_price is not None:
        return float(obs.reference_price)
    if thesis and thesis.last_price_observed is not None:
        return float(thesis.last_price_observed)
    if report and report.market_price is not None:
        return float(report.market_price)
    if obs and obs.previous_close is not None:
        return float(obs.previous_close)
    return position.current_price


def assemble_facts(
    position: Position,
    context: PortfolioContext,
    *,
    thesis: ThesisRecord | None,
    report: ResearchReport | None,
    observation: PositionObservation | None = None,
    now: datetime | None = None,
    config: dict | None = None,
) -> MonitoringFacts:
    now = now or datetime.now(timezone.utc)
    cfg = config or load_monitoring_config()
    obs = observation or PositionObservation(symbol=position.symbol)
    price = obs.current_price if obs.current_price is not None else position.current_price
    ref = reference_price(position, thesis, report, obs)
    move = price_move_pct(price, ref, obs.price_move_pct)
    freshness = None
    refresh_triggers: list[str] = []
    if report is not None:
        freshness, refresh_triggers = evaluate_freshness(
            report,
            now=now,
            earnings_event=obs.earnings_event,
            major_news=obs.major_news,
            material_filing=obs.material_filing or _filing_is_material(obs, cfg),
            price_move_pct=move,
        )
    exit_raw = _exit_dict(thesis.exit_policy if thesis else None)
    sleeve = thesis.sleeve if thesis else position.sleeve
    nav = context.current_nav or 0.0
    pct = (position.market_value / nav) if nav else 0.0
    return MonitoringFacts(
        symbol=position.symbol.upper(),
        sleeve=sleeve,
        position_pct=pct,
        current_price=price,
        reference_price=ref,
        price_move_pct=move,
        earnings_event=bool(obs.earnings_event),
        major_news=bool(obs.major_news),
        material_filing=bool(obs.material_filing or _filing_is_material(obs, cfg)),
        research_freshness=freshness.value if freshness else None,
        refresh_triggers=list(refresh_triggers),
        thesis_id=thesis.thesis_id if thesis else None,
        thesis_status=thesis.status.value if thesis else None,
        invalidation_conditions=list(thesis.invalidation_conditions) if thesis else [],
        review_triggers=list(thesis.review_triggers) if thesis else [],
        exit_policy=exit_raw,
        predefined_price_or_technical=_has_predefined_price_or_technical(exit_raw, sleeve),
        predefined_risk_or_catalyst=_has_predefined_risk(exit_raw, sleeve),
        portfolio_risk_state=context.risk_state.value if context.risk_state else None,
        daily_risk_halt=bool(context.daily_risk_halt),
        technicals=dict(obs.technicals),
        missing_thesis=thesis is None,
        missing_research=report is None,
        sources_observed=list(obs.sources_observed),
    )


def build_packet(
    facts: MonitoringFacts,
    triggers: list,
    preliminary_state,
    context: PortfolioContext,
    *,
    thesis: ThesisRecord | None = None,
    report: ResearchReport | None = None,
    now: datetime | None = None,
    config: dict | None = None,
) -> MonitoringPacket:
    now = now or datetime.now(timezone.utc)
    cfg = config or load_monitoring_config()
    policy = load_policy()
    sleeve_key = facts.sleeve.value if facts.sleeve else None
    return MonitoringPacket(
        packet_id=str(uuid4()),
        assembled_at=now.isoformat(),
        facts=facts,
        triggers=list(triggers),
        preliminary_state=preliminary_state,
        policy_context={
            "price_move_alone_does_not_invalidate_core": True,
            "exit_condition_is_not_a_broker_stop": True,
            "no_action_always_valid": True,
            "risk_state": facts.portfolio_risk_state,
            "daily_risk_halt": facts.daily_risk_halt,
            "concentration": policy.get("concentration"),
        },
        portfolio_facts=freeze_portfolio(context),
        research_brief=_report_brief(report) if report else None,
        thesis=to_dict(thesis) if thesis else None,
        sleeve_behavior=dict((cfg.get("sleeve_behavior") or {}).get(sleeve_key) or {}),
    )


def _exit_dict(policy: ExitPolicy | dict[str, Any] | None) -> dict[str, Any] | None:
    if policy is None:
        return None
    if isinstance(policy, dict):
        return dict(policy)
    return to_dict(policy)


def _has_predefined_price_or_technical(exit_raw: dict[str, Any] | None, sleeve: Sleeve | None) -> bool:
    if sleeve != Sleeve.TACTICAL or not exit_raw:
        return False
    return bool(exit_raw.get("price_invalidation") or exit_raw.get("technical_invalidation"))


def _has_predefined_risk(exit_raw: dict[str, Any] | None, sleeve: Sleeve | None) -> bool:
    if sleeve != Sleeve.SPECULATIVE or not exit_raw:
        return False
    return bool(exit_raw.get("risk_invalidation") or exit_raw.get("event_invalidation"))


def _filing_is_material(obs: PositionObservation, config: dict) -> bool:
    forms = {str(f).upper() for f in (config.get("material_forms") or MATERIAL_FORMS)}
    for row in obs.filings:
        form = str(row.get("form_type") or row.get("form") or "").upper()
        if form in forms:
            return True
    return False


def _report_brief(report: ResearchReport) -> dict[str, Any]:
    return {
        "research_id": report.research_id,
        "symbol": report.symbol,
        "provisional_sleeve": report.provisional_sleeve.value,
        "security_class": report.security_class.value if report.security_class else None,
        "market_price": report.market_price,
        "research_status": report.research_status.value,
        "research_conclusion": report.research_conclusion.value if report.research_conclusion else None,
        "freshness": report.freshness.value,
        "executive_summary": report.executive_summary,
        "invalidation_candidates": list(report.invalidation_candidates),
        "key_risks": list(report.key_risks),
        "key_catalysts": list(report.key_catalysts),
    }
