"""Authenticate the standalone read-only Robinhood MCP transport.

Uses Robinhood's advertised OAuth 2.1 authorization-code + PKCE flow.
Does not accept a broker password, does not print tokens, and never calls
place/review/cancel. Tokens are stored outside the repository.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))

from agentic_portfolio.adapters.portfolio_facts import LiveDataUnavailable
from agentic_portfolio.adapters.readonly_mcp_auth import (
    DEFAULT_MCP_URL,
    LOGIN_HINT,
    oauth_session_status,
    oauth_store_path,
    run_oauth_login,
)
from agentic_portfolio.adapters.readonly_runtime import (
    bootstrap_readonly_broker_runtime,
    reset_readonly_broker_runtime,
    resolve_readonly_mcp_url,
)


def _print_status() -> int:
    status = oauth_session_status()
    print(f"store: {status.store_path}")
    if status.error:
        print("status: unavailable")
        print(status.error)
        return 1
    if not status.present:
        print("status: not authenticated")
        print(f"authenticate with: python scripts/login_readonly_mcp.py")
        return 1
    expiry = "expired" if status.expired else "valid"
    refresh = "yes" if status.has_refresh_token else "no"
    print(f"status: authenticated ({expiry})")
    print(f"refresh_token: {refresh}")
    print("token value is not printed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Authorize the local Python process for read-only Robinhood MCP access."
    )
    parser.add_argument("--status", action="store_true", help="Show whether a session exists. Does not print tokens.")
    parser.add_argument("--no-browser", action="store_true", help="Print the authorization URL instead of opening a browser.")
    args = parser.parse_args(argv)
    if args.status:
        return _print_status()
    url = resolve_readonly_mcp_url()
    printed: list[str] = []

    def capture_url(auth_url: str) -> None:
        printed.append(auth_url)
        print("Approve access in the desktop browser that opens.")
        print("Robinhood will collect the password on robinhood.com; this script never asks for it.")
        print(auth_url)
        if not args.no_browser:
            import webbrowser

            webbrowser.open(auth_url, new=1, autoraise=True)

    try:
        path = run_oauth_login(mcp_url=url or DEFAULT_MCP_URL, open_browser=capture_url)
    except LiveDataUnavailable as exc:
        print(str(exc))
        print(LOGIN_HINT)
        return 1
    reset_readonly_broker_runtime()
    runtime = bootstrap_readonly_broker_runtime(force=True)
    if not runtime.bound:
        print(runtime.initialization_error or "authorized Robinhood MCP transport is not bound")
        print(f"session file written but initialize failed: {path}")
        return 1
    print("read-only Robinhood MCP transport bound")
    print(f"session stored outside the repo at {oauth_store_path()}")
    print("this client will refuse place/review/cancel even though the MCP server advertises those tools")
    return 0


if __name__ == "__main__":
    sys.exit(main())
