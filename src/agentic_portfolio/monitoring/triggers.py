"""Detect monitoring triggers from facts. Not an investment-rule engine."""

from __future__ import annotations

from agentic_portfolio.monitoring.types import (
    PRICE_ONLY_KINDS,
    MonitoringFacts,
    MonitoringState,
    PositionObservation,
    Trigger,
    TriggerKind,
)
from agentic_portfolio.policy import load_research_config
from agentic_portfolio.research.types import ResearchFreshness
from agentic_portfolio.schemas import RiskState, Sleeve


def detect_triggers(
    facts: MonitoringFacts,
    observation: PositionObservation | None = None,
    *,
    research_config: dict | None = None,
) -> list[Trigger]:
    """Flag observed events. Does not decide attractiveness or CORE invalidation."""
    obs = observation or PositionObservation(symbol=facts.symbol)
    out: list[Trigger] = []
    if facts.missing_thesis:
        out.append(Trigger(TriggerKind.MISSING_THESIS, "held name has no thesis record"))
    if facts.missing_research:
        out.append(Trigger(TriggerKind.MISSING_RESEARCH, "held name has no ResearchReport"))
    if facts.earnings_event:
        out.append(Trigger(TriggerKind.EARNINGS_EVENT, "earnings event observed", ["earnings"]))
    if facts.major_news:
        out.append(Trigger(TriggerKind.MAJOR_NEWS, "major news observed", ["news"]))
    if facts.material_filing:
        out.append(Trigger(TriggerKind.MATERIAL_FILING, "material SEC filing observed", ["filings"]))
    if _material_price_move(facts, research_config):
        out.append(
            Trigger(
                TriggerKind.PRICE_MOVE,
                f"material price move {facts.price_move_pct}",
                ["price"],
            )
        )
    if facts.research_freshness == ResearchFreshness.RESEARCH_REFRESH_REQUIRED.value:
        out.append(
            Trigger(
                TriggerKind.RESEARCH_REFRESH_REQUIRED,
                "research freshness requires refresh",
                list(facts.refresh_triggers),
            )
        )
    elif facts.research_freshness == ResearchFreshness.STALE.value:
        out.append(Trigger(TriggerKind.RESEARCH_STALE, "research is stale vs sleeve horizon"))
    if _review_trigger_matched(facts):
        out.append(Trigger(TriggerKind.THESIS_REVIEW_TRIGGER, "stored review trigger matched observed event"))
    if obs.fundamental_invalidation_observed:
        out.append(
            Trigger(
                TriggerKind.THESIS_INVALIDATION_CANDIDATE,
                "fundamental invalidation evidence observed",
                ["fundamentals"],
            )
        )
    if _portfolio_risk_elevated(facts):
        out.append(
            Trigger(
                TriggerKind.PORTFOLIO_RISK_STATE,
                f"portfolio risk {facts.portfolio_risk_state} halt={facts.daily_risk_halt}",
            )
        )
    out.extend(_sleeve_triggers(facts, obs))
    return out


def preliminary_state(facts: MonitoringFacts, triggers: list[Trigger]) -> MonitoringState:
    kinds = {t.kind for t in triggers}
    if TriggerKind.TACTICAL_PRICE_OR_TECHNICAL in kinds or TriggerKind.SPECULATIVE_RISK_OR_CATALYST in kinds:
        return MonitoringState.EXIT_CONDITION_TRIGGERED
    if TriggerKind.EXIT_POLICY_CONDITION in kinds:
        return MonitoringState.EXIT_CONDITION_TRIGGERED
    if TriggerKind.RESEARCH_REFRESH_REQUIRED in kinds or TriggerKind.MISSING_RESEARCH in kinds:
        return MonitoringState.RESEARCH_REFRESH_REQUIRED
    if triggers:
        return MonitoringState.REVIEW_REQUIRED
    return MonitoringState.HEALTHY


def is_price_move_alone(triggers: list[Trigger]) -> bool:
    kinds = {t.kind for t in triggers}
    return bool(kinds) and TriggerKind.PRICE_MOVE in kinds and kinds <= PRICE_ONLY_KINDS


def _material_price_move(facts: MonitoringFacts, research_config: dict | None) -> bool:
    if facts.price_move_pct is None or facts.sleeve is None:
        return False
    cfg = research_config or load_research_config()
    thresh = ((cfg.get("refresh_triggers") or {}).get("price_move_pct") or {}).get(facts.sleeve.value)
    if thresh is None:
        return False
    return abs(float(facts.price_move_pct)) >= float(thresh)


def _review_trigger_matched(facts: MonitoringFacts) -> bool:
    labels = " ".join(facts.review_triggers).lower()
    if not labels:
        return False
    if facts.earnings_event and "earn" in labels:
        return True
    if facts.major_news and ("news" in labels or "disclosure" in labels):
        return True
    if facts.material_filing and ("filing" in labels or "10-q" in labels or "10-k" in labels or "8-k" in labels):
        return True
    return False


def _portfolio_risk_elevated(facts: MonitoringFacts) -> bool:
    if facts.daily_risk_halt:
        return True
    return facts.portfolio_risk_state in {
        RiskState.RISK_REDUCTION.value,
        RiskState.DEFENSIVE.value,
        RiskState.HALTED.value,
    }


def _sleeve_triggers(facts: MonitoringFacts, obs: PositionObservation) -> list[Trigger]:
    out: list[Trigger] = []
    sleeve = facts.sleeve
    if sleeve == Sleeve.OPPORTUNISTIC and (
        facts.earnings_event or facts.major_news or facts.material_filing or _material_price_move(facts, None)
    ):
        out.append(
            Trigger(
                TriggerKind.OPPORTUNISTIC_DISLOCATION_REVIEW,
                "recovery thesis vs structural deterioration requires assessment",
                sleeve_specific=True,
            )
        )
    if sleeve == Sleeve.TACTICAL and facts.predefined_price_or_technical and _tactical_invalidation_observed(facts, obs):
        out.append(
            Trigger(
                TriggerKind.TACTICAL_PRICE_OR_TECHNICAL,
                "predefined tactical price/technical invalidation observed",
                ["price", "technicals"],
                sleeve_specific=True,
            )
        )
        out.append(
            Trigger(
                TriggerKind.EXIT_POLICY_CONDITION,
                "tactical exit-policy condition observed (not a broker stop)",
                sleeve_specific=True,
            )
        )
    if sleeve == Sleeve.SPECULATIVE and facts.predefined_risk_or_catalyst and _spec_invalidation_observed(facts, obs):
        out.append(
            Trigger(
                TriggerKind.SPECULATIVE_RISK_OR_CATALYST,
                "predefined speculative risk/catalyst invalidation observed",
                ["catalyst", "risk"],
                sleeve_specific=True,
            )
        )
        out.append(
            Trigger(
                TriggerKind.EXIT_POLICY_CONDITION,
                "speculative exit-policy condition observed (not a broker stop)",
                sleeve_specific=True,
            )
        )
    return out


def _tactical_invalidation_observed(facts: MonitoringFacts, obs: PositionObservation) -> bool:
    if obs.price_invalidation_observed or obs.technical_invalidation_observed:
        return True
    policy = facts.exit_policy or {}
    price = facts.current_price
    sma = facts.technicals.get("sma_50") or obs.technicals.get("sma_50")
    if policy.get("technical_invalidation") and price is not None and sma is not None:
        try:
            if float(price) < float(sma):
                return True
        except (TypeError, ValueError):
            pass
    return _material_price_move(facts, None) and bool(policy.get("price_invalidation") or policy.get("technical_invalidation"))


def _spec_invalidation_observed(facts: MonitoringFacts, obs: PositionObservation) -> bool:
    if obs.catalyst_failed or obs.risk_event:
        return True
    return bool(facts.earnings_event or facts.material_filing or facts.major_news)
