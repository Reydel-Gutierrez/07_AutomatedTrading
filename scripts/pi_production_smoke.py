"""Production smoke test for the firm. Read-only against Robinhood; never reviews or places orders.

  sudo -u agentic /opt/agentic-portfolio/.venv/bin/python scripts/pi_production_smoke.py

Lines are ``PASS|FAIL  <check>  <detail>`` (deploy_pi.py compares them before/after a
deploy) or ``INFO  <check>  <detail>`` (reported, never gating). Exit 1 on any FAIL.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

lines: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    lines.append(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}".rstrip())
    return ok


def info(name: str, detail: str) -> None:
    lines.append(f"INFO  {name}  {detail}")


def main() -> int:
    check("python_version", sys.version_info >= (3, 11), sys.version.split()[0])
    try:
        import importlib

        for mod in ("cryptography", "fastapi", "uvicorn"):
            importlib.import_module(mod)

        check("dependencies", True, "fastapi uvicorn cryptography")
    except ImportError as exc:
        check("dependencies", False, str(exc))

    from firm.broker import connect_reader, live_writes_enabled
    from firm.broker.errors import BrokerWriteRefused
    from firm.broker.mcp_auth import oauth_session_status
    from firm.config import account_number
    from firm.paths import db_path, state_root

    root = state_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
        check("state_writable", True, str(root))
    except OSError as exc:
        check("state_writable", False, str(exc))
    if db_path().exists():
        conn = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True)
        try:
            check("database_integrity", conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", str(db_path()))
            opened = conn.execute("SELECT COUNT(*) FROM ledger WHERE kind = 'OPENING_BALANCE'").fetchone()[0] > 0
            info("books_open", str(opened))
        finally:
            conn.close()
    else:
        info("database", "not created yet (books open at migration)")

    st = oauth_session_status()
    check("robinhood_oauth_session", st.present and (not st.expired or st.has_refresh_token), f"store {st.store_path}")
    try:
        reader = connect_reader(account_number())
        account = reader.confirm_account()
        check("broker_account_confirmed", True, f"••••{account_number()[-4:]} {account.get('type', '')}")
        facts = reader.portfolio()
        check("broker_portfolio", facts.nav > 0, f"cash ${facts.cash}")
        quote = reader.quotes(["VTI"]).get("VTI")
        check("broker_quotes", quote is not None and quote.last is not None, f"VTI {quote and quote.last}")
        check("robinhood_app_approval_off", not reader.trade_approval_required(), "agent orders are not held for in-app approval")
        try:
            reader.review({"symbol": "VTI"})
            check("reader_cannot_write", False, "review was allowed on the read client")
        except BrokerWriteRefused:
            check("reader_cannot_write", True, "write tools refused on the read client")
    except Exception as exc:  # noqa: BLE001
        check("broker_account_confirmed", False, f"{type(exc).__name__}: {exc}")
    info("live_order_placement", str(live_writes_enabled()).lower())
    from firm import backup

    info("backup_key", "present" if backup.key_path(os.environ).exists() else "missing (run firm_admin.py backup-init)")
    try:
        with urllib.request.urlopen("http://127.0.0.1:3100/healthz", timeout=5) as res:
            health = json.load(res)
        check("service_healthz", health.get("agent") == "ONLINE", json.dumps({k: health.get(k) for k in ("books_open", "kill_switch", "live_writes_enabled")}))
    except Exception as exc:  # noqa: BLE001
        check("service_healthz", False, f"{type(exc).__name__}: {exc}")

    failed = sum(1 for line in lines if line.startswith("FAIL"))
    total = sum(1 for line in lines if line.startswith(("PASS", "FAIL")))
    print("Firm production smoke (read-only, never places)\n")
    print("\n".join(lines))
    print(f"\n{'PASS' if not failed else 'FAIL'}  {total - failed}/{total} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
