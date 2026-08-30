"""Candidate Discovery: research-queue generation, not trading."""

from agentic_portfolio.discovery.engine import DiscoveryResult, expire_candidates, run_discovery
from agentic_portfolio.discovery.safety import (
    DISCOVERY_FORBIDDEN_TOOLS,
    DISCOVERY_READ_TOOLS,
    DiscoverySafetyError,
    as_proposed_action,
    candidate_cannot_become_buy,
)
from agentic_portfolio.discovery.snapshot import SecuritySnapshot
from agentic_portfolio.discovery.store import CandidateStore, DiscoveryRunStore, ResearchQueue
from agentic_portfolio.schemas import Candidate, DiscoveryRun, MarketRegime, ResearchQueueEntry

__all__ = [
    "Candidate",
    "CandidateStore",
    "DISCOVERY_FORBIDDEN_TOOLS",
    "DISCOVERY_READ_TOOLS",
    "DiscoveryResult",
    "DiscoveryRun",
    "DiscoveryRunStore",
    "DiscoverySafetyError",
    "MarketRegime",
    "ResearchQueue",
    "ResearchQueueEntry",
    "SecuritySnapshot",
    "as_proposed_action",
    "candidate_cannot_become_buy",
    "expire_candidates",
    "run_discovery",
]
