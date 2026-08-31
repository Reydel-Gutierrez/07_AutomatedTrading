"""Service control: start / stop / restart / status / health."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from agentic_portfolio.agent.lifecycle import start_argv, status, stop
from agentic_portfolio.paths import project_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Agentic Portfolio 24/7 service control. Never places orders.")
    parser.add_argument("action", choices=["start", "stop", "restart", "status", "health"])
    parser.add_argument("--no-dashboard", action="store_true")
    args = parser.parse_args()
    root = project_root()
    if args.action == "status" or args.action == "health":
        print(json.dumps(status(root), indent=2, default=str))
        return 0
    if args.action == "stop":
        print(json.dumps(stop(root), indent=2, default=str))
        return 0
    if args.action == "restart":
        stop(root)
    cmd = start_argv(no_dashboard=args.no_dashboard)
    subprocess.Popen(cmd, cwd=str(root))
    print(json.dumps({"ok": True, "started": cmd}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
