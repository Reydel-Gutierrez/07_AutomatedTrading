"""Autonomous portfolio operation: who may authorize an order, and when everything stops.

* ``policy``      — ExecutionMode (OBSERVE / HUMAN_APPROVAL / AUTONOMOUS) + autonomy limits.
* ``kill_switch`` — persistent emergency stop; human release only.
* ``authority``   — deterministic OrderAuthority (proposal review + pre-send checks).
* ``intake``      — the single door every order proposal goes through.
* ``send_risk``   — full Risk Gate re-run on fresh facts at send time.
* ``paper_broker``— simulated broker so PAPER runs the identical pipeline.

Nothing here calls an AI provider. Nothing here is callable by AI.
"""

from agentic_portfolio.autonomy.authority import (
    AUTO_APPROVE,
    AUTONOMOUS_DECIDER,
    ESCALATE,
    OBSERVE_ONLY,
    AuthorizationDecision,
    OrderAuthority,
    is_autonomous_approval,
)
from agentic_portfolio.autonomy.intake import IntakeResult, ProposalIntake, build_intake
from agentic_portfolio.autonomy.kill_switch import KillSwitch, KillSwitchStatus
from agentic_portfolio.autonomy.policy import (
    AutonomyLimits,
    ExecutionMode,
    ExecutionPolicy,
    load_execution_policy,
    resolve_state_root,
)

__all__ = [
    "AUTO_APPROVE",
    "AUTONOMOUS_DECIDER",
    "ESCALATE",
    "OBSERVE_ONLY",
    "AuthorizationDecision",
    "AutonomyLimits",
    "ExecutionMode",
    "ExecutionPolicy",
    "IntakeResult",
    "KillSwitch",
    "KillSwitchStatus",
    "OrderAuthority",
    "ProposalIntake",
    "build_intake",
    "is_autonomous_approval",
    "load_execution_policy",
    "resolve_state_root",
]
