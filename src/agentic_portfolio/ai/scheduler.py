"""Always-on scheduler for the Raspberry Pi production runtime.

Does not depend on Cursor. Most ticks are deterministic. AI runs only on triggers.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from agentic_portfolio.ai.config import load_ai_config
from agentic_portfolio.ai.errors import DuplicateJobError, MissingBrokerFacts, StaleSnapshotError
from agentic_portfolio.ai.gateway import AIGateway, build_gateway
from agentic_portfolio.ai.locks import FileLock
from agentic_portfolio.ai.pipeline import PipelineResult, run_candidate_pipeline
from agentic_portfolio.ai.safety import assert_proposal_only
from agentic_portfolio.calendar import EASTERN, NyseEquityCalendar, REGULAR_CLOSE, REGULAR_OPEN
from agentic_portfolio.discovery.snapshot import SecuritySnapshot
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.live.engine import load_live_context, refresh_live_portfolio
from agentic_portfolio.live.store import LivePortfolioStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import RuntimeMode, resolve_runtime_mode
from agentic_portfolio.schemas import PortfolioContext, to_dict

JobFn = Callable[..., Any]


def _parse_hhmm(value: str) -> time:
    hours, minutes = str(value).split(":")
    return time(int(hours), int(minutes))


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


class JobLock:
    def __init__(self, path: Path, *, timeout: float = 0.2, stale_seconds: int = 900) -> None:
        self.path = path
        self.stale_seconds = stale_seconds
        self._lock = FileLock(path.with_suffix(path.suffix + ".guard"), timeout=timeout)
        self.held = False

    def acquire(self, *, job: str, now: datetime) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._lock.acquire()
        except TimeoutError:
            return False
        if self.path.exists():
            try:
                meta = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
            pid = int(meta.get("pid") or 0)
            started = str(meta.get("started_at") or "")
            stale = True
            if pid_alive(pid):
                stale = False
            if started:
                try:
                    stamp = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    if (now - stamp).total_seconds() <= self.stale_seconds:
                        stale = False if pid_alive(pid) else True
                    else:
                        stale = True
                except ValueError:
                    stale = True
            if not stale:
                self._lock.release()
                return False
        self.path.write_text(
            json.dumps({"job": job, "pid": os.getpid(), "started_at": now.isoformat()}),
            encoding="utf-8",
        )
        self.held = True
        return True

    def release(self) -> None:
        if self.held and self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass
        self._lock.release()
        self.held = False


class Scheduler:
    """Weekday PREMARKET / MARKET HOURS / POSTMARKET jobs. Restart-safe."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        config: dict[str, Any] | None = None,
        gateway: AIGateway | None = None,
        runtime_mode: RuntimeMode | str | None = None,
        snapshots_fn: Callable[[], list[SecuritySnapshot]] | None = None,
        refresh_fn: Callable[..., Any] | None = None,
        context_fn: Callable[[], PortfolioContext] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.base = root or project_root()
        self.config = config or load_ai_config()
        self.sched = dict(self.config.get("scheduler") or {})
        self.runtime_mode = runtime_mode or resolve_runtime_mode()
        if isinstance(self.runtime_mode, str):
            self.runtime_mode = RuntimeMode(self.runtime_mode)
        self.gateway = gateway
        self.snapshots_fn = snapshots_fn or (lambda: [])
        self.refresh_fn = refresh_fn
        self.context_fn = context_fn
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.state_path = self.base / str(self.sched.get("state_path") or "state/scheduler/state.json")
        self.lock_dir = self.base / str(self.sched.get("lock_dir") or "state/scheduler/locks")
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._last: dict[str, Any] = self._load_state()

    def now(self) -> datetime:
        stamp = self._now()
        if stamp.tzinfo is None:
            return stamp.replace(tzinfo=timezone.utc)
        return stamp

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"jobs": {}, "last_tick": None}

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._last, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.state_path)

    def due_jobs(self, now: datetime | None = None) -> list[str]:
        stamp = now or self.now()
        et = stamp.astimezone(EASTERN)
        cal = NyseEquityCalendar()
        session = cal.session_for(stamp)
        weekday = et.weekday() < 5 and session is not None
        due: list[str] = []
        pre = dict(self.sched.get("premarket") or {})
        market = dict(self.sched.get("market_hours") or {})
        post = dict(self.sched.get("postmarket") or {})
        if pre.get("enabled", True) and (not pre.get("weekday_only", True) or weekday):
            target = _parse_hhmm(str(pre.get("time") or "07:00"))
            if et.hour == target.hour and et.minute == target.minute:
                if self._not_run("PREMARKET", et.date().isoformat()):
                    due.append("PREMARKET")
        if market.get("enabled", True) and (not market.get("weekday_only", True) or weekday):
            start = _parse_hhmm(str(market.get("window_start") or "09:30"))
            end = _parse_hhmm(str(market.get("window_end") or "16:00"))
            if start <= et.time() < end:
                every = int(market.get("every_minutes") or 15)
                last = (self._last.get("jobs") or {}).get("MARKET_HOURS") or {}
                last_at = last.get("at")
                run = True
                if last_at:
                    try:
                        prev = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
                        if (stamp - prev).total_seconds() < every * 60:
                            run = False
                    except ValueError:
                        run = True
                if run:
                    due.append("MARKET_HOURS")
        if post.get("enabled", True) and (not post.get("weekday_only", True) or weekday):
            target = _parse_hhmm(str(post.get("time") or "16:15"))
            if et.hour == target.hour and et.minute == target.minute:
                if self._not_run("POSTMARKET", et.date().isoformat()):
                    due.append("POSTMARKET")
        return due

    def _not_run(self, job: str, day: str) -> bool:
        last = (self._last.get("jobs") or {}).get(job) or {}
        return str(last.get("session_date") or "") != day

    def tick(self, now: datetime | None = None, *, snapshots: list[SecuritySnapshot] | None = None) -> list[dict[str, Any]]:
        stamp = now or self.now()
        results = []
        for job in self.due_jobs(stamp):
            results.append(self.run_job(job, now=stamp, snapshots=snapshots))
        self._last["last_tick"] = stamp.isoformat()
        self._save_state()
        return results

    def run_job(
        self,
        job: str,
        *,
        now: datetime | None = None,
        snapshots: list[SecuritySnapshot] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        stamp = now or self.now()
        assert_proposal_only()
        lock = JobLock(self.lock_dir / f"{job}.lock", stale_seconds=int(self.sched.get("job_timeout_seconds") or 900))
        if not lock.acquire(job=job, now=stamp):
            row = {
                "job": job,
                "status": "SKIPPED_ALREADY_RUNNING",
                "at": stamp.isoformat(),
                "placement_attempted": False,
            }
            append_jsonl({"type": "SCHEDULER_JOB", **row}, self.base / "logs" / "scheduler.jsonl")
            if force:
                raise DuplicateJobError(f"{job} is already running")
            return row
        try:
            payload = self._execute(job, stamp, snapshots=snapshots)
        except (MissingBrokerFacts, StaleSnapshotError) as exc:
            payload = {"job": job, "status": "FAIL_CLOSED", "reason": str(exc), "placement_attempted": False}
        except Exception as exc:  # noqa: BLE001 — scheduler must survive a job failure
            payload = {"job": job, "status": "ERROR", "reason": f"{type(exc).__name__}: {exc}", "placement_attempted": False}
        finally:
            lock.release()
        payload.setdefault("job", job)
        payload.setdefault("at", stamp.isoformat())
        payload.setdefault("placement_attempted", False)
        payload["session_date"] = stamp.astimezone(EASTERN).date().isoformat()
        jobs = dict(self._last.get("jobs") or {})
        jobs[job] = payload
        self._last["jobs"] = jobs
        self._save_state()
        append_jsonl({"type": "SCHEDULER_JOB", **payload}, self.base / "logs" / "scheduler.jsonl")
        return payload

    def _execute(self, job: str, now: datetime, *, snapshots: list[SecuritySnapshot] | None) -> dict[str, Any]:
        mode = self.runtime_mode
        context = None
        snapshot_id = None
        live_snap = None
        if self.refresh_fn and job in {"PREMARKET", "MARKET_HOURS", "POSTMARKET"}:
            refreshed = self.refresh_fn()
            snapshot_id = getattr(refreshed, "snapshot_id", None)
            context = getattr(refreshed, "context", None)
            live_snap = getattr(refreshed, "snapshot", None)
        if context is None and self.context_fn:
            context = self.context_fn()
        if context is None and mode is RuntimeMode.LIVE:
            context = load_live_context(self.base)
            live_snap = LivePortfolioStore(self.base).current_book()
        if context is None:
            raise MissingBrokerFacts("scheduler has no portfolio context")

        ai_this_tick = job == "PREMARKET"
        if job == "MARKET_HOURS":
            # Deterministic monitor by default. AI only if reassessment triggers exist.
            ai_this_tick = bool((self._last.get("jobs") or {}).get("MARKET_HOURS", {}).get("reassessment_due"))
        if job == "POSTMARKET":
            ai_this_tick = False

        pipeline: PipelineResult | None = None
        if ai_this_tick and job == "PREMARKET":
            gw = self.gateway or build_gateway(self.base, runtime_mode=mode, now_fn=self._now)
            universe = snapshots if snapshots is not None else self.snapshots_fn()
            pipeline = run_candidate_pipeline(
                universe,
                context,
                gw,
                runtime_mode=mode,
                root=self.base,
                now=now,
                snapshot=live_snap,
                snapshot_id=snapshot_id,
            )
        return {
            "job": job,
            "status": "OK",
            "ai_invoked": bool(pipeline),
            "scan_id": pipeline.scan_id if pipeline else None,
            "proposals": [p.proposal_id for p in pipeline.proposals] if pipeline else [],
            "placement_attempted": False,
            "nav": context.current_nav,
            "cash": context.cash,
            "buying_power": context.buying_power,
            "snapshot_id": snapshot_id,
        }


def run_forever(
    root: Path | None = None,
    *,
    sleep_seconds: int = 30,
    stop: Callable[[], bool] | None = None,
) -> None:
    """Production loop for the Raspberry Pi. Cursor is not required."""
    sched = Scheduler(root)
    while True:
        if stop and stop():
            return
        sched.tick()
        if stop and stop():
            return
        import time

        time.sleep(sleep_seconds)
