"""Read-only production pipeline diagnostic. Never places an order or spends AI."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from agentic_portfolio.agent.jobs import catalog_preview
from agentic_portfolio.agent.heartbeat import load_health
from agentic_portfolio.agent.pipeline import resolve_queue_stores
from agentic_portfolio.agent.session import classify_market_phase
from agentic_portfolio.ai.budget import BudgetManager
from agentic_portfolio.ai.config import load_ai_config
from agentic_portfolio.ai.ledger import UsageLedger
from agentic_portfolio.dashboard.queries import dashboard_state, list_approvals, watchlist_view
from agentic_portfolio.discovery.live import LIVE_DISCOVERY_WIRED, live_discovery_status
from agentic_portfolio.live.store import LivePortfolioStore
from agentic_portfolio.live_execution.audit import read_audit
from agentic_portfolio.live_execution.safety import inspect_broker_mutation_surface, placement_call_sites, release_readiness
from agentic_portfolio.live_execution.store import ExecutionStore
from agentic_portfolio.notify import NotificationStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.runtime import (
    AUTO_EXECUTION,
    LIVE_ORDER_PLACEMENT,
    REQUIRE_HUMAN_APPROVAL,
    RuntimeMode,
    get_active_runtime,
    live_placement_enabled,
)
from agentic_portfolio.schemas import ResearchQueueStatus, to_dict
from agentic_portfolio.thesis_registry import ThesisRegistry


def _print(title: str, payload: dict) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only autonomous pipeline diagnostic. Never places.")
    parser.add_argument("--root", default=None, help="Project root (defaults to repo root)")
    args = parser.parse_args()
    root = project_root() if args.root is None else __import__("pathlib").Path(args.root)
    mode = get_active_runtime()
    health = load_health(root) or {}
    book = LivePortfolioStore(root).current_book() or {}
    ctx = book.get("context") if isinstance(book.get("context"), dict) else {}
    candidates, queue = resolve_queue_stores(root, runtime_mode=mode)
    queued = [e for e in queue.all() if e.status is ResearchQueueStatus.QUEUED]
    reports = ResearchStore(root).all_reports()
    theses = ThesisRegistry(root / "state" / "thesis_registry.json").all_records()
    watches = watchlist_view(dashboard_state(root))
    approvals = list_approvals(dashboard_state(root))
    cfg = load_ai_config()
    budget = BudgetManager(UsageLedger(root, config=cfg), cfg).status()
    errors = list((health.get("job_skips") or []))[-8:]
    live_err = LivePortfolioStore(root).last_error()
    notes = [n.to_dict() for n in NotificationStore(root).all()[-8:]]
    exec_store = ExecutionStore(root, runtime_mode=mode)
    intents = [i.to_dict() for i in exec_store.intents()]
    orders = [o.to_dict() for o in exec_store.orders()]
    session = classify_market_phase()
    latest = None
    runs = dashboard_state(root).discovery.all()
    if runs:
        latest = max(runs, key=lambda r: r.started_at)
    universe = ((latest.market_session_context or {}).get("live_universe") if latest else None) or {}
    recon_unknown = [o for o in orders if str(o.get("status")) == "UNKNOWN_RECONCILIATION_REQUIRED"]

    _print("RUNTIME", {
        "runtime_mode": mode.value if isinstance(mode, RuntimeMode) else str(mode),
        "LIVE_ORDER_PLACEMENT": LIVE_ORDER_PLACEMENT,
        "live_placement_enabled": live_placement_enabled(),
        "REQUIRE_HUMAN_APPROVAL": REQUIRE_HUMAN_APPROVAL,
        "AUTO_EXECUTION": AUTO_EXECUTION,
        "agent": health.get("agent"),
        "phase": (health.get("market") or {}).get("phase") or session.phase.value,
        "cycles": health.get("cycles"),
        "last_cycle": health.get("last_cycle"),
        "next_jobs": health.get("next_jobs"),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })
    _print("MARKET PHASE", {
        "phase": session.phase.value,
        "regular_hours_open": session.regular_hours_open,
        "executable_liquidity": session.executable_liquidity,
        "reason": session.reason,
    })
    scheduled = catalog_preview(session.phase)
    health_jobs = health.get("next_jobs") or scheduled
    _print("SCHEDULED JOBS", {
        "phase": session.phase.value,
        "source": "health.next_jobs" if health.get("next_jobs") else "catalog_preview",
        "jobs": health_jobs,
        "research_queue_worker_valid": any(row.get("job") == "RESEARCH_QUEUE_WORKER" for row in health_jobs),
        "lightweight_discovery_valid": any(
            row.get("job") == "CANDIDATE_DISCOVERY" and (row.get("mode") == "lightweight" or session.phase.value == "MARKET_OPEN")
            for row in health_jobs
        ) if session.phase.value == "MARKET_OPEN" else any(row.get("job") == "CANDIDATE_DISCOVERY" for row in health_jobs),
        "MARKET_OPEN_jobs_include_research_and_discovery": {
            "RESEARCH_QUEUE_WORKER": any(row.get("job") == "RESEARCH_QUEUE_WORKER" for row in catalog_preview(session.phase)),
            "CANDIDATE_DISCOVERY": any(row.get("job") == "CANDIDATE_DISCOVERY" for row in catalog_preview(session.phase)),
        },
    })
    _print("BROKER CONNECTION", health.get("robinhood") or {"connected": None})
    _print("LIVE ACCOUNT", {
        "nav": ctx.get("current_nav"),
        "cash": ctx.get("cash"),
        "buying_power": ctx.get("buying_power"),
        "holdings_count": ctx.get("holdings_count"),
        "risk_state": ctx.get("risk_state"),
        "source_of_truth": book.get("source_of_truth"),
    })
    _print("DISCOVERY", {
        "LIVE_DISCOVERY_WIRED": LIVE_DISCOVERY_WIRED,
        "status": live_discovery_status(),
        "last_run": to_dict(latest) if latest else {"runs": 0},
    })
    _print("DISCOVERY UNIVERSE", universe or {"unique_universe_size": 0, "note": "no persisted universe"})
    _print("RESEARCH QUEUE", {
        "path": str(queue.path),
        "total": len(queue.all()),
        "queued": len(queued),
        "working": sum(1 for e in queue.all() if e.status.value in {"RESEARCHING", "IN_PROGRESS"}),
        "symbols": [e.symbol for e in queue.all()],
        "statuses": {e.symbol: e.status.value for e in queue.all()},
    })
    _print("RESEARCH REPORTS", {
        "count": len(reports),
        "symbols": [r.symbol for r in reports],
        "conclusions": {r.symbol: (r.research_conclusion.value if r.research_conclusion else None) for r in reports},
    })
    _print("THESES", {
        "count": len(theses),
        "symbols": [t.symbol for t in theses],
        "statuses": {t.symbol: (t.status.value if t.status else None) for t in theses},
    })
    _print("WATCHES", watches)
    decisions = [r for r in reports if getattr(r, "research_conclusion", None)]
    _print("DECISIONS", {"research_conclusions": len(decisions)})
    _print("PROPOSALS", {"pending_count": approvals.get("pending_count"), "pending": approvals.get("pending")})
    _print("RISK GATE", {"risk_state": ctx.get("risk_state"), "daily_risk_halt": ctx.get("daily_risk_halt")})
    _print("APPROVALS", {
        "pending_count": approvals.get("pending_count"),
        "live_pending_count": approvals.get("live_pending_count"),
        "LIVE_ORDER_PLACEMENT": live_placement_enabled(),
        "REQUIRE_HUMAN_APPROVAL": REQUIRE_HUMAN_APPROVAL,
    })
    _print("EXECUTION INTENTS", {
        "count": len(intents),
        "statuses": {i.get("intent_id"): i.get("status") for i in intents[-12:]},
    })
    _print("BROKER ORDERS", {
        "count": len(orders),
        "pending": sum(1 for o in orders if o.get("status") == "PENDING_SUBMISSION"),
        "submitted": sum(1 for o in orders if o.get("status") in {"SUBMITTED", "OPEN", "PARTIALLY_FILLED"}),
        "filled": sum(1 for o in orders if o.get("status") == "FILLED"),
        "rejected": sum(1 for o in orders if o.get("status") == "REJECTED"),
        "canceled": sum(1 for o in orders if o.get("status") == "CANCELED"),
        "unknown": len(recon_unknown),
        "recent": orders[:8],
    })
    _print("RECONCILIATION", {
        "unknown_required": len(recon_unknown),
        "fail_closed": True,
        "does_not_blind_retry": True,
    })
    _print("POSITIONS", {
        "holdings": ctx.get("positions") or [],
        "count": ctx.get("holdings_count"),
    })
    _print("AI BUDGET", {
        "cap": float(budget.cap),
        "spent": float(budget.spent),
        "remaining": float(budget.remaining),
        "mode": budget.mode.value,
        "calls_month": budget.calls_month,
    })
    _print("RECENT NOTIFICATIONS", {"notifications": notes})
    _print("RECENT FAILURES", {"live_error": live_err, "job_skips": errors})
    _print("BROKER MUTATION SURFACE", {
        "place_call_sites": inspect_broker_mutation_surface(root)["place_equity_order"],
        "unexpected_place": placement_call_sites(root),
        "audit_recent": read_audit(root, limit=12),
    })
    verdict = release_readiness(root)
    _print("RELEASE READINESS", verdict)
    print("\nREADY_FOR_PI_VALIDATION=" + str(verdict.get("READY_FOR_PI_VALIDATION")).lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
