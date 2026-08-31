"""Job catalog. Cadence is session-aware; the process itself never stops."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from agentic_portfolio.agent.session import MarketPhase

ALL_PHASES = frozenset(MarketPhase)
OPEN = frozenset({MarketPhase.MARKET_OPEN})
PRE = frozenset({MarketPhase.PREMARKET})
POST = frozenset({MarketPhase.AFTER_CLOSE})
NIGHT = frozenset({MarketPhase.OVERNIGHT})
CLOSED_DAYS = frozenset({MarketPhase.WEEKEND, MarketPhase.HOLIDAY})
OFF = frozenset({MarketPhase.PREMARKET, MarketPhase.AFTER_CLOSE, MarketPhase.OVERNIGHT, MarketPhase.WEEKEND, MarketPhase.HOLIDAY})


@dataclass(frozen=True)
class JobSpec:
    name: str
    phases: frozenset[MarketPhase]
    cadence: str
    every_minutes: int | None = None
    allow_ai: bool = False
    requires_broker: bool = False
    requires_regular_hours: bool = False
    description: str = ""


def catalog() -> list[JobSpec]:
    return [
        JobSpec("HEARTBEAT", ALL_PHASES, "interval", every_minutes=1, description="Persist health/uptime"),
        JobSpec("BROKER_RECONNECT", ALL_PHASES, "interval", every_minutes=5, requires_broker=False, description="Load/refresh OAuth and reconnect"),
        JobSpec("APPROVAL_EXPIRY", ALL_PHASES, "interval", every_minutes=5, description="Expire pending approvals"),
        JobSpec("WATCH_STALE_CLEANUP", ALL_PHASES, "interval", every_minutes=60, description="Expire stale theses"),
        JobSpec("LIVE_ACCOUNT_REFRESH", ALL_PHASES, "interval", every_minutes=15, requires_broker=True, description="Live account/position refresh"),
        JobSpec("POSITION_MONITOR", OPEN, "interval", every_minutes=15, requires_broker=True, requires_regular_hours=True, description="Position monitoring"),
        JobSpec("QUOTE_REFRESH", OPEN, "interval", every_minutes=15, requires_broker=True, requires_regular_hours=True, description="Quote/liquidity refresh"),
        JobSpec("CANDIDATE_DISCOVERY", OPEN, "interval", every_minutes=30, requires_broker=True, description="Candidate discovery"),
        JobSpec("WATCH_CONDITION_MONITOR", OPEN, "interval", every_minutes=15, requires_broker=True, requires_regular_hours=True, description="Watch condition monitoring"),
        JobSpec("RISK_MONITOR", OPEN, "interval", every_minutes=15, requires_broker=True, description="Deterministic risk monitoring"),
        JobSpec("MARKET_OPEN_CONDITIONAL_VALIDATE", OPEN, "interval", every_minutes=15, requires_broker=True, requires_regular_hours=True, description="Validate next-session conditional plans"),
        JobSpec("AI_REASSESS_IF_WARRANTED", OPEN, "interval", every_minutes=15, allow_ai=True, description="AI reassessment only on material events"),
        JobSpec("PREMARKET_NEWS", PRE, "once_per_session", allow_ai=False, description="Refresh overnight news"),
        JobSpec("PREMARKET_THESIS_REVALIDATE", PRE, "once_per_session", allow_ai=False, description="Revalidate watchlist theses"),
        JobSpec("PREMARKET_PREPARE_CONDITIONAL", PRE, "once_per_session", allow_ai=False, description="Prepare conditional opening-session proposals"),
        JobSpec("POSTMARKET_CLOSE_ANALYSIS", POST, "once_per_session", description="Closing-price analysis"),
        JobSpec("POSTMARKET_RECONCILE", POST, "once_per_session", requires_broker=True, description="Portfolio reconciliation"),
        JobSpec("POSTMARKET_DAILY_SNAPSHOT", POST, "once_per_session", description="Daily performance snapshot"),
        JobSpec("POSTMARKET_CANDIDATE_RANK", POST, "once_per_session", description="Candidate ranking from completed session"),
        JobSpec("LUNA_SCREEN", POST | CLOSED_DAYS, "once_per_day", allow_ai=True, description="Cheap Luna screening when warranted"),
        JobSpec("TERRA_RESEARCH", POST | CLOSED_DAYS, "once_per_day", allow_ai=True, description="Terra research when warranted"),
        JobSpec("RESEARCH_QUEUE_WORKER", NIGHT | PRE | POST | CLOSED_DAYS, "interval", every_minutes=30, allow_ai=True, description="Consume research queue through AI Gateway"),
        JobSpec("THESIS_WATCH_CREATE", POST | CLOSED_DAYS, "once_per_day", description="Thesis/watchlist creation"),
        JobSpec("NEXT_SESSION_PLANS", POST | CLOSED_DAYS, "once_per_day", description="Conditional next-session plans"),
        JobSpec("OVERNIGHT_NEWS", NIGHT, "interval", every_minutes=120, description="News/catalyst monitoring"),
        JobSpec("OVERNIGHT_THESIS", NIGHT, "interval", every_minutes=240, description="Thesis updates"),
        JobSpec("OVERNIGHT_FUNDAMENTALS", NIGHT, "interval", every_minutes=360, description="Fundamentals when new data exists"),
        JobSpec("OVERNIGHT_RISK", NIGHT, "interval", every_minutes=240, description="Portfolio risk review"),
        JobSpec("OVERNIGHT_WATCH_MAINTAIN", NIGHT, "interval", every_minutes=120, description="Watchlist maintenance"),
        JobSpec("WEEKEND_SESSION_ANALYSIS", CLOSED_DAYS, "once_per_day", description="Latest completed-session analysis"),
        JobSpec("WEEKEND_DEEP_RESEARCH", CLOSED_DAYS, "once_per_day", allow_ai=True, description="Deeper candidate research"),
        JobSpec("WEEKEND_PORTFOLIO_REVIEW", CLOSED_DAYS, "once_per_day", requires_broker=True, description="Read-only portfolio review"),
        JobSpec("WEEKEND_WATCH_CONSTRUCT", CLOSED_DAYS, "once_per_day", description="Watchlist construction"),
        JobSpec("WEEKEND_STALE_CLEANUP", CLOSED_DAYS, "once_per_day", description="Stale thesis cleanup"),
        JobSpec("WEEKEND_NEXT_SESSION_PREP", CLOSED_DAYS, "once_per_day", description="Prepare for next trading session"),
    ]


def specs_by_name() -> dict[str, JobSpec]:
    return {spec.name: spec for spec in catalog()}


def specs_for_phase(phase: MarketPhase) -> list[JobSpec]:
    return [spec for spec in catalog() if phase in spec.phases]


def iter_catalog() -> Iterable[JobSpec]:
    return catalog()
