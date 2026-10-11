from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from firm.ai.providers import OpenAIResponses, ProviderError, ScriptedProvider, Step, ToolCall
from firm.api.app import create_app
from firm.api.auth import set_password
from firm.cio.proposals import ProposalError
from firm.core import Firm
from firm.money import D
from firm.runtime import build_cio

from tests.firm_suite.fakes import FakeRobinhood

SRC = Path(__file__).resolve().parents[2] / "src" / "firm" / "cio"


def call(name, **args):
    return ToolCall(id=f"c-{name}", name=name, arguments=args)


@pytest.fixture
def firm(db, clock):
    rh = FakeRobinhood(clock=clock)
    f = Firm(db, reader=rh, writer=lambda: None, clock=clock, environ={})
    f.reconciler.bootstrap(rh)
    f.allocate("tim", D("909"), by="reydel")
    f.registry.set_mode("tim", "SIM", by="reydel")
    f.run_manager("tim")
    return f


def office(firm, script):
    provider = ScriptedProvider(script)
    return build_cio(firm, lambda: {"live_writes_enabled": False, "broker_connected": True, "books_open": True}, {}, provider=provider), provider


def test_grounded_answer_with_verified_citations(firm):
    run = firm.events.last_of("MANAGER_RUN", manager_id="tim")
    cio, provider = office(firm, [
        Step(tool_calls=[call("get_manager", manager_id="tim")]),
        Step(tool_calls=[call("respond", message="Tim invested $899.89 in simulation today.", citations=[{"type": "event", "id": run.id}])]),
    ])
    conv = cio.create_conversation()
    reply = cio.ask(conv["id"], "What is Tim doing?")
    assert reply["role"] == "assistant" and reply["flags"] == []
    assert reply["citations"][0]["id"] == run.id and "Tim" in reply["citations"][0]["label"]
    assert [t["tool"] for t in reply["tools"]] == ["get_manager"]
    # The model saw real data: the firm snapshot is in its instructions and the tool returned Tim's book.
    assert '"tim"' in provider.requests[0]["instructions"]
    assert "909" in provider.requests[1]["tool_results"][0][1]
    saved = cio.conversation(conv["id"])
    assert [m["role"] for m in saved["messages"]] == ["user", "assistant"] and saved["title"] == "What is Tim doing?"


def test_fabricated_citations_are_removed_and_uncited_activity_is_flagged(firm):
    cio, _ = office(firm, [Step(tool_calls=[call("respond", message="Tim sold all his VTI yesterday.", citations=[{"type": "order", "id": 9999}])])])
    reply = cio.ask(cio.create_conversation()["id"], "Did Tim sell anything?")
    assert reply["citations"] == []
    assert any("do not exist" in f for f in reply["flags"])
    assert any("without citing" in f for f in reply["flags"])


def test_proposals_change_nothing_until_the_owner_approves(firm):
    before = firm.ledger.balances("LIVE")
    cio, _ = office(firm, [
        Step(tool_calls=[call("create_proposal", kind="reallocate", title="Move $50 of idle cash to Tim", rationale="Unallocated cash earns nothing.", payload={"moves": [{"from": "firm", "to": "tim", "amount": "50"}]})]),
        Step(tool_calls=[call("respond", message="I've proposed moving $50 to Tim (#1).", citations=[{"type": "proposal", "id": 1}])]),
    ])
    reply = cio.ask(cio.create_conversation()["id"], "Should we put idle cash to work?")
    assert reply["proposals"] == [1]
    assert firm.ledger.balances("LIVE") == before
    p = cio.proposals.get(1)
    assert p["status"] == "PENDING" and p["created_by"] == "cio"
    applied = cio.proposals.approve(1, by="reydel", note="ok")
    assert applied["status"] == "APPLIED"
    assert firm.ledger.balances("LIVE")["tim"] == D("959")
    assert firm.events.last_of("PROPOSAL_APPLIED").actor == "reydel"
    with pytest.raises(ProposalError):
        cio.proposals.approve(1, by="reydel")


def test_invalid_and_forbidden_proposals_are_refused_at_filing(firm):
    cio, _ = office(firm, [])
    with pytest.raises(ProposalError, match="free cash|unallocated"):
        cio.proposals.create("reallocate", "too much", {"moves": [{"from": "firm", "to": "tim", "amount": "5000"}]}, "r")
    with pytest.raises(ProposalError, match="owner's decision"):
        cio.proposals.create("mode_change", "go live", {"manager_id": "tim", "mode": "LIVE"}, "r")
    with pytest.raises(ProposalError, match="sum to 100%"):
        cio.proposals.create("mandate_change", "bad weights", {"manager_id": "tim", "changes": {"targets": {"VTI": 0.9}}}, "r")


def test_stale_mandate_proposal_fails_instead_of_overwriting(firm):
    cio, _ = office(firm, [])
    p = cio.proposals.create("mandate_change", "More bonds", {"manager_id": "tim", "changes": {"targets": {"VTI": 0.65, "VXUS": 0.25, "BND": 0.10}}}, "Lower volatility.")
    _, m = firm.registry.mandate("tim")
    firm.registry.update_mandate("tim", {**m, "min_trade_usd": 10}, by="reydel", note="owner edit")
    with pytest.raises(ProposalError, match="mandate changed"):
        cio.proposals.approve(p["id"], by="reydel")
    assert cio.proposals.get(p["id"])["status"] == "FAILED"
    assert firm.registry.mandate("tim")[1]["targets"]["BND"] == 0.05


def test_memory_persists_across_conversations(firm):
    cio, provider = office(firm, [
        Step(tool_calls=[call("remember", kind="objective", text="Retire early; prefers steady compounding over speculation.")]),
        Step(tool_calls=[call("respond", message="Noted.", citations=[{"type": "memory", "id": 1}])]),
        Step(tool_calls=[call("respond", message="Your objective is steady compounding.", citations=[{"type": "memory", "id": 1}])]),
    ])
    cio.ask(cio.create_conversation()["id"], "My goal is early retirement; I prefer steady compounding.")
    cio.ask(cio.create_conversation()["id"], "What are my goals?")
    assert "steady compounding" in provider.requests[-1]["instructions"]
    assert firm.events.last_of("CIO_MEMORY") is not None


def test_budget_exhaustion_stops_calls_before_spending(firm):
    firm.settings.put("ai_budget", {"monthly_usd": 0.0, "per_agent": {"cio": 1.0}}, by="reydel")
    cio, provider = office(firm, [Step(text="should never be requested")])
    cio.model = "gpt-5.6-terra"
    reply = cio.ask(cio.create_conversation()["id"], "hello")
    assert reply["role"] == "notice" and "budget_exhausted" in reply["flags"]
    assert provider.requests == []


def test_provider_account_problem_is_reported(firm):
    class Broke(ScriptedProvider):
        def step(self, **kw):
            raise ProviderError("OpenAI HTTP 429: insufficient_quota", account_problem="billing")

    cio = build_cio(firm, lambda: {}, {}, provider=Broke([]))
    reply = cio.ask(cio.create_conversation()["id"], "hi")
    assert reply["role"] == "notice" and "provider_error" in reply["flags"]
    assert firm.events.last_of("AI_PROVIDER_UNAVAILABLE") is not None


def test_cio_code_cannot_change_the_firm_except_through_owner_approved_apply():
    forbidden = {"allocate", "deallocate", "update_mandate", "set_mode", "engage", "release", "record_fill", "submit", "external_in", "external_out", "resume_brake"}
    for path in SRC.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and path.name == "proposals.py" and node.name == "apply":
                continue
            calls = {n.func.attr for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)} if isinstance(node, ast.FunctionDef) else set()
            assert not calls & forbidden, f"{path.name}:{getattr(node, 'name', '?')} calls {calls & forbidden}"
    # apply() is reached only from Proposals.approve, which the owner-only API calls.
    text = (SRC / "tools.py").read_text(encoding="utf-8") + (SRC / "agent.py").read_text(encoding="utf-8")
    assert ".approve(" not in text and "apply(" not in text


def test_openai_adapter_chains_tool_results():
    seen = []

    def post(body):
        seen.append(body)
        if len(seen) == 1:
            return {"id": "resp_1", "status": "completed", "usage": {"input_tokens": 100, "output_tokens": 20}, "output": [{"type": "function_call", "call_id": "call_1", "name": "get_firm_overview", "arguments": "{}"}]}
        return {"id": "resp_2", "status": "completed", "usage": {"input_tokens": 50, "output_tokens": 10}, "output": [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}]}

    p = OpenAIResponses(api_key="sk-test", post=post)
    first = p.step(model="gpt-5.6-terra", instructions="i", messages=[{"role": "user", "content": "q"}], tool_results=None, tools=[], state=None, max_output_tokens=100)
    assert first.tool_calls[0].name == "get_firm_overview" and first.state == "resp_1"
    second = p.step(model="gpt-5.6-terra", instructions="i", messages=None, tool_results=[(first.tool_calls[0], '{"ok":1}')], tools=[], state=first.state, max_output_tokens=100)
    assert second.text == "done"
    assert seen[1]["previous_response_id"] == "resp_1"
    assert seen[1]["input"] == [{"type": "function_call_output", "call_id": "call_1", "output": '{"ok":1}'}]


def test_api_approval_requires_owner_session_and_confirmation(firm):
    set_password(firm.db, "reydel", "correct horse battery")
    cio, _ = office(firm, [])

    class Host:
        pass

    host = Host()
    host.firm, host.cio = firm, cio
    host.health = lambda: {"agent": "ONLINE"}
    host.jobs = lambda: {}
    client = TestClient(create_app(host))
    p = cio.proposals.create("deposit_split", "All deposits to Tim", {"split": {"tim": 1.0}}, "Only Tim is built.")
    assert client.get("/api/cio/proposals").status_code == 401
    csrf = client.post("/api/login", json={"username": "reydel", "password": "correct horse battery"}).json()["csrf"]
    h = {"X-CSRF-Token": csrf}
    assert client.post(f"/api/cio/proposals/{p['id']}/approve", json={}, headers=h).status_code == 422
    assert client.post(f"/api/cio/proposals/{p['id']}/approve", json={"confirm": True}, headers=h).json()["status"] == "APPLIED"
    assert firm.settings.get("deposit_split") == {"tim": 1.0}
