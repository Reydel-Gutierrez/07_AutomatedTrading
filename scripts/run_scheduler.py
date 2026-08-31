"""Always-on scheduler entrypoint. Prefers the 24/7 Agent Runtime.

Legacy --job/--once flags still drive the internal orchestrator.
"""

from __future__ import annotations

import argparse
import sys

from agentic_portfolio.agent.orchestrator import JobOrchestrator
from agentic_portfolio.agent.runtime import AgentRuntime
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import bootstrap_readonly_broker_runtime


def main() -> int:
    parser = argparse.ArgumentParser(description="24/7 agent runtime / internal scheduler. Never places orders.")
    parser.add_argument("--once", action="store_true", help="Run due jobs once and exit")
    parser.add_argument("--job", default=None, help="Run one named job and exit")
    parser.add_argument("--sleep", type=int, default=30)
    args = parser.parse_args()
    bootstrap_readonly_broker_runtime()
    root = project_root()
    if args.job:
        row = JobOrchestrator(root).run_job(args.job)
        print(row)
        return 0 if row.get("status") in {"OK", "SKIPPED_ALREADY_RUNNING", "FAIL_CLOSED"} else 1
    if args.once:
        rows = AgentRuntime(root, max_cycles=1).cycle()
        print(rows)
        return 0
    AgentRuntime(root).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
