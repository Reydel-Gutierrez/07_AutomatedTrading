"""Raspberry Pi production smoke test. Read-only. Never places or reviews an order.

Safe to run as the service user:

  sudo -u agentic -H env PYTHONPATH=src \\
    /opt/agentic-portfolio/.venv/bin/python scripts/pi_production_smoke.py

Exits nonzero on any FAIL. Does not call place/review/cancel/crypto/transfer tools.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agentic_portfolio.adapters.portfolio_facts import confirm_agentic_account
from agentic_portfolio.adapters.readonly_mcp_auth import oauth_store_path, path_is_inside_repo
from agentic_portfolio.adapters.readonly_runtime import (
    bootstrap_readonly_broker_runtime,
    reset_readonly_broker_runtime,
)
from agentic_portfolio.adapters.robinhood_read import FORBIDDEN_MCP_TOOLS
from agentic_portfolio.ai.config import load_ai_config, monthly_cap
from agentic_portfolio.dashboard.queries import dashboard_state, dashboard_view
from agentic_portfolio.discovery.live_readonly import LIVE_DISCOVERY_WIRED
from agentic_portfolio.live.engine import refresh_live_portfolio
from agentic_portfolio.live.store import LivePortfolioStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, RuntimeMode, get_active_runtime


WRITE_TOOLS = frozenset(FORBIDDEN_MCP_TOOLS) | {
    "review_equity_order",
    "place_option_order",
    "review_option_order",
    "cancel_option_order",
    "place_crypto_order",
    "preview_crypto_order",
    "cancel_crypto_order",
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def _merge_env_file(environ: dict[str, str], path: Path) -> dict[str, str]:
    """Load systemd EnvironmentFile keys without printing values."""
    if not path.is_file():
        return environ
    merged = dict(environ)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return merged
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, _value = stripped.partition("=")
        name = key.strip()
        if name and name not in merged:
            merged[name] = _value.strip().strip('"').strip("'")
    return merged


def _home(environ: Mapping[str, str]) -> Path:
    raw = str(environ.get("HOME") or "").strip()
    return Path(raw) if raw else Path.home()


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".smoke_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def run_smoke(
    *,
    root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    transport: Any | None = None,
    persist: bool = True,
    connect: bool = True,
) -> list[Check]:
    """Return a PASS/FAIL checklist. Never places."""
    env = dict(environ if environ is not None else os.environ)
    env = _merge_env_file(env, Path("/etc/agentic-portfolio/env"))
    base = root or project_root()
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append(Check(name, bool(ok), detail))

    runtime = get_active_runtime(environ=env)
    add("runtime_mode_LIVE", runtime is RuntimeMode.LIVE, runtime.value)
    add("LIVE_ORDER_PLACEMENT_false", LIVE_ORDER_PLACEMENT is False, str(LIVE_ORDER_PLACEMENT).lower())
    rules = load_account_rules()
    exe = dict(rules.get("execution") or {})
    add("auto_execution_false", exe.get("auto_execution") is False, str(bool(exe.get("auto_execution"))).lower())
    add(
        "live_trade_actions_allowed_false",
        exe.get("live_trade_actions_allowed") is False,
        str(bool(exe.get("live_trade_actions_allowed"))).lower(),
    )

    home = _home(env)
    store = oauth_store_path(environ=env)
    add("HOME_set", bool(str(env.get("HOME") or "").strip() or home), str(home))
    add("oauth_store_not_in_repo", not path_is_inside_repo(store, root=project_root()), str(store))
    override = str(env.get("AGENTIC_READONLY_MCP_HOME") or "").strip()
    if override:
        add("oauth_store_uses_override", Path(override) in store.parents or store.parent == Path(override), str(store))
    else:
        add(
            "oauth_store_under_HOME",
            str(store).startswith(str(home)),
            str(store),
        )
    windows_like = "localappdata" in str(store).lower() or str(store).startswith("C:\\\\Users")
    add("oauth_store_not_windows_path", not windows_like, str(store))

    for rel in ("state", "logs", "reports"):
        add(f"writable_{rel}", _writable(base / rel), str(base / rel))

    cfg = load_ai_config()
    cap = monthly_cap(cfg)
    add("ai_monthly_cap_10", float(cap) == 10.0, str(cap))
    add("live_discovery_wired", LIVE_DISCOVERY_WIRED is True, "LIVE_DISCOVERY_WIRED")

    if not connect:
        return checks

    reset_readonly_broker_runtime()
    bound = bootstrap_readonly_broker_runtime(transport=transport, environ=env, force=True)
    add("mcp_initialize", bound.bound is True, bound.initialization_error or "bound")
    if not bound.bound or bound.fetcher is None:
        add("get_accounts", False, bound.initialization_error or "unbound")
        add("agentic_account", False, "skipped")
        add("get_portfolio", False, "skipped")
        add("get_equity_positions", False, "skipped")
        add("get_equity_quotes", False, "skipped")
        add("snapshot_persists", False, "skipped")
        add("dashboard_reads_snapshot", False, "skipped")
        return checks

    fetcher = bound.fetcher
    for tool in WRITE_TOOLS:
        add(f"no_write_method_{tool}", not callable(getattr(fetcher, tool, None)), tool)

    expected = str(rules["account"]["account_number"])
    try:
        accounts = fetcher.get_accounts()
        account = confirm_agentic_account(accounts, expected_number=expected, rules=rules)
        add("get_accounts", True, expected)
        add("agentic_account", account.get("account_number") == expected and account.get("agentic_allowed") is True, expected)
    except Exception as exc:  # noqa: BLE001
        add("get_accounts", False, str(exc))
        add("agentic_account", False, str(exc))
        return checks

    try:
        refresh = refresh_live_portfolio(fetcher, root=base, persist=persist)
        add("get_portfolio", True, str(refresh.context.current_nav))
        add("get_equity_positions", True, str(refresh.context.holdings_count))
        add("get_equity_quotes", "get_equity_quotes" in refresh.tools_used, ",".join(refresh.tools_used))
        add("snapshot_persists", persist and LivePortfolioStore(base).current_book() is not None, refresh.snapshot_id)
        add("placement_disabled", refresh.placement_disabled is True, "true")
        add("no_write_tools_called", not any(t in WRITE_TOOLS for t in refresh.tools_used), ",".join(refresh.tools_used))
    except Exception as exc:  # noqa: BLE001
        add("get_portfolio", False, str(exc))
        add("get_equity_positions", False, getattr(exc, "code", "") or str(exc))
        add("get_equity_quotes", False, str(exc))
        add("snapshot_persists", False, str(exc))
        return checks

    prior = {k: os.environ.get(k) for k in ("AGENTIC_RUNTIME_MODE", "DASHBOARD_ENVIRONMENT")}
    os.environ["AGENTIC_RUNTIME_MODE"] = "LIVE"
    os.environ["DASHBOARD_ENVIRONMENT"] = "LIVE"
    try:
        view = dashboard_view(dashboard_state(base))
        add("dashboard_reads_snapshot", view.get("live_data_unavailable") is False and view.get("nav") is not None, str(view.get("nav")))
        add("dashboard_not_paper_fallback", view.get("nav") != 10000.0 and view.get("paper_environment") is False, str(view.get("nav")))
    except Exception as exc:  # noqa: BLE001
        add("dashboard_reads_snapshot", False, str(exc))
        add("dashboard_not_paper_fallback", False, str(exc))
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return checks


def render(checks: list[Check]) -> str:
    lines = ["Raspberry Pi production smoke (read-only, never places)", ""]
    for item in checks:
        mark = "PASS" if item.ok else "FAIL"
        extra = f"  {item.detail}" if item.detail else ""
        lines.append(f"{mark}  {item.name}{extra}")
    failed = sum(1 for item in checks if not item.ok)
    lines.append("")
    lines.append(f"{'PASS' if failed == 0 else 'FAIL'}  {len(checks) - failed}/{len(checks)} checks")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Pi production smoke test. Never places.")
    parser.add_argument("--no-connect", action="store_true", help="Skip MCP/OAuth live calls (local compile check)")
    args = parser.parse_args(argv)
    checks = run_smoke(connect=not args.no_connect)
    print(render(checks))
    return 0 if all(item.ok for item in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
