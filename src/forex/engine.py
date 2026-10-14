"""The Forex engine: the 537 Academy on real OANDA market data. Read-only at the broker.

Floors: RESEARCH (Floor 1: no trading; a research sprint every night at 01:00 New York and a
morning report), PAPER (Floor 2: paper trading on live prices), DEMO (Floor 3: OANDA practice
account; seats assigned by the owner), RETIRED.

Loop (every few seconds while the forex market is open):
  * poll OANDA prices for the universe (one request) and publish them for the rates board;
  * every minute, check stops/targets on the latest completed 1-minute candles for every
    instrument an agent holds;
  * when an agent's decision timeframe closes a bar, run its strategy on completed candles
    from marketdata.db and apply the actions through its risk envelope (paper fills at the
    live bid/ask);
  * enforce time rules (day traders flat before the rollover, weekend rules for swing),
    financing at the 17:00 New York rollover, drawdown pauses, and record equity.
The loop below applies to Floor 2 agents. Background worker (one job at a time): history
backfill, validations, nightly sprints, the morning report.
Owner commands arrive as JSON files in the inbox (pause, resume, validate, sprint, auto_on,
auto_off, to_demo, to_research, retire).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from forex.academy import AcademyDB
from forex import demo as demo_desk
from forex.agents import ALL_AGENTS, default_params
from forex.backtest import USD_PAIR, AgentConfig
from forex.fxclock import market_open, ny, rollover_window
from forex.instruments import UNIVERSE, Converter, Spec
from forex.market import SECONDS, MarketStore
from forex.oanda import OandaClient, OandaError
from forex.research import RETIRE_AFTER, sprint
from forex.sim import BarPrices, Book, Executor, Quote
from forex.strategies import get as get_strategy
from forex.strategy import Context, Strategy
from forex.validation import validate

log = logging.getLogger("forex.engine")

# history kept for validation and live decisions (seconds)
HISTORY = {"M15": int(2.75 * 365 * 86400), "H1": int(3 * 365 * 86400), "H4": int(8.6 * 365 * 86400), "D": int(10 * 365 * 86400)}
EQUITY_EVERY = 15 * 60
# Bump to force a full history reload and re-validation of every agent on the next start
# (2: the first deploy's backfill skipped older history; its validations used too little data).
DATA_EPOCH = 4  # 4: decisions at the 17:00 rollover (every daily bar) were refused; re-validate and re-try variants
REVALIDATE_DAYS = 28
SPRINT_HOUR_NY = 1  # nightly research sprints start at 01:00 New York; the report is ready well before 08:00
DEMO_SEATS = 2


@dataclass
class Runtime:
    agent_id: str
    name: str
    version: int
    strategy: Strategy
    cfg: AgentConfig
    executor: Executor
    book: Book
    status: str
    stage: str = "RESEARCH"
    last_decision: int = 0
    pending: tuple[int, list] | None = None  # (decision time, actions) waiting for the rollover window to pass
    activity: str = "idle"
    errors: int = 0


@dataclass
class Job:
    name: str
    future: Future
    started: float = field(default_factory=time.time)


class Engine:
    def __init__(self, client: OandaClient | None, academy: AcademyDB, store: MarketStore, inbox: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.client = client
        self.db = academy
        self.store = store
        self.inbox = inbox
        self.clock = clock
        self.specs: dict[str, Spec] = {}
        self.agents: dict[str, Runtime] = {}
        self.quotes: dict[str, Quote] = {}
        self.tradeable: dict[str, bool] = {}
        self.last_bar: dict[tuple[str, str], int] = {}
        self.next_check: dict[tuple[str, str], float] = {}
        self.last_m1: dict[str, int] = {}
        self.last_equity_mark = 0
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="academy-worker")
        self.job: Job | None = None
        self.queue: list[tuple[str, Callable[[], Any]]] = []
        self.last_error: str | None = None
        self.started_at = int(clock())
        self.demo: tuple[Any, list[str]] | None = None  # (PracticeTrader, seat account IDs) once configured
        self.demo_ids: dict[str, set[str]] = {}
        self.demo_problem: str | None = None

    # -- setup ---------------------------------------------------------------------------------
    def start(self) -> None:
        orphans = self.db.close_unfinished_validations("INTERRUPTED (engine restarted)") + self.db.close_unfinished_sprints()
        if orphans:
            self.db.event(None, "VALIDATION_INTERRUPTED", f"Closed {orphans} validation/sprint run(s) left unfinished by a restart.")
        created = self.db.seed(ALL_AGENTS, default_params)
        if created:
            log.info("enrolled %s", created)
        self.load_specs()
        self.load_agents()
        self.load_demo()
        for key in self.feeds():
            last = self.store.last_time(*key)
            if last is not None:
                self.last_bar[key] = last
        stale = self.db.get("data_epoch") != DATA_EPOCH
        if stale:
            self.db.put("data", {"ready": False, "progress": "reloading market history"})
            self.db.write("DELETE FROM tested")  # variants scored before a fix get tried again
        self.enqueue("backfill", self.backfill)
        for rt in self.agents.values():
            if stale or self.db.latest_validation(rt.agent_id, rt.version) is None:
                self.enqueue(f"validate:{rt.agent_id}", self._validate_job(rt.agent_id, "initial" if not stale else "history reloaded"))
        self.db.event(None, "ENGINE_STARTED", "Forex engine started (paper trading on OANDA data; no broker orders).")

    def load_demo(self, desk: tuple[Any, list[str]] | None = None) -> None:
        """The Demo Desk trades only when its separate practice credentials exist.
        `desk` = (trader, seat account IDs) lets tests inject a fake practice broker."""
        if desk is None:
            try:
                loaded = demo_desk.load_practice()
                desk = (demo_desk.PracticeTrader(loaded[0]), loaded[1]) if loaded else None
            except Exception as exc:  # noqa: BLE001
                self.demo, self.demo_problem = None, f"practice credentials: {exc}"
                return
        if desk is None:
            self.demo, self.demo_problem = None, "waiting for the OANDA practice account"
            return
        self.demo, self.demo_problem = desk, (None if desk[1] else "no demo seat accounts configured")

    def seat_for(self, agent_id: str) -> str | None:
        if not self.demo:
            return None
        seats = self.db.get("demo_seats") or {}
        if agent_id in seats:
            return seats[agent_id]
        taken = {v for a, v in seats.items() if a in self.agents and self.agents[a].stage == "DEMO"}
        free = [acct for acct in self.demo[1] if acct not in taken]
        if not free:
            return None
        seats = {a: v for a, v in seats.items() if a in self.agents and self.agents[a].stage == "DEMO"}
        seats[agent_id] = free[0]
        self.db.put("demo_seats", seats)
        self.db.event(agent_id, "DEMO_SEAT", f"{self.agents[agent_id].name} takes Demo Desk seat on practice account {free[0]}.")
        return free[0]

    def demo_step(self, rt: Runtime, now: int, fresh: set[tuple[str, str]], data_ready: bool) -> None:
        """One Demo Desk pass for a Floor 3 agent: mirror the practice account, decide, send the difference."""
        if not self.demo:
            rt.activity = self.demo_problem or "waiting for the OANDA practice account"
            return
        acct = self.seat_for(rt.agent_id)
        if not acct:
            rt.activity = "waiting for a free practice seat"
            return
        trader = self.demo[0]
        try:
            before = demo_desk.mirror(trader, acct, rt.agent_id, rt.version)
            ids = set(before.positions)
            prev = self.demo_ids.get(rt.agent_id)
            if prev is not None:
                before.closed = demo_desk.closed_since(trader, acct, prev, ids, rt.version)
            self.demo_ids[rt.agent_id] = ids
            conv = self._conv()
            actions = []
            trig = rt.strategy.trigger(rt.cfg.params)
            closes = [self.last_bar[(i, trig)] + SECONDS[trig] for i in rt.cfg.params["instruments"] if (i, trig) in fresh and (i, trig) in self.last_bar]
            if data_ready and closes and max(closes) > rt.last_decision:
                T = max(closes)
                rt.last_decision = T
                if now - T <= SECONDS[trig]:
                    data = {(i, g): self.store.bars(i, g, last=n) for (i, g), n in rt.strategy.feeds(rt.cfg.params).items()}
                    ctx = Context(now=T, params=rt.cfg.params, data=data, positions=before.views(), equity=before.equity(self.quotes, conv), specs=self.specs)
                    actions = rt.strategy.on_bar(ctx)
            if rollover_window(now):
                if actions:
                    rt.pending = (now, actions)
                actions = []
            elif rt.pending and now - rt.pending[0] <= 7200:
                actions, rt.pending = list(rt.pending[1]) + actions, None
            rt.executor.roll(before, now, self.quotes, conv)
            after, decided = demo_desk.decide_on_mirror(rt.executor, before, actions, self.quotes, now, conv)
            sent = demo_desk.execute(trader, acct, before, after, agent_id=rt.agent_id, version=rt.version, now=now)
            for e in [x for x in decided if x["type"] == "ENTRY_REFUSED"] + sent:
                self.db.event(rt.agent_id, e["type"], f"{rt.name}: {e['summary']}", e["payload"])
            rt.book = before
            self.db.save_book(before)
            if now - self.last_equity_mark < 5:
                self.db.mark(rt.agent_id, now, before.equity(self.quotes, conv), before.cash)
            rt.activity = "trading on the demo account" if after.positions else "watching (demo)"
        except (OandaError, KeyError) as exc:
            self.last_error = f"demo {rt.agent_id}: {exc}"
            rt.activity = "demo account error"

    def load_specs(self) -> None:
        if self.client is not None:
            try:
                raws = [i.raw for i in self.client.instruments(self.client.account_id, UNIVERSE)]
                self.store.save_instruments(raws)
            except OandaError as exc:
                log.warning("instrument refresh failed: %s", exc)
        self.specs = {n: self.store.spec(n) for n in UNIVERSE}

    def load_agents(self) -> None:
        self.agents = {}
        for a in self.db.agents():
            if a["status"] == "RETIRED" or a["stage"] == "RETIRED":
                continue
            v, params, env = self.db.version(a["id"])
            book = self.db.load_book(a["id"])
            if a["status"] == "PAUSED" and not book.paused:
                book.paused = a["status_reason"] or "paused by owner"
            self.agents[a["id"]] = Runtime(
                a["id"], a["name"], v, get_strategy(a["strategy"]), AgentConfig(a["id"], a["strategy"], params, env, v),
                Executor(env, self.specs, v), book, a["status"], a["stage"],
            )

    def feeds(self) -> set[tuple[str, str]]:
        """Every (instrument, granularity) any agent reads, plus hourly USD conversion pairs."""
        out: set[tuple[str, str]] = set()
        for rt in self.agents.values():
            out |= set(rt.strategy.feeds(rt.cfg.params))
            trig = rt.strategy.trigger(rt.cfg.params)
            out |= {(i, trig) for i in rt.cfg.params["instruments"]}
        out |= {(p, g) for p in USD_PAIR.values() for g in ("H1", "H4")}  # USD conversion (live and backtests)
        return out

    # -- background jobs -----------------------------------------------------------------------
    def enqueue(self, name: str, fn: Callable[[], Any]) -> None:
        if all(n != name for n, _ in self.queue) and not (self.job and self.job.name == name):
            self.queue.append((name, fn))

    def _pump(self) -> None:
        if self.job and self.job.future.done():
            exc = self.job.future.exception()
            if exc:
                self.last_error = f"{self.job.name}: {type(exc).__name__}: {exc}"
                log.error("job %s failed: %s", self.job.name, exc, exc_info=exc)
                self.db.event(None, "JOB_FAILED", f"Academy job {self.job.name} failed: {exc}")
            self.job = None
        if self.job is None and self.queue:
            name, fn = self.queue.pop(0)
            self.job = Job(name, self.worker.submit(fn))

    def backfill(self) -> dict[str, int]:
        if self.client is None:
            return {}
        now = int(self.clock())
        stored: dict[str, int] = {}
        store = MarketStore(self.store.path)
        keys = sorted(self.feeds(), key=lambda k: SECONDS[k[1]], reverse=True)
        for i, (inst, gran) in enumerate(keys):
            self.db.put("data", {"ready": False, "progress": f"{i + 1}/{len(keys)} {inst} {gran}"})
            n = store.backfill(self.client, inst, gran, since=now - HISTORY.get(gran, 86400 * 30))
            stored[f"{inst}:{gran}"] = n
        store.close()
        self.db.put("data", {"ready": True, "progress": "complete", "at": now, "rows": sum(stored.values())})
        self.db.put("data_epoch", DATA_EPOCH)
        self.db.event(None, "DATA_READY", f"Market history loaded from OANDA ({sum(stored.values()):,} new candles).")
        return stored

    def _validate_job(self, agent_id: str, kind: str) -> Callable[[], Any]:
        def job() -> dict[str, Any]:
            rt = self.agents[agent_id]
            vid = self.db.start_validation(agent_id, rt.version, kind)
            trials = self.db.agent(agent_id)["trials"]
            store = MarketStore(self.store.path)  # the worker's own connection
            try:
                report = validate(store, rt.cfg, self.specs, trials=trials, progress=lambda s: self.db.progress_validation(vid, f"RUNNING: {s}"))
            except Exception as exc:
                self.db.finish_validation(vid, {"status": "ERROR", "passed": False, "error": f"{type(exc).__name__}: {exc}", "checks": []})
                raise
            finally:
                store.close()
            self.db.finish_validation(vid, report)
            self.db.set_validation_status(agent_id, rt.version, report["status"])
            fails = [c["name"] for c in report.get("checks", []) if not c["passed"]]
            self.db.event(agent_id, "VALIDATION", f"{rt.name} v{rt.version} validation {report['status'].lower()}" + (f" (failed: {', '.join(fails)})" if fails else " (all checks)"), {"validation_id": vid, "score": report.get("score")})
            return {"status": report["status"]}

        return job

    def _sprint_job(self, agent_id: str, day: str) -> Callable[[], Any]:
        def job() -> dict[str, Any]:
            rt = self.agents[agent_id]
            a = self.db.agent(agent_id)
            if a["stage"] != "RESEARCH":
                return {"skipped": "not on Floor 1"}
            sid = self.db.start_sprint(agent_id, day, rt.version)
            self.db.put(f"sprint:{agent_id}", {"running": True, "since": int(self.clock()), "step": "starting"})
            evidence = {k: v["avg_r"] * v["trades"] for k, v in paper_by_instrument(self.db.trades(agent_id)).items()}
            last_v = self.db.latest_validation(agent_id, rt.version)
            for k, v in ((last_v or {}).get("by_instrument") or {}).items():
                evidence[k] = evidence.get(k, 0.0) + float(v.get("avg_r") or 0) * int(v.get("trades") or 0)
            store = MarketStore(self.store.path)
            try:
                result = sprint(store, rt.cfg, self.specs, tested=self.db.tested_hashes(agent_id), trials=a["trials"], day=day, evidence=evidence,
                                progress=lambda st: self.db.put(f"sprint:{agent_id}", {"running": True, "since": int(self.clock()), "step": st}))
            except Exception as exc:
                self.db.finish_sprint(sid, outcome="ERROR", to_version=None, passed=False, summary={"error": f"{type(exc).__name__}: {exc}"})
                raise
            finally:
                store.close()
                self.db.put(f"sprint:{agent_id}", {"running": False, "at": int(self.clock())})
            self.db.mark_tested(agent_id, [(e["hash"], e["score"]) for e in result.get("evaluated", [])])
            self.db.add_trials(agent_id, int(result.get("trials") or 0))
            new_v = None
            outcome = result["outcome"]
            if outcome in ("IMPROVED", "PROMOTE"):
                new_v = self.db.add_version(agent_id, result["params"], rt.cfg.envelope, note=f"sprint {day}: {result['change']}", by="research-sprint", validation_status=result["validation"]["status"])
            target_v = new_v or rt.version
            if result.get("validation"):
                vid = self.db.start_validation(agent_id, target_v, "sprint")
                self.db.finish_validation(vid, {**result["validation"], "day1": result.get("day1")})
                self.db.set_validation_status(agent_id, target_v, result["validation"]["status"])
            if new_v:
                self.reload_agent(agent_id)
            if outcome in ("PROMOTE", "PROMOTE_CURRENT"):
                self.db.set_stage(agent_id, "PAPER", by="research-sprint", reason=f"passed every gate on {day}")
                self.reload_stage(agent_id)
                self.db.event(agent_id, "PROMOTED", f"{rt.name} v{target_v} passed every gate and moves to Floor 2 (paper trading with a fresh $10,000).")
            sprints_done = len(self.db.q("SELECT 1 FROM sprints WHERE agent_id = ? AND finished_at IS NOT NULL", (agent_id,))) + 1
            summary = {k: v for k, v in result.items() if k not in ("validation", "day1", "evaluated", "params")}
            summary.update({
                "agent": rt.name, "style": rt.cfg.envelope.style, "from_version": rt.version if not new_v else new_v - 1, "to_version": new_v,
                "validation": _slim_validation(result.get("validation")), "day1": {k: v for k, v in (result.get("day1") or {}).items() if k != "starts"},
                "top_candidates": result.get("evaluated", [])[:5], "sprints_so_far": sprints_done,
                "recommend_retire": outcome not in ("PROMOTE", "PROMOTE_CURRENT") and sprints_done >= RETIRE_AFTER,
            })
            self.db.finish_sprint(sid, outcome=outcome, to_version=new_v, passed=bool(result.get("passed")), summary=summary)
            self.db.event(agent_id, "SPRINT", f"{rt.name} research sprint {day}: {result.get('verdict', outcome)}", {"sprint_id": sid, "outcome": outcome, "to_version": new_v})
            return {"outcome": outcome}

        return job

    def _report_job(self, day: str) -> Callable[[], Any]:
        def job() -> dict[str, Any]:
            rows = self.db.q("SELECT * FROM sprints WHERE day = ? ORDER BY id", (day,))
            agents = []
            for r in rows:
                sm = json.loads(r["summary_json"] or "{}")
                agents.append({"agent_id": r["agent_id"], "name": sm.get("agent", r["agent_id"]), "outcome": r["outcome"], "passed": bool(r["passed"]),
                               "from_version": r["from_version"], "to_version": r["to_version"], "verdict": sm.get("verdict"), "tested": sm.get("tested"),
                               "unseen": (sm.get("validation") or {}).get("unseen"), "day1": sm.get("day1"), "recommend_retire": sm.get("recommend_retire")})
            report = {"day": day, "generated_at": int(self.clock()), "agents": agents,
                      "passed": [a["name"] for a in agents if a["passed"]], "improved": [a["name"] for a in agents if a["outcome"] == "IMPROVED"],
                      "floor2": [rt.name for rt in self.agents.values() if rt.stage == "PAPER"]}
            self.db.put(f"sprint_report:{day}", report)
            self.db.put("sprint_report_latest", report)
            self.db.event(None, "SPRINT_REPORT", f"Research sprint report for {day} ready: {len(agents)} agent(s) tested, {len(report['passed'])} passed.")
            return {"agents": len(agents)}

        return job

    def reload_stage(self, agent_id: str) -> None:
        """Re-read stage and account after a floor change (entering Floor 2 starts a fresh account)."""
        rt = self.agents[agent_id]
        rt.stage = self.db.agent(agent_id)["stage"]
        rt.book = self.db.load_book(agent_id)
        rt.last_decision = 0

    def reload_agent(self, agent_id: str, *, resume: bool = False) -> None:
        """Switch the runtime to the agent's current version (open positions keep their version tag)."""
        rt = self.agents[agent_id]
        v, params, env = self.db.version(agent_id)
        rt.version, rt.cfg, rt.executor = v, AgentConfig(agent_id, rt.cfg.strategy, params, env, v), Executor(env, self.specs, v)
        if resume and rt.book.paused and rt.book.paused.startswith("drawdown"):
            rt.book.paused = None
            rt.book.peak_equity = rt.book.equity(self.quotes, self._conv())
            self.db.save_book(rt.book)

    # -- owner commands ----------------------------------------------------------------------------
    def process_inbox(self) -> None:
        for f in sorted(self.inbox.glob("*.json")):
            try:
                cmd = json.loads(f.read_text(encoding="utf-8"))
                self.command(cmd)
            except Exception as exc:  # noqa: BLE001
                log.warning("bad command %s: %s", f.name, exc)
                self.db.event(None, "COMMAND_REJECTED", f"Ignored a malformed Academy command ({f.name}).")
            finally:
                f.unlink(missing_ok=True)

    def command(self, cmd: dict[str, Any]) -> None:
        agent_id, action, by = cmd.get("agent_id"), cmd.get("action"), str(cmd.get("by") or "owner")[:40]
        if agent_id not in self.agents:
            raise KeyError(agent_id)
        rt = self.agents[agent_id]
        if action == "pause":
            rt.book.paused = f"paused by {by}"
            rt.status = "PAUSED"
            self.db.set_status(agent_id, "PAUSED", rt.book.paused)
        elif action == "resume":
            rt.book.paused = None
            rt.book.peak_equity = rt.book.equity(self.quotes, self._conv())
            rt.status = "ACTIVE"
            self.db.set_status(agent_id, "ACTIVE")
        elif action == "validate":
            self.enqueue(f"validate:{agent_id}", self._validate_job(agent_id, "owner"))
        elif action == "sprint":
            self.enqueue(f"sprint:{agent_id}", self._sprint_job(agent_id, ny(int(self.clock())).date().isoformat()))
        elif action in ("auto_on", "auto_off"):
            self.db.set_auto_revise(agent_id, action == "auto_on")
        elif action == "to_demo":
            if rt.stage != "PAPER":
                raise ValueError("only Floor 2 agents can take a Demo Desk seat")
            if sum(1 for x in self.agents.values() if x.stage == "DEMO") >= DEMO_SEATS:
                raise ValueError("both Demo Desk seats are taken")
            self._leave_paper(rt, "moved to the Demo Desk")
            self.db.set_stage(agent_id, "DEMO", by=by, reason=str(cmd.get("note") or "owner approved a Demo Desk seat"))
            self.reload_stage(agent_id)
        elif action == "to_research":
            self._leave_paper(rt, "sent back to research")
            self.db.set_stage(agent_id, "RESEARCH", by=by, reason=str(cmd.get("note") or "sent back to research"))
            self.reload_stage(agent_id)
        elif action == "retire":
            self._leave_paper(rt, "retired")
            self.db.set_stage(agent_id, "RETIRED", by=by, reason=str(cmd.get("note") or "retired by the owner"))
            self.db.set_status(agent_id, "RETIRED", "retired")
            self.db.event(agent_id, "RETIRED", f"{rt.name} retired to the Alumni wall (records kept).")
            del self.agents[agent_id]
            self.db.event(agent_id, "OWNER_COMMAND", f"{by}: retire {rt.name}")
            return
        else:
            raise ValueError(action)
        self.db.save_book(rt.book)
        self.db.event(agent_id, "OWNER_COMMAND", f"{by}: {action} {rt.name}" + (f" ({cmd.get('note')})" if cmd.get("note") else ""))

    def _leave_paper(self, rt: Runtime, reason: str) -> None:
        """Close any paper positions at the market before an agent leaves Floor 2."""
        conv = self._conv()
        for p in list(rt.book.positions.values()):
            q = self.quotes.get(p.instrument)
            if q:
                self._record(rt, [rt.executor._close(rt.book, p, q.bid if p.side > 0 else q.ask, int(self.clock()), reason, conv)])
        self.db.save_book(rt.book)

    # -- market data --------------------------------------------------------------------------------
    def _conv(self) -> Converter:
        return Converter({i: q.mid for i, q in self.quotes.items()})

    def poll_prices(self) -> None:
        if self.client is None:
            return
        prices = self.client.pricing(self.client.account_id, UNIVERSE)
        for p in prices:
            if p.bid is not None and p.ask is not None:
                self.quotes[p.instrument] = Quote(float(p.bid), float(p.ask), int(p.time.timestamp()) if p.time else int(self.clock()))
                self.tradeable[p.instrument] = p.tradeable
        self.db.put_prices(self.quotes, self.tradeable)

    def refresh_bars(self, now: int) -> set[tuple[str, str]]:
        """Fetch any bar that should have closed by now. Returns the (instrument, gran) with a new bar."""
        fresh: set[tuple[str, str]] = set()
        if self.client is None:
            return fresh
        for key in sorted(self.feeds()):
            inst, gran = key
            due = self.next_check.get(key, 0)
            last = self.last_bar.get(key)
            expected = (last + 2 * SECONDS[gran]) if last is not None else 0
            if now < due or (last is not None and now < expected + 3):
                continue
            try:
                self.store.refresh(self.client, inst, gran, count=5)
            except OandaError as exc:
                self.last_error = f"candles {inst} {gran}: {exc}"
                self.next_check[key] = now + 60
                continue
            new_last = self.store.last_time(inst, gran)
            if new_last is not None and new_last != last:
                self.last_bar[key] = new_last
                fresh.add(key)
                self.next_check[key] = 0
            else:
                self.next_check[key] = now + (20 if gran in ("M1", "M15") else 60)
        return fresh

    def check_m1_exits(self, now: int) -> None:
        held = {p.instrument for rt in self.agents.values() for p in rt.book.positions.values()}
        if not held or self.client is None:
            return
        conv = self._conv()
        for inst in sorted(held):
            try:
                candles = [c for c in self.client.candles(inst, "M1", count=6) if c.complete and c.bid and c.ask]
            except OandaError as exc:
                self.last_error = f"M1 {inst}: {exc}"
                continue
            last = self.last_m1.get(inst, now - 180)
            for c in candles:
                t = int(c.time.timestamp())
                if t <= last:
                    continue
                bar = BarPrices(t, t + 60, float(c.bid.o), float(c.bid.h), float(c.bid.l), float(c.bid.c), float(c.ask.o), float(c.ask.h), float(c.ask.l), float(c.ask.c))
                for rt in self.agents.values():
                    if any(p.instrument == inst for p in rt.book.positions.values()):
                        self._record(rt, rt.executor.check_exits(rt.book, inst, bar, conv))
                self.last_m1[inst] = t

    # -- agents ---------------------------------------------------------------------------------------
    def decide(self, rt: Runtime, now: int, fresh: set[tuple[str, str]]) -> None:
        trig = rt.strategy.trigger(rt.cfg.params)
        closes = [self.last_bar[(i, trig)] + SECONDS[trig] for i in rt.cfg.params["instruments"] if (i, trig) in fresh and (i, trig) in self.last_bar]
        if not closes:
            return
        T = max(closes)
        if T <= rt.last_decision:
            return
        rt.last_decision = T
        if now - T > SECONDS[trig]:
            return  # that bar closed too long ago (restart or catch-up); wait for the next one
        data = {}
        for (inst, gran), n in rt.strategy.feeds(rt.cfg.params).items():
            data[(inst, gran)] = self.store.bars(inst, gran, last=n)
        conv = self._conv()
        rt.activity = "analyzing"
        ctx = Context(now=T, params=rt.cfg.params, data=data, positions=rt.book.views(), equity=rt.book.equity(self.quotes, conv), specs=self.specs)
        try:
            actions = rt.strategy.on_bar(ctx)
            rt.errors = 0
        except Exception as exc:  # noqa: BLE001
            rt.errors += 1
            rt.activity = "error"
            self.db.event(rt.agent_id, "AGENT_ERROR", f"{rt.name}'s strategy raised {type(exc).__name__}: {exc}")
            log.exception("strategy %s failed", rt.agent_id)
            return
        if rollover_window(now):
            rt.pending = (T, actions)  # bars that close at 17:00 decide now and execute after the rollover
            rt.activity = "waiting for the rollover to pass"
        else:
            self._record(rt, rt.executor.apply(rt.book, actions, self.quotes, now, conv))
            rt.activity = "trading" if rt.book.positions else "watching"
        self.db.put(f"decision:{rt.agent_id}", {"at": now, "bar": T, "trigger": trig, "actions": len(actions), "note": ctx.note})

    def release_pending(self, rt: Runtime, now: int) -> None:
        """Execute decisions held through the 17:00 rollover window (dropped if more than 2 hours old)."""
        if rt.pending is None or rollover_window(now):
            return
        T, actions = rt.pending
        rt.pending = None
        if now - T > 7200:
            return
        self._record(rt, rt.executor.apply(rt.book, actions, self.quotes, now, self._conv()))
        rt.activity = "trading" if rt.book.positions else "watching"

    def _record(self, rt: Runtime, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        for e in events:
            if e["type"] == "ENTRY_REFUSED" and e["payload"].get("reason", "").split(":")[0] in ("MARKET_WINDOW", "ALREADY_IN_INSTRUMENT", "MAX_POSITIONS"):
                continue  # routine; not worth a journal line
            self.db.event(rt.agent_id, e["type"], f"{rt.name}: {e['summary']}", e["payload"])
        self.db.save_book(rt.book)

    def housekeeping(self, now: int) -> None:
        conv = self._conv()
        for rt in self.agents.values():
            if rt.stage == "DEMO":
                continue  # the Demo Desk mirrors and manages its broker positions itself
            if rt.stage != "PAPER":
                if rt.book.positions:
                    self._leave_paper(rt, "left the paper floor")
                continue
            try:
                events = rt.executor.financing(rt.book, now, self.quotes, conv)  # once, at each 17:00 NY rollover
                events += rt.executor.time_rules(rt.book, now, self.quotes, conv)
                events += rt.executor.roll(rt.book, now, self.quotes, conv)
            except KeyError:
                continue  # a conversion price is missing; try next tick
            self._record(rt, events)
        if now - self.last_equity_mark >= EQUITY_EVERY:
            for rt in self.agents.values():
                if rt.stage != "PAPER":
                    continue
                try:
                    eq = rt.book.equity(self.quotes, conv)
                except KeyError:
                    continue
                self.db.mark(rt.agent_id, now, eq, rt.book.cash)
                self.db.save_book(rt.book)
            self.last_equity_mark = now

    def weekly(self, now: int) -> None:
        """Saturdays: re-validate Floor 2 and 3 agents whose last validation is older than four weeks.
        Agents above Floor 1 are not modified (a changed strategy goes back to research)."""
        t = ny(now)
        if t.weekday() != 5 or t.hour < 8:
            return
        week = t.strftime("%G-W%V")
        if self.db.get("weekly_done") == week:
            return
        for rt in self.agents.values():
            if rt.stage not in ("PAPER", "DEMO"):
                continue
            last = self.db.latest_validation(rt.agent_id, rt.version)
            if last is None or (datetime.now(timezone.utc) - datetime.fromisoformat(last["finished_at"])).days >= REVALIDATE_DAYS:
                self.enqueue(f"validate:{rt.agent_id}", self._validate_job(rt.agent_id, "weekly"))
        self.db.put("weekly_done", week)

    def nightly(self, now: int, data_ready: bool) -> None:
        """One research sprint per Floor 1 agent each night from 01:00 New York, then the report."""
        t = ny(now)
        day = t.date().isoformat()
        if not data_ready or t.hour < SPRINT_HOUR_NY or self.db.get("sprint_day") == day:
            return
        for rt in self.agents.values():
            if rt.stage == "RESEARCH" and self.db.agent(rt.agent_id)["auto_revise"]:
                self.enqueue(f"sprint:{rt.agent_id}", self._sprint_job(rt.agent_id, day))
        self.enqueue(f"report:{day}", self._report_job(day))
        self.db.put("sprint_day", day)

    # -- the loop -------------------------------------------------------------------------------------
    def tick(self) -> None:
        now = int(self.clock())
        self.process_inbox()
        self._pump()
        open_ = market_open(now)
        data_ready = bool((self.db.get("data") or {}).get("ready"))
        if open_:
            try:
                self.poll_prices()
                fresh = self.refresh_bars(now)
                self.check_m1_exits(now)
                if data_ready:
                    for rt in self.agents.values():
                        if rt.stage == "PAPER" and rt.status != "RETIRED":
                            self.release_pending(rt, now)
                            self.decide(rt, now, fresh)
                self.housekeeping(now)
                for rt in list(self.agents.values()):
                    if rt.stage == "DEMO" and rt.status != "PAUSED":
                        self.demo_step(rt, now, fresh, data_ready)
            except OandaError as exc:
                self.last_error = str(exc)
                log.warning("OANDA: %s", exc)
        else:
            self.weekly(now)
        self.nightly(now, data_ready)
        self.heartbeat(now, open_, data_ready)

    def heartbeat(self, now: int, open_: bool, data_ready: bool) -> None:
        self.db.put("engine", {
            "heartbeat": now, "started_at": self.started_at, "market_open": open_, "data_ready": data_ready,
            "job": {"name": self.job.name, "since": int(self.job.started)} if self.job else None,
            "queue": [n for n, _ in self.queue], "last_error": self.last_error, "connected": self.client is not None,
            "demo": {"configured": self.demo is not None, "problem": self.demo_problem, "seats": len(self.demo[1]) if self.demo else 0},
            "agents": {a: {"activity": rt.activity if rt.stage == "PAPER" else ("researching" if self.db.get(f"sprint:{a}", {}).get("running") else "studying"),
                           "stage": rt.stage, "version": rt.version, "paused": rt.book.paused, "positions": len(rt.book.positions)} for a, rt in self.agents.items()},
        })

    def run(self, stop: threading.Event, *, interval: float = 10.0) -> None:
        self.start()
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("tick failed")
            stop.wait(interval)
        self.worker.shutdown(wait=False, cancel_futures=True)


def paper_by_instrument(rows) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = out.setdefault(r["instrument"], {"trades": 0, "sum_r": 0.0})
        d["trades"] += 1
        d["sum_r"] += r["r"]
    return {k: {"trades": v["trades"], "avg_r": v["sum_r"] / v["trades"]} for k, v in out.items()}


def _slim_validation(v: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not v:
        return None
    return {"status": v.get("status"), "passed": v.get("passed"), "score": v.get("score"), "checks": v.get("checks"),
            "unseen": (v.get("metrics") or {}).get("unseen"), "holdout": (v.get("metrics") or {}).get("holdout"),
            "trial_adjusted_sharpe": v.get("trial_adjusted_sharpe")}
