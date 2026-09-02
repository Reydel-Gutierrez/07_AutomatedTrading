"""Ticker lifecycle events: why a name changed state without grepping raw JSON."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentic_portfolio.journal import append_jsonl, read_jsonl
from agentic_portfolio.paths import project_root


def lifecycle_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "logs" / "lifecycle.jsonl"


def log_lifecycle(
    *,
    symbol: str,
    source: str,
    reason: str,
    from_status: str | None = None,
    to_status: str | None = None,
    extra: dict[str, Any] | None = None,
    path: Path | None = None,
    root: Path | None = None,
) -> None:
    payload: dict[str, Any] = {
        "type": "LIFECYCLE",
        "symbol": str(symbol or "").upper(),
        "source": source,
        "reason": reason,
        "from_status": from_status,
        "to_status": to_status,
        "investment_conclusion": None,
        "operational_failure": source in {"research_error", "schema_validation", "ai_unavailable", "budget", "decision_error"},
    }
    if extra:
        payload.update(extra)
    append_jsonl(payload, path or lifecycle_path(root))


def recent_for_symbol(symbol: str, *, root: Path | None = None, limit: int = 8) -> list[dict[str, Any]]:
    rows = [
        row
        for row in read_jsonl(lifecycle_path(root), limit=400)
        if str(row.get("symbol") or "").upper() == str(symbol or "").upper()
    ]
    return rows[-limit:]


def latest_by_symbol(root: Path | None = None, *, limit_lines: int = 800) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(lifecycle_path(root), limit=limit_lines):
        key = str(row.get("symbol") or "").upper()
        if key:
            latest[key] = row
    return latest
