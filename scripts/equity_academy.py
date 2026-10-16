"""Run the Stock Swing Class engine (equity Academy research and paper records on the firm's
daily bars). Started by the firm service as a separate, low-priority process; can also be run
by hand. Places no orders and talks to no broker.

    python scripts/equity_academy.py            # run until stopped
    python scripts/equity_academy.py --once     # setup + a single loop pass
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from equity.academy import AcademyDB  # noqa: E402
from equity.engine import Engine  # noqa: E402
from equity.paths import academy_db, firm_db, inbox_dir, state_dir  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=30.0)
    args = ap.parse_args()
    handler = RotatingFileHandler(state_dir() / "engine.log", maxBytes=1_000_000, backupCount=3)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=[handler, logging.StreamHandler()])
    engine = Engine(AcademyDB(academy_db()), firm_db(), inbox_dir())
    if args.once:
        engine.start()
        engine.tick()
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    logging.getLogger("equity").info("equity academy engine running (state %s)", state_dir())
    engine.run(stop, interval=args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
