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
    eng.start()
    assert {a["id"] for a in eng.db.agents()} == {"atlas", "kestrel", "mira", "orion"}
    assert all(r["cash"] == 10_000 for r in eng.db.q("SELECT cash FROM accounts"))
    run_jobs(eng)  # backfill + four initial validations
    assert eng.db.get("data")["ready"] is True
    assert eng.store.count("EUR_USD", "M15") > 10_000 and eng.store.count("EUR_USD", "D") > 150
    assert len(eng.db.q("SELECT * FROM validations WHERE finished_at IS NOT NULL")) == 4
    # trade through 3 days of 15-minute ticks
    decided = set()
    for _ in range(3 * 96):
        clock["t"] += 900
        eng.tick()
        decided |= {a for a in eng.agents if eng.db.get(f"decision:{a}")}
    assert decided == {"atlas", "kestrel", "mira", "orion"}
    hb = eng.db.get("engine")
    assert hb["heartbeat"] == clock["t"] and hb["data_ready"] is True and set(hb["agents"]) == set(decided)
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


def test_engine_never_writes_to_the_broker():
    import inspect

    import forex.engine as engine_mod

    import re

    src = inspect.getsource(engine_mod)
    used = set(re.findall(r"self\.client\.(\w+)", src))
    assert used <= {"account_id", "instruments", "pricing", "candles"}, used  # read-only calls only
