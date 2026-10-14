from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from forex import demo
from forex.oanda import Credentials, OandaConfigError
from forex.strategy import Open

from tests.forex_suite.test_engine import NOW0, FakeClient, run_jobs, world  # noqa: F401  (fixture)


class FakePractice:
    """An in-memory OANDA practice account: orders fill at the ask/bid, stops can be hit."""

    def __init__(self, prices):
        self.prices = prices  # () -> {instrument: Quote}
        self.balance = 10_000.0
        self.trades: dict[str, dict] = {}
        self.next = 100
        self.requests: list[tuple[str, str, dict]] = []

    def fetch(self, url, headers, timeout):
        path = url.split(".com", 1)[1].split("?")[0]
        assert url.startswith(demo.PRACTICE_REST)
        acct = path.split("/")[3] if path.startswith("/v3/accounts/") else None
        if path.endswith("/summary"):
            body = {"account": {"balance": f"{self.balance:.2f}", "NAV": f"{self.balance:.2f}", "id": acct}}
        elif path.endswith("/openTrades"):
            body = {"trades": [t for t in self.trades.values() if t["state"] == "OPEN"]}
        elif "/trades/" in path:
            body = {"trade": self.trades[path.rsplit("/", 1)[1]]}
        elif path == "/v3/accounts":
            body = {"accounts": [{"id": "101-001-1-001"}, {"id": "101-001-1-002"}]}
        else:
            return 404, {}, b"{}"
        return 200, {}, json.dumps(body).encode()

    def send(self, method, url, headers, body, timeout):
        assert url.startswith(demo.PRACTICE_REST + "/")
        path = url.split(".com", 1)[1]
        data = json.loads(body)
        self.requests.append((method, path, data))
        if method == "POST" and path.endswith("/orders"):
            o = data["order"]
            units = int(o["units"])
            q = self.prices()[o["instrument"]]
            bid, ask = q.bid, q.ask
            px = ask if units > 0 else bid
            tid = str(self.next)
            self.next += 1
            self.trades[tid] = {"id": tid, "instrument": o["instrument"], "price": str(px), "initialUnits": str(units), "currentUnits": str(units), "state": "OPEN",
                                "openTime": "2026-10-13T14:00:00.000000000Z", "stopLossOrder": {"price": o["stopLossOnFill"]["price"]},
                                "takeProfitOrder": {"price": o["takeProfitOnFill"]["price"]} if "takeProfitOnFill" in o else None, "clientExtensions": o["clientExtensions"]}
            return 201, json.dumps({"orderFillTransaction": {"id": tid, "price": str(px)}}).encode()
        if method == "PUT" and path.endswith("/close"):
            self.stop_out(path.split("/")[-2], reason=None)
            return 200, b"{}"
        if method == "PUT" and path.endswith("/orders"):
            self.trades[path.split("/")[-2]]["stopLossOrder"] = {"price": data["stopLoss"]["price"]}
            return 200, b"{}"
        return 404, b"{}"

    def stop_out(self, tid, reason="stop"):
        t = self.trades[tid]
        units = int(t["initialUnits"])
        exit_px = float(t["stopLossOrder"]["price"])
        pnl = (exit_px - float(t["price"])) * units
        t.update(state="CLOSED", currentUnits="0", averageClosePrice=str(exit_px), realizedPL=f"{pnl:.2f}", financing="0", closeTime="2026-10-13T15:00:00.000000000Z")
        self.balance += pnl


def trader(fake):
    return demo.PracticeTrader(Credentials("practice", "tok", None), fetch=fake.fetch, send=fake.send, sleep=lambda s: None, per_second=1e6)


def test_demo_desk_cannot_reach_the_live_host(tmp_path):
    with pytest.raises(demo.LiveHostRefused):
        demo.PracticeTrader(Credentials("live", "tok", None))
    with pytest.raises(demo.LiveHostRefused):
        demo._urllib_send("POST", "https://api-fxtrade.oanda.com/v3/accounts/x/orders", {}, b"{}", 5)
    f = tmp_path / "p.env"
    f.write_text("OANDA_ENV=live\nOANDA_API_TOKEN=t\nDEMO_ACCOUNT_1=1\n", encoding="utf-8")
    os.chmod(f, 0o600)
    with pytest.raises(OandaConfigError):
        demo.load_practice({"OANDA_PRACTICE_ENV_FILE": str(f)})
    f.write_text("OANDA_ENV=practice\nOANDA_API_TOKEN=t\nDEMO_ACCOUNT_1=101-1\nDEMO_ACCOUNT_2=101-2\n", encoding="utf-8")
    creds, seats = demo.load_practice({"OANDA_PRACTICE_ENV_FILE": str(f)})
    assert creds.env == "practice" and seats == ["101-1", "101-2"]
    assert demo.load_practice({"OANDA_PRACTICE_ENV_FILE": str(tmp_path / "missing")}) is None


def test_demo_agent_trades_the_practice_account(world, monkeypatch):
    eng, clock, client, _ = world
    monkeypatch.setattr("forex.engine.validate", lambda *a, **k: {"status": "FAILED", "passed": False, "score": 0, "checks": []})
    monkeypatch.setattr("forex.engine.SPRINT_HOUR_NY", 99)
    eng.start()
    run_jobs(eng)
    fake = FakePractice(lambda: eng.quotes)
    eng.load_demo((trader(fake), ["101-001-1-001", "101-001-1-002"]))
    for a in ("kestrel", "mira"):
        eng.db.set_stage(a, "PAPER", by="test", reason="test")
        eng.reload_stage(a)
        eng.command({"agent_id": a, "action": "to_demo", "by": "owner"})
    assert eng.db.agent("kestrel")["stage"] == "DEMO"
    rt = eng.agents["kestrel"]
    calls = {"n": 0}

    def strategy_once(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            q = eng.quotes["EUR_USD"]
            return [Open("EUR_USD", 1, stop=q.bid - 0.0040, target=q.ask + 0.0080, reason="test entry")]
        return []

    monkeypatch.setattr(rt.strategy, "on_bar", strategy_once)
    for _ in range(8):
        clock["t"] += 900
        eng.tick()
    orders = [r for r in fake.requests if r[0] == "POST"]
    assert len(orders) == 1
    o = orders[0][2]["order"]
    assert o["instrument"] == "EUR_USD" and o["positionFill"] == "OPEN_ONLY" and "stopLossOnFill" in o and o["clientExtensions"]["id"].startswith("firm:demo:kestrel")
    # each demo agent has its own practice sub-account
    seats = eng.db.get("demo_seats")
    assert seats["kestrel"] != seats["mira"] and set(seats.values()) == {"101-001-1-001", "101-001-1-002"}
    # OANDA hits the stop: the next pass records the closed trade from OANDA's own record
    tid = next(iter(fake.trades))
    fake.stop_out(tid)
    clock["t"] += 60
    eng.tick()
    trades = eng.db.trades("kestrel")
    assert len(trades) == 1 and trades[0]["exit_reason"] == "closed at OANDA" and trades[0]["pnl_usd"] < 0
    assert eng.db.get("engine")["demo"]["configured"] is True


def test_without_practice_credentials_demo_agents_wait(world, monkeypatch):
    eng, clock, client, _ = world
    monkeypatch.setattr("forex.engine.validate", lambda *a, **k: {"status": "FAILED", "passed": False, "score": 0, "checks": []})
    monkeypatch.setenv("OANDA_PRACTICE_ENV_FILE", "/nonexistent/practice.env")
    eng.start()
    eng.db.set_stage("orion", "DEMO", by="test", reason="test")
    eng.reload_stage("orion")
    clock["t"] += 900
    eng.tick()
    assert eng.agents["orion"].activity == "waiting for the OANDA practice account"
    assert eng.db.get("engine")["demo"]["configured"] is False
