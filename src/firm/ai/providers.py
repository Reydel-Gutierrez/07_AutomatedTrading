"""Tool-calling chat providers.

A provider runs one model step: given instructions, conversation input, and
tool definitions, it returns either tool calls or a final text. The agent
loop (firm.cio.agent) executes the tools and continues. Providers never see
the broker or the ledger; they only see what tools return.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol


class ProviderError(RuntimeError):
    """The provider failed. ``account_problem`` is set for billing/auth failures that retrying won't fix."""

    def __init__(self, message: str, *, account_problem: str | None = None) -> None:
        super().__init__(message)
        self.account_problem = account_problem


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Step:
    """One model step."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    model: str = ""
    state: Any = None  # provider continuation handle


class Provider(Protocol):
    name: str

    def available(self) -> bool: ...

    def step(
        self,
        *,
        model: str,
        instructions: str,
        messages: list[dict[str, str]] | None,
        tool_results: list[tuple[ToolCall, str]] | None,
        tools: list[ToolSpec],
        state: Any,
        max_output_tokens: int,
    ) -> Step: ...


_REDACT = re.compile(r"(sk-[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]+")


def _redact(text: str) -> str:
    return _REDACT.sub(r"\1…", text)


def _account_problem(code: int, body: str) -> str | None:
    low = body.lower()
    if code in (401, 403) or "invalid_api_key" in low:
        return "auth"
    if "insufficient_quota" in low or "credit_balance" in low or "billing" in low:
        return "billing"
    return None


class OpenAIResponses:
    """OpenAI Responses API with function tools, chained by previous_response_id."""

    name = "openai"
    url = "https://api.openai.com/v1/responses"

    def __init__(self, *, api_key: str | None = None, timeout: float = 90.0, post: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        self.timeout = timeout
        self._post = post or self._http

    def available(self) -> bool:
        return bool(self.api_key)

    def _http(self, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise ProviderError(_redact(f"OpenAI HTTP {exc.code}: {detail}"), account_problem=_account_problem(exc.code, detail)) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError(f"OpenAI unreachable: {getattr(exc, 'reason', exc)}") from exc

    def step(self, *, model, instructions, messages, tool_results, tools, state, max_output_tokens) -> Step:
        body: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "max_output_tokens": max_output_tokens,
            "tools": [{"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters} for t in tools],
            "reasoning": {"effort": "low"},
        }
        if state:
            body["previous_response_id"] = state
            body["input"] = [{"type": "function_call_output", "call_id": c.id, "output": out} for c, out in (tool_results or [])]
        else:
            body["input"] = [{"role": m["role"], "content": m["content"]} for m in (messages or [])]
        raw = self._post(body)
        if raw.get("error"):
            raise ProviderError(_redact(f"OpenAI error: {raw['error']}"))
        usage = raw.get("usage") or {}
        out = Step(input_tokens=int(usage.get("input_tokens") or 0), output_tokens=int(usage.get("output_tokens") or 0), model=str(raw.get("model") or model), state=raw.get("id"))
        texts = []
        for item in raw.get("output") or []:
            if item.get("type") == "function_call":
                try:
                    args = json.loads(item.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {"_unparseable": item.get("arguments")}
                out.tool_calls.append(ToolCall(id=str(item.get("call_id")), name=str(item.get("name")), arguments=args))
            elif item.get("type") == "message":
                for part in item.get("content") or []:
                    if part.get("type") in ("output_text", "text"):
                        texts.append(str(part.get("text") or ""))
        out.text = "".join(texts).strip()
        if raw.get("status") == "incomplete" and not out.tool_calls and not out.text:
            raise ProviderError(f"OpenAI response incomplete: {(raw.get('incomplete_details') or {}).get('reason')}")
        return out


class ScriptedProvider:
    """Deterministic provider for tests: replays a list of steps (tool calls and/or final text)."""

    name = "scripted"

    def __init__(self, script: list[Step | Callable[[Mapping[str, Any]], Step]]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def available(self) -> bool:
        return True

    def step(self, *, model, instructions, messages, tool_results, tools, state, max_output_tokens) -> Step:
        request = {"instructions": instructions, "messages": messages, "tool_results": tool_results, "tools": [t.name for t in tools], "state": state}
        self.requests.append(request)
        if not self.script:
            return Step(text="(script exhausted)", model="scripted")
        nxt = self.script.pop(0)
        step = nxt(request) if callable(nxt) else nxt
        step.model = step.model or "scripted"
        step.state = (state or 0) + 1
        return step
