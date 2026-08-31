"""Job orchestrator. Internal scheduler; not a process lifetime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from agentic_portfolio.agent.jobs import JobSpec, specs_by_name, specs_for_phase
from agentic_portfolio.agent.persist import atomic_write_json, read_json
from agentic_portfolio.agent.safety import assert_execution_disabled
from agentic_portfolio.agent.session import MarketPhase, SessionSnapshot, classify_market_phase
from agentic_portfolio.ai.scheduler import JobLock
from agentic_portfolio.calendar import EASTERN
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_agent_config

Handler = Callable[[dict[str, Any]], dict[str, Any]]


class JobOrchestrator:
    def __init__(
        self,
        root: Path | None = None,
        *,
        config: dict[str, Any] | None = None,
        handlers: Mapping[str, Handler] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.base = root or project_root()
        self.config = config or load_agent_config()
        self.handlers = dict(handlers or {})
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.state_path = self.base / str(self.config.get("state_path") or "state/runtime/orchestrator.json")
        self.lock_dir = self.base / str(self.config.get("lock_dir") or "state/runtime/locks")
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state: dict[str, Any] = read_json(self.state_path, {"jobs": {}, "last_tick": None, "last_cycle": None})
        self.specs = specs_by_name()

    def now(self) -> datetime:
        stamp = self._now()
        if stamp.tzinfo is None:
            return stamp.replace(tzinfo=timezone.utc)
        return stamp

    def _save(self) -> None:
        atomic_write_json(self.state_path, self.state)

    def due_jobs(self, now: datetime | None = None, *, session: SessionSnapshot | None = None) -> list[str]:
        stamp = now or self.now()
        snap = session or classify_market_phase(stamp)
        due: list[str] = []
        for spec in specs_for_phase(snap.phase):
            if self._is_due(spec, stamp, snap):
                due.append(spec.name)
        return due

    def next_job_delay_seconds(self, now: datetime | None = None, *, session: SessionSnapshot | None = None) -> float:
        stamp = now or self.now()
        max_sleep = float(self.config.get("max_sleep_seconds") or 30)
        min_sleep = float(self.config.get("min_sleep_seconds") or 0.05)
        snap = session or classify_market_phase(stamp)
        soonest = max_sleep
        for spec in specs_for_phase(snap.phase):
            wait = self._wait_seconds(spec, stamp, snap)
            if wait is not None:
                soonest = min(soonest, wait)
        return max(min_sleep, min(soonest, max_sleep))

    def _key(self, spec: JobSpec, stamp: datetime, session: SessionSnapshot) -> str:
        et = stamp.astimezone(EASTERN)
        if spec.cadence == "once_per_session":
            return session.session_id or et.date().isoformat()
        if spec.cadence == "once_per_day":
            return et.date().isoformat()
        return ""

    def _is_due(self, spec: JobSpec, stamp: datetime, session: SessionSnapshot) -> bool:
        last = (self.state.get("jobs") or {}).get(spec.name) or {}
        if spec.cadence in {"once_per_session", "once_per_day"}:
            return str(last.get("run_key") or "") != self._key(spec, stamp, session)
        every = int(spec.every_minutes or 1) * 60
        last_at = last.get("at")
        if not last_at:
            return True
        try:
            prev = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
        except ValueError:
            return True
        if prev.tzinfo is None:
            prev = prev.replace(tzinfo=timezone.utc)
        return (stamp - prev).total_seconds() >= every

    def _wait_seconds(self, spec: JobSpec, stamp: datetime, session: SessionSnapshot) -> float | None:
        if spec.cadence in {"once_per_session", "once_per_day"}:
            if self._is_due(spec, stamp, session):
                return 0.0
            return None
        last = (self.state.get("jobs") or {}).get(spec.name) or {}
        last_at = last.get("at")
        every = int(spec.every_minutes or 1) * 60
        if not last_at:
            return 0.0
        try:
            prev = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        if prev.tzinfo is None:
            prev = prev.replace(tzinfo=timezone.utc)
        remain = every - (stamp - prev).total_seconds()
        return max(0.0, remain)

    def tick(self, now: datetime | None = None, *, context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        stamp = now or self.now()
        session = classify_market_phase(stamp)
        results = []
        for name in self.due_jobs(stamp, session=session):
            results.append(self.run_job(name, now=stamp, context=context, session=session))
        self.state["last_tick"] = stamp.isoformat()
        self.state["last_cycle"] = {
            "at": stamp.isoformat(),
            "phase": session.phase.value,
            "jobs": [row.get("job") for row in results],
        }
        self._save()
        return results

    def run_job(
        self,
        job: str,
        *,
        now: datetime | None = None,
        context: dict[str, Any] | None = None,
        session: SessionSnapshot | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        stamp = now or self.now()
        snap = session or classify_market_phase(stamp)
        assert_execution_disabled()
        spec = self.specs.get(job) or JobSpec(job, frozenset({snap.phase}), "interval", every_minutes=1)
        lock = JobLock(self.lock_dir / f"{job}.lock", stale_seconds=int(self.config.get("job_timeout_seconds") or 900))
        if not lock.acquire(job=job, now=stamp):
            return {"job": job, "status": "SKIPPED_ALREADY_RUNNING", "at": stamp.isoformat(), "placement_attempted": False}
        try:
            handler = self.handlers.get(job)
            payload: dict[str, Any]
            if handler is None:
                payload = {"job": job, "status": "OK", "skipped": "no_handler"}
            else:
                ctx = dict(context or {})
                ctx.update({"now": stamp, "session": snap, "job": job, "spec": spec, "root": self.base})
                payload = dict(handler(ctx) or {})
            payload.setdefault("status", "OK")
        except Exception as exc:  # noqa: BLE001 — one failed job must not kill the runtime
            payload = {"job": job, "status": "ERROR", "reason": f"{type(exc).__name__}: {exc}"}
        finally:
            lock.release()
        payload.setdefault("job", job)
        payload.setdefault("at", stamp.isoformat())
        payload.setdefault("placement_attempted", False)
        payload["phase"] = snap.phase.value
        payload["run_key"] = self._key(spec, stamp, snap)
        jobs = dict(self.state.get("jobs") or {})
        jobs[job] = payload
        self.state["jobs"] = jobs
        self._save()
        return payload

    def scheduled_preview(self, now: datetime | None = None) -> list[dict[str, Any]]:
        stamp = now or self.now()
        session = classify_market_phase(stamp)
        rows = []
        for spec in specs_for_phase(session.phase):
            last = (self.state.get("jobs") or {}).get(spec.name) or {}
            rows.append(
                {
                    "job": spec.name,
                    "cadence": spec.cadence,
                    "every_minutes": spec.every_minutes,
                    "allow_ai": spec.allow_ai,
                    "due": self._is_due(spec, stamp, session),
                    "last_status": last.get("status"),
                    "last_at": last.get("at"),
                    "description": spec.description,
                }
            )
        return rows
