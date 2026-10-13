"""Run the Forex engine (Academy paper trading on OANDA data). Started by the firm service as
a separate, low-priority process; can also be run by hand. Places no broker orders.

    python scripts/forex_engine.py            # run until stopped
    python scripts/forex_engine.py --once     # one tick (setup + a single loop pass)
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

from forex.academy import AcademyDB  # noqa: E402
from forex.engine import Engine  # noqa: E402
from forex.market import MarketStore  # noqa: E402
from forex.oanda import OandaClient, OandaConfigError, load_credentials  # noqa: E402
from forex.paths import academy_db, inbox_dir, marketdata_db, state_dir  # noqa: E402

EXIT_NOT_CONFIGURED = 3


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=10.0)
    args = ap.parse_args()
    handler = RotatingFileHandler(state_dir() / "engine.log", maxBytes=1_000_000, backupCount=3)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=[handler, logging.StreamHandler()])
    log = logging.getLogger("forex")
    try:
        client = OandaClient(load_credentials())
    except OandaConfigError as exc:
        log.error("OANDA not configured: %s", exc)
        return EXIT_NOT_CONFIGURED
    engine = Engine(client, AcademyDB(academy_db()), MarketStore(marketdata_db()), inbox_dir())
    if args.once:
        engine.start()
        engine.tick()
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("forex engine running (state %s)", state_dir())
    engine.run(stop, interval=args.interval)
    log.info("forex engine stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
