"""Central AI Gateway. The only module allowed to call an AI provider."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from agentic_portfolio.ai.budget import BudgetManager, Reservation
from agentic_portfolio.ai.config import load_ai_config, role_spec
from agentic_portfolio.ai.errors import (
    AIError,
    BudgetDenied,
    BudgetExhausted,
    MalformedResponse,
    ProviderOutage,
    ProviderTimeout,
    SchemaViolation,
)
from agentic_portfolio.ai.ledger import UsageLedger
from agentic_portfolio.ai.pricing import estimate_cost, estimate_tokens
from agentic_portfolio.ai.providers.anthropic import AnthropicProvider
from agentic_portfolio.ai.providers.base import ProviderAdapter, ProviderRequest
from agentic_portfolio.ai.providers.openai import OpenAIProvider
from agentic_portfolio.ai.providers.scripted import ScriptedProvider
from agentic_portfolio.ai.schemas import SCHEMAS, validate_against_schema
from agentic_portfolio.ai.types import BudgetMode, GatewayResult, ModelRole
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import RuntimeMode

ZERO = Decimal("0")


def _role_enum(role_name: str) -> ModelRole:
    try:
        return ModelRole(role_name)
    except ValueError:
        return ModelRole.RESEARCH


@dataclass
class SeenCache:
    """Idempotent duplicate-response cache for a process. Not a second budget."""

    items: dict[str, GatewayResult]

    def get(self, key: str) -> GatewayResult | None:
        return self.items.get(key)

    def put(self, key: str, result: GatewayResult) -> None:
        self.items[key] = result


class AIGateway:
    """Provider-neutral structured-output client with a hard monthly budget."""

    def __init__(
        self,
        *,
        budget: BudgetManager,
        providers: Mapping[str, ProviderAdapter],
        config: dict[str, Any] | None = None,
        runtime_mode: RuntimeMode | str = RuntimeMode.PAPER,
        seen: SeenCache | None = None,
    ) -> None:
        self.budget = budget
        self.providers = dict(providers)
        self.config = config or load_ai_config()
        self.runtime_mode = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode)
        self.seen = seen or SeenCache(items={})
        self.calls: list[GatewayResult] = []

    def provider_availability(self) -> dict[str, bool]:
        return {name: bool(adapter.available()) for name, adapter in self.providers.items()}

    def complete_structured(
        self,
        *,
        role: ModelRole | str,
        purpose: str,
        schema_name: str,
        messages: list[dict[str, str]],
        ticker: str | None = None,
        schema: dict[str, Any] | None = None,
        critical: bool = False,
        allow_fallback: bool = True,
        estimated_input_tokens: int | None = None,
        estimated_output_tokens: int | None = None,
    ) -> GatewayResult:
        role_name = role.value if isinstance(role, ModelRole) else str(role)
        spec = role_spec(self.config, role_name)
        schema_body = schema or SCHEMAS[schema_name]
        status = self.budget.status()
        if status.mode is BudgetMode.EXHAUSTED:
            raise BudgetExhausted("AI monthly cap reached; external AI is blocked")
        provider_name, model = self._route(role_name, spec, status.mode, allow_fallback=allow_fallback, critical=critical)
        prompt_text = "\n".join(m.get("content") or "" for m in messages)
        in_tok = estimated_input_tokens if estimated_input_tokens is not None else estimate_tokens(prompt_text)
        out_tok = estimated_output_tokens if estimated_output_tokens is not None else int(spec.get("default_max_output_tokens") or 400)
        estimated = estimate_cost(model=model, input_tokens=in_tok, output_tokens=out_tok, config=self.config)
        fingerprint = _fingerprint(role_name, schema_name, ticker, messages)
        cached = self.seen.get(fingerprint)
        if cached is not None:
            return cached
        reservation = self.budget.authorize(
            estimated,
            purpose=purpose,
            role=role_name,
            provider=provider_name,
            model=model,
            ticker=ticker,
            critical=critical,
            runtime_mode=self.runtime_mode,
        )
        request = ProviderRequest(
            model=model,
            messages=messages,
            schema_name=schema_name,
            schema=schema_body,
            max_output_tokens=int(spec.get("default_max_output_tokens") or 800),
            timeout_seconds=float(((self.config.get("providers") or {}).get(provider_name) or {}).get("timeout_seconds") or 90),
            purpose=purpose,
            ticker=ticker,
            reasoning_effort=str(spec["reasoning_effort"]) if spec.get("reasoning_effort") else None,
        )
        try:
            result = self._execute(provider_name, request, reservation, schema_body, role_name, purpose, ticker, allow_fallback)
        except Exception:
            self.budget.release(reservation, reason="failed")
            raise
        self.calls.append(result)
        self.seen.put(fingerprint, result)
        return result

    def _execute(
        self,
        provider_name: str,
        request: ProviderRequest,
        reservation: Reservation,
        schema_body: dict[str, Any],
        role_name: str,
        purpose: str,
        ticker: str | None,
        allow_fallback: bool,
    ) -> GatewayResult:
        adapter = self.providers.get(provider_name)
        fallback_used = False
        last_error: Exception | None = None
        tried = [provider_name]
        if adapter is None or not adapter.available():
            last_error = ProviderOutage(f"provider {provider_name} is unavailable")
            adapter = None
        if adapter is not None:
            try:
                return self._call_adapter(adapter, request, reservation, schema_body, role_name, purpose, ticker, False)
            except (ProviderOutage, ProviderTimeout, MalformedResponse, SchemaViolation) as exc:
                last_error = exc
        if allow_fallback:
            fallback_spec = role_spec(self.config, ModelRole.FALLBACK.value)
            fb_provider = str(fallback_spec["provider"])
            if self._is_live_scripted(fb_provider, self.providers.get(fb_provider)):
                last_error = ProviderOutage("scripted provider is not allowed as a LIVE fallback")
            elif fb_provider not in tried:
                fb_adapter = self.providers.get(fb_provider)
                if fb_adapter is not None and fb_adapter.available():
                    fb_request = ProviderRequest(
                        model=str(fallback_spec["model"]),
                        messages=request.messages,
                        schema_name=request.schema_name,
                        schema=request.schema,
                        max_output_tokens=int(fallback_spec.get("default_max_output_tokens") or request.max_output_tokens),
                        timeout_seconds=request.timeout_seconds,
                        purpose=purpose,
                        ticker=ticker,
                        reasoning_effort=str(fallback_spec["reasoning_effort"]) if fallback_spec.get("reasoning_effort") else None,
                    )
                    # Fallback still uses the original reservation; actual cost is recorded against it.
                    try:
                        return self._call_adapter(
                            fb_adapter, fb_request, reservation, schema_body, role_name, purpose, ticker, True
                        )
                    except (ProviderOutage, ProviderTimeout, MalformedResponse, SchemaViolation) as exc:
                        last_error = exc
        if last_error is None:
            last_error = ProviderOutage("no AI provider available")
        raise last_error

    def _call_adapter(
        self,
        adapter: ProviderAdapter,
        request: ProviderRequest,
        reservation: Reservation,
        schema_body: dict[str, Any],
        role_name: str,
        purpose: str,
        ticker: str | None,
        fallback_used: bool,
    ) -> GatewayResult:
        response = adapter.complete(request)
        payload = validate_against_schema(response.payload, schema_body, name=request.schema_name)
        rate_model = response.model if response.model else request.model
        actual = estimate_cost(
            model=rate_model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            config=self.config,
        )
        self.budget.record(
            reservation,
            actual_cost=actual,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            runtime_mode=self.runtime_mode,
        )
        return GatewayResult(
            payload=payload,
            provider=adapter.name,
            model=response.model,
            role=_role_enum(role_name),
            purpose=purpose,
            ticker=ticker,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            estimated_cost=reservation.estimated_cost,
            actual_cost=actual,
            reservation_id=reservation.reservation_id,
            fallback_used=fallback_used,
        )

    def _route(
        self,
        role_name: str,
        spec: dict[str, Any],
        mode: BudgetMode,
        *,
        allow_fallback: bool,
        critical: bool,
    ) -> tuple[str, str]:
        budget_cfg = dict(self.config.get("budget") or {})
        if mode is BudgetMode.CONSERVING and not critical:
            if budget_cfg.get("conserving_skip_escalation") and role_name == ModelRole.ESCALATION.value:
                raise BudgetDenied("escalation is skipped in CONSERVING mode")
            if budget_cfg.get("conserving_skip_fallback") and role_name == ModelRole.FALLBACK.value:
                raise BudgetDenied("fallback is skipped in CONSERVING mode")
            if role_name not in {str(r) for r in (budget_cfg.get("conserving_allowed_roles") or ["screening"])} and role_name != ModelRole.SCREENING.value:
                # Prefer the cheap screening model when conserving, unless this is a critical call.
                screen = role_spec(self.config, ModelRole.SCREENING.value)
                return str(screen["provider"]), str(screen["model"])
        del allow_fallback
        return str(spec["provider"]), str(spec["model"])

    def _is_live(self) -> bool:
        return str(self.runtime_mode).upper() == RuntimeMode.LIVE.value

    def _is_live_scripted(self, provider_name: str | None, adapter: ProviderAdapter | None) -> bool:
        if not self._is_live():
            return False
        if str(provider_name or "").lower() == "scripted":
            return True
        return bool(adapter is not None and str(getattr(adapter, "name", "") or "").lower() == "scripted")


def default_providers(
    config: dict[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    scripted: ScriptedProvider | None = None,
) -> dict[str, ProviderAdapter]:
    cfg = config or load_ai_config()
    providers_cfg = dict(cfg.get("providers") or {})
    openai_cfg = dict(providers_cfg.get("openai") or {})
    anthropic_cfg = dict(providers_cfg.get("anthropic") or {})
    out: dict[str, ProviderAdapter] = {}
    if openai_cfg.get("enabled", True):
        kwargs = {"environ": environ, "timeout_seconds": float(openai_cfg.get("timeout_seconds") or 90)}
        if openai_cfg.get("base_url"):
            kwargs["base_url"] = str(openai_cfg["base_url"])
        out["openai"] = OpenAIProvider(**kwargs)
    if anthropic_cfg.get("enabled", True):
        kwargs = {"environ": environ, "timeout_seconds": float(anthropic_cfg.get("timeout_seconds") or 45)}
        if anthropic_cfg.get("base_url"):
            kwargs["base_url"] = str(anthropic_cfg["base_url"])
        if anthropic_cfg.get("version_header"):
            kwargs["version_header"] = str(anthropic_cfg["version_header"])
        out["anthropic"] = AnthropicProvider(**kwargs)
    if scripted is not None or (providers_cfg.get("scripted") or {}).get("enabled", True):
        out["scripted"] = scripted or ScriptedProvider()
    return out


def build_gateway(
    root: Path | None = None,
    *,
    config: dict[str, Any] | None = None,
    providers: Mapping[str, ProviderAdapter] | None = None,
    runtime_mode: RuntimeMode | str = RuntimeMode.PAPER,
    now_fn=None,
    scripted: ScriptedProvider | None = None,
    environ: Mapping[str, str] | None = None,
) -> AIGateway:
    base = root or project_root()
    cfg = config or load_ai_config()
    ledger = UsageLedger(base, config=cfg)
    budget = BudgetManager(ledger, cfg, now_fn=now_fn)
    adapters = dict(providers) if providers is not None else default_providers(cfg, environ=environ, scripted=scripted)
    mode_name = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode).upper()
    if mode_name == RuntimeMode.LIVE.value and providers is None:
        adapters.pop("scripted", None)
    return AIGateway(budget=budget, providers=adapters, config=cfg, runtime_mode=runtime_mode)


def _fingerprint(role: str, schema_name: str, ticker: str | None, messages: list[dict[str, str]]) -> str:
    blob = json.dumps({"role": role, "schema": schema_name, "ticker": ticker, "messages": messages}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
