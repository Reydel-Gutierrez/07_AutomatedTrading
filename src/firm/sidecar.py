"""Supervise a separate program next to the firm (the Forex engine).

The child is a different process with its own databases: if it crashes, hangs, or leaks
memory, the firm keeps running. It starts at low CPU priority, receives only an allowlisted
environment (no broker, AI, or Telegram secrets), and is restarted with backoff. Nothing in
the firm imports the child's code; this module only runs a command line.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Any, Mapping, Sequence

log = logging.getLogger(__name__)

ENV_ALLOW = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "FOREX_STATE_ROOT", "OANDA_ENV_FILE", "SYSTEMROOT")
NOT_CONFIGURED = 3  # child exit code: nothing to run yet (e.g. no credentials); retry slowly


def child_env(environ: Mapping[str, str]) -> dict[str, str]:
    return {k: environ[k] for k in ENV_ALLOW if k in environ}


class Sidecar:
    def __init__(self, name: str, argv: Sequence[str], *, cwd: str, environ: Mapping[str, str], nice: int = 10, min_backoff: float = 10, max_backoff: float = 600) -> None:
        self.name = name
        self.argv = list(argv)
        self.cwd = cwd
        self.env = child_env(environ)
        self.nice = nice
        self.min_backoff, self.max_backoff = min_backoff, max_backoff
        self.proc: subprocess.Popen | None = None
        self.restarts = 0
        self.last_exit: int | None = None
        self.started_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _spawn(self) -> subprocess.Popen:
        kwargs: dict[str, Any] = {"cwd": self.cwd, "env": self.env, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "posix":
            nice = self.nice
            kwargs["preexec_fn"] = lambda: os.nice(nice)
            kwargs["start_new_session"] = True  # signals to the firm's group do not hit the child twice
        return subprocess.Popen(self.argv, **kwargs)

    def _loop(self) -> None:
        backoff = self.min_backoff
        while not self._stop.is_set():
            try:
                self.proc = self._spawn()
            except OSError as exc:
                log.error("%s could not start: %s", self.name, exc)
                self._stop.wait(self.max_backoff)
                continue
            self.started_at = time.time()
            while self.proc.poll() is None and not self._stop.is_set():
                self._stop.wait(2)
            if self._stop.is_set():
                break
            self.last_exit = self.proc.returncode
            ran = time.time() - (self.started_at or time.time())
            self.restarts += 1
            backoff = self.max_backoff if self.last_exit == NOT_CONFIGURED else (self.min_backoff if ran > 300 else min(backoff * 2, self.max_backoff))
            log.warning("%s exited with %s after %.0fs; restarting in %.0fs", self.name, self.last_exit, ran, backoff)
            self._stop.wait(backoff)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"sidecar-{self.name}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
        if self._thread:
            self._thread.join(timeout=timeout)

    def status(self) -> dict[str, Any]:
        running = bool(self.proc and self.proc.poll() is None)
        return {
            "name": self.name, "running": running, "pid": self.proc.pid if running and self.proc else None,
            "restarts": self.restarts, "last_exit": self.last_exit,
            "since": int(self.started_at) if running and self.started_at else None,
        }
