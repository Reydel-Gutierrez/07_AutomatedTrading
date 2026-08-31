"""PAPER vs LIVE runtime mode.

LIVE uses the Agentic Robinhood account as the single source of truth.
PAPER keeps the isolated paper book for tests/dev. The two books never mix.
Live order placement stays disabled in both modes.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from agentic_portfolio.adapters.readonly_runtime import (  # noqa: F401
    SHARED_PRODUCTION_TRANSPORT,
    bootstrap_readonly_broker_runtime,
    reset_readonly_broker_runtime,
)
from agentic_portfolio.policy import load_dashboard_config, load_runtime_config


LIVE_AI_ALLOWED = True
LIVE_PROPOSALS_ALLOWED = True
LIVE_ORDER_PLACEMENT = False


class RuntimeMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


LIVE_SOURCE_OF_TRUTH = "robinhood_agentic_account"
PAPER_SOURCE_OF_TRUTH = "isolated_paper_book"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def parse_runtime_mode(raw: Any) -> RuntimeMode | None:
    text = str(raw or "").strip().upper()
    if text in {RuntimeMode.PAPER.value, RuntimeMode.LIVE.value}:
        return RuntimeMode(text)
    return None


def resolve_runtime_mode(
    *,
    environ: Mapping[str, str] | None = None,
    runtime_config: dict[str, Any] | None = None,
    dashboard_config: dict[str, Any] | None = None,
) -> RuntimeMode:
    env = environ if environ is not None else os.environ
    runtime_cfg = dict(runtime_config if runtime_config is not None else load_runtime_config())
    env_names = list((runtime_cfg.get("env") or {}).get("mode") or ["AGENTIC_RUNTIME_MODE", "DASHBOARD_ENVIRONMENT"])
    for name in env_names:
        parsed = parse_runtime_mode(env.get(name))
        if parsed is not None:
            return parsed
    parsed = parse_runtime_mode(runtime_cfg.get("mode"))
    if parsed is not None:
        return parsed
    dash = dict(dashboard_config if dashboard_config is not None else load_dashboard_config())
    parsed = parse_runtime_mode(dash.get("environment"))
    if parsed is not None:
        return parsed
    return RuntimeMode.PAPER


def is_live(*, environ: Mapping[str, str] | None = None) -> bool:
    return resolve_runtime_mode(environ=environ) is RuntimeMode.LIVE


def live_placement_enabled(
    *,
    environ: Mapping[str, str] | None = None,
    runtime_config: dict[str, Any] | None = None,
    account_rules: dict[str, Any] | None = None,
) -> bool:
    """Always false until a future explicit human enable. Do not infer from LIVE mode."""
    del environ, runtime_config, account_rules
    return False


def source_of_truth(mode: RuntimeMode | None = None) -> str:
    current = mode or resolve_runtime_mode()
    if current is RuntimeMode.LIVE:
        return LIVE_SOURCE_OF_TRUTH
    return PAPER_SOURCE_OF_TRUTH


def get_active_runtime(
    *,
    environ: Mapping[str, str] | None = None,
    runtime_config: dict[str, Any] | None = None,
    dashboard_config: dict[str, Any] | None = None,
) -> RuntimeMode:
    """Authoritative active runtime. Operational services must not choose independently."""
    return resolve_runtime_mode(
        environ=environ,
        runtime_config=runtime_config,
        dashboard_config=dashboard_config,
    )


def get_active_portfolio_source(
    *,
    environ: Mapping[str, str] | None = None,
    mode: RuntimeMode | None = None,
) -> str:
    """PAPER → isolated paper book. LIVE → Agentic Robinhood account. Never mixed."""
    current = mode or get_active_runtime(environ=environ)
    return source_of_truth(current)


def get_active_artifact_environment(
    *,
    environ: Mapping[str, str] | None = None,
    runtime_config: dict[str, Any] | None = None,
    dashboard_config: dict[str, Any] | None = None,
) -> str:
    """PAPER or LIVE. Operational queries may only consume this environment's artifacts."""
    return get_active_runtime(
        environ=environ,
        runtime_config=runtime_config,
        dashboard_config=dashboard_config,
    ).value


def artifact_environment(record: Any, *, default: str = "PAPER") -> str:
    """Classify a stored artifact. Untagged historical records are PAPER. Never infer LIVE."""
    payload: dict[str, Any] = {}
    if isinstance(record, Mapping):
        payload = dict(record)
    elif record is not None:
        for key in (
            "runtime_mode",
            "environment",
            "paper_environment",
            "execution_status",
            "order_plan_summary",
            "plans",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
    mode = str(payload.get("runtime_mode") or payload.get("environment") or "").strip().upper()
    if mode in {RuntimeMode.PAPER.value, RuntimeMode.LIVE.value}:
        return mode
    if payload.get("paper_environment") is True:
        return RuntimeMode.PAPER.value
    summary = payload.get("order_plan_summary") if isinstance(payload.get("order_plan_summary"), Mapping) else {}
    status = str(payload.get("execution_status") or (summary.get("execution_status") if summary else "") or "").upper()
    if status in {"PAPER_ONLY", "DEMO"}:
        return RuntimeMode.PAPER.value
    for plan in payload.get("plans") or []:
        if isinstance(plan, Mapping) and str(plan.get("execution_status") or "").upper() in {"PAPER_ONLY", "DEMO"}:
            return RuntimeMode.PAPER.value
    return default


def discovery_state_dir(root: Path, *, mode: RuntimeMode | None = None) -> Path:
    """LIVE discovery is recomputed under state/live_ai. PAPER stays at state/."""
    current = mode or get_active_runtime()
    base = Path(root)
    if current is RuntimeMode.LIVE:
        return base / "state" / "live_ai"
    return base / "state"
