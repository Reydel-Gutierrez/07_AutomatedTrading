"""Owner/operator commands for the firm. Run on the Pi as the service user:

  sudo -u agentic /opt/agentic-portfolio/.venv/bin/python scripts/firm_admin.py <command>

Commands
  status                         health, books, managers, balances
  set-password [--username U]    create/replace the HQ admin (prompts)
  login-mcp [--status]           Robinhood MCP OAuth for this OS user
  migrate --stamp YYYYMMDD       archive legacy records, import history, open the books
  allocate <manager> <amount>    allocate unallocated cash (e.g. 909.00)
  mode <manager> <SIM|LIVE|PAUSED|INACTIVE> --note "..." [--confirm]
  kill-switch engage|release --note "..."
  backup-init                    create the backup encryption key (once)
  backup-show-key                print the key for your password manager (owner only)
  backup-now                     run an encrypted off-Pi backup with restore verification
  backup-restore <file> <out.db> decrypt a backup into a database file
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from firm import backup  # noqa: E402
from firm.money import D  # noqa: E402


def _service():
    from firm.runtime import Service

    return Service()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sp = sub.add_parser("set-password")
    sp.add_argument("--username", default="reydel")
    lg = sub.add_parser("login-mcp")
    lg.add_argument("--status", action="store_true")
    mg = sub.add_parser("migrate")
    mg.add_argument("--stamp", required=True)
    al = sub.add_parser("allocate")
    al.add_argument("manager")
    al.add_argument("amount")
    md = sub.add_parser("mode")
    md.add_argument("manager")
    md.add_argument("mode")
    md.add_argument("--note", required=True)
    md.add_argument("--confirm", action="store_true")
    ks = sub.add_parser("kill-switch")
    ks.add_argument("action", choices=["engage", "release"])
    ks.add_argument("--note", required=True)
    sub.add_parser("backup-init")
    sub.add_parser("backup-show-key")
    sub.add_parser("backup-now")
    br = sub.add_parser("backup-restore")
    br.add_argument("file")
    br.add_argument("out")
    args = p.parse_args(argv)
    who = os.environ.get("SUDO_USER") or getpass.getuser()

    if args.cmd == "login-mcp":
        from firm.broker import mcp_auth

        if args.status:
            st = mcp_auth.oauth_session_status()
            print(json.dumps({"store": str(st.store_path), "present": st.present, "expired": st.expired, "has_refresh_token": st.has_refresh_token}, indent=2))
            return 0
        print(f"saved: {mcp_auth.run_oauth_login()}")
        return 0
    if args.cmd == "backup-init":
        print(f"created {backup.create_key(os.environ)}. Now run backup-show-key and store the key in your password manager.")
        return 0
    if args.cmd == "backup-show-key":
        print(backup.key_path(os.environ).read_text().strip())
        return 0
    if args.cmd == "backup-restore":
        print(json.dumps(backup.restore_file(Path(args.file), Path(args.out), backup.load_key(os.environ)), indent=2))
        return 0

    svc = _service()
    firm = svc.firm
    if args.cmd == "status":
        print(json.dumps({"health": svc.health(), "balances": {k: str(v) for k, v in firm.ledger.balances("LIVE").items()}, "positions": {k: {s: str(q) for s, q in v.items()} for k, v in firm.ledger.holdings("LIVE").items()}}, indent=2, default=str))
    elif args.cmd == "set-password":
        from firm.api.auth import set_password

        pw = getpass.getpass("New HQ password (12+ chars): ")
        if pw != getpass.getpass("Repeat: "):
            print("passwords differ", file=sys.stderr)
            return 1
        set_password(firm.db, args.username, pw)
        firm.events.emit("ADMIN_PASSWORD_SET", f"HQ admin password set for {args.username}.", actor=who)
        print("ok")
    elif args.cmd == "migrate":
        from firm.migrate import archive_legacy, import_legacy

        archive = archive_legacy(stamp=args.stamp)
        print(json.dumps(archive, indent=2))
        import_legacy(firm, archive)
        print(json.dumps(firm.reconciler.bootstrap(svc.reader).__dict__, indent=2, default=str))
    elif args.cmd == "allocate":
        firm.allocate(args.manager, D(args.amount), by=who, note="initial allocation")
        print(json.dumps({k: str(v) for k, v in firm.ledger.balances("LIVE").items()}))
    elif args.cmd == "mode":
        if args.mode == "LIVE" and not args.confirm:
            print("switching to LIVE requires --confirm", file=sys.stderr)
            return 1
        firm.registry.set_mode(args.manager, args.mode, by=who, note=args.note)
        print("ok")
    elif args.cmd == "kill-switch":
        if args.action == "engage":
            firm.controls.engage(args.note, by=who)
        else:
            firm.controls.release(by=who, note=args.note)
        print(json.dumps(firm.controls.kill_switch().__dict__))
    elif args.cmd == "backup-now":
        print(json.dumps(backup.run_backup(firm), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
