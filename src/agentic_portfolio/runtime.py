"""PAPER vs LIVE runtime mode.

LIVE uses the Agentic Robinhood account as the single source of truth.
PAPER keeps the isolated paper book for tests/dev. The two books never mix.

Committed default for live order placement is false. Production enables it only
via AGENTIC_LIVE_ORDER_PLACEMENT. Display and health must not report placement
ON unless the write transport is actually bound. Observation MCP stays READ_ONLY.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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
REQUIRE_HUMAN_APPROVAL = True
AUTO_EXECUTION = False
AI_MONTHLY_HARD_CAP_USD = 10.0


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


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def live_placement_enabled(
    *,
    environ: Mapping[str, str] | None = None,
    runtime_config: dict[str, Any] | None = None,
    account_rules: dict[str, Any] | None = None,
) -> bool:
    """Explicit opt-in intent. Default false. Ambiguous values fail closed.

    LIVE mode does not imply placement. Committed default remains false.
    This is the master switch LiveOrderExecutor consults. Dashboard/health
    display must use live_execution_authority so ON cannot appear when the
    write transport is unbound or still READ_ONLY.
    """
    env = environ if environ is not None else os.environ
    for name in ("AGENTIC_LIVE_ORDER_PLACEMENT", "LIVE_ORDER_PLACEMENT"):
        if name not in env:
            continue
        raw = str(env.get(name) or "").strip().lower()
        if not raw:
            continue
        if raw in _TRUE:
            return True
        if raw in _FALSE:
            return False
        return False
    cfg = dict(runtime_config if runtime_config is not None else load_runtime_config())
    if "live_order_placement_enabled" in cfg:
        value = cfg.get("live_order_placement_enabled")
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in _TRUE:
            return True
        return False
    if account_rules is not None:
        exe = dict(account_rules.get("execution") or {})
        if "live_order_placement_enabled" in exe:
            return _truthy(exe.get("live_order_placement_enabled"))
    return False


OBSERVATION_MODE = "READ_ONLY"
EXECUTION_MODE_LIVE_WRITE = "LIVE_WRITE"
EXECUTION_MODE_DISABLED = "DISABLED"
EXECUTION_MODE_UNAVAILABLE = "UNAVAILABLE"
EXECUTION_MODE_PAPER = "PAPER"


@dataclass(frozen=True)
class LiveExecutionAuthority:
    """Single runtime source of truth for live placement after human approval.

    Committed config files keep placement off for tests/dev. Production opt-in
    is AGENTIC_LIVE_ORDER_PLACEMENT. auto_execution is never true. Observation
    MCP remains READ_ONLY; only LiveOrderExecutor uses the write transport.
    """

    runtime_mode: RuntimeMode
    placement_requested: bool
    write_transport_ready: bool
    LIVE_ORDER_PLACEMENT: bool
    live_trade_actions_allowed: bool
    auto_execution: bool = False
    require_human_approval: bool = True
    observation_mode: str = OBSERVATION_MODE
    execution_mode: str = EXECUTION_MODE_DISABLED

    def as_dict(self) -> dict[str, Any]:
        return {
            "runtime_mode": self.runtime_mode.value,
            "placement_requested": self.placement_requested,
            "write_transport_ready": self.write_transport_ready,
            "LIVE_ORDER_PLACEMENT": self.LIVE_ORDER_PLACEMENT,
            "live_order_placement_enabled": self.LIVE_ORDER_PLACEMENT,
            "live_trade_actions_allowed": self.live_trade_actions_allowed,
            "auto_execution": False,
            "require_human_approval": True,
            "observation_mode": self.observation_mode,
            "execution_mode": self.execution_mode,
            "autonomous_trading_disabled": True,
            "approved_does_not_place_order": not self.LIVE_ORDER_PLACEMENT,
        }


def live_execution_authority(
    *,
    environ: Mapping[str, str] | None = None,
    runtime_config: dict[str, Any] | None = None,
    dashboard_config: dict[str, Any] | None = None,
    write_transport_ready: bool | None = None,
) -> LiveExecutionAuthority:
    """Authoritative live-execution snapshot. Dashboard, health, and executor agree here."""
    requested = live_placement_enabled(environ=environ, runtime_config=runtime_config)
    mode = resolve_runtime_mode(
        environ=environ,
        runtime_config=runtime_config,
        dashboard_config=dashboard_config,
    )
    ready = bool(write_transport_ready) if write_transport_ready is not None else _write_transport_ready()
    executable = bool(requested and mode is RuntimeMode.LIVE and ready)
    if mode is RuntimeMode.PAPER:
        execution_mode = EXECUTION_MODE_PAPER
    elif executable:
        execution_mode = EXECUTION_MODE_LIVE_WRITE
    elif requested and mode is RuntimeMode.LIVE:
        execution_mode = EXECUTION_MODE_UNAVAILABLE
    else:
        execution_mode = EXECUTION_MODE_DISABLED
    return LiveExecutionAuthority(
        runtime_mode=mode,
        placement_requested=requested,
        write_transport_ready=ready,
        LIVE_ORDER_PLACEMENT=executable,
        live_trade_actions_allowed=executable,
        auto_execution=False,
        require_human_approval=True,
        observation_mode=OBSERVATION_MODE,
        execution_mode=execution_mode,
    )


def _write_transport_ready() -> bool:
    try:
        from agentic_portfolio.live_execution.broker import write_transport_is_ready

        return bool(write_transport_is_ready())
    except Exception:  # noqa: BLE001 — fail closed: cannot display ON
        return False


def require_human_approval(
    *,
    runtime_config: dict[str, Any] | None = None,
    account_rules: dict[str, Any] | None = None,
) -> bool:
    cfg = dict(runtime_config if runtime_config is not None else load_runtime_config())
    if "require_human_approval" in cfg:
        return bool(cfg.get("require_human_approval"))
    rules = dict(account_rules if account_rules is not None else {})
    if not rules:
        from agentic_portfolio.policy import load_account_rules

        rules = load_account_rules()
    exe = dict(rules.get("execution") or {})
    if "require_human_approval" in exe:
        return bool(exe.get("require_human_approval"))
    return True


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
