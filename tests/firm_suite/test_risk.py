"""The owner's risk policy: department limits, every agent inside them, Graham may only propose."""

from __future__ import annotations

import json

import pytest

from fastapi.testclient import TestClient

from firm.api import academy
from firm.api.app import create_app
from firm.api.auth import set_password
from firm.cio.proposals import ProposalError, Proposals

from tests.firm_suite.test_academy_api import academy_db  # noqa: F401  (fixture)
from tests.firm_suite.test_api import PASSWORD, Host, _login, firm  # noqa: F401  (fixture)


@pytest.fixture
def client(firm):
    set_password(firm.db, "reydel", PASSWORD)
    c = TestClient(create_app(Host(firm)))
    c.firm = firm
    return c


def _inbox():
    return [json.loads(p.read_text()) for p in sorted((academy.forex_dir() / "inbox").glob("*.json"))]


def test_overview_has_every_department_and_agent(client, academy_db):
    h = _login(client)
    ov = client.get("/api/risk").json()
    assert [d["id"] for d in ov["departments"]] == ["core", "trading", "forex", "options"]
    assert ov["planned_total"] == 50_000 and sum(d["share"] for d in ov["departments"]) == pytest.approx(1)
    assert {m["id"] for m in ov["managers"]} == {"tim", "alex", "max"} and all(not m["problems"] for m in ov["managers"])
    k = next(a for a in ov["academy"] if a["id"] == "kestrel")
    assert k["department"] == "forex" and k["risk"]["risk_pct"] == 0.005 and k["editable"]
    assert h


def test_owner_sets_a_forex_agents_risk_inside_the_limit(client, academy_db):
    h = _login(client)
    r = client.post("/api/academy/agents/kestrel/risk", json={"changes": {"risk_pct": 0.01}, "note": "proven on paper"}, headers=h)
    assert r.status_code == 200 and r.json()["changes"] == {"risk_pct": 0.01}
    assert _inbox()[-1] == {"agent_id": "kestrel", "action": "set_risk", "by": "reydel", "note": "proven on paper", "risk": {"risk_pct": 0.01}}
    # beyond the Forex Division's 1.5% ceiling, an unknown field, or the generic command route: refused
    assert client.post("/api/academy/agents/kestrel/risk", json={"changes": {"risk_pct": 0.02}, "note": "x"}, headers=h).status_code == 422
    assert client.post("/api/academy/agents/kestrel/risk", json={"changes": {"style": "swing"}, "note": "x"}, headers=h).status_code == 422
    assert client.post("/api/academy/agents/kestrel/risk", json={"changes": {"drawdown_pause": 0.3}, "note": "x"}, headers=h).status_code == 422
    assert client.post("/api/academy/agents/kestrel/set_risk", json={"note": "x"}, headers=h).status_code == 422
    assert len(_inbox()) == 1
    # the engine's applied override is what HQ shows
    academy_db.put("owner_risk:kestrel", {"risk_pct": 0.01})
    assert academy.agent("kestrel")["envelope"]["risk_pct"] == 0.01 and academy.agent("kestrel")["risk_pct"] == 0.01


def test_raising_the_department_limit_allows_more_risk(client, academy_db):
    h = _login(client)
    r = client.put("/api/risk/policy", json={"departments": {"forex": {"max_risk_per_trade": 0.025}}, "note": "more room"}, headers=h)
    assert r.status_code == 200
    assert client.post("/api/academy/agents/kestrel/risk", json={"changes": {"risk_pct": 0.02}, "note": "x"}, headers=h).status_code == 200
    # a limit below what an agent already carries is refused, naming who to lower first
    academy_db.put("owner_risk:kestrel", {"risk_pct": 0.02})
    r = client.put("/api/risk/policy", json={"departments": {"forex": {"max_risk_per_trade": 0.01}}, "note": "tighten"}, headers=h)
    assert r.status_code == 422 and "Kestrel" in r.json()["detail"]
    assert client.put("/api/risk/policy", json={"departments": {"forex": {"expected_low": 0.2, "expected_high": 0.1}}}, headers=h).status_code == 422
    assert client.put("/api/risk/policy", json={"departments": {"crypto": {}}}, headers=h).status_code == 422


def test_manager_mandates_stay_inside_their_department(client):
    h = _login(client)
    ov = client.get("/api/risk").json()
    mx = next(m for m in ov["managers"] if m["id"] == "max")
    assert mx["risk"]["risk_per_trade"] == pytest.approx(mx["risk"]["position_pct"] * mx["risk"]["stop_pct"])
    detail = client.get("/api/managers/max").json()
    params = detail["mandate"]["params"]
    too_wide = {**params, "sleeve_drawdown_pause": 0.3}  # Equity Trading's limit is 20%
    r = client.post("/api/managers/max/mandate", json={"params": too_wide, "note": "x"}, headers=h)
    assert r.status_code == 422 and "Equity Trading" in r.json()["detail"]
    too_big = {**params, "position_pct": 0.5, "max_positions": 2, "stop_pct": 0.06}  # 3% per trade > 2%
    assert client.post("/api/managers/max/mandate", json={"params": too_big, "note": "x"}, headers=h).status_code == 422


def test_graham_proposes_and_only_the_owner_applies(client, academy_db):
    _login(client)
    firm = client.firm
    props = Proposals(firm)
    with pytest.raises(ProposalError):
        props.create("risk_change", "More risk", {"agent_id": "kestrel", "changes": {"risk_pct": 0.05}}, "behind target")
    with pytest.raises(ProposalError):
        props.create("risk_change", "Risk for Tim", {"agent_id": "tim", "changes": {"risk_pct": 0.01}}, "x")
    p = props.create("risk_change", "Kestrel to 0.75%", {"agent_id": "kestrel", "changes": {"risk_pct": 0.0075}}, "steady paper record")
    assert p["status"] == "PENDING" and p["payload"]["diff"] == {"risk_pct": {"from": 0.005, "to": 0.0075}} and _inbox() == []
    props.approve(p["id"], by="reydel")
    assert _inbox()[-1]["action"] == "set_risk" and _inbox()[-1]["risk"] == {"risk_pct": 0.0075} and _inbox()[-1]["by"] == "reydel"
