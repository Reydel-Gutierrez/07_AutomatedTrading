"""Anthropic Claude provider (official SDK), implementing the firm's tool-calling Provider interface.

- Messages API is stateless: the continuation state is the full message list,
  and each assistant turn is appended exactly as returned (thinking blocks
  included) before the tool results.
- Automatic prompt caching (top-level cache_control) so the instructions,
  tools, and earlier steps of a turn are billed at cache rates on later steps.
- Server-side refusal fallback ("default" routing) is enabled; a refusal that
  survives it is returned as a plain explanation, never as fabricated content.
- The API key comes only from the constructor (read from the Pi env file by the
  service); it is never logged or included in errors.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from firm.ai.providers import ProviderError, Step, ToolCall

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicMessages:
    name = "anthropic"

    def __init__(self, *, api_key: str, effort: str = "high", timeout: float = 300.0, client: Any = None) -> None:
        self._has_key = bool(api_key)
        self.effort = effort
        self._client = client or (anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2) if api_key else None)

    def available(self) -> bool:
        return self._has_key or self._client is not None

    def step(self, *, model, instructions, messages, tool_results, tools, state, max_output_tokens) -> Step:
        if self._client is None:
            raise ProviderError("ANTHROPIC_API_KEY is not configured", account_problem="auth")
        if state is None:
            history = [{"role": m["role"], "content": m["content"]} for m in (messages or [])]
        else:
            history = list(state)
            history.append(
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": call.id, "content": out} for call, out in (tool_results or [])],
                }
            )
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_output_tokens,
            "system": instructions,
            "messages": history,
            "cache_control": {"type": "ephemeral"},
            "output_config": {"effort": self.effort},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        if tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        try:
            response = self._client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError:
            raise ProviderError("Anthropic rejected the API key", account_problem="auth") from None
        except anthropic.PermissionDeniedError:
            raise ProviderError("the Anthropic API key lacks permission for this model", account_problem="auth") from None
        except anthropic.BadRequestError as exc:
            text = str(getattr(exc, "message", exc))
            if "credit balance" in text.lower() or "billing" in text.lower():
                raise ProviderError("Anthropic account is out of credits", account_problem="billing") from None
            raise ProviderError(f"Anthropic rejected the request: {text[:300]}") from None
        except anthropic.RateLimitError:
            raise ProviderError("Anthropic rate limit reached; try again shortly") from None
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"Anthropic API error {exc.status_code}") from None
        except (anthropic.APIConnectionError, anthropic.APITimeoutError):
            raise ProviderError("Anthropic API unreachable") from None

        usage = response.usage
        out = Step(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            model=str(getattr(response, "model", model) or model),
        )
        if response.stop_reason == "refusal":
            out.text = "I can't help with that request as phrased. Could you rephrase or narrow it?"
            return out
        texts = []
        for block in response.content:
            if block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else _parse(block.input)
                out.tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=args))
            elif block.type == "text":
                texts.append(block.text)
        out.text = "".join(texts).strip()
        if response.stop_reason == "max_tokens" and not out.tool_calls:
            raise ProviderError("the answer was cut off at the output limit")
        # Continuation state: history plus this assistant turn exactly as returned.
        out.state = history + [{"role": "assistant", "content": response.content}]
        return out


def _parse(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else dict(raw)
        return value if isinstance(value, dict) else {"_unparseable": raw}
    except (TypeError, ValueError):
        return {"_unparseable": str(raw)[:200]}
