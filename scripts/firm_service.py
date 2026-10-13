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


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def bind_host(environ, cfg) -> tuple[str, str | None]:
    """HQ binds loopback only; remote access goes through Cloudflare Access or an SSH tunnel.

    A legacy DASHBOARD_HOST pointing elsewhere is ignored (with a warning) rather
    than exposing HQ on the network or refusing to start.
    """
    host = str(cfg.get("host") or "127.0.0.1")
    if host not in LOOPBACK:
        host = "127.0.0.1"
    requested = str(environ.get("DASHBOARD_HOST") or "").strip()
    if requested and requested not in LOOPBACK:
        return host, f"DASHBOARD_HOST={requested} ignored: HQ binds {host} only (use Cloudflare Access or an SSH tunnel)"
    return (requested or host), None


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
    host, warning = bind_host(os.environ, cfg)
    if warning:
        logging.warning(warning)
        service.firm.events.emit("CONFIG_WARNING", warning, actor="system", severity="warning")
    stop = threading.Event()
    worker = threading.Thread(target=service.run_scheduler, args=(stop,), name="firm-scheduler", daemon=True)
    worker.start()
    forex = start_forex_engine(service)
    server = uvicorn.Server(uvicorn.Config(create_app(service), host=host, port=int(cfg["port"]), log_level="warning", proxy_headers=True))

    def shutdown(*_):
        stop.set()
        server.should_exit = True

    signal.signal(signal.SIGTERM, shutdown)
    server.run()
    stop.set()
    if forex:
        forex.stop()
    worker.join(timeout=15)
    return 0


def start_forex_engine(service):
    """The Forex engine runs as its own low-priority process (FIRM_FOREX_ENGINE=off disables it)."""
    if str(os.environ.get("FIRM_FOREX_ENGINE", "on")).lower() in ("off", "0", "false", "no"):
        return None
    from firm.sidecar import Sidecar

    root = Path(__file__).resolve().parents[1]
    sidecar = Sidecar("forex-engine", [sys.executable, str(root / "scripts" / "forex_engine.py")], cwd=str(root), environ=os.environ)
    sidecar.start()
    service.sidecars["forex-engine"] = sidecar
    return sidecar


if __name__ == "__main__":
    sys.exit(main())
