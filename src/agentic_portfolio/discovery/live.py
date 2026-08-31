"""Live autonomous candidate discovery. Wired to the production read adapter.

Does not use the frozen 25-name snapshot. AI is not used to build the universe.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentic_portfolio.discovery.engine import DiscoveryResult, run_discovery
from agentic_portfolio.discovery.store import CandidateStore, DiscoveryRunStore, ResearchQueue
from agentic_portfolio.discovery.universe import construct_universe, snapshots_for_universe
from agentic_portfolio.policy import load_discovery_config
from agentic_portfolio.runtime import RuntimeMode, discovery_state_dir
from agentic_portfolio.schemas import PortfolioContext

LIVE_DISCOVERY_WIRED = True
LIVE_DISCOVERY_SKIP_REASON = "no_live_fetcher"
LIGHTWEIGHT_DISCOVERY_SOURCES = (
    "account_positions",
    "account_watchlists",
    "popular_watchlists",
    "earnings_calendar",
    "core_liquid",
    "liquid_etfs",
)
LIGHTWEIGHT_MAX_UNIVERSE = 24
LIGHTWEIGHT_MAX_SNAPSHOTS = 20
LIGHTWEIGHT_MAX_PER_SOURCE = 12


def live_discovery_status() -> dict[str, object]:
    return {
        "wired": LIVE_DISCOVERY_WIRED,
        "skip_reason": None if LIVE_DISCOVERY_WIRED else LIVE_DISCOVERY_SKIP_REASON,
        "static_snapshot_allowed": False,
        "uses_run_live_readonly_discovery_hardcoded_names": False,
        "ai_required": False,
    }


def run_live_discovery(
    fetcher: Any,
    context: PortfolioContext,
    *,
    root: Path,
    runtime_mode: RuntimeMode | str = RuntimeMode.LIVE,
    config: dict | None = None,
    now: datetime | None = None,
    source_filter: list[str] | None = None,
    persist: bool = True,
    lightweight: bool = False,
) -> DiscoveryResult:
    """Construct a live universe, score snapshots, enqueue research. Never buys.

    AI is never used for universe construction. MARKET_OPEN lightweight passes
    query a smaller source set; POSTMARKET / closed days remain broad.
    """
    stamp = now or datetime.now(timezone.utc)
    cfg = dict(config or load_discovery_config())
    if lightweight:
        uc = dict(cfg.get("universe_construction") or {})
        uc["max_universe_size"] = min(int(uc.get("max_universe_size") or 40), LIGHTWEIGHT_MAX_UNIVERSE)
        uc["max_snapshots_to_score"] = min(int(uc.get("max_snapshots_to_score") or 30), LIGHTWEIGHT_MAX_SNAPSHOTS)
        uc["max_per_source"] = min(int(uc.get("max_per_source") or 25), LIGHTWEIGHT_MAX_PER_SOURCE)
        cfg["universe_construction"] = uc
        source_filter = list(source_filter or LIGHTWEIGHT_DISCOVERY_SOURCES)
    mode = runtime_mode if isinstance(runtime_mode, RuntimeMode) else RuntimeMode(str(runtime_mode).upper())
    held = [p.symbol for p in (context.positions or []) if getattr(p, "symbol", None)]
    universe = construct_universe(
        fetcher,
        held_symbols=held,
        config=cfg,
        now=stamp,
        source_filter=source_filter,
    )
    snapshots = snapshots_for_universe(fetcher, universe, config=cfg, now=stamp)
    sources_queried = [s.name for s in universe.sources if s.attempted]
    state_dir = discovery_state_dir(Path(root), mode=mode)
    candidate_store = CandidateStore(state_dir / "candidates.json", runtime_mode=mode.value) if persist else None
    queue_store = ResearchQueue(state_dir / "research_queue.json", runtime_mode=mode.value) if persist else None
    run_store = DiscoveryRunStore(state_dir / "discovery_runs.json", runtime_mode=mode.value) if persist else None
    result = run_discovery(
        snapshots,
        context,
        config=cfg,
        candidate_store=candidate_store,
        queue_store=queue_store,
        run_store=run_store,
        now=stamp,
        sources_queried=sources_queried,
        session_context={
            "trading_session_id": context.trading_session_id,
            "session_fail_safe": context.session_fail_safe,
            "live_universe": universe.as_dict(),
            "discovery_mode": "lightweight" if lightweight else "broad",
        },
        persist=persist,
        promote_shortlist=True,
    )
    run = result.run
    extra = universe.as_dict()
    run.sources_queried = sources_queried
    run.errors = list(run.errors) + list(universe.errors)
    if persist and run_store is not None:
        # Preserve universe stats on the persisted run via session context.
        run.market_session_context = {
            **dict(run.market_session_context or {}),
            "live_universe": extra,
            "sources_successful": extra.get("sources_successful"),
            "unique_universe_size": universe.unique_universe_size,
            "skipped_symbols": extra.get("skipped"),
            "discovery_mode": "lightweight" if lightweight else "broad",
        }
        run_store.save_run(run)
    result.run = run
    return result
