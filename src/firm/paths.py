"""Filesystem locations. The state root is overridable for tests and simulation."""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def state_root() -> Path:
    override = os.environ.get("FIRM_STATE_ROOT", "").strip()
    return Path(override) if override else project_root() / "state" / "firm"


def db_path() -> Path:
    return state_root() / "firm.db"
