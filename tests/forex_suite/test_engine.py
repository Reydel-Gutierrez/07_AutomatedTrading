from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache

import pytest

from forex.academy import AcademyDB
from forex.engine import Engine
from forex.instruments import UNIVERSE
from forex.market import SECONDS, MarketStore
from forex.oanda import OHLC, Candle, Instrument, Price, parse_time

from tests.forex_suite import synth

NOW0 = synth.START + 197 * 86400 + 16 * 3600  # a Tuesday, 10:00 New York


@lru_cache(maxsize=None)
def _series(inst: str, gran: str):
    n = (NOW0 + 3 * 86400 - synth.START) // SECONDS[gran] + 2
    return synth.series(inst, gran, n)


class FakeClient:
    """Serves synthetic candles that have closed by the test clock; read-only like the real client."""

    env = "practice"
    account_id = "000-000-0000000-000"

    def __init__(self, clock):
        self.clock = clock
        self.calls = 0

    def instruments(self, account_id, names=None):
        return [Instrument.parse({"name": n, "type": "CURRENCY", "pipLocation": -2 if n.endswith("JPY") else -4, "marginRate": "0.02", "minimumTradeSize": "1", "financing": {"longRate": "-0.01", "shortRate": "0.005", "financingDaysOfWeek": []}}) for n in names or UNIVERSE]

    def _visible(self, inst, gran):
        now = self.clock()
        return [c for c in _series(inst, gran) if c.time.timestamp() + SECONDS[gran] <= now]

    def candles(self, instrument, granularity, *, count=None, start=None, end=None, **_):
        self.calls += 1
        if granularity == "M1":
            last = self._visible(instrument, "M15")[-1]
            t0 = int(self.clock()) // 60 * 60 - 360
            return [Candle(datetime.fromtimestamp(t0 + 60 * i, tz=timezone.utc), 1, True, last.bid, last.ask, None) for i in range(6)]
        rows = self._visible(instrument, granularity)
        if start is not None:
            st = parse_time(start).timestamp()
            rows = [c for c in rows if c.time.timestamp() >= st][: count or 5000]
        else:
            rows = rows[-(count or 5):]
        return rows

    def pricing(self, account_id, names):
        out = []
        for n in names:
            c = self._visible(n, "M15")[-1]
            out.append(Price(n, Decimal(str(c.bid.c)), Decimal(str(c.ask.c)), datetime.fromtimestamp(self.clock(), tz=timezone.utc), True))
        return out


@pytest.fixture
def world(tmp_path):
    clock = {"t": NOW0}
    now = lambda: clock["t"]  # noqa: E731
    client = FakeClient(now)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    eng = Engine(client, AcademyDB(tmp_path / "academy.db"), MarketStore(tmp_path / "marketdata.db"), inbox, clock=now)
    return eng, clock, client, inbox


def run_jobs(eng):
    while eng.queue or eng.job:
        eng._pump()
        if eng.job:
            eng.job.future.result()
        eng._pump()


def test_engine_end_to_end(world, monkeypatch):
    eng, clock, client, inbox = world
    # keep validation cheap in tests
    monkeypatch.setattr("forex.engine.validate", lambda store, cfg, specs, **kw: {"status": "FAILED", "passed": False, "score": 0.1, "checks": [{"name": "evidence", "passed": False}]})
    monkeypatch.setattr("forex.engine.SPRINT_HOUR_NY", 99)  # trading test only; sprints have their own tests
    eng.start()
    assert {a["id"] for a in eng.db.agents()} == {"atlas", "kestrel", "mira", "orion", "sable", "halcyon", "vega"}
    assert {a["stage"] for a in eng.db.agents()} == {"RESEARCH"}
    assert all(r["cash"] == 10_000 for r in eng.db.q("SELECT cash FROM accounts"))
    run_jobs(eng)  # backfill + four initial validations
    assert eng.db.get("data")["ready"] is True
    assert eng.store.count("EUR_USD", "M15") > 10_000 and eng.store.count("EUR_USD", "D") > 150
    assert len(eng.db.q("SELECT * FROM validations WHERE finished_at IS NOT NULL")) == 7
    # Floor 1 agents never trade: put the founding class on Floor 2 for this test
    for a in ("atlas", "kestrel", "mira", "orion"):
        eng.db.set_stage(a, "PAPER", by="test", reason="test")
        eng.reload_stage(a)
    # trade through 3 days of 15-minute ticks
    decided = set()
    for _ in range(3 * 96):
        clock["t"] += 900
        eng.tick()
        decided |= {a for a in eng.agents if eng.db.get(f"decision:{a}")}
    assert decided == {"atlas", "kestrel", "mira", "orion"}
    hb = eng.db.get("engine")
    assert hb["heartbeat"] == clock["t"] and hb["data_ready"] is True and set(hb["agents"]) == set(eng.agents)
    assert {a for a, v in hb["agents"].items() if v["stage"] == "PAPER"} == decided
    assert eng.db.q("SELECT count(*) AS n FROM equity")[0]["n"] >= 4 * 50
    assert eng.db.q("SELECT count(*) AS n FROM prices")[0]["n"] == len(UNIVERSE)
    activity = eng.db.q("SELECT count(*) AS n FROM events WHERE type IN ('POSITION_OPENED','POSITION_CLOSED')")[0]["n"]
    # books in the database match memory
    for a, rt in eng.agents.items():
        stored = eng.db.load_book(a)
        assert set(stored.positions) == set(rt.book.positions) and stored.cash == pytest.approx(rt.book.cash)
    assert activity >= 0


def test_owner_commands_and_isolation(world, monkeypatch):
    eng, clock, client, inbox = world
    monkeypatch.setattr("forex.engine.validate", lambda *a, **k: {"status": "PASSED", "passed": True, "score": 1.0, "checks": []})
    eng.start()
    (inbox / "1.json").write_text(json.dumps({"agent_id": "mira", "action": "pause", "by": "owner", "note": "test"}))
    eng.tick()
    assert eng.agents["mira"].book.paused == "paused by owner" and eng.db.agent("mira")["status"] == "PAUSED"
    assert not list(inbox.glob("*.json"))
    (inbox / "2.json").write_text(json.dumps({"agent_id": "mira", "action": "resume"}))
    (inbox / "3.json").write_text(json.dumps({"agent_id": "nobody", "action": "pause"}))
    eng.tick()
    assert eng.agents["mira"].book.paused is None
    assert eng.db.q("SELECT count(*) AS n FROM events WHERE type = 'COMMAND_REJECTED'")[0]["n"] == 1
    # a paused agent's books are its own; nothing it does touches another agent's account
    assert eng.db.load_book("atlas").cash == 10_000


def test_backfill_fills_history_older_than_recent_candles(world):
    """Regression: recent candles stored first (by the live loop) must not stop the history load."""
    eng, clock, client, _ = world
    eng.store.refresh(client, "EUR_USD", "H1", count=5)  # the live loop got there first
    assert eng.store.count("EUR_USD", "H1") == 5
    since = int(clock["t"]) - 120 * 86400
    eng.store.backfill(client, "EUR_USD", "H1", since=since)
    assert eng.store.first_time("EUR_USD", "H1") <= since + 3600
    assert eng.store.count("EUR_USD", "H1") >= 120 * 24 - 2


def test_history_reload_revalidates_everyone(world, monkeypatch):
    eng, clock, client, _ = world
    monkeypatch.setattr("forex.engine.validate", lambda *a, **k: {"status": "FAILED", "passed": False, "score": 0, "checks": []})
    eng.start()
    run_jobs(eng)
    from forex.engine import DATA_EPOCH

    assert eng.db.get("data_epoch") == DATA_EPOCH
    n = len(eng.db.q("SELECT * FROM validations"))
    eng.db.put("data_epoch", DATA_EPOCH - 1)  # an older, incomplete load
    eng.start()
    assert eng.db.get("data")["ready"] is False
    run_jobs(eng)
    assert len(eng.db.q("SELECT * FROM validations")) == n + 7 and eng.db.get("data")["ready"] is True


def test_restart_closes_interrupted_validations(world, monkeypatch):
    eng, clock, client, _ = world
    monkeypatch.setattr("forex.engine.validate", lambda *a, **k: {"status": "FAILED", "passed": False, "score": 0, "checks": []})
    eng.db.seed(__import__("forex.agents", fromlist=["x"]).FOUNDING_CLASS, __import__("forex.agents", fromlist=["x"]).default_params)
    eng.db.start_validation("atlas", 1, "initial")  # cut off by a restart: never finished
    eng.start()
    rows = eng.db.q("SELECT status, finished_at FROM validations WHERE agent_id = 'atlas' ORDER BY id")
    assert rows[0]["finished_at"] and rows[0]["status"].startswith("INTERRUPTED")


def test_crashed_validation_is_recorded_not_left_running(world, monkeypatch):
    eng, clock, client, _ = world

    def boom(*a, **k):
        raise KeyError("no USD conversion for CHF")

    monkeypatch.setattr("forex.engine.validate", boom)
    eng.start()
    with pytest.raises(KeyError):
        run_jobs(eng)
    assert not eng.db.q("SELECT * FROM validations WHERE finished_at IS NULL AND agent_id = 'atlas'")


def test_engine_never_writes_to_the_broker():
    import inspect

    import forex.engine as engine_mod

    import re

    src = inspect.getsource(engine_mod)
    used = set(re.findall(r"self\.client\.(\w+)", src))
    assert used <= {"account_id", "instruments", "pricing", "candles"}, used  # read-only calls only



def test_floor_one_agents_do_not_trade(world, monkeypatch):
    eng, clock, client, _ = world
    monkeypatch.setattr("forex.engine.validate", lambda *a, **k: {"status": "FAILED", "passed": False, "score": 0, "checks": []})
    eng.start()
    run_jobs(eng)
    for _ in range(96):
        clock["t"] += 900
        eng.tick()
    assert not any(eng.db.get(f"decision:{a}") for a in eng.agents)
    assert eng.db.q("SELECT count(*) AS n FROM positions")[0]["n"] == 0 and eng.db.q("SELECT count(*) AS n FROM equity")[0]["n"] == 0


def test_nightly_sprints_report_and_promotion(world, monkeypatch):
    eng, clock, client, _ = world
    monkeypatch.setattr("forex.engine.validate", lambda *a, **k: {"status": "FAILED", "passed": False, "score": 0, "checks": []})
    calls = []

    def fake_sprint(store, cfg, specs, *, tested, trials, day, evidence=None, progress=None):
        calls.append(cfg.agent_id)
        win = cfg.agent_id == "sable"
        params = {**cfg.params, "entry_n": 50} if win else dict(cfg.params)
        return {
            "outcome": "PROMOTE" if win else "NO_IMPROVEMENT", "passed": win, "params": params, "change": "entry_n 55 -> 50" if win else None,
            "verdict": "test", "tested": 3, "trials": 4, "current": {"score": 0.0}, "best": {"score": 1.0},
            "evaluated": [{"label": "x", "hash": f"h-{cfg.agent_id}", "score": 0.1}],
            "validation": {"status": "PASSED" if win else "FAILED", "passed": win, "score": 1.0, "checks": [], "metrics": {"unseen": {"avg_r": 0.2, "trades": 40}}},
            "day1": {"passed": win, "profitable_share": 0.7, "starts": []},
        }

    monkeypatch.setattr("forex.engine.sprint", fake_sprint)
    eng.start()
    run_jobs(eng)
    # 01:30 New York on the next day: sprints for every Floor 1 agent, then the report
    from forex.fxclock import ny

    t = ny(int(clock["t"]))
    clock["t"] += ((24 - t.hour) + 1) * 3600 + 1800
    eng.tick()
    run_jobs(eng)
    assert sorted(calls) == sorted(["atlas", "kestrel", "mira", "orion", "sable", "halcyon", "vega"])
    rep = eng.db.get("sprint_report_latest")
    assert rep["passed"] == ["Sable"] and len(rep["agents"]) == 7 and "Sable" in rep["floor2"]
    sable = eng.db.agent("sable")
    assert sable["stage"] == "PAPER" and sable["current_version"] == 2 and eng.agents["sable"].cfg.params["entry_n"] == 50
    assert eng.db.tested_hashes("sable") == {"h-sable"} and eng.db.agent("atlas")["trials"] > 1
    eng.tick()
    run_jobs(eng)
    assert len(calls) == 7  # once per night


def test_owner_floor_commands(world, monkeypatch):
    eng, clock, client, inbox = world
    monkeypatch.setattr("forex.engine.validate", lambda *a, **k: {"status": "FAILED", "passed": False, "score": 0, "checks": []})
    eng.start()
    import json as _json

    (inbox / "a.json").write_text(_json.dumps({"agent_id": "kestrel", "action": "to_demo"}))
    eng.tick()
    assert eng.db.agent("kestrel")["stage"] == "RESEARCH"  # refused: not on Floor 2
    for a in ("kestrel", "mira", "orion"):
        eng.db.set_stage(a, "PAPER", by="test", reason="test")
        eng.reload_stage(a)
    for i, a in enumerate(("kestrel", "mira", "orion")):
        (inbox / f"b{i}.json").write_text(_json.dumps({"agent_id": a, "action": "to_demo"}))
    eng.tick()
    stages = {a: eng.db.agent(a)["stage"] for a in ("kestrel", "mira", "orion")}
    assert sorted(stages.values()) == ["DEMO", "DEMO", "PAPER"]  # only two seats
    (inbox / "c.json").write_text(_json.dumps({"agent_id": "atlas", "action": "retire", "note": "no edge"}))
    (inbox / "d.json").write_text(_json.dumps({"agent_id": "mira", "action": "to_research"}))
    eng.tick()
    assert eng.db.agent("atlas")["stage"] == "RETIRED" and "atlas" not in eng.agents
    assert eng.db.agent("mira")["stage"] == "RESEARCH"
    assert eng.db.q("SELECT count(*) AS n FROM stage_transitions")[0]["n"] >= 5
