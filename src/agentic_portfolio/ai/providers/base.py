"""Provider adapter protocol. Only the gateway constructs these."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ProviderRequest:
    model: str
    messages: list[dict[str, str]]
    schema_name: str
    schema: dict[str, Any]
    max_output_tokens: int = 800
    timeout_seconds: float = 45.0
    purpose: str = ""
    ticker: str | None = None
    reasoning_effort: str | None = None


@dataclass
class ProviderResponse:
    payload: dict[str, Any]
    input_tokens: int
    output_tokens: int
    model: str
    provider: str
    raw: dict[str, Any] = field(default_factory=dict)


class ProviderAdapter(Protocol):
    name: str

    def available(self) -> bool: ...

    def complete(self, request: ProviderRequest) -> ProviderResponse: ...
