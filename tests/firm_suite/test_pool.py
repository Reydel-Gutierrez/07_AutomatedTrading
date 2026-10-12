from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from firm.ai.providers import ScriptedProvider, Step, ToolCall
from firm.api.app import create_app
from firm.api.auth import set_password
from firm.capital_pool import POOL, CapitalPool, PoolError
from firm.core import Firm
from firm.money import D
from firm.runtime import build_cio

from tests.firm_suite.fakes import FakeRobinhood

CIO_SRC = Path(__file__).resolve().parents[2] / "src" / "firm" / "cio"
WHY = "Tim is the only live strategy and is tracking the S&P 500."


@pytest.fixture
def firm(db, clock):
    rh = FakeRobinhood(clock=clock)
    f = Firm(db, reader=rh, writer=lambda: None, clock=clock, environ={})
    f.reconciler.bootstrap(rh)
    return f


def test_owner_funds_the_pool_and_it_counts_in_the_firm(firm):
    set_password(firm.db, "reydel", "correct horse battery")

    class Host:
        def __init__(self):
            self.firm = firm

        def health(self):
            return {"books_open": True, "live_writes_enabled": False, "broker_connected": True}

        def jobs(self):
            return {}

    c = TestClient(create_app(Host()))
    h = {"X-CSRF-Token": c.post("/api/login", json={"username": "reydel", "password": "correct horse battery"}).json()["csrf"]}
    assert c.post("/api/capital/allocate", json={"manager": "cio", "amount": "200"}, headers=h).status_code == 200
    over = c.get("/api/firm").json()
    alloc = {a["id"]: a["nav"] for a in over["allocation"]}
    assert alloc["cio"] == 200.0 and alloc["unallocated"] == 810.0 and over["firm"]["nav"] == 1010.0
    assert firm.settings.get("cio_pool_state")["review_requested"] is True
    assert c.put("/api/settings/deposit_split", json={"split": {"tim": 0.5, "cio": 0.5}}, headers=h).status_code == 200
    assert c.put("/api/settings/cio_pool", json={"caps": {"tim": 0.8, "alex": 0.2, "max": 0.05}}, headers=h).status_code == 200
    assert CapitalPool(firm).policy()["caps"]["tim"] == 0.8
    assert c.get("/api/capital").json()["pool"]["cash"] == "200.00"


def test_grant_rules(firm):
    pool = CapitalPool(firm)
    firm.allocate(POOL, D("300"), by="reydel")
    firm.allocate("tim", D("650"), by="reydel")
    with pytest.raises(PoolError, match="real money"):
        pool.grant("tim", D("50"), reason=WHY)  # Tim is INACTIVE
    firm.registry.set_mode("tim", "LIVE", by="reydel")
    with pytest.raises(PoolError, match="cap"):
        pool.grant("tim", D("280"), reason=WHY)  # 930 of 1010 = 92% > the 90% cap
    out = pool.grant("tim", D("250"), reason=WHY)  # 900 of 1010 = 89%
    assert out["ok"] and firm.ledger.cash("LIVE", "tim") == D("900") and firm.ledger.cash("SIM", "tim") == D("900")
    assert firm.events.last_of("CIO_ALLOCATION").summary.startswith("Graham moved $250.00")
    # A pool grant is new capital for the manager, not investment gain: it counts as a flow for returns.
    assert firm.ledger.net_capital("LIVE", "tim") == D("900") and firm.ledger.net_capital("LIVE", POOL) == D("50")
    with pytest.raises(PoolError, match="reason"):
        pool.grant("tim", D("5"), reason="ok")
    with pytest.raises(PoolError, match="at least"):
        pool.grant("tim", D("1"), reason=WHY)


def test_recall_only_own_grants_and_daily_limit(firm):
    pool = CapitalPool(firm)
    firm.allocate(POOL, D("100"), by="reydel")
    firm.allocate("tim", D("500"), by="reydel")
    firm.registry.set_mode("tim", "LIVE", by="reydel")
    pool.grant("tim", D("40"), reason=WHY)
    with pytest.raises(PoolError, match="only recall what he granted"):
        pool.recall("tim", D("100"), reason="Rebalancing toward a steadier mix.")  # the owner's $500 is not his
    pool.recall("tim", D("40"), reason="Rebalancing toward a steadier mix.")
    assert pool.grants() == {} and firm.ledger.cash("LIVE", POOL) == D("100")
    pool.grant("tim", D("10"), reason=WHY)
    pool.grant("tim", D("10"), reason=WHY)
    with pytest.raises(PoolError, match="daily limit"):
        pool.grant("tim", D("10"), reason=WHY)


def call(name, **args):
    return ToolCall(id=f"c-{name}", name=name, arguments=args)


def test_graham_moves_pool_money_through_his_tools_and_refusals_come_back_as_errors(firm):
    firm.allocate(POOL, D("100"), by="reydel")
    firm.allocate("tim", D("500"), by="reydel")
    firm.registry.set_mode("tim", "LIVE", by="reydel")
    script = [
        Step(tool_calls=[call("get_capital_pool")]),
        Step(tool_calls=[call("allocate_from_pool", manager_id="alex", amount=20, reason=WHY)]),  # Alex is not LIVE: refused
        Step(tool_calls=[call("allocate_from_pool", manager_id="tim", amount=60, reason=WHY)]),
        lambda req: Step(tool_calls=[call("respond", message="Moved $60 to Tim.", citations=[{"type": "event", "id": firm.events.last_of("CIO_ALLOCATION").id}])]),
    ]
    provider = ScriptedProvider(script)
    cio = build_cio(firm, lambda: {}, {}, provider=provider)
    reply = cio.review_pool()
    assert reply["role"] == "assistant" and reply["flags"] == [] and reply["citations"][0]["type"] == "event"
    assert "real money" in provider.requests[2]["tool_results"][0][1]
    assert firm.ledger.cash("LIVE", "tim") == D("560") and CapitalPool(firm).grants() == {"tim": Decimal("60")}
    assert cio.conversations()[0]["title"].startswith("Capital pool review")


def test_review_skips_when_there_is_nothing_to_decide(firm):
    cio = build_cio(firm, lambda: {}, {}, provider=ScriptedProvider([]))
    assert cio.review_pool() is None


def test_cio_package_moves_capital_only_through_the_pool():
    for path in CIO_SRC.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "transfer" not in calls, f"{path.name} moves ledger cash directly"
        if path.name != "tools.py":
            assert not calls & {"grant", "recall"}, f"{path.name} moves pool money outside the CIO's tools"
