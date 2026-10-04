"""Execution policy: the single answer to "who may authorize an order?".

Three modes, one switch (``AGENTIC_EXECUTION_MODE`` or ``config/execution_policy.json``):

* ``OBSERVE``         — research, decide and risk-check; never create an order.
* ``HUMAN_APPROVAL``  — every order waits for a human APPROVE.
* ``AUTONOMOUS``      — the deterministic OrderAuthority authorizes clean orders;
                        exceptions are escalated to the human queue.

This module never talks to a broker and never loosens a Risk Gate limit. Risk
limits live in ``config/portfolio_policy.json`` and apply in every mode.
Broker writes are a separate, independent switch (see ``broker_writes_allowed``).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from agentic_portfolio.paths import project_root

MODE_ENV = "AGENTIC_EXECUTION_MODE"
KILL_SWITCH_ENV = "AGENTIC_KILL_SWITCH"
STATE_ROOT_ENV = "AGENTIC_STATE_ROOT"

RISK_UP_ACTIONS = frozenset({"BUY", "ADD"})
RISK_DOWN_ACTIONS = frozenset({"SELL", "REDUCE"})
EXECUTABLE_ACTIONS = RISK_UP_ACTIONS | RISK_DOWN_ACTIONS


class ExecutionMode(str, Enum):
    OBSERVE = "OBSERVE"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"
    AUTONOMOUS = "AUTONOMOUS"


@dataclass(frozen=True)
class AutonomyLimits:
    """Circuit breakers for autonomous authorization. They only ever restrict."""

    allowed_actions: frozenset[str] = EXECUTABLE_ACTIONS
    required_risk_verdict: str = "PASS"
    max_orders_per_day: int = 6
    max_buy_notional_pct_nav_per_day: float = 30.0
    max_single_order_pct_nav: float = 20.0
    min_cash_reserve_pct_nav: float = 1.0
    max_spread_bps: float = 50.0
    min_minutes_after_open: int = 5
    min_minutes_before_close: int = 10
    max_consecutive_execution_failures: int = 3
    authorization_ttl_hours: float = 8.0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["allowed_actions"] = sorted(self.allowed_actions)
        return data


@dataclass(frozen=True)
class ExecutionPolicy:
    mode: ExecutionMode
    mode_source: str
    limits: AutonomyLimits = field(default_factory=AutonomyLimits)
    version: str = "1.0.0"
    kill_switch_path: str = "state/runtime/kill_switch.json"
    kill_switch_journal: str = "logs/kill_switch.jsonl"
    auto_trip: Mapping[str, bool] = field(default_factory=dict)
    invalid_mode_value: str | None = None

    @property
    def autonomous(self) -> bool:
        return self.mode is ExecutionMode.AUTONOMOUS

    @property
    def creates_orders(self) -> bool:
        return self.mode is not ExecutionMode.OBSERVE

    def auto_trip_enabled(self, name: str) -> bool:
        return bool(self.auto_trip.get(name, True))

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "mode_source": self.mode_source,
            "version": self.version,
            "limits": self.limits.as_dict(),
            "kill_switch_path": self.kill_switch_path,
            "auto_trip": dict(self.auto_trip),
            "invalid_mode_value": self.invalid_mode_value,
            "ai_has_broker_access": False,
        }


def config_path() -> Path:
    return project_root() / "config" / "execution_policy.json"


def load_execution_policy_config(path: Path | None = None) -> dict[str, Any]:
    p = path or config_path()
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def parse_mode(raw: Any) -> ExecutionMode | None:
    text = str(raw or "").strip().upper().replace("-", "_")
    aliases = {"AUTO": "AUTONOMOUS", "AUTONOMOUS_EXECUTION": "AUTONOMOUS", "HUMAN": "HUMAN_APPROVAL", "APPROVAL": "HUMAN_APPROVAL", "READ_ONLY": "OBSERVE", "RESEARCH_ONLY": "OBSERVE"}
    text = aliases.get(text, text)
    try:
        return ExecutionMode(text)
    except ValueError:
        return None


def _limits(raw: Mapping[str, Any] | None) -> AutonomyLimits:
    data = dict(raw or {})
    base = AutonomyLimits()
    actions = data.get("allowed_actions")
    allowed = frozenset(str(a).upper() for a in actions) & EXECUTABLE_ACTIONS if actions is not None else base.allowed_actions

    def num(name: str, cast=float):
        value = data.get(name)
        if value is None:
            return getattr(base, name)
        try:
            out = cast(value)
        except (TypeError, ValueError):
            return getattr(base, name)
        # Limits only restrict: negative values fall back to defaults.
        return out if out >= 0 else getattr(base, name)

    return AutonomyLimits(
        allowed_actions=allowed,
        required_risk_verdict=str(data.get("required_risk_verdict") or base.required_risk_verdict).upper(),
        max_orders_per_day=num("max_orders_per_day", int),
        max_buy_notional_pct_nav_per_day=num("max_buy_notional_pct_nav_per_day"),
        max_single_order_pct_nav=num("max_single_order_pct_nav"),
        min_cash_reserve_pct_nav=num("min_cash_reserve_pct_nav"),
        max_spread_bps=num("max_spread_bps"),
        min_minutes_after_open=num("min_minutes_after_open", int),
        min_minutes_before_close=num("min_minutes_before_close", int),
        max_consecutive_execution_failures=max(1, num("max_consecutive_execution_failures", int)),
        authorization_ttl_hours=num("authorization_ttl_hours"),
    )


def load_execution_policy(
    *,
    environ: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> ExecutionPolicy:
    """Resolve the active execution policy. Invalid values fail safe to HUMAN_APPROVAL."""
    env = environ if environ is not None else os.environ
    cfg = dict(config if config is not None else load_execution_policy_config())
    env_names = dict(cfg.get("env") or {})
    mode_env = str(env_names.get("mode") or MODE_ENV)
    invalid = None
    mode = None
    source = "default"
    raw_env = env.get(mode_env)
    if raw_env is not None and str(raw_env).strip():
        mode = parse_mode(raw_env)
        source = f"env:{mode_env}"
        if mode is None:
            invalid = str(raw_env)
    if mode is None and invalid is None:
        raw_cfg = cfg.get("mode")
        if raw_cfg is not None:
            mode = parse_mode(raw_cfg)
            source = "config:execution_policy.json"
            if mode is None:
                invalid = str(raw_cfg)
    if mode is None:
        mode = ExecutionMode.HUMAN_APPROVAL
        source = "fail_safe_invalid_value" if invalid else "default"
    ks = dict(cfg.get("kill_switch") or {})
    return ExecutionPolicy(
        mode=mode,
        mode_source=source,
        limits=_limits(cfg.get("autonomous")),
        version=str(cfg.get("version") or "1.0.0"),
        kill_switch_path=str(ks.get("path") or "state/runtime/kill_switch.json"),
        kill_switch_journal=str(ks.get("journal") or "logs/kill_switch.jsonl"),
        auto_trip=dict(ks.get("auto_trip") or {}),
        invalid_mode_value=invalid,
    )


def resolve_state_root(default: Path | None = None, *, environ: Mapping[str, str] | None = None, runtime_mode: Any = None) -> Path:
    """Where runtime state lives. LIVE keeps the project root (existing Pi state).

    PAPER defaults to ``<project>/var/paper`` so a simulated account can never
    share files with the real Agentic account. ``AGENTIC_STATE_ROOT`` overrides both.
    """
    env = environ if environ is not None else os.environ
    override = str(env.get(STATE_ROOT_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    base = default or project_root()
    mode = str(getattr(runtime_mode, "value", runtime_mode) or "").upper()
    if mode == "PAPER":
        return base / "var" / "paper"
    return base
