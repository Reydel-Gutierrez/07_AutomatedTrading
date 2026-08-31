"""Research-queue freshness is measured from enqueue time."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentic_portfolio.policy import load_discovery_config
from agentic_portfolio.schemas import ResearchQueueEntry, Sleeve


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def freshness_deadline_at(sleeve: Sleeve | str, enqueued_at: datetime, cfg: dict | None = None) -> str:
    """Deadline is measured from enqueue time, never from a stale discovery clock."""
    config = cfg or load_discovery_config()
    key = sleeve.value if isinstance(sleeve, Sleeve) else str(sleeve)
    ttl = float((config.get("ttl_hours") or {}).get(key, 72))
    stamp = enqueued_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (stamp + timedelta(hours=ttl)).isoformat()


def normalize_queue_freshness(entry: ResearchQueueEntry, cfg: dict | None = None) -> ResearchQueueEntry:
    """Reject freshness_deadline values that predate enqueue (script clock vs wall clock)."""
    enqueued = _parse(entry.enqueued_at) if entry.enqueued_at else datetime.now(timezone.utc)
    deadline = _parse(entry.freshness_deadline) if entry.freshness_deadline else None
    if deadline is None or deadline <= enqueued:
        entry.freshness_deadline = freshness_deadline_at(entry.provisional_sleeve, enqueued, cfg)
    return entry


def is_queue_expired(entry: ResearchQueueEntry, now: datetime) -> bool:
    if not entry.freshness_deadline:
        return False
    try:
        deadline = _parse(entry.freshness_deadline)
    except ValueError:
        return False
    stamp = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return stamp >= deadline
