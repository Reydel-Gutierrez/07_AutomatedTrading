"""Where the Forex engine keeps its state. Separate from the equity engine's state/firm."""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def state_dir() -> Path:
    override = os.environ.get("FOREX_STATE_ROOT", "").strip()
    path = Path(override) if override else project_root() / "state" / "forex"
    path.mkdir(parents=True, exist_ok=True)
    return path


def academy_db() -> Path:
    return state_dir() / "academy.db"


def marketdata_db() -> Path:
    return state_dir() / "marketdata.db"


def inbox_dir() -> Path:
    """Owner commands from HQ arrive here as JSON files (the engine is the only DB writer)."""
    path = state_dir() / "inbox"
    path.mkdir(parents=True, exist_ok=True)
    return path
