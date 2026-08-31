"""Scripted provider for tests and launch checks. No network."""

from __future__ import annotations

import copy
from typing import Any, Callable

from agentic_portfolio.ai.errors import MalformedResponse, ProviderOutage, ProviderTimeout, SchemaViolation
from agentic_portfolio.ai.providers.base import ProviderRequest, ProviderResponse


class ScriptedProvider:
    name = "scripted"

    def __init__(
        self,
        responses: dict[str, Any] | Callable[[ProviderRequest], dict[str, Any]] | None = None,
        *,
        fail: str | None = None,
        input_tokens: int = 80,
        output_tokens: int = 40,
        name: str | None = None,
    ) -> None:
        if name:
            self.name = name
        self._responses = responses or {}
        self.fail = fail
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls: list[ProviderRequest] = []

    def available(self) -> bool:
        return self.fail != "unavailable"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        if self.fail == "outage":
            raise ProviderOutage(f"{self.name} outage")
        if self.fail == "timeout":
            raise ProviderTimeout(f"{self.name} timeout")
        if self.fail == "malformed":
            raise MalformedResponse(f"{self.name} malformed response")
        if self.fail == "schema":
            raise SchemaViolation(f"{self.name} schema violation")
        payload = self._lookup(request)
        if self.fail == "duplicate":
            payload = copy.deepcopy(payload)
        if not isinstance(payload, dict):
            raise MalformedResponse("scripted provider returned non-object")
        return ProviderResponse(
            payload=copy.deepcopy(payload),
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            model=request.model,
            provider=self.name,
        )

    def _lookup(self, request: ProviderRequest) -> dict[str, Any]:
        if callable(self._responses):
            return self._responses(request)
        table = dict(self._responses)
        for key in (
            f"{request.schema_name}:{request.ticker}",
            request.ticker,
            request.schema_name,
            request.purpose,
            "*",
        ):
            if key and key in table:
                return table[key]
        raise KeyError(f"no scripted AI response for {request.schema_name}/{request.ticker}")
