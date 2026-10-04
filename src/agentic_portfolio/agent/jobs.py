"""Job catalog. Cadence is session-aware; the process itself never stops."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from agentic_portfolio.agent.session import MarketPhase

ALL_PHASES = frozenset(MarketPhase)
OPEN = frozenset({MarketPhase.MARKET_OPEN})
PRE = frozenset({MarketPhase.PREMARKET})
POST = frozenset({MarketPhase.AFTER_CLOSE})
NIGHT = frozenset({MarketPhase.OVERNIGHT})
CLOSED_DAYS = frozenset({MarketPhase.WEEKEND, MarketPhase.HOLIDAY})
OFF = frozenset({MarketPhase.PREMARKET, MarketPhase.AFTER_CLOSE, MarketPhase.OVERNIGHT, MarketPhase.WEEKEND, MarketPhase.HOLIDAY})

MARKET_OPEN_DEEP_RESEARCH_PER_CYCLE = 1
OFF_HOURS_DEEP_RESEARCH_PER_CYCLE = 2
MARKET_OPEN_DISCOVERY_MINUTES = 15
RESEARCH_QUEUE_MINUTES = 30


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
    open_cadence: str | None = None
    open_every_minutes: int | None = None

    def cadence_for(self, phase: MarketPhase) -> str:
        if phase is MarketPhase.MARKET_OPEN and self.open_cadence:
            return self.open_cadence
        return self.cadence

    def every_minutes_for(self, phase: MarketPhase) -> int | None:
        if phase is MarketPhase.MARKET_OPEN and self.open_every_minutes is not None:
            return self.open_every_minutes
        return self.every_minutes

    def mode_for(self, phase: MarketPhase) -> str | None:
        if self.name != "CANDIDATE_DISCOVERY":
            return None
        return "lightweight" if phase is MarketPhase.MARKET_OPEN else "broad"

    def as_preview(self, phase: MarketPhase) -> dict[str, Any]:
        return {
            "job": self.name,
            "cadence": self.cadence_for(phase),
            "every_minutes": self.every_minutes_for(phase),
            "allow_ai": self.allow_ai,
            "description": self.description,
            "mode": self.mode_for(phase),
            "valid_for_phase": True,
            "phase": phase.value,
        }


def catalog() -> list[JobSpec]:
    """One job per responsibility. The runtime is a single portfolio loop:

    observe (account/positions) → discover → research → decide → Risk Gate →
    ProposalIntake (human or autonomous authorization) → execute → reconcile → monitor.

    Removed in the autonomy consolidation (they duplicated another job's handler):
    QUOTE_REFRESH, MARKET_OPEN_CONDITIONAL_VALIDATE, PREMARKET_NEWS, POSTMARKET_CLOSE_ANALYSIS,
    POSTMARKET_DAILY_SNAPSHOT, POSTMARKET_CANDIDATE_RANK, TERRA_RESEARCH, THESIS_WATCH_CREATE,
    OVERNIGHT_NEWS, OVERNIGHT_THESIS, OVERNIGHT_FUNDAMENTALS, WEEKEND_SESSION_ANALYSIS,
    WEEKEND_DEEP_RESEARCH, WEEKEND_WATCH_CONSTRUCT, WEEKEND_STALE_CLEANUP, WEEKEND_NEXT_SESSION_PREP.
    Their handlers remain callable by name (``LEGACY_JOB_ALIASES``) for manual runs.
    """
    return [
        JobSpec("HEARTBEAT", ALL_PHASES, "interval", every_minutes=1, description="Persist health/uptime"),
        JobSpec("BROKER_RECONNECT", ALL_PHASES, "interval", every_minutes=5, requires_broker=False, description="Load/refresh OAuth and reconnect"),
        JobSpec("APPROVAL_EXPIRY", ALL_PHASES, "interval", every_minutes=5, description="Expire/supersede stale order proposals"),
        JobSpec("WATCH_STALE_CLEANUP", ALL_PHASES, "interval", every_minutes=60, description="Expire stale theses"),
        JobSpec("LIVE_ACCOUNT_REFRESH", ALL_PHASES, "interval", every_minutes=15, requires_broker=True, description="Account/position refresh (broker is the source of truth)"),
        JobSpec("LIVE_ORDER_RECONCILE", OPEN | POST, "interval", every_minutes=5, requires_broker=True, requires_regular_hours=False, description="Reconcile broker orders"),
        JobSpec("APPROVED_EXECUTION_DRAIN", OPEN, "interval", every_minutes=5, requires_broker=True, requires_regular_hours=True, description="Execute authorized orders (human or autonomous) inside the send window"),
        JobSpec("POSITION_MONITOR", OPEN, "interval", every_minutes=15, requires_broker=True, requires_regular_hours=True, description="Position monitoring and thesis reassessment"),
        JobSpec("WATCH_CONDITION_MONITOR", OPEN, "interval", every_minutes=15, requires_broker=True, requires_regular_hours=True, description="Watch entry conditions → Risk Gate → ProposalIntake"),
        JobSpec("RISK_MONITOR", OPEN, "interval", every_minutes=15, requires_broker=True, description="Deterministic risk monitoring"),
        JobSpec("AI_REASSESS_IF_WARRANTED", OPEN, "interval", every_minutes=15, allow_ai=True, description="AI reassessment only on material events"),
        JobSpec(
            "CANDIDATE_DISCOVERY",
            OPEN | POST | CLOSED_DAYS,
            "once_per_session",
            requires_broker=True,
            description="Broad discovery once after each session (and once per closed day); lightweight MARKET_OPEN pass every 15 minutes",
            open_cadence="interval",
            open_every_minutes=MARKET_OPEN_DISCOVERY_MINUTES,
        ),
        JobSpec("PREMARKET_EVENT_DISCOVERY", PRE, "once_per_session", requires_broker=True, description="Overnight event / earnings refresh"),
        JobSpec("POSTMARKET_EARNINGS_DISCOVERY", POST, "once_per_session", requires_broker=True, description="Earnings and event discovery"),
        JobSpec(
            "RESEARCH_QUEUE_WORKER",
            OPEN | NIGHT | PRE | POST | CLOSED_DAYS,
            "interval",
            every_minutes=RESEARCH_QUEUE_MINUTES,
            allow_ai=True,
            description="Luna screen then Terra research; skip paid Terra when core evidence is missing (max 1 deep-research item during MARKET_OPEN)",
        ),
        JobSpec("LUNA_SCREEN", POST | CLOSED_DAYS, "once_per_day", allow_ai=True, description="Cheap Luna screening of queued names before Terra"),
        JobSpec(
            "CORE_COMMITTEE_REVIEW",
            POST | CLOSED_DAYS,
            "once_per_day",
            allow_ai=True,
            description="CORE portfolio investment committee: residual allocation among cash, broad-market, and qualified CORE research",
        ),
        JobSpec("PREMARKET_THESIS_REVALIDATE", PRE, "once_per_session", allow_ai=False, description="Revalidate watchlist theses and open proposals"),
        JobSpec("PREMARKET_PREPARE_CONDITIONAL", PRE, "once_per_session", allow_ai=False, description="Prepare conditional opening-session proposals"),
        JobSpec("POSTMARKET_RECONCILE", POST, "once_per_session", requires_broker=True, description="Portfolio reconciliation"),
        JobSpec("NEXT_SESSION_PLANS", POST | CLOSED_DAYS, "once_per_day", description="Conditional next-session plans"),
        JobSpec("OVERNIGHT_WATCH_MAINTAIN", NIGHT, "interval", every_minutes=120, description="Watchlist/thesis maintenance (no AI)"),
        JobSpec("OVERNIGHT_RISK", NIGHT, "interval", every_minutes=240, description="Portfolio risk review"),
        JobSpec("WEEKEND_PORTFOLIO_REVIEW", CLOSED_DAYS, "once_per_day", requires_broker=True, description="Read-only portfolio review"),
    ]


LEGACY_JOB_ALIASES = {
    "QUOTE_REFRESH": "QUOTE_REFRESH",
    "MARKET_OPEN_CONDITIONAL_VALIDATE": "WATCH_CONDITION_MONITOR",
    "PREMARKET_NEWS": "OVERNIGHT_WATCH_MAINTAIN",
    "POSTMARKET_CLOSE_ANALYSIS": "CANDIDATE_DISCOVERY",
    "POSTMARKET_DAILY_SNAPSHOT": "HEARTBEAT",
    "POSTMARKET_CANDIDATE_RANK": "CANDIDATE_DISCOVERY",
    "TERRA_RESEARCH": "RESEARCH_QUEUE_WORKER",
    "THESIS_WATCH_CREATE": "CANDIDATE_DISCOVERY",
    "OVERNIGHT_NEWS": "OVERNIGHT_WATCH_MAINTAIN",
    "OVERNIGHT_THESIS": "OVERNIGHT_WATCH_MAINTAIN",
    "OVERNIGHT_FUNDAMENTALS": "OVERNIGHT_WATCH_MAINTAIN",
    "WEEKEND_SESSION_ANALYSIS": "CANDIDATE_DISCOVERY",
    "WEEKEND_DEEP_RESEARCH": "RESEARCH_QUEUE_WORKER",
    "WEEKEND_WATCH_CONSTRUCT": "CANDIDATE_DISCOVERY",
    "WEEKEND_STALE_CLEANUP": "WATCH_STALE_CLEANUP",
    "WEEKEND_NEXT_SESSION_PREP": "NEXT_SESSION_PLANS",
}


def specs_by_name() -> dict[str, JobSpec]:
    return {spec.name: spec for spec in catalog()}


def specs_for_phase(phase: MarketPhase) -> list[JobSpec]:
    return [spec for spec in catalog() if phase in spec.phases]


def catalog_preview(phase: MarketPhase) -> list[dict[str, Any]]:
    """Scheduler diagnostics: jobs valid for a phase, including MARKET_OPEN research/discovery."""
    return [spec.as_preview(phase) for spec in specs_for_phase(phase)]


def research_queue_max_items(phase: MarketPhase | None) -> int:
    if phase is MarketPhase.MARKET_OPEN:
        return MARKET_OPEN_DEEP_RESEARCH_PER_CYCLE
    return OFF_HOURS_DEEP_RESEARCH_PER_CYCLE


def iter_catalog() -> Iterable[JobSpec]:
    return catalog()
