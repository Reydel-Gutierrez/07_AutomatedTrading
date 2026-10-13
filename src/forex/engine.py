"""The Forex engine: Academy paper trading on real OANDA market data. Read-only at the broker.

Loop (every few seconds while the forex market is open):
  * poll OANDA prices for the universe (one request) and publish them for the rates board;
  * every minute, check stops/targets on the latest completed 1-minute candles for every
    instrument an agent holds;
  * when an agent's decision timeframe closes a bar, run its strategy on completed candles
    from marketdata.db and apply the actions through its risk envelope (paper fills at the
    live bid/ask);
  * enforce time rules (day traders flat before the rollover, weekend rules for swing),
    financing at the 17:00 New York rollover, drawdown pauses, and record equity.
Background worker (one at a time): history backfill, validations, weekly revisions.
Owner commands arrive as JSON files in the inbox (pause, resume, validate, review, auto).
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
from typing import Any, Callable

from forex.academy import AcademyDB
from forex.agents import FOUNDING_CLASS, default_params
from forex.backtest import USD_PAIR, AgentConfig
from forex.fxclock import market_open, ny
from forex.instruments import UNIVERSE, Converter, Spec
from forex.market import SECONDS, MarketStore
from forex.oanda import OandaClient, OandaError
from forex.revision import MIN_DAYS_BETWEEN, review
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
DATA_EPOCH = 3  # 3: swing validations converted P&L with hourly prices that only cover 3 of 8 years
REVALIDATE_DAYS = 28


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
    last_decision: int = 0
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

    # -- setup ---------------------------------------------------------------------------------
    def start(self) -> None:
        created = self.db.seed(FOUNDING_CLASS, default_params)
        if created:
            log.info("enrolled %s", created)
        self.load_specs()
        self.load_agents()
        for key in self.feeds():
            last = self.store.last_time(*key)
            if last is not None:
                self.last_bar[key] = last
        stale = self.db.get("data_epoch") != DATA_EPOCH
        if stale:
            self.db.put("data", {"ready": False, "progress": "reloading market history"})
        self.enqueue("backfill", self.backfill)
        for rt in self.agents.values():
            if stale or self.db.latest_validation(rt.agent_id, rt.version) is None:
                self.enqueue(f"validate:{rt.agent_id}", self._validate_job(rt.agent_id, "initial" if not stale else "history reloaded"))
        self.db.event(None, "ENGINE_STARTED", "Forex engine started (paper trading on OANDA data; no broker orders).")

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
            if a["status"] == "RETIRED":
                continue
            v, params, env = self.db.version(a["id"])
            book = self.db.load_book(a["id"])
            if a["status"] == "PAUSED" and not book.paused:
                book.paused = a["status_reason"] or "paused by owner"
            self.agents[a["id"]] = Runtime(
                a["id"], a["name"], v, get_strategy(a["strategy"]), AgentConfig(a["id"], a["strategy"], params, env, v),
                Executor(env, self.specs, v), book, a["status"],
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
            finally:
                store.close()
            self.db.finish_validation(vid, report)
            self.db.set_validation_status(agent_id, rt.version, report["status"])
            fails = [c["name"] for c in report.get("checks", []) if not c["passed"]]
            self.db.event(agent_id, "VALIDATION", f"{rt.name} v{rt.version} validation {report['status'].lower()}" + (f" (failed: {', '.join(fails)})" if fails else " (all checks)"), {"validation_id": vid, "score": report.get("score")})
            return {"status": report["status"]}

        return job

    def _review_job(self, agent_id: str) -> Callable[[], Any]:
        def job() -> dict[str, Any]:
            rt = self.agents[agent_id]
            a = self.db.agent(agent_id)
            current = self.db.latest_validation(agent_id, rt.version)
            paper = paper_by_instrument(self.db.trades(agent_id))
            self.db.put(f"review:{agent_id}", {"running": True, "since": int(self.clock())})
            store = MarketStore(self.store.path)
            try:
                result = review(store, rt.cfg, self.specs, current_validation=current, trials=a["trials"], paper_by_instrument=paper)
            finally:
                store.close()
                self.db.put(f"review:{agent_id}", {"running": False, "at": int(self.clock())})
            self.db.add_trials(agent_id, int(result.get("trials") or 0))
            new_v = None
            if result["outcome"] == "ADOPTED":
                v = result["validation"]
                new_v = self.db.add_version(agent_id, result["params"], rt.cfg.envelope, note=f"revision: {result['chosen']}", by="academy-review", validation_status=v["status"])
                vid = self.db.start_validation(agent_id, new_v, "revision")
                self.db.finish_validation(vid, v)
                self.reload_agent(agent_id, resume=True)
            self.db.add_revision(agent_id, rt.version, new_v, result["outcome"], result.get("chosen"), result.get("reason", ""), result)
            self.db.event(agent_id, "REVISION", f"{rt.name} weekly review: {result['outcome'].lower()}" + (f" ({result.get('chosen')}: {result.get('reason')})" if result.get("chosen") else f" ({result.get('reason', '')})"), {"to_version": new_v})
            return {"outcome": result["outcome"]}

        return job

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
        elif action == "review":
            self.enqueue(f"review:{agent_id}", self._review_job(agent_id))
        elif action in ("auto_on", "auto_off"):
            self.db.set_auto_revise(agent_id, action == "auto_on")
        else:
            raise ValueError(action)
        self.db.save_book(rt.book)
        self.db.event(agent_id, "OWNER_COMMAND", f"{by}: {action} {rt.name}" + (f" ({cmd.get('note')})" if cmd.get("note") else ""))

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
        self._record(rt, rt.executor.apply(rt.book, actions, self.quotes, now, conv))
        rt.activity = "trading" if rt.book.positions else "watching"
        self.db.put(f"decision:{rt.agent_id}", {"at": now, "bar": T, "trigger": trig, "actions": len(actions), "note": ctx.note})

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
            try:
                events = rt.executor.financing(rt.book, now, self.quotes, conv)  # once, at each 17:00 NY rollover
                events += rt.executor.time_rules(rt.book, now, self.quotes, conv)
                events += rt.executor.roll(rt.book, now, self.quotes, conv)
            except KeyError:
                continue  # a conversion price is missing; try next tick
            self._record(rt, events)
        if now - self.last_equity_mark >= EQUITY_EVERY:
            for rt in self.agents.values():
                try:
                    eq = rt.book.equity(self.quotes, conv)
                except KeyError:
                    continue
                self.db.mark(rt.agent_id, now, eq, rt.book.cash)
                self.db.save_book(rt.book)
            self.last_equity_mark = now

    def weekly(self, now: int) -> None:
        """Saturday reviews (market closed): revisions and stale re-validations."""
        t = ny(now)
        if t.weekday() != 5 or t.hour < 8:
            return
        week = t.strftime("%G-W%V")
        if self.db.get("weekly_done") == week:
            return
        for rt in self.agents.values():
            a = self.db.agent(rt.agent_id)
            last = self.db.latest_validation(rt.agent_id, rt.version)
            stale = last is None or (datetime.now(timezone.utc) - datetime.fromisoformat(last["finished_at"])).days >= REVALIDATE_DAYS
            since_rev = a["last_revision_at"] or a["created_at"]
            if a["auto_revise"] and (datetime.now(timezone.utc) - datetime.fromisoformat(since_rev)).days >= MIN_DAYS_BETWEEN:
                self.enqueue(f"review:{rt.agent_id}", self._review_job(rt.agent_id))
            elif stale:
                self.enqueue(f"validate:{rt.agent_id}", self._validate_job(rt.agent_id, "weekly"))
        self.db.put("weekly_done", week)

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
                        if rt.status != "RETIRED":
                            self.decide(rt, now, fresh)
                self.housekeeping(now)
            except OandaError as exc:
                self.last_error = str(exc)
                log.warning("OANDA: %s", exc)
        else:
            self.weekly(now)
        self.heartbeat(now, open_, data_ready)

    def heartbeat(self, now: int, open_: bool, data_ready: bool) -> None:
        self.db.put("engine", {
            "heartbeat": now, "started_at": self.started_at, "market_open": open_, "data_ready": data_ready,
            "job": {"name": self.job.name, "since": int(self.job.started)} if self.job else None,
            "queue": [n for n, _ in self.queue], "last_error": self.last_error, "connected": self.client is not None,
            "agents": {a: {"activity": rt.activity, "version": rt.version, "paused": rt.book.paused, "positions": len(rt.book.positions)} for a, rt in self.agents.items()},
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
