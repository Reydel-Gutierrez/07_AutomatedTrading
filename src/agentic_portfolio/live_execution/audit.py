"""Append-only execution audit trail."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.paths import project_root


def audit_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "logs" / "execution_audit.jsonl"


def record_audit(
    kind: str,
    *,
    root: Path | None = None,
    now: datetime | None = None,
    **fields: Any,
) -> None:
    stamp = now or datetime.now(timezone.utc)
    payload = {
        "type": kind,
        "at": stamp.isoformat() if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc).isoformat(),
        **fields,
    }
    append_jsonl(payload, audit_path(root))


def read_audit(root: Path | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
    path = audit_path(root)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            import json

            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return rows[-limit:]
