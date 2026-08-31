"""Live autonomous candidate discovery is NOT wired on the Raspberry Pi runtime.

CANDIDATE_DISCOVERY currently returns skipped=no_live_discovery. That skip is
intentional and visible in health/activity. It must never be filled with the
24 hardcoded names from scripts/run_live_readonly_discovery.py — those are a
frozen 2026-08-29 snapshot, not current LIVE MCP observations.

Why this is a redesign, not a one-line wire-up
----------------------------------------------
The production object graph is:

  StreamableHttpMcpTransport -> GuardedReadonlyTransport -> AuthorizedMcpReadAdapter

GuardedReadonlyTransport allowlists only observation tools used for portfolio
refresh (accounts/portfolio/positions/quotes/orders + classification reads).
DiscoveryFetcher requires additional read tools that the guard currently
refuses: get_scans, run_scan, get_watchlists, get_watchlist_items,
get_popular_watchlists, get_earnings_calendar, get_equity_historicals,
get_equity_technical_indicators, get_equity_news, get_financials (partially
allowlisted), get_indexes, get_sec_filing_index, and related reads.

Existing pieces that are already safe to reuse later
----------------------------------------------------
- run_discovery() over SecuritySnapshot (no BUY / no ACTIVE thesis)
- assemble_snapshot() from read-only MCP payloads
- DISCOVERY_READ_TOOLS / DISCOVERY_FORBIDDEN_TOOLS split
- Channel nomination helpers (symbols_from_search/scan/watchlist/earnings)

Implementation plan (do not execute as part of this hardening pass)
-------------------------------------------------------------------
1. Expand READONLY_OBSERVATION_TOOLS to include DISCOVERY_READ_TOOLS only.
   Keep create_scan / watchlist mutations / order tools forbidden.
2. Add AuthorizedMcpReadAdapter methods for those tools using verified MCP
   argument schemas (never guessed names).
3. Build a live channel source that:
   - runs existing scans only (run_scan), never create_scan
   - reads watchlists / popular watchlists / earnings calendar / search
   - fetches per-symbol snapshots via assemble_snapshot
4. Wire AgentServices.candidates_fn to that live source.
   Persist under state/live_ai, never paper state/candidates.json.
5. Integration-test the real adapter + fake MCP HTTP. Reject any path that
   loads reports/2026-08-29_discovery.json or state/candidates.json as LIVE.

LIVE_DISCOVERY_WIRED must stay False until that work lands.
"""

from __future__ import annotations

LIVE_DISCOVERY_WIRED = False
LIVE_DISCOVERY_SKIP_REASON = "no_live_discovery"


def live_discovery_status() -> dict[str, object]:
    return {
        "wired": LIVE_DISCOVERY_WIRED,
        "skip_reason": LIVE_DISCOVERY_SKIP_REASON,
        "static_snapshot_allowed": False,
        "uses_run_live_readonly_discovery_hardcoded_names": False,
    }
