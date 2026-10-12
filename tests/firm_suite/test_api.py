from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from firm.api.app import create_app
from firm.api.auth import set_password
from firm.core import Firm
from firm.broker.types import Bar
from firm.money import D

from tests.firm_suite.fakes import FakeRobinhood

PASSWORD = "correct horse battery"


class Host:
    def __init__(self, firm):
        self.firm = firm

    def health(self):
        return {"agent": "ONLINE", "books_open": self.firm.reconciler.bootstrapped(), "live_writes_enabled": False, "broker_connected": True}

    def jobs(self):
        return {}


@pytest.fixture
def firm(db, clock):
    rh = FakeRobinhood(clock=clock)
    f = Firm(db, reader=rh, writer=lambda: None, clock=clock, environ={})
    f.reconciler.bootstrap(rh)
    return f


@pytest.fixture
def client(firm):
    set_password(firm.db, "reydel", PASSWORD)
    return TestClient(create_app(Host(firm)))


def _login(client):
    res = client.post("/api/login", json={"username": "reydel", "password": PASSWORD})
    assert res.status_code == 200
    return {"X-CSRF-Token": res.json()["csrf"]}


def test_reads_require_login_and_healthz_is_public(client):
    assert client.get("/api/firm").status_code == 401
    assert client.get("/healthz").json()["agent"] == "ONLINE"


def test_no_default_password(firm):
    c = TestClient(create_app(Host(firm)))
    assert c.post("/api/login", json={"username": "reydel", "password": "reydel"}).status_code == 409
    set_password(firm.db, "reydel", PASSWORD)
    assert c.post("/api/login", json={"username": "reydel", "password": "reydel"}).status_code == 401
    with pytest.raises(ValueError):
        set_password(firm.db, "reydel", "short")


def test_writes_need_csrf(client):
    headers = _login(client)
    body = {"manager": "tim", "amount": "100"}
    assert client.post("/api/capital/allocate", json=body).status_code == 403
    assert client.post("/api/capital/allocate", json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/api/capital/allocate", json=body, headers=headers).status_code == 200


def test_allocate_by_percent_and_overview(client, firm):
    headers = _login(client)
    assert client.post("/api/capital/allocate", json={"manager": "tim", "percent": 90}, headers=headers).json()["amount"] == "909.00"
    over = client.get("/api/firm").json()
    alloc = {a["id"]: a for a in over["allocation"]}
    assert alloc["tim"]["nav"] == 909.0 and alloc["unallocated"]["nav"] == 101.0
    assert over["firm"]["nav"] == 1010.0
    too_much = client.post("/api/capital/allocate", json={"manager": "tim", "amount": "500"}, headers=headers)
    assert too_much.status_code == 409
    ev = firm.events.last_of("CAPITAL_ALLOCATED")
    assert ev.actor == "reydel"


def test_mode_live_needs_confirmation_and_unbuilt_managers_cannot_trade(client, firm):
    headers = _login(client)
    assert client.post("/api/managers/tim/mode", json={"mode": "LIVE"}, headers=headers).status_code == 422
    assert client.post("/api/managers/tim/mode", json={"mode": "SIM", "note": "start"}, headers=headers).status_code == 200
    assert firm.registry.get("tim").mode == "SIM"


def test_mandate_validation_and_versioning(client, firm):
    headers = _login(client)
    bad = client.post("/api/managers/tim/mandate", json={"params": {"targets": {"VTI": 0.5}}, "note": "x"}, headers=headers)
    assert bad.status_code == 422
    _, current = firm.registry.mandate("tim")
    new = {**current, "targets": {"VTI": 0.6, "VXUS": 0.3, "BND": 0.1}}
    ok = client.post("/api/managers/tim/mandate", json={"params": new, "note": "more bonds"}, headers=headers)
    assert ok.json()["version"] == 2
    detail = client.get("/api/managers/tim").json()
    assert detail["mandate"]["version"] == 2 and detail["mandate"]["history"][1]["version"] == 1


def test_kill_switch_release_requires_note(client, firm):
    headers = _login(client)
    client.post("/api/kill-switch", json={"action": "engage", "note": "drill"}, headers=headers)
    assert firm.controls.kill_switch().engaged
    assert client.post("/api/kill-switch", json={"action": "release", "note": ""}, headers=headers).status_code == 422
    assert client.post("/api/kill-switch", json={"action": "release", "note": "drill done"}, headers=headers).status_code == 200
    assert not firm.controls.kill_switch().engaged


def test_login_lockout(client):
    for _ in range(8):
        client.post("/api/login", json={"username": "reydel", "password": "nope-nope-nope"})
    assert client.post("/api/login", json={"username": "reydel", "password": PASSWORD}).status_code == 401


def test_session_cookie_is_secure_behind_https_proxy(client):
    res = client.post("/api/login", json={"username": "reydel", "password": PASSWORD}, headers={"X-Forwarded-Proto": "https"})
    cookies = res.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith("firm_session="))
    assert "Secure" in session and "HttpOnly" in session and "SameSite=strict" in session.replace("samesite", "SameSite")


def test_space_reports_real_state_without_side_effects(client, firm):
    headers = _login(client)
    client.post("/api/capital/allocate", json={"manager": "tim", "percent": 90}, headers=headers)
    client.post("/api/managers/tim/mode", json={"mode": "SIM", "note": "start"}, headers=headers)
    firm.run_manager("tim")
    before = firm.events.recent(limit=1)[0].id
    space = client.get("/api/space").json()
    agents = {a["id"]: a for a in space["agents"]}
    assert set(agents) == {"tim", "alex", "max"}
    assert agents["tim"]["implemented"] and agents["tim"]["mode"] == "SIM" and not agents["tim"]["running"]
    assert agents["tim"]["last_run"]["summary"].startswith("Tim:") and "detail" not in agents["tim"]["last_run"]
    assert any(e["type"] == "MANAGER_RUN" for e in agents["tim"]["recent"])
    assert agents["alex"]["implemented"] and agents["alex"]["mode"] == "INACTIVE"
    assert set(agents["tim"]["mood"]) == {"book", "marks", "twr", "excess", "drawdown", "day_change"} and agents["tim"]["mood"]["book"] == "SIM"
    assert agents["max"]["implemented"]
    assert space["cio"] == {"configured": False, "provider_ready": False, "working": None, "pending_proposals": [], "recent": []}
    assert space["market"]["next_open"] and space["benchmarks"] == []  # no stored bars yet: nothing shown
    firm.market.store_bars("SPY", [Bar("2026-10-09", D("660"), D("671"), D("659"), D("668"), 1), Bar("2026-10-12", D("668"), D("675"), D("667"), D("672"), 1)])
    assert [c["close"] for c in client.get("/api/space").json()["benchmarks"][0]["closes"]] == [668.0, 672.0]
    assert firm.events.recent(limit=1)[0].id == before  # reading the space writes nothing
    client.cookies.clear()
    assert client.get("/api/space").status_code == 401


def test_unbuilt_managers_cannot_trade(db, clock):
    from firm.managers.tim import Tim

    rh = FakeRobinhood(clock=clock)
    f = Firm(db, reader=rh, writer=lambda: None, clock=clock, environ={}, managers={"tim": Tim()})
    f.reconciler.bootstrap(rh)
    set_password(f.db, "reydel", PASSWORD)
    c = TestClient(create_app(Host(f)))
    headers = _login(c)
    assert c.post("/api/managers/max/mode", json={"mode": "SIM"}, headers=headers).status_code == 409
