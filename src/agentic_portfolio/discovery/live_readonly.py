"""Live autonomous candidate discovery is wired through discovery.live.

Kept as a stable import path for health/smoke tests.
"""

from agentic_portfolio.discovery.live import (
    LIVE_DISCOVERY_SKIP_REASON,
    LIVE_DISCOVERY_WIRED,
    live_discovery_status,
    run_live_discovery,
)

__all__ = [
    "LIVE_DISCOVERY_SKIP_REASON",
    "LIVE_DISCOVERY_WIRED",
    "live_discovery_status",
    "run_live_discovery",
]
