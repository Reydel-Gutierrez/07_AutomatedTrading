"""Job scheduler with failure isolation.

Each job runs in its own worker thread with a timeout. A job that raises,
hangs, or is still running is simply skipped for that tick; other jobs and
managers keep going.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from firm.clock import Clock, iso, utc_now

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    due: Callable[[datetime, "JobState"], bool]
    run: Callable[[], Any]
    timeout_seconds: float = 300


@dataclass
class JobState:
    last_started: datetime | None = None
    last_finished: datetime | None = None
    last_ok: bool | None = None
    last_error: str | None = None
    last_result: Any = None
    runs: int = 0
    failures: int = 0
    future: Future | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.future is not None and not self.future.done()

    def as_dict(self) -> dict[str, Any]:
        return {
            "last_started": iso(self.last_started) if self.last_started else None,
            "last_finished": iso(self.last_finished) if self.last_finished else None,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "runs": self.runs,
            "failures": self.failures,
            "running": self.running,
        }


class Scheduler:
    def __init__(self, *, clock: Clock = utc_now, workers: int = 4, on_failure: Callable[[str, str], None] | None = None) -> None:
        self.clock = clock
        self.jobs: dict[str, Job] = {}
        self.state: dict[str, JobState] = {}
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="firm-job")
        self._on_failure = on_failure
        self._lock = threading.Lock()

    def add(self, job: Job) -> None:
        self.jobs[job.name] = job
        self.state.setdefault(job.name, JobState())

    def tick(self, *, wait: bool = False) -> list[str]:
        """Start every due job that is not already running. Returns names started."""
        now = self.clock()
        started = []
        with self._lock:
            for name, job in self.jobs.items():
                st = self.state[name]
                self._collect(name, st)
                if st.running:
                    if st.last_started and (now - st.last_started).total_seconds() > job.timeout_seconds and st.last_ok is not False:
                        st.last_ok = False
                        st.last_error = f"timed out after {job.timeout_seconds}s (still running)"
                        st.failures += 1
                        self._report(name, st.last_error)
                    continue
                try:
                    if not job.due(now, st):
                        continue
                except Exception as exc:  # noqa: BLE001
                    self._report(name, f"due() failed: {exc}")
                    continue
                st.last_started = now
                st.runs += 1
                st.future = self._pool.submit(job.run)
                started.append(name)
        if wait:
            for name in started:
                fut = self.state[name].future
                if fut is not None:
                    try:
                        fut.result(timeout=self.jobs[name].timeout_seconds)
                    except Exception:  # noqa: BLE001 — recorded by _collect
                        pass
                self._collect(name, self.state[name])
        return started

    def _collect(self, name: str, st: JobState) -> None:
        fut = st.future
        if fut is None or not fut.done() or st.last_finished and st.last_started and st.last_finished >= st.last_started:
            return
        st.last_finished = self.clock()
        exc = fut.exception()
        if exc is None:
            st.last_ok = True
            st.last_error = None
            st.last_result = fut.result()
        else:
            st.last_ok = False
            st.last_error = f"{type(exc).__name__}: {exc}"
            st.failures += 1
            self._report(name, st.last_error)

    def _report(self, name: str, error: str) -> None:
        log.error("job %s failed: %s", name, error)
        if self._on_failure:
            try:
                self._on_failure(name, error)
            except Exception:  # noqa: BLE001
                log.exception("failure hook raised")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
