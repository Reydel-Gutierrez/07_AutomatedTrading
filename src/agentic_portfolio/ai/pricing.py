"""Token-cost estimates. Used before every external AI call."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping

from agentic_portfolio.ai.config import load_ai_config, money

CENTS = Decimal("0.000001")


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def resolve_pricing_model(model: str, config: Mapping[str, Any] | None = None) -> str:
    """Map a provider model id (including snapshots) onto a pricing-table key."""
    cfg = dict(config or load_ai_config())
    table = dict(cfg.get("pricing_per_million") or {})
    if model in table:
        return model
    known = [key for key in table if key not in {"default", "scripted"}]
    known.sort(key=len, reverse=True)
    for key in known:
        if model.startswith(key):
            return key
    return "default"


def model_rates(model: str, config: Mapping[str, Any] | None = None) -> tuple[Decimal, Decimal]:
    cfg = dict(config or load_ai_config())
    table = dict(cfg.get("pricing_per_million") or {})
    key = resolve_pricing_model(model, cfg)
    row = table.get(key) or table.get("default") or {"input": 4.0, "output": 20.0}
    return money(row.get("input") or 0), money(row.get("output") or 0)


def estimate_cost(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    config: Mapping[str, Any] | None = None,
) -> Decimal:
    inp, out = model_rates(model, config)
    raw = (Decimal(max(0, input_tokens)) * inp + Decimal(max(0, output_tokens)) * out) / Decimal("1000000")
    return raw.quantize(CENTS, rounding=ROUND_HALF_UP)


def quantize(value: Decimal | str | float) -> Decimal:
    return money(value).quantize(CENTS, rounding=ROUND_HALF_UP)
