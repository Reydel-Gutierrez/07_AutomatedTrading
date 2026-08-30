"""Bind host/port. Localhost by default. Cloudflare Tunnel config is prepared, not enabled."""

from __future__ import annotations

import os
from typing import Any, Mapping

from agentic_portfolio.dashboard.safety import DashboardSafetyError, assert_localhost_bind
from agentic_portfolio.policy import load_dashboard_config

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3100
PAPER_BOOK_LABEL = "PAPER BOOK"
LIVE_ACCOUNT_LABEL = "LIVE ACCOUNT (read-only)"
NO_LIVE_PLACEMENT_BANNER = "NO LIVE ORDER PLACEMENT ENABLED"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _first_env(environ: Mapping[str, str], names: list[str]) -> str | None:
    for name in names:
        raw = environ.get(name)
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip()
    return None


def resolve_bind(
    *,
    environ: Mapping[str, str] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = dict(config or load_dashboard_config())
    env = environ if environ is not None else os.environ
    env_names = cfg.get("env") or {}
    port_names = list(env_names.get("port") or ["DASHBOARD_PORT", "AGENTIC_DASHBOARD_PORT"])
    host_names = list(env_names.get("host") or ["DASHBOARD_HOST", "AGENTIC_DASHBOARD_HOST"])
    public_name = env_names.get("allow_public_bind") or "DASHBOARD_ALLOW_PUBLIC_BIND"
    port_raw = _first_env(env, port_names)
    host_raw = _first_env(env, host_names)
    port = int(port_raw if port_raw is not None else cfg.get("port") or DEFAULT_PORT)
    host = host_raw if host_raw is not None else str(cfg.get("host") or DEFAULT_HOST)
    allow_public = _truthy(env.get(public_name)) or _truthy(cfg.get("allow_public_bind"))
    assert_localhost_bind(host, allow_public_bind=allow_public)
    if port <= 0 or port > 65535:
        raise DashboardSafetyError(f"invalid dashboard port: {port}")
    cloudflare = dict(cfg.get("cloudflare") or {})
    return {
        "host": host,
        "port": port,
        "allow_public_bind": allow_public,
        "bind_localhost_only": bool(cfg.get("bind_localhost_only", True)) and not allow_public,
        "public_exposure": False,
        "cloudflare": {
            "tunnel_enabled": bool(cloudflare.get("tunnel_enabled")),
            "access_enabled": bool(cloudflare.get("access_enabled")),
            "tunnel_hostname": cloudflare.get("tunnel_hostname"),
            "access_aud": cloudflare.get("access_aud"),
            "trusted_proxy_ips": list(cloudflare.get("trusted_proxy_ips") or []),
            "origin": cloudflare.get("origin") or f"http://{host}:{port}",
            "notes": cloudflare.get("notes"),
        },
        "writes_allowed": list(cfg.get("writes_allowed") or ["approve_packet", "reject_packet"]),
        "writes_forbidden": list(cfg.get("writes_forbidden") or []),
    }


def _flag(
    env: Mapping[str, str],
    env_name: str,
    cfg: Mapping[str, Any],
    cfg_name: str,
    default: bool = False,
) -> bool:
    raw = env.get(env_name)
    if raw is not None and str(raw).strip() != "":
        return _truthy(raw)
    if cfg_name in cfg:
        return _truthy(cfg.get(cfg_name))
    return default


def resolve_ui_flags(
    *,
    environ: Mapping[str, str] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = dict(config or load_dashboard_config())
    env = environ if environ is not None else os.environ
    env_names = cfg.get("env") or {}
    env_key = env_names.get("environment") or "DASHBOARD_ENVIRONMENT"
    paper_key = env_names.get("allow_paper_packet_decisions") or "DASHBOARD_ALLOW_PAPER_PACKET_DECISIONS"
    demo_key = env_names.get("allow_demo_packet_decisions") or "DASHBOARD_ALLOW_DEMO_PACKET_DECISIONS"
    stale_key = env_names.get("allow_stale_packet_decisions") or "DASHBOARD_ALLOW_STALE_PACKET_DECISIONS"
    raw_env = (_first_env(env, [env_key]) or str(cfg.get("environment") or "PAPER")).strip().upper()
    environment = raw_env if raw_env in {"PAPER", "LIVE"} else "PAPER"
    return {
        "environment": environment,
        "environment_banner": f"{environment} ENVIRONMENT",
        "paper_book_label": PAPER_BOOK_LABEL,
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "live_order_placement_enabled": False,
        "no_live_placement_banner": NO_LIVE_PLACEMENT_BANNER,
        "allow_paper_packet_decisions": _flag(env, paper_key, cfg, "allow_paper_packet_decisions", False),
        "allow_demo_packet_decisions": _flag(env, demo_key, cfg, "allow_demo_packet_decisions", False),
        "allow_stale_packet_decisions": _flag(env, stale_key, cfg, "allow_stale_packet_decisions", False),
        "bind_localhost_only": True,
    }
