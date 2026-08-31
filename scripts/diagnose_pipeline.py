"""Read-only production pipeline diagnostic. Never places an order or spends AI."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from agentic_portfolio.agent.heartbeat import load_health
from agentic_portfolio.agent.pipeline import resolve_queue_stores
from agentic_portfolio.ai.budget import BudgetManager
from agentic_portfolio.ai.config import load_ai_config
from agentic_portfolio.ai.ledger import UsageLedger
from agentic_portfolio.dashboard.queries import dashboard_state, list_approvals, watchlist_view
from agentic_portfolio.live.store import LivePortfolioStore
from agentic_portfolio.notify import NotificationStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, RuntimeMode, get_active_runtime
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

    _print("RUNTIME", {
        "runtime_mode": mode.value if isinstance(mode, RuntimeMode) else str(mode),
        "LIVE_ORDER_PLACEMENT": LIVE_ORDER_PLACEMENT,
        "agent": health.get("agent"),
        "phase": (health.get("market") or {}).get("phase"),
        "cycles": health.get("cycles"),
        "last_cycle": health.get("last_cycle"),
        "next_jobs": health.get("next_jobs"),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })
    _print("BROKER", health.get("robinhood") or {"connected": None})
    _print("ACCOUNT", {
        "nav": ctx.get("current_nav"),
        "cash": ctx.get("cash"),
        "buying_power": ctx.get("buying_power"),
        "holdings_count": ctx.get("holdings_count"),
        "risk_state": ctx.get("risk_state"),
        "source_of_truth": book.get("source_of_truth"),
    })
    latest = None
    runs = dashboard_state(root).discovery.all()
    if runs:
        latest = max(runs, key=lambda r: r.started_at)
    _print("DISCOVERY", to_dict(latest) if latest else {"runs": 0})
    _print("RESEARCH QUEUE", {
        "path": str(queue.path),
        "total": len(queue.all()),
        "queued": len(queued),
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
    _print("PROPOSALS", {"pending_count": approvals.get("pending_count"), "pending": approvals.get("pending")})
    _print("RISK", {"risk_state": ctx.get("risk_state"), "daily_risk_halt": ctx.get("daily_risk_halt")})
    _print("APPROVALS", {
        "pending_count": approvals.get("pending_count"),
        "live_pending_count": approvals.get("live_pending_count"),
        "LIVE_ORDER_PLACEMENT": False,
    })
    _print("AI BUDGET", {
        "cap": float(budget.cap),
        "spent": float(budget.spent),
        "remaining": float(budget.remaining),
        "mode": budget.mode.value,
        "calls_month": budget.calls_month,
    })
    _print("RECENT ERRORS", {"live_error": live_err, "job_skips": errors, "notifications": notes})
    return 0


if __name__ == "__main__":
    sys.exit(main())
