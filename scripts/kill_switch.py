"""Emergency stop from a shell on the Pi (as the service user).

  python scripts/kill_switch.py engage --reason "..."
  python scripts/kill_switch.py release --note "..."
  python scripts/kill_switch.py status
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from firm.controls import Controls  # noqa: E402
from firm.db import Database  # noqa: E402
from firm.events import EventLog  # noqa: E402
from firm.paths import db_path  # noqa: E402
from firm.settings import Settings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["engage", "release", "status"])
    p.add_argument("--reason", default="engaged from the shell")
    p.add_argument("--note", default="")
    args = p.parse_args(argv)
    db = Database(db_path())
    settings = Settings(db)
    controls = Controls(settings, EventLog(db))
    who = os.environ.get("SUDO_USER") or getpass.getuser()
    if args.action == "engage":
        controls.engage(args.reason, by=who)
    elif args.action == "release":
        controls.release(by=who, note=args.note)
    print(json.dumps(controls.kill_switch().__dict__, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
