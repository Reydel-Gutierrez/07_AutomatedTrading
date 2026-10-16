"""The Stock Swing Class engine. A separate, low-priority process next to the firm; it places no
orders and talks to no broker.

  * Market data: the firm's daily bars (firm.db, read only), which the firm refreshes at 17:15
    New York for the names this engine asks for (kv "wanted_symbols").
  * Floor 1: a research sprint for every research agent at 01:30 New York (after the forex
    sprints start), and once as soon as an agent has enough history and no sprint yet.
  * Floor 2: when a new trading day's bars arrive, each paper agent's record is rebuilt from its
    promotion day with its frozen version (decisions at each close, fills at the next open).
  * Owner commands arrive as JSON files in the inbox: sprint, pause, resume, to_research, retire.
One background job at a time; the loop keeps a heartbeat so HQ can show whether it is alive.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from equity.academy import AcademyDB
from equity.agents import CLASS, default_params, wanted_symbols
from equity.data import Panel, coverage, load_panel
from equity.research import RETIRE_AFTER, evidence_from, sprint
from equity.sim import Config, run
from forex.fxclock import NY

log = logging.getLogger("equity.engine")
SPRINT_AT = (1, 30)
RELOAD_EVERY = 600


class Engine:
    def __init__(self, db: AcademyDB, firm_db: Path, inbox: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.db, self.firm_db, self.inbox, self.clock = db, firm_db, inbox, clock
        self.panel: Panel | None = None
        self.panel_sig: tuple | None = None
        self.loaded_at = 0.0
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="equity-worker")
        self.job: tuple[str, Future, float] | None = None
        self.queue: list[tuple[str, Callable[[], Any]]] = []
        self.last_error: str | None = None
        self.activity: dict[str, str] = {}

    # -- setup ------------------------------------------------------------------------------
    def start(self) -> None:
        if self.db.close_unfinished():
            self.db.event(None, "SPRINT_INTERRUPTED", "Closed sprint runs left unfinished by a restart.")
        created = self.db.seed(CLASS, default_params)
        if created:
            log.info("enrolled %s", created)
        self.db.put("wanted_symbols", wanted_symbols())

    def ny_now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc).astimezone(NY)

    # -- loop -------------------------------------------------------------------------------
    def run(self, stop: threading.Event, interval: float = 30.0) -> None:
        self.start()
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("tick failed")
            stop.wait(interval)
        self.worker.shutdown(wait=False, cancel_futures=True)

    def tick(self) -> None:
        self.reap()
        self.commands()
        self.load_data()
        if self.panel is not None and self.job is None:
            self.schedule()
        self.next_job()
        self.heartbeat()

    def heartbeat(self) -> None:
        job = {"name": self.job[0], "since": int(self.job[2])} if self.job else None
        self.db.put("engine", {"heartbeat": int(self.clock()), "job": job, "queue": [n for n, _ in self.queue], "last_error": self.last_error,
                               "data_ready": self.panel is not None and bool(self.panel.dates), "activity": self.activity})

    # -- data -------------------------------------------------------------------------------
    def load_data(self) -> None:
        # reload every 10 minutes, and soon after start (the firm may still be fetching history)
        every = RELOAD_EVERY if self.panel is not None and len(self.panel.dates) >= 1000 else 60
        if self.job is not None or self.clock() - self.loaded_at < every and self.panel is not None:
            return
        self.loaded_at = self.clock()
        panel = load_panel(self.firm_db, wanted_symbols())
        if not panel.dates:
            self.db.put("data", {"ready": False, "reason": "no daily bars in the firm's market cache yet"})
            return
        sig = (panel.dates[-1], sum(len([x for x in s.c if x is not None]) for s in panel.series.values()))
        if sig != self.panel_sig:
            self.panel, self.panel_sig = panel, sig
            cov = coverage(panel)
            self.db.put("data", {"ready": True, "last_date": panel.dates[-1], "first_date": panel.dates[0], "sessions": len(panel.dates), "symbols": cov})
            log.info("market data: %d sessions to %s", len(panel.dates), panel.dates[-1])

    # -- scheduling -------------------------------------------------------------------------
    def schedule(self) -> None:
        assert self.panel is not None
        now = self.ny_now()
        day = now.date().isoformat()
        active = [a for a in self.db.agents() if a["status"] == "ACTIVE"]
        # Floor 2: rebuild paper records when a new day's bars have arrived
        last = self.panel.dates[-1]
        if self.db.get("paper_through") != last and any(a["stage"] == "PAPER" for a in active):
            self.enqueue("paper", self.paper_all)
            return
        # Floor 1: the nightly sprint, and a first sprint as soon as an agent can be tested
        nightly = (now.hour, now.minute) >= SPRINT_AT and self.db.get("sprint_day") != day
        for a in active:
            if a["stage"] != "RESEARCH":
                continue
            if nightly or self.untested(a["id"]):
                self.enqueue(f"sprint:{a['id']}", lambda aid=a["id"], d=day: self.run_sprint(aid, d))
        if nightly:
            self.db.put("sprint_day", day)
            self.enqueue("report", lambda d=day: self.report(d))

    def untested(self, agent_id: str) -> bool:
        """No real sprint yet: never run, or only interrupted / waiting for history that has since grown."""
        last = self.db.one("SELECT outcome, summary_json FROM sprints WHERE agent_id = ? AND outcome NOT IN ('INTERRUPTED') AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", (agent_id,))
        if last is None:
            return True
        if last["outcome"] != "NO_DATA":
            return False
        return (json.loads(last["summary_json"] or "{}").get("sessions") or 0) < len(self.panel.dates if self.panel else [])

    def enqueue(self, name: str, fn: Callable[[], Any]) -> None:
        if any(n == name for n, _ in self.queue) or (self.job and self.job[0] == name):
            return
        self.queue.append((name, fn))

    def next_job(self) -> None:
        if self.job is not None or not self.queue:
            return
        name, fn = self.queue.pop(0)
        self.job = (name, self.worker.submit(fn), self.clock())

    def reap(self) -> None:
        if self.job is None or not self.job[1].done():
            return
        name, fut, _ = self.job
        self.job = None
        exc = fut.exception()
        if exc is not None:
            self.last_error = f"{name}: {type(exc).__name__}: {exc}"
            log.error("job %s failed: %s", name, "".join(traceback.format_exception(exc))[-2000:])
            self.db.event(None, "JOB_FAILED", f"{name} failed: {exc}")

    # -- Floor 1 ----------------------------------------------------------------------------
    def run_sprint(self, agent_id: str, day: str) -> dict[str, Any]:
        assert self.panel is not None
        a = self.db.agent(agent_id)
        if a["stage"] != "RESEARCH" or a["status"] != "ACTIVE":
            return {}
        version, params, env = self.db.version(agent_id)
        cfg = Config(agent_id, a["strategy"], params, env, version)
        sid = self.db.start_sprint(agent_id, day, version)
        self.activity[agent_id] = "sprint: starting"

        def say(step: str) -> None:
            self.activity[agent_id] = f"sprint: {step}"
            self.db.put(f"sprint:{agent_id}", {"running": True, "step": step})

        try:
            r = sprint(self.panel, cfg, tested=self.db.tested_hashes(agent_id), trials=a["trials"], day=day,
                       evidence=evidence_from(self.db.latest_validation(agent_id)), progress=say)
        finally:
            self.db.put(f"sprint:{agent_id}", {"running": False})
            self.activity[agent_id] = "idle"
        self.db.mark_tested(agent_id, [(e["hash"], e["score"]) for e in r.get("evaluated", [])])
        self.db.add_trials(agent_id, r.get("trials", 0))
        to_version = None
        if r["outcome"] in ("PROMOTE", "IMPROVED"):
            to_version = self.db.add_version(agent_id, r["params"], env, note=f"sprint {day}: {r.get('change')}", by="research",
                                             validation_status="VALIDATED" if r["passed"] else "FAILED")
        if r.get("validation"):
            self.db.save_validation(agent_id, to_version or version, "sprint", {**r["validation"], "day1": r.get("day1")})
            if not to_version:
                self.db.set_validation_status(agent_id, version, "VALIDATED" if r["passed"] else r["validation"].get("status", "FAILED"))
        summary = {k: v for k, v in r.items() if k not in ("validation",)}
        summary["sessions"] = len(self.panel.dates)
        summary["validation_checks"] = (r.get("validation") or {}).get("checks")
        summary["unseen"] = ((r.get("validation") or {}).get("metrics") or {}).get("unseen")
        self.db.finish_sprint(sid, outcome=r["outcome"], to_version=to_version, passed=bool(r.get("passed")), summary=summary)
        name = a["name"]
        self.db.event(agent_id, "SPRINT", f"{name}: {r.get('verdict')}", {"outcome": r["outcome"], "day": day})
        if r.get("passed"):
            self.db.set_stage(agent_id, "PAPER", by="research", reason=r.get("verdict") or "passed every gate")
            self.db.event(agent_id, "PROMOTED", f"{name} passed every gate and moved to Floor 2 (paper trading on new bars).")
        return r

    def report(self, day: str) -> dict[str, Any]:
        """The night's sprint report (stored; HQ shows it and the 08:00 line mentions it)."""
        rows = self.db.q("SELECT s.*, a.name FROM sprints s JOIN agents a ON a.id = s.agent_id WHERE s.day = ? AND s.finished_at IS NOT NULL ORDER BY s.id", (day,))
        agents = []
        for r in rows:
            s = json.loads(r["summary_json"] or "{}")
            agents.append({"agent_id": r["agent_id"], "name": r["name"], "outcome": r["outcome"], "passed": bool(r["passed"]),
                           "verdict": s.get("verdict"), "change": s.get("change"), "tested": s.get("tested"),
                           "suggest_retire": self.db.sprints_since_pass(r["agent_id"]) >= RETIRE_AFTER})
        rep = {"day": day, "agents": agents, "passed": [a["name"] for a in agents if a["passed"]],
               "improved": [a["name"] for a in agents if a["outcome"] == "IMPROVED"],
               "floor2": [a["name"] for a in self.db.agents() if a["stage"] == "PAPER"]}
        self.db.put(f"sprint_report:{day}", rep)
        self.db.put("sprint_report_latest", rep)
        return rep

    # -- Floor 2 ----------------------------------------------------------------------------
    def paper_all(self) -> None:
        assert self.panel is not None
        for a in self.db.agents():
            if a["stage"] == "PAPER" and a["status"] == "ACTIVE":
                self.paper(a["id"])
        self.db.put("paper_through", self.panel.dates[-1])

    def paper(self, agent_id: str) -> dict[str, Any]:
        """Rebuild the paper record from the promotion day: the first decision is the last close
        before promotion, so its first fill is the next open after it (never a known price)."""
        assert self.panel is not None
        a = self.db.agent(agent_id)
        version, params, env = self.db.version(agent_id)
        promoted = datetime.fromisoformat(a["stage_since"]).astimezone(NY)
        start = self.panel.index_on_or_before(promoted.date().isoformat())
        if start >= 0 and self.panel.dates[start] == promoted.date().isoformat() and promoted.hour < 16:
            start -= 1  # promoted before that day's close: its last known close is the day before
        if start < 0:
            return {}
        res = run(Config(agent_id, a["strategy"], params, env, version), self.panel, start, len(self.panel.dates))
        by_epoch = dict(zip(self.panel.times, self.panel.dates))
        self.db.save_paper(agent_id, version, res, self.panel.dates, lambda ts: by_epoch.get(ts, ""))
        return {"trades": len(res.trades), "equity": res.equity[-1][1] if res.equity else None}

    # -- owner commands ---------------------------------------------------------------------
    def commands(self) -> None:
        for path in sorted(self.inbox.glob("*.json")):
            try:
                cmd = json.loads(path.read_text(encoding="utf-8"))
                self.apply(cmd)
            except Exception as exc:  # noqa: BLE001
                self.db.event(None, "COMMAND_FAILED", f"Could not apply {path.name}: {exc}")
            finally:
                path.unlink(missing_ok=True)

    def apply(self, cmd: dict[str, Any]) -> None:
        aid, action, by = cmd["agent_id"], cmd["action"], cmd.get("by", "owner")
        a = self.db.agent(aid)
        note = cmd.get("note") or ""
        if action == "sprint":
            if a["stage"] != "RESEARCH":
                raise ValueError("only Floor 1 agents run research sprints")
            self.enqueue(f"sprint:{aid}", lambda: self.run_sprint(aid, self.ny_now().date().isoformat()))
        elif action == "pause":
            self.db.set_status(aid, "PAUSED", note or f"paused by {by}")
        elif action == "resume":
            self.db.set_status(aid, "ACTIVE", None)
        elif action == "to_research":
            self.db.set_stage(aid, "RESEARCH", by=by, reason=note or "sent back to research")
        elif action == "retire":
            self.db.set_stage(aid, "RETIRED", by=by, reason=note or "retired")
            self.db.set_status(aid, "RETIRED", note or None)
        else:
            raise ValueError(f"unknown action {action}")
        self.db.event(aid, "OWNER_COMMAND", f"{a['name']}: {action} by {by}" + (f" ({note})" if note else ""))
