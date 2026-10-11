"""Run the firm: scheduler thread + Investment HQ API (127.0.0.1:3100).

  python scripts/firm_service.py            # 24/7 service (systemd runs this via run_service.py)
  python scripts/firm_service.py --once     # one scheduler tick, then exit (smoke test)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from firm.config import firm_config  # noqa: E402
from firm.runtime import Service  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    service = Service()
    if args.once:
        started = service.scheduler.tick(wait=True)
        print({"started": started, "health": service.health()})
        service.scheduler.shutdown()
        return 0

    import uvicorn

    from firm.api.app import create_app

    cfg = firm_config()["dashboard"]
    host = os.environ.get("DASHBOARD_HOST") or cfg["host"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("refusing to bind HQ to a non-loopback address", file=sys.stderr)
        return 2
    stop = threading.Event()
    worker = threading.Thread(target=service.run_scheduler, args=(stop,), name="firm-scheduler", daemon=True)
    worker.start()
    server = uvicorn.Server(uvicorn.Config(create_app(service), host=host, port=int(cfg["port"]), log_level="warning", proxy_headers=True))

    def shutdown(*_):
        stop.set()
        server.should_exit = True

    signal.signal(signal.SIGTERM, shutdown)
    server.run()
    stop.set()
    worker.join(timeout=15)
    return 0


if __name__ == "__main__":
    sys.exit(main())
