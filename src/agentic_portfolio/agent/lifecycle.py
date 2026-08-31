"""Production service lifecycle: start / stop / restart / health / status."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from agentic_portfolio.agent.heartbeat import load_health, pid_path, read_pid
from agentic_portfolio.paths import project_root


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except SystemError:
        return False
    return True


def status(root: Path | None = None) -> dict[str, Any]:
    base = root or project_root()
    health = load_health(base)
    pid = read_pid(base)
    alive = bool(pid and pid_alive(pid))
    health["pid"] = pid
    health["process_alive"] = alive
    if not alive:
        health["agent"] = "OFFLINE"
        health["alive"] = False
    return health


def stop(root: Path | None = None, *, timeout: float = 15.0) -> dict[str, Any]:
    base = root or project_root()
    pid = read_pid(base)
    if pid is None or not pid_alive(pid):
        return {"ok": True, "stopped": True, "pid": pid, "already_stopped": True}
    sig = getattr(signal, "SIGTERM", signal.SIGINT)
    os.kill(pid, sig)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(0.2)
    if pid_alive(pid) and hasattr(signal, "SIGKILL"):
        os.kill(pid, signal.SIGKILL)
    path = pid_path(base)
    if path.exists() and not pid_alive(pid):
        try:
            path.unlink()
        except OSError:
            pass
    return {"ok": not pid_alive(pid), "stopped": not pid_alive(pid), "pid": pid}


def start_argv(*, no_dashboard: bool = False) -> list[str]:
    root = project_root()
    script = root / "scripts" / "run_service.py"
    cmd = [sys.executable, str(script)]
    if no_dashboard:
        cmd.append("--no-dashboard")
    return cmd
