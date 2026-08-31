"""Run the complete 24/7 Agentic Portfolio application.

Starts the dashboard control room and the long-running agent runtime in one process.
Does not place orders. Normal operation does not require PowerShell after this starts.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from wsgiref.simple_server import make_server

from agentic_portfolio.agent.runtime import AgentRuntime
from agentic_portfolio.dashboard.app import create_app
from agentic_portfolio.dashboard.settings import resolve_bind
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import bootstrap_readonly_broker_runtime, get_active_runtime


def _serve_dashboard(app, host: str, port: int) -> None:
    httpd = make_server(host, port, app)
    httpd.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="24/7 Agentic Portfolio service. Never places orders.")
    parser.add_argument("--no-dashboard", action="store_true", help="Run the agent runtime without the dashboard thread")
    parser.add_argument("--cycles", type=int, default=None, help="Stop after N cycles (tests/dev only)")
    parser.add_argument("--once", action="store_true", help="Run one orchestration cycle and exit")
    args = parser.parse_args()

    root = project_root()
    bootstrap_readonly_broker_runtime()
    runtime = AgentRuntime(root, max_cycles=1 if args.once else args.cycles)

    if not args.no_dashboard:
        bind = resolve_bind()
        app = create_app(root)
        thread = threading.Thread(target=_serve_dashboard, args=(app, bind["host"], bind["port"]), daemon=True, name="dashboard")
        thread.start()
        print(
            f"Agentic Portfolio service http://{bind['host']}:{bind['port']} "
            f"runtime={get_active_runtime().value} LIVE_ORDER_PLACEMENT=false"
        )

    def _stop(signum, _frame) -> None:
        runtime.request_stop()

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    runtime.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
