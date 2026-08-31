"""Classify scripted/PAPER/duplicate LIVE artifacts without deleting history."""

from __future__ import annotations

import json
from pathlib import Path

from agentic_portfolio.live.classify import classify_non_production_artifacts
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import RuntimeMode


def main() -> None:
    root = project_root()
    payload = classify_non_production_artifacts(root, runtime_mode=RuntimeMode.LIVE)
    print(json.dumps({"count": payload["count"], "path": str(Path(root) / "state" / "live_ai" / "artifact_classification.json")}, indent=2))


if __name__ == "__main__":
    main()
