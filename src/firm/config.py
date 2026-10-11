"""Static identity and binding (config/firm.json). Owner-tunable settings live in the database."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from firm.paths import project_root


@lru_cache(maxsize=1)
def firm_config() -> dict[str, Any]:
    return json.loads((project_root() / "config" / "firm.json").read_text(encoding="utf-8"))


def account_number() -> str:
    return str(firm_config()["account_number"])


def deployed_commit() -> dict[str, Any]:
    path = project_root() / "DEPLOYED_COMMIT"
    if not path.is_file():
        return {"sha": "development"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: data.get(k) for k in ("sha", "subject", "deployed_at")}
    except (OSError, json.JSONDecodeError):
        return {"sha": "unknown"}
