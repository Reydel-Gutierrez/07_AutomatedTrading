"""Graham sees the whole firm: the Academy (floors, agents, sprints) and the firm's money analytics."""

from __future__ import annotations

import time

import pytest

from firm.ai.providers import ScriptedProvider, Step
from firm.api import views
from firm.money import D

from tests.firm_suite.test_cio import call, firm, office  # noqa: F401  (fixture)


@pytest.fixture
def academy_db(tmp_path, monkeypatch):
    from forex.academy import AcademyDB
    from forex.agents import ALL_AGENTS, default_params
    from forex.engine import Engine

    root = tmp_path / "forex"
    root.mkdir()
    monkeypatch.setenv("FOREX_STATE_ROOT", str(root))
    db = AcademyDB(root / "academy.db")
    db.seed(ALL_AGENTS, default_params)
    Engine(None, db, None, root, clock=time.time).publish_catalog()
    sid = db.start_sprint("kestrel", "2026-10-14", 2)
    db.finish_sprint(sid, outcome="PROMOTE", to_version=3, passed=True, summary={"agent": "Kestrel", "verdict": "Passed every gate.", "change": "buffer_pips 2.0 -> 4.0"})
    db.set_stage("kestrel", "PAPER", by="research-sprint", reason="passed every gate")
    db.put("sprint_report:2026-10-14", {"day": "2026-10-14", "passed": ["Kestrel"], "improved": [], "floor2": ["Kestrel"], "agents": [{"name": "Kestrel", "outcome": "PROMOTE"}]})
    db.put("sprint_report_latest", {"day": "2026-10-14", "passed": ["Kestrel"], "improved": [], "floor2": ["Kestrel"], "agents": []})
    db.put("engine", {"heartbeat": int(time.time())})
    yield db
    db.close()


def test_graham_reads_the_academy_and_cites_it(firm, academy_db):
    ev = academy_db.q("SELECT id FROM events WHERE agent_id = 'kestrel' ORDER BY id DESC LIMIT 1")[0]["id"]
    cio, provider = office(firm, [
        Step(tool_calls=[call("get_academy"), call("get_academy_agent", agent_id="kestrel"), call("get_sprint_report")]),
        Step(tool_calls=[call("respond", message="Kestrel passed its gates and is paper trading.", citations=[{"type": "academy", "id": ev}, {"type": "academy", "id": 999999}])]),
    ])
    conv = cio.create_conversation()
    reply = cio.ask(conv["id"], "How is the Academy doing?")
    # the Academy is in his instructions, and the tools returned real records
    instructions = provider.requests[0]["instructions"]
    assert "The 537 Academy right now" in instructions and "Kestrel" in instructions and "Floor 2" in instructions
    joined = " ".join(r[1] for r in provider.requests[1]["tool_results"])
    assert "London-open Asian range breakout" in joined  # the strategy's own description (the algorithm)
    assert "buffer_pips 2.0 -> 4.0" in joined and "PAPER" in joined
    # Academy citations are verified against academy.db; fabricated ones are removed
    assert [c["id"] for c in reply["citations"]] == [ev] and reply["citations"][0]["type"] == "academy"


def test_graham_without_academy_is_told_plainly(firm, tmp_path, monkeypatch):
    monkeypatch.setenv("FOREX_STATE_ROOT", str(tmp_path / "none"))
    from firm.cio.agent import academy_snapshot
    from firm.cio.tools import CIOTools

    assert academy_snapshot() is None
    tools = CIOTools(firm, None, None, health=lambda: {})
    assert "not created the Academy" in tools.call("get_academy", {}) or '"available": false' in tools.call("get_academy", {})


def test_money_analytics_contributions_and_daily_returns(firm, clock):
    firm.ledger.external_in(D("500"), by="reconciler", memo="deposit")
    m = views.money_analytics(firm)
    assert m["deposits"] >= 1510 - 1e-9 and m["withdrawals"] == 0  # opening balance + deposit
    assert m["contributed"] == m["deposits"] and m["flows"][-1]["kind"] == "EXTERNAL_IN"
    assert m["days_tracked"] == len(firm.performance.marks("LIVE", "_firm"))
    space = views.space(firm, {"market": {}}, {})
    assert space["treasury"]["contributed"] == m["contributed"]
