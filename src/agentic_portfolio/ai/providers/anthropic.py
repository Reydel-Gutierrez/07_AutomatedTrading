"""Anthropic Messages adapter. Structured output via forced tool schema."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Mapping

from agentic_portfolio.ai.errors import MalformedResponse, ProviderOutage, ProviderTimeout, SchemaViolation
from agentic_portfolio.ai.providers.base import ProviderRequest, ProviderResponse


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com/v1",
        timeout_seconds: float = 45.0,
        version_header: str = "2023-06-01",
        environ: Mapping[str, str] | None = None,
        transport=None,
    ) -> None:
        env = environ if environ is not None else os.environ
        self.api_key = api_key if api_key is not None else env.get("ANTHROPIC_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.version_header = version_header
        self._transport = transport

    def available(self) -> bool:
        return bool(self.api_key)

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        if not self.available():
            raise ProviderOutage("Anthropic API key is not configured")
        system = ""
        messages = []
        for msg in request.messages:
            role = msg.get("role")
            if role == "system":
                system = (system + "\n" + msg.get("content", "")).strip()
            else:
                messages.append({"role": role or "user", "content": msg.get("content") or ""})
        if not messages:
            messages = [{"role": "user", "content": "Return the structured result."}]
        body = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "messages": messages,
            "tools": [
                {
                    "name": request.schema_name,
                    "description": "Return the structured result. No prose.",
                    "input_schema": request.schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": request.schema_name},
        }
        if system:
            body["system"] = system
        url = f"{self.base_url}/messages"
        raw = self._post(url, body, timeout=request.timeout_seconds or self.timeout_seconds)
        payload = None
        for block in raw.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                payload = block["input"]
                break
        if payload is None:
            text = ""
            for block in raw.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text += str(block.get("text") or "")
            if not text:
                raise MalformedResponse("Anthropic returned no structured content")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise MalformedResponse("Anthropic response was not JSON") from exc
        if not isinstance(payload, dict):
            raise SchemaViolation("Anthropic payload is not an object")
        usage = raw.get("usage") or {}
        return ProviderResponse(
            payload=payload,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            model=str(raw.get("model") or request.model),
            provider=self.name,
            raw=raw,
        )

    def _post(self, url: str, body: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        if self._transport is not None:
            return self._transport(url, body, timeout=timeout)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "x-api-key": str(self.api_key),
                "anthropic-version": self.version_header,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except TimeoutError as exc:
            raise ProviderTimeout("Anthropic request timed out") from exc
        except urllib.error.HTTPError as exc:
            raise ProviderOutage(f"Anthropic HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            if "timed out" in reason.lower():
                raise ProviderTimeout("Anthropic request timed out") from exc
            raise ProviderOutage(f"Anthropic unreachable: {reason}") from exc
