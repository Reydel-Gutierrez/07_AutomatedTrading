from __future__ import annotations

from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest

from firm.ai.anthropic_provider import FALLBACK_BETA, AnthropicMessages
from firm.ai.budget import cost
from firm.ai.providers import ProviderError, ToolSpec
from firm.core import Firm
from firm.money import D
from firm.runtime import build_cio

from tests.firm_suite.fakes import FakeRobinhood

SECRET = "sk-ant-api03-SECRETSECRETSECRET"


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def usage(i=100, o=20, cw=0, cr=0):
    return NS(input_tokens=i, output_tokens=o, cache_creation_input_tokens=cw, cache_read_input_tokens=cr)


def msg(content, stop="end_turn", **u):
    return NS(content=content, stop_reason=stop, usage=usage(**u), model="claude-opus-5-5")


TOOLS = [ToolSpec("get_firm_overview", "overview", {"type": "object", "properties": {}})]


def test_request_shape_tool_loop_and_thinking_preserved():
    thinking = NS(type="thinking", thinking="", signature="sig")
    call = NS(type="tool_use", id="toolu_1", name="get_firm_overview", input={})
    client = FakeClient([msg([thinking, call], stop="tool_use", cw=900), msg([NS(type="text", text="Done.")], cr=900)])
    p = AnthropicMessages(api_key="", client=client, effort="high")
    first = p.step(model="claude-opus-5-5", instructions="SYS", messages=[{"role": "user", "content": "q"}], tool_results=None, tools=TOOLS, state=None, max_output_tokens=16000)
    req = client.calls[0]
    assert req["fallbacks"] == "default" and req["betas"] == [FALLBACK_BETA]
    assert req["cache_control"] == {"type": "ephemeral"} and req["output_config"] == {"effort": "high"}
    assert req["system"] == "SYS" and req["tools"][0]["input_schema"] == {"type": "object", "properties": {}}
    assert "thinking" not in req and "tool_choice" not in req  # always-on thinking; no forced tool use
    assert first.tool_calls[0].id == "toolu_1" and first.cache_write_tokens == 900
    second = p.step(model="claude-opus-5-5", instructions="SYS", messages=None, tool_results=[(first.tool_calls[0], '{"nav": 1010}')], tools=TOOLS, state=first.state, max_output_tokens=16000)
    sent = client.calls[1]["messages"]
    assert sent[1] == {"role": "assistant", "content": [thinking, call]}  # returned content appended unchanged
    assert sent[2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"nav": 1010}'}]}
    assert second.text == "Done." and second.cache_read_tokens == 900


def test_refusal_is_returned_as_a_plain_decline():
    client = FakeClient([msg([], stop="refusal")])
    out = AnthropicMessages(api_key="", client=client).step(model="claude-opus-5-5", instructions="s", messages=[{"role": "user", "content": "q"}], tool_results=None, tools=[], state=None, max_output_tokens=100)
    assert out.tool_calls == [] and "can't help" in out.text


def _err(cls, status, message):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages", headers={"x-api-key": SECRET})
    return cls(message, response=httpx2.Response(status, request=req), body={"error": {"message": message}})


@pytest.mark.parametrize(
    ("exc", "problem"),
    [
        (_err(anthropic.BadRequestError, 400, "Your credit balance is too low to access the Anthropic API."), "billing"),
        (_err(anthropic.AuthenticationError, 401, "invalid x-api-key"), "auth"),
        (_err(anthropic.RateLimitError, 429, "rate limited"), None),
    ],
)
def test_errors_map_to_account_problems_without_leaking_the_key(exc, problem):
    p = AnthropicMessages(api_key="", client=FakeClient([exc]))
    with pytest.raises(ProviderError) as info:
        p.step(model="claude-opus-5-5", instructions="s", messages=[{"role": "user", "content": "q"}], tool_results=None, tools=[], state=None, max_output_tokens=100)
    assert info.value.account_problem == problem
    assert SECRET not in str(info.value) and "x-api-key" not in str(info.value).lower().replace("invalid x-api-key", "")


def test_cache_aware_pricing():
    assert cost("claude-opus-5-5", 1_000_000, 0) == D("4.00")
    assert cost("claude-opus-5-5", 0, 0, cache_read_tokens=1_000_000) == D("0.20")
    assert cost("claude-opus-5-5", 0, 0, cache_write_tokens=1_000_000) == D("5.00")
    assert cost("some-unknown-model", 1_000_000, 0) == D("15.00")  # unknown models priced high


def test_claude_is_the_default_and_a_missing_key_never_calls_out(db, clock):
    rh = FakeRobinhood(clock=clock)
    firm = Firm(db, reader=rh, writer=lambda: None, clock=clock, environ={})
    cio = build_cio(firm, lambda: {}, {})
    assert cio.provider.name == "anthropic" and cio.model == "claude-opus-5-5" and cio.summary_model == "claude-haiku-5-5"
    reply = cio.ask(cio.create_conversation()["id"], "hello")
    assert reply["role"] == "notice" and "provider_not_configured" in reply["flags"]
    firm.settings.put("ai_models", {"cio": {"provider": "openai"}}, by="owner")
    assert build_cio(firm, lambda: {}, {}).provider.name == "openai"
