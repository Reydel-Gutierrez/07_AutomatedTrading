"""HQ's read-only Academy views, owner commands, and the Forex engine supervisor."""

from __future__ import annotations

import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from firm.api import academy
from firm.api.app import create_app
from firm.api.auth import set_password
from firm.core import Firm
from firm.sidecar import Sidecar, child_env

from tests.firm_suite.fakes import FakeRobinhood
from tests.firm_suite.test_api import PASSWORD, Host, _login


@pytest.fixture
def academy_db(tmp_path, monkeypatch):
    """A real academy.db written by the Forex engine's own code, with one closed trade and a mark."""
    from forex.academy import AcademyDB
    from forex.agents import FOUNDING_CLASS, default_params
    from forex.sim import Trade

    root = tmp_path / "forex"
    root.mkdir()
    monkeypatch.setenv("FOREX_STATE_ROOT", str(root))
    db = AcademyDB(root / "academy.db")
    db.seed(FOUNDING_CLASS, default_params)
    book = db.load_book("kestrel")
    book.closed.append(Trade("t1", 1, "EUR_USD", 1, 5000, 1.1, 1.101, 1_760_000_000, 1_760_003_600, 50.0, 0.0, 1.0, "target", "test"))
    book.cash += 50
    db.save_book(book)
    db.mark("kestrel", 1_760_003_600, 10_050, 10_050)
    db.put("engine", {"heartbeat": int(time.time()), "market_open": True, "agents": {"kestrel": {"activity": "watching"}}})
    db.put_prices({}, {})
    yield db
    db.close()


def test_views_read_the_engine_records(academy_db):
    ov = academy.overview()
    assert ov["available"] and {a["id"] for a in ov["agents"]} == {"atlas", "kestrel", "mira", "orion"}
    k = next(a for a in ov["agents"] if a["id"] == "kestrel")
    assert k["equity"] == 10_050 and k["return_pct"] == pytest.approx(0.005) and k["paper"]["trades"] == 1
    assert k["readiness"]["eligible_for_review"] is False and k["readiness"]["checks"][-1]["passed"] is False  # owner authorization never auto-passes
    assert ov["engine"]["online"] is True
    detail = academy.agent("kestrel")
    assert detail["trades"][0]["id"] == "t1" and detail["versions"][0]["version"] == 1 and "by_instrument" in detail["diagnostics"]
    assert academy.agent("nobody") is None
    sb = academy.space_block()
    assert sb["available"] and len(sb["agents"]) == 4 and sb["engine"]["online"]


def test_views_without_academy(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREX_STATE_ROOT", str(tmp_path / "none"))
    assert academy.overview()["available"] is False and academy.space_block() == {"available": False}


def test_commands_go_to_the_inbox_only(academy_db):
    academy.send_command("mira", "pause", by="reydel", note="checking")
    files = list((academy.forex_dir() / "inbox").glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["action"] == "pause"
    with pytest.raises(ValueError):
        academy.send_command("mira", "go_live", by="reydel")  # there is no live promotion command
    with pytest.raises(LookupError):
        academy.send_command("nobody", "pause", by="reydel")


@pytest.fixture
def client(db, clock):
    rh = FakeRobinhood(clock=clock)
    f = Firm(db, reader=rh, writer=lambda: None, clock=clock, environ={})
    f.reconciler.bootstrap(rh)
    set_password(f.db, "reydel", PASSWORD)
    return TestClient(create_app(Host(f)))


def test_api_endpoints(client, academy_db):
    assert client.get("/api/academy").status_code == 401
    h = _login(client)
    ov = client.get("/api/academy").json()
    assert ov["available"] and len(ov["agents"]) == 4
    assert client.get("/api/academy/agents/orion").json()["name"] == "Orion"
    assert client.get("/api/academy/agents/nobody").status_code == 404
    assert client.post("/api/academy/agents/orion/pause", json={"note": "x"}).status_code == 403  # CSRF
    assert client.post("/api/academy/agents/orion/pause", json={"note": "x"}, headers=h).json()["queued"]
    assert client.post("/api/academy/agents/orion/promote", json={}, headers=h).status_code == 422
    space = client.get("/api/space").json()
    assert space["academy"]["available"] and len(space["academy"]["agents"]) == 4


def test_sidecar_restarts_and_passes_no_secrets(tmp_path):
    env = child_env({"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "k", "AGENTIC_LIVE_ORDER_PLACEMENT": "true", "TELEGRAM_BOT_TOKEN": "t"})
    assert env == {"PATH": "/bin", "HOME": "/h"}
    marker = tmp_path / "runs.txt"
    code = f"open(r'{marker}', 'a').write('x'); raise SystemExit(1)"
    sc = Sidecar("t", [sys.executable, "-c", code], cwd=str(tmp_path), environ={"PATH": ""}, min_backoff=0.1, max_backoff=0.2)
    sc.start()
    deadline = time.time() + 15
    while time.time() < deadline and (not marker.exists() or len(marker.read_text()) < 2):
        time.sleep(0.1)
    sc.stop()
    assert len(marker.read_text()) >= 2 and sc.status()["restarts"] >= 1 and sc.status()["last_exit"] == 1


def test_firm_package_does_not_import_forex():
    import pathlib

    root = pathlib.Path(academy.__file__).resolve().parents[1]
    offenders = [str(p) for p in root.rglob("*.py") if "import forex" in p.read_text(encoding="utf-8") or "from forex" in p.read_text(encoding="utf-8")]
    assert offenders == []
