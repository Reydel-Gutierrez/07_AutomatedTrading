"""Deploy tooling: smoke parsing, regression detection, and the remote script contract."""

from __future__ import annotations

from pathlib import Path

from scripts.deploy_pi import parse_smoke, smoke_regressions

ROOT = Path(__file__).resolve().parents[1]

SMOKE = """Raspberry Pi production smoke (read-only, never places)

PASS  runtime_mode_LIVE  LIVE
FAIL  LIVE_ORDER_PLACEMENT_false  true
PASS  get_accounts  549688554

FAIL  2/3 checks
"""


def test_parse_smoke_ignores_header_and_summary():
    assert parse_smoke(SMOKE) == {
        "runtime_mode_LIVE": True,
        "LIVE_ORDER_PLACEMENT_false": False,
        "get_accounts": True,
    }


def test_regression_is_pass_to_fail_or_missing_only():
    before = {"a": True, "b": False, "c": True}
    after = {"a": False, "b": False}
    # "b" failed before too (configuration, not the release); "c" vanished.
    assert smoke_regressions(before, after) == ["a", "c"]
    assert smoke_regressions(before, {"a": True, "b": True, "c": True}) == []


def test_remote_script_never_syncs_runtime_state_or_reads_secrets():
    script = (ROOT / "deploy" / "pi_remote.sh").read_text(encoding="utf-8")
    for protected in ("/.venv/", "/state/", "/logs/", "/reports/", "/var/", "/DEPLOYED_COMMIT"):
        assert f"--exclude={protected}" in script
    assert "/etc/agentic-portfolio/env" not in script.replace("never reads /etc/agentic-portfolio/env", "")
    assert "AGENTIC_LIVE_ORDER_PLACEMENT" not in script
    assert "AGENTIC_EXECUTION_MODE" not in script
    assert b"\r\n" not in (ROOT / "deploy" / "pi_remote.sh").read_bytes()
