from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from agentic_portfolio.paths import project_root


@lru_cache(maxsize=1)
def load_policy(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "portfolio_policy.json")
    return json.loads(p.read_text(encoding="utf-8"))


def load_account_rules(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "account_rules.json")
    return json.loads(p.read_text(encoding="utf-8"))


def load_discovery_config(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "discovery.json")
    return json.loads(p.read_text(encoding="utf-8"))


def load_research_config(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "research.json")
    return json.loads(p.read_text(encoding="utf-8"))


def load_decision_config(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "decision.json")
    return json.loads(p.read_text(encoding="utf-8"))


def load_monitoring_config(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "monitoring.json")
    return json.loads(p.read_text(encoding="utf-8"))


def load_execution_config(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "execution.json")
    return json.loads(p.read_text(encoding="utf-8"))


def load_paper_fill_config(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "paper_fill.json")
    return json.loads(p.read_text(encoding="utf-8"))


def load_approval_config(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "approval.json")
    return json.loads(p.read_text(encoding="utf-8"))


def load_review_config(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "review.json")
    return json.loads(p.read_text(encoding="utf-8"))


def load_dashboard_config(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "dashboard.json")
    return json.loads(p.read_text(encoding="utf-8"))


def load_runtime_config(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "runtime.json")
    if not p.exists():
        return {"mode": "PAPER", "live_order_placement_enabled": False}
    return json.loads(p.read_text(encoding="utf-8"))


def load_ai_config(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "ai.json")
    return json.loads(p.read_text(encoding="utf-8"))


def load_pipeline_config(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "pipeline.json")
    return json.loads(p.read_text(encoding="utf-8"))


def load_agent_config(path: Path | None = None) -> dict[str, Any]:
    p = path or (project_root() / "config" / "agent.json")
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))
