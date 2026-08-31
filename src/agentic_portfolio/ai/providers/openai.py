"""OpenAI Responses API adapter. Structured JSON schema only.

Uses POST /v1/responses (not /v1/chat/completions). GPT-5.6 reasoning models
support structured outputs on this endpoint; chat-completions `max_tokens` is
not compatible with them. The API key is read only from OPENAI_API_KEY.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Mapping

from agentic_portfolio.ai.errors import MalformedResponse, ProviderOutage, ProviderTimeout, SchemaViolation
from agentic_portfolio.ai.providers.base import ProviderRequest, ProviderResponse

_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+)", re.IGNORECASE)

ENDPOINT_PATH = "/responses"


def _redact(text: str) -> str:
    return _SECRET.sub("[REDACTED]", text)


class OpenAIProvider:
    name = "openai"
    endpoint_path = ENDPOINT_PATH

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 90.0,
        environ: Mapping[str, str] | None = None,
        transport=None,
    ) -> None:
        env = environ if environ is not None else os.environ
        if api_key is not None:
            self.api_key = api_key
        else:
            self.api_key = env.get("OPENAI_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    def available(self) -> bool:
        return bool(self.api_key)

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        if not self.available():
            raise ProviderOutage("OpenAI API key is not configured")
        body: dict[str, Any] = {
            "model": request.model,
            "input": list(request.messages),
            "max_output_tokens": request.max_output_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.schema,
                }
            },
        }
        if request.reasoning_effort:
            body["reasoning"] = {"effort": request.reasoning_effort}
        url = f"{self.base_url}{ENDPOINT_PATH}"
        raw = self._post(url, body, timeout=request.timeout_seconds or self.timeout_seconds)
        payload = self._parse_payload(raw)
        usage = raw.get("usage") or {}
        return ProviderResponse(
            payload=payload,
            input_tokens=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
            model=str(raw.get("model") or request.model),
            provider=self.name,
            raw=raw,
        )

    def _parse_payload(self, raw: dict[str, Any]) -> dict[str, Any]:
        err = raw.get("error")
        if err:
            msg = err if isinstance(err, str) else str(err.get("message") or err)
            raise ProviderOutage(_redact(f"OpenAI error: {msg}"))
        status = raw.get("status")
        if status == "incomplete":
            reason = (raw.get("incomplete_details") or {}).get("reason") or "incomplete"
            raise MalformedResponse(f"OpenAI response incomplete ({reason})")
        if status and status not in {"completed"}:
            raise MalformedResponse(f"OpenAI response status is {status}")
        parsed = raw.get("output_parsed")
        if isinstance(parsed, dict):
            return parsed
        text = raw.get("output_text")
        if not text:
            chunks: list[str] = []
            for item in raw.get("output") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "reasoning":
                    continue
                for part in item.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") in {"refusal"}:
                        raise MalformedResponse("OpenAI refused the structured request")
                    if part.get("type") in {"output_text", "text"}:
                        chunks.append(str(part.get("text") or ""))
            text = "".join(chunks)
        if not text:
            raise MalformedResponse("OpenAI returned empty structured content")
        try:
            payload = json.loads(text) if isinstance(text, str) else text
        except (TypeError, json.JSONDecodeError) as exc:
            raise MalformedResponse(f"OpenAI response was not structured JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SchemaViolation("OpenAI payload is not an object")
        return payload

    def _post(self, url: str, body: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        if self._transport is not None:
            return self._transport(url, body, timeout=timeout)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except TimeoutError as exc:
            raise ProviderTimeout("OpenAI request timed out") from exc
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                detail = ""
            raise ProviderOutage(_redact(f"OpenAI HTTP {exc.code}: {detail}".rstrip())) from None
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            if "timed out" in reason.lower():
                raise ProviderTimeout("OpenAI request timed out") from exc
            raise ProviderOutage(_redact(f"OpenAI unreachable: {reason}")) from exc
