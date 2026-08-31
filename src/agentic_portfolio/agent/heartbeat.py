"""Persist 24/7 runtime health for the dashboard and systemd."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentic_portfolio.agent.persist import atomic_write_json, read_json
from agentic_portfolio.agent.session import SessionSnapshot
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_agent_config


def health_path(root: Path | None = None, *, config: dict[str, Any] | None = None) -> Path:
    cfg = config or load_agent_config()
    return (root or project_root()) / str(cfg.get("health_path") or "state/runtime/health.json")


def pid_path(root: Path | None = None, *, config: dict[str, Any] | None = None) -> Path:
    cfg = config or load_agent_config()
    return (root or project_root()) / str(cfg.get("pid_path") or "state/runtime/agent.pid")


def write_pid(root: Path | None = None, *, pid: int | None = None, config: dict[str, Any] | None = None) -> Path:
    path = pid_path(root, config=config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid if pid is not None else os.getpid()), encoding="utf-8")
    return path


def read_pid(root: Path | None = None, *, config: dict[str, Any] | None = None) -> int | None:
    path = pid_path(root, config=config)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def load_health(root: Path | None = None, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    return read_json(health_path(root, config=config), {"agent": "OFFLINE", "alive": False})


def write_health(
    root: Path,
    *,
    started_at: str,
    session: SessionSnapshot,
    last_cycle: dict[str, Any] | None,
    next_jobs: list[dict[str, Any]],
    broker: dict[str, Any],
    openai: dict[str, Any],
    budget: dict[str, Any],
    cycles: int,
    runtime_mode: str = "LIVE",
    config: dict[str, Any] | None = None,
    live_error: dict[str, Any] | None = None,
    job_skips: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    uptime = max(0, int((now - started).total_seconds()))
    payload = {
        "agent": "ONLINE",
        "alive": True,
        "pid": os.getpid(),
        "started_at": started_at,
        "observed_at": now.isoformat(),
        "uptime_seconds": uptime,
        "runtime_mode": runtime_mode,
        "market": session.to_dict(),
        "last_cycle": last_cycle,
        "next_jobs": next_jobs,
        "robinhood": broker,
        "openai": openai,
        "ai_budget": budget,
        "cycles": cycles,
        "LIVE_ORDER_PLACEMENT": False,
        "auto_execution": False,
        "live_trade_actions_allowed": False,
        "live_error": live_error,
        "job_skips": list(job_skips or []),
    }
    atomic_write_json(health_path(root, config=config), payload)
    return payload


def mark_offline(root: Path | None = None, *, config: dict[str, Any] | None = None) -> None:
    path = health_path(root, config=config)
    current = read_json(path, {})
    current["agent"] = "OFFLINE"
    current["alive"] = False
    current["stopped_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(path, current)
    pid = pid_path(root, config=config)
    if pid.exists():
        try:
            pid.unlink()
        except OSError:
            pass
