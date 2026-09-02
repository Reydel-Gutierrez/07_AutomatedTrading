"""Load AI gateway config. Model names stay here, not in trading code."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from agentic_portfolio.paths import project_root


def load_ai_config(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "ai.json")
    return json.loads(p.read_text(encoding="utf-8"))


def money(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else "0"))


def role_spec(config: dict[str, Any], role: str) -> dict[str, Any]:
    roles = dict(config.get("roles") or {})
    spec = roles.get(role)
    if not spec:
        raise KeyError(f"AI role {role!r} is not configured in config/ai.json")
    return dict(spec)


def monthly_cap(config: dict[str, Any] | None = None) -> Decimal:
    cfg = config or load_ai_config()
    return money((cfg.get("budget") or {}).get("monthly_cap") or 10)


def pipeline_limits(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or load_ai_config()
    return dict(cfg.get("pipeline") or {})


def committee_output_token_limits(config: dict[str, Any] | None = None) -> tuple[int, int]:
    """Committee-only output ceilings. Research/screening/singleton decision stay on role defaults."""
    limits = pipeline_limits(config)
    first = int(limits.get("committee_max_output_tokens") or 8000)
    retry = int(limits.get("committee_retry_max_output_tokens") or 12000)
    if retry < first:
        retry = first
    return first, retry
