"""LIVE-only CORE committee reevaluation.

Uses existing fresh ADVANCE_TO_THESIS artifacts and current LIVE portfolio
context. Does not rediscover, collect research, force BUYs, bypass Risk Gate,
or place orders.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from agentic_portfolio.ai.gateway import build_gateway
from agentic_portfolio.ai.reasoners import GatewayDecisionReasoner
from agentic_portfolio.decision.committee import reevaluate_live_core_committee
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import RuntimeMode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(project_root()))
    args = parser.parse_args()
    root = Path(args.root)
    gateway = build_gateway(root, runtime_mode=RuntimeMode.LIVE, now_fn=lambda: datetime.now(timezone.utc))
    result = reevaluate_live_core_committee(
        root=root,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=GatewayDecisionReasoner(gateway),
        now=datetime.now(timezone.utc),
        persist=True,
        force=True,
    )
    print(json.dumps(result.as_dict(), indent=2, default=str))
    return 0 if result.status not in {"DEGRADED", "FAILED", "BLOCKED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
