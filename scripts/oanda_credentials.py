"""Store or inspect the OANDA credentials for the Forex engine. Never prints the token.

On the Pi, run as the service account so the file belongs to it:

    sudo -u agentic /opt/agentic-portfolio/.venv/bin/python /opt/agentic-portfolio/scripts/oanda_credentials.py set
    sudo -u agentic /opt/agentic-portfolio/.venv/bin/python /opt/agentic-portfolio/scripts/oanda_credentials.py status
    sudo -u agentic /opt/agentic-portfolio/.venv/bin/python /opt/agentic-portfolio/scripts/oanda_credentials.py set-practice

`set` prompts for the token without echo (it is not on the command line, in shell
history, or in the process list), writes ~/.agentic-portfolio/forex/oanda.env
atomically with mode 0600 inside a 0700 directory, then makes one read-only call to
confirm the token sees the account. This file is separate from the equity engine's
/etc/agentic-portfolio/env.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from forex.oanda import DEFAULT_ENV_FILE, OandaClient, OandaConfigError, OandaError, check_private, load_credentials  # noqa: E402

TOKEN_RE = re.compile(r"^[0-9a-f]{32}-[0-9a-f]{32}$")
ACCOUNT_RE = re.compile(r"^\d{3}-\d{3}-\d+-\d{3}$")


def write_env(path: Path, *, env: str, token: str, account_id: str) -> None:
    """Atomically write the credentials file, owner-only, in an owner-only directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, f"OANDA_ENV={env}\nOANDA_API_TOKEN={token}\nOANDA_ACCOUNT_ID={account_id}\n".encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _ask_token() -> str:
    if not sys.stdin.isatty():
        raise SystemExit("refusing: run this from an interactive terminal so the token is not echoed or piped")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)  # never fall back to an echoing prompt
        return getpass.getpass("OANDA personal access token (input hidden): ").strip()


def cmd_set(args: argparse.Namespace) -> int:
    path = Path(args.file)
    env = args.env
    account = input("OANDA v20 account ID (e.g. 001-001-1234567-001): ").strip()
    if not ACCOUNT_RE.match(account):
        print("That does not look like a v20 account ID (###-###-#######-###).", file=sys.stderr)
        return 2
    token = _ask_token()
    if not TOKEN_RE.match(token):
        print(f"Warning: the token is {len(token)} characters and not in OANDA's usual 32hex-32hex format.")
        if input("Store it anyway? [y/N] ").strip().lower() != "y":
            return 2
    write_env(path, env=env, token=token, account_id=account)
    del token
    print(f"Stored {env} credentials in {path} (mode 600). The token was not displayed.")
    return verify(path)


def cmd_set_practice(args: argparse.Namespace) -> int:
    """The Demo Desk's practice credentials and its two seat sub-accounts, in their own file."""
    from forex.demo import DEFAULT_PRACTICE_FILE

    path = Path(args.file if args.file != str(DEFAULT_ENV_FILE) else DEFAULT_PRACTICE_FILE)
    seats = []
    for i in (1, 2):
        acct = input(f"Practice sub-account ID for Demo seat {i} (e.g. 101-001-1234567-00{i}): ").strip()
        if not ACCOUNT_RE.match(acct):
            print("That does not look like a v20 account ID (###-###-#######-###).", file=sys.stderr)
            return 2
        seats.append(acct)
    if seats[0] == seats[1]:
        print("Each seat needs its own sub-account (US FIFO and no-hedging rules apply per account).", file=sys.stderr)
        return 2
    token = _ask_token()
    write_env(path, env="practice", token=token, account_id=seats[0])
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"DEMO_ACCOUNT_1={seats[0]}\nDEMO_ACCOUNT_2={seats[1]}\n")
    del token
    print(f"Stored practice credentials and 2 Demo Desk seats in {path} (mode 600). The token was not displayed.")
    creds = load_credentials({"OANDA_ENV_FILE": str(path)})
    if creds.env != "practice":
        print("Refusing: this file must be a practice account.", file=sys.stderr)
        return 1
    return verify(path)


def verify(path: Path) -> int:
    try:
        creds = load_credentials({"OANDA_ENV_FILE": str(path)})
        client = OandaClient(creds)
        accounts = [a["id"] for a in client.accounts()]
        client.account_id  # checks the configured account is visible to the token
    except (OandaConfigError, OandaError) as exc:
        print(f"Verification FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"Verified read-only: token works on {creds.env}, sees {len(accounts)} account(s), including {creds.account_id}.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"No credentials file at {path}.")
        return 1
    st = path.stat()
    print(f"{path}: mode {oct(st.st_mode & 0o777)}, owner uid {st.st_uid}, {st.st_size} bytes")
    try:
        check_private(path)
        creds = load_credentials({"OANDA_ENV_FILE": str(path)})
    except OandaConfigError as exc:
        print(f"Problem: {exc}")
        return 1
    print(f"environment: {creds.env} | account: {creds.account_id or '(not set)'} | token: present ({len(creds.token)} chars, not shown)")
    return verify(path) if args.verify else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Store or inspect OANDA credentials (never prints the token).")
    ap.add_argument("--file", default=str(DEFAULT_ENV_FILE))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="prompt for and store credentials, then verify read-only")
    s.add_argument("--env", choices=["live", "practice"], default="live")
    s.set_defaults(func=cmd_set)
    sp = sub.add_parser("set-practice", help="store the Demo Desk's OANDA practice token and two seat sub-accounts")
    sp.set_defaults(func=cmd_set_practice)
    st = sub.add_parser("status", help="show where the credentials are and whether they are well-formed")
    st.add_argument("--verify", action="store_true", help="also make one read-only call to OANDA")
    st.set_defaults(func=cmd_status)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
