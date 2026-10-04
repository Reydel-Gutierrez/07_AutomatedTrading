"""Emergency kill switch CLI (works even if the dashboard is down).

    python scripts/kill_switch.py status
    python scripts/kill_switch.py engage --reason "something looks wrong"
    python scripts/kill_switch.py release --by reydel --note "reconciled; safe to resume"

Engaging stops every order submission (autonomous and human-approved) at the
executor. Releasing is always a named human action. If AGENTIC_KILL_SWITCH is
set in the service environment, only editing that environment releases it.
"""

from __future__ import annotations

import argparse
import json
import sys

from agentic_portfolio.autonomy.kill_switch import KillSwitch
from agentic_portfolio.autonomy.policy import load_execution_policy, resolve_state_root
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import get_active_runtime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    engage = sub.add_parser("engage")
    engage.add_argument("--reason", required=True)
    engage.add_argument("--by", default="cli")
    release = sub.add_parser("release")
    release.add_argument("--by", required=True, help="name of the human releasing the switch")
    release.add_argument("--note", required=True, help="why it is safe to resume")
    args = parser.parse_args()

    root = resolve_state_root(project_root(), runtime_mode=get_active_runtime())
    ks = KillSwitch(root)
    if args.cmd == "engage":
        status = ks.engage(args.reason, by=args.by)
    elif args.cmd == "release":
        try:
            status = ks.release(by=args.by, note=args.note)
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    else:
        status = ks.status()
    print(json.dumps({"state_root": str(root), "execution_mode": load_execution_policy().mode.value, "kill_switch": status.as_dict()}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
