"""Raspberry Pi production unit, venv, secrets file, and localhost bind."""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

from agentic_portfolio.adapters.readonly_mcp_auth import oauth_home, oauth_store_path
from agentic_portfolio.dashboard.app import PUBLIC_ENDPOINTS, create_app
from agentic_portfolio.dashboard.settings import resolve_bind
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules, load_agent_config, load_dashboard_config
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, get_active_runtime


UNIT_PATH = project_root() / "deploy" / "systemd" / "agentic-portfolio.service"
DEPLOY_README = project_root() / "deploy" / "README.md"
ENV_EXAMPLE = project_root() / "deploy" / "env.example"
RUN_SERVICE = project_root() / "scripts" / "run_service.py"


def _parse_unit(text: str) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        parsed.setdefault(key.strip(), []).append(value.strip())
    return parsed


def _first(parsed: dict[str, list[str]], key: str) -> str:
    values = parsed.get(key) or []
    assert values, f"systemd unit missing {key}"
    return values[0]


def _readme() -> str:
    return DEPLOY_README.read_text(encoding="utf-8")


def test_systemd_runs_as_dedicated_non_root_user():
    text = UNIT_PATH.read_text(encoding="utf-8")
    parsed = _parse_unit(text)
    assert _first(parsed, "User") == "agentic"
    assert _first(parsed, "Group") == "agentic"
    assert "root" not in {v.lower() for v in parsed.get("User", [])}
    assert "root" not in {v.lower() for v in parsed.get("Group", [])}
    assert "Replace User=/Group=" in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not stripped.lower().startswith("protecthome="), stripped


def test_systemd_uses_venv_python_and_external_env_file():
    parsed = _parse_unit(UNIT_PATH.read_text(encoding="utf-8"))
    assert _first(parsed, "ExecStart") == "/opt/agentic-portfolio/.venv/bin/python scripts/run_service.py"
    assert _first(parsed, "EnvironmentFile") == "/etc/agentic-portfolio/env"
    assert _first(parsed, "WorkingDirectory") == "/opt/agentic-portfolio"
    assert "PYTHONPATH=src" in parsed.get("Environment", [])
    assert "AGENTIC_RUNTIME_MODE=LIVE" in parsed.get("Environment", [])
    assert "DASHBOARD_ENVIRONMENT=LIVE" in parsed.get("Environment", [])
    assert not any(item.startswith("OPENAI_API_KEY=") for item in parsed.get("Environment", []))


def test_systemd_unit_never_contains_openai_secret():
    text = UNIT_PATH.read_text(encoding="utf-8")
    parsed = _parse_unit(text)
    for values in parsed.values():
        for value in values:
            assert "sk-" not in value
            assert not value.startswith("OPENAI_API_KEY=")
    assert re.search(r"^OPENAI_API_KEY=", text, re.M) is None


def test_env_example_has_placeholders_not_secrets():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    keys: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        keys[name.strip()] = value.strip()
    assert keys["OPENAI_API_KEY"] == ""
    assert keys["AGENTIC_RUNTIME_MODE"] == "LIVE"
    assert keys["DASHBOARD_ENVIRONMENT"] == "LIVE"
    assert "sk-" not in text
    assert "chown root:agentic" in text
    assert "chmod 640" in text


def test_deploy_readme_documents_pi_hardening():
    text = _readme()
    required = [
        "User=agentic",
        "Group=agentic",
        "python3 -m venv",
        "/opt/agentic-portfolio/.venv",
        ".venv/bin/python -m pip install --upgrade pip",
        ".venv/bin/pip install -e .",
        "EnvironmentFile=/etc/agentic-portfolio/env",
        "sudo chown root:agentic /etc/agentic-portfolio/env",
        "sudo chmod 640 /etc/agentic-portfolio/env",
        "OPENAI_API_KEY",
        "AGENTIC_RUNTIME_MODE=LIVE",
        "DASHBOARD_ENVIRONMENT=LIVE",
        "sudo chown -R agentic:agentic",
        "state/",
        "logs/",
        "reports/",
        "sudo -u agentic",
        "scripts/login_readonly_mcp.py",
        "login_readonly_mcp.py --status",
        "127.0.0.1:3100",
        "ssh -L 3100:127.0.0.1:3100",
        "python3 -c",
        "sys.version_info >= (3, 11)",
        "LIVE_ORDER_PLACEMENT=false",
        "scripts/run_service.py --once",
        "sudo systemctl enable --now agentic-portfolio",
        "sudo systemctl status agentic-portfolio",
        "journalctl -u agentic-portfolio -f",
        "state/runtime/health.json",
        "/healthz",
        "sudo systemctl restart agentic-portfolio",
        "state/live_ai/watch/",
        "state/live_ai/approvals/",
        "state/ai_budget/",
        "auto_execution=false",
        "live_trade_actions_allowed=false",
        "Do **not** run the application as root",
    ]
    for snippet in required:
        assert snippet in text, snippet
    assert "python3 -m pip install -e ." not in text
    assert "/usr/bin/python3 scripts/run_service.py" not in text


def test_oauth_store_is_per_user_home_on_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agentic_portfolio.adapters.readonly_mcp_auth.Path.home",
        lambda *args: tmp_path,
    )
    home = oauth_home(environ={})
    store = oauth_store_path(environ={})
    assert home == tmp_path / ".agentic-portfolio" / "readonly-mcp"
    assert store == home / "oauth.json"
    xdg = oauth_home(environ={"XDG_STATE_HOME": str(tmp_path / "xdg")})
    assert xdg == tmp_path / "xdg" / "agentic-portfolio" / "readonly-mcp"
    override = oauth_home(environ={"AGENTIC_READONLY_MCP_HOME": str(tmp_path / "oauth-home")})
    assert override == tmp_path / "oauth-home"
    systemd = oauth_home(environ={"HOME": "/home/agentic", "USER": "agentic", "PATH": "/usr/bin"})
    assert systemd == Path("/home/agentic") / ".agentic-portfolio" / "readonly-mcp"
    stray_windows = oauth_home(
        environ={
            "HOME": "/home/agentic",
            "LOCALAPPDATA": "C:\\Users\\developer\\AppData\\Local",
            "USERPROFILE": "C:\\Users\\developer",
        }
    )
    if os.name != "nt":
        assert stray_windows == Path("/home/agentic") / ".agentic-portfolio" / "readonly-mcp"


def test_dashboard_stays_on_localhost():
    bind = resolve_bind(environ={})
    assert bind["host"] == "127.0.0.1"
    assert bind["port"] == 3100
    assert bind["public_exposure"] is False
    dash = load_dashboard_config()
    assert dash["host"] == "127.0.0.1"
    assert dash["port"] == 3100
    notes = str((dash.get("raspberry_pi") or {}).get("notes") or "")
    assert "127.0.0.1:3100" in notes
    assert "ssh -L 3100:127.0.0.1:3100" in notes
    assert "Do not expose the origin publicly" in notes


def test_healthz_is_public_and_reports_placement_disabled(tmp_path):
    health_dir = tmp_path / "state" / "runtime"
    health_dir.mkdir(parents=True)
    (health_dir / "health.json").write_text(
        json.dumps(
            {
                "agent": "ONLINE",
                "runtime_mode": "LIVE",
                "LIVE_ORDER_PLACEMENT": False,
                "auto_execution": False,
                "live_trade_actions_allowed": False,
            }
        ),
        encoding="utf-8",
    )
    assert "healthz" in PUBLIC_ENDPOINTS
    client = create_app(tmp_path).test_client()
    res = client.get("/healthz")
    assert res.status_code == 200
    payload = res.get_json()
    assert payload["agent"] == "ONLINE"
    assert payload["runtime_mode"] == "LIVE"
    assert payload["live_order_placement_enabled"] is False
    assert payload["LIVE_ORDER_PLACEMENT"] is False


def test_pi_smoke_script_is_read_only():
    path = project_root() / "scripts" / "pi_production_smoke.py"
    text = path.read_text(encoding="utf-8")
    assert path.is_file()
    assert "Never places" in text
    assert "LIVE_ORDER_PLACEMENT" in text
    assert "sudo -u agentic" in text
    assert "place_equity_order(" not in text
    assert "review_equity_order(" not in text
    assert "cancel_equity_order(" not in text
    source = RUN_SERVICE.read_text(encoding="utf-8")
    assert "ai_call_fn" not in source
    assert "OPENAI_API_KEY" not in source
    assert "--once" in source
    assert "Does not wire paid AI" in source
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    names = []
    for node in calls:
        func = node.func
        if isinstance(func, ast.Name):
            names.append(func.id)
        elif isinstance(func, ast.Attribute):
            names.append(func.attr)
    assert "AgentRuntime" in names
    assert "OpenAIProvider" not in names


def test_execution_safety_unchanged_for_live_pi():
    exe = dict(load_account_rules().get("execution") or {})
    agent = load_agent_config()
    invariants = dict(agent.get("invariants") or {})
    assert exe.get("auto_execution") is False
    assert exe.get("live_trade_actions_allowed") is False
    assert invariants.get("auto_execution") is False
    assert invariants.get("live_trade_actions_allowed") is False
    assert invariants.get("LIVE_ORDER_PLACEMENT") is False
    assert LIVE_ORDER_PLACEMENT is False
    assert get_active_runtime(environ={"AGENTIC_RUNTIME_MODE": "LIVE"}).value == "LIVE"
    assert get_active_runtime(environ={"DASHBOARD_ENVIRONMENT": "LIVE"}).value == "LIVE"
