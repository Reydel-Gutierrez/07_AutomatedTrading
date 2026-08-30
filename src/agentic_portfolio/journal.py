from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentic_portfolio.paths import project_root
from agentic_portfolio.schemas import RiskGateResult


def append_jsonl(row: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(row)
    payload.setdefault("logged_at", datetime.now(timezone.utc).isoformat())
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str) + "\n")
    return path


def read_jsonl(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Read an existing journal file. Does not create or mutate it."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if limit is not None and limit >= 0:
        return rows[-limit:]
    return rows


def append_risk_decision(result: RiskGateResult, path: Path | None = None) -> Path:
    logs = path or (project_root() / "logs" / "risk_gate.jsonl")
    row = dict(result.journal_record)
    return append_jsonl(row, logs)
