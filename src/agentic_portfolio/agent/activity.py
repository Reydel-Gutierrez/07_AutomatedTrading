"""Autonomous activity log for the dashboard control room."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentic_portfolio.journal import append_jsonl, read_jsonl
from agentic_portfolio.paths import project_root


ACTIVITY_KINDS = (
    "CANDIDATE_DISCOVERED",
    "CANDIDATE_REJECTED",
    "THESIS_UPDATED",
    "WATCH_CONDITION_TRIGGERED",
    "RISK_GATE_REJECTED",
    "APPROVAL_CREATED",
    "APPROVAL_EXPIRED",
    "APPROVAL_APPROVED",
    "APPROVAL_REJECTED",
    "CONNECTION_FAILURE",
    "CONNECTION_RECOVERY",
    "LIVE_REFRESH_FAILED",
    "JOB_ERROR",
    "JOB_OK",
    "JOB_SKIPPED",
    "AI_SKIPPED",
    "WATCH_UPSERTED",
)


def activity_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "logs" / "agent_activity.jsonl"


def log_activity(root: Path | None, kind: str, **fields: Any) -> None:
    payload = {"type": kind, **fields, "placement_attempted": False, "LIVE_ORDER_PLACEMENT": False}
    payload.setdefault("logged_at", datetime.now(timezone.utc).isoformat())
    append_jsonl(payload, activity_path(root))


def read_activity(root: Path | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
    return read_jsonl(activity_path(root), limit=limit)
