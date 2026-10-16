"""Where the Stock Swing Class keeps its state (separate from state/firm and state/forex)."""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def state_dir() -> Path:
    override = os.environ.get("EQUITY_STATE_ROOT", "").strip()
    path = Path(override) if override else project_root() / "state" / "equity_academy"
    path.mkdir(parents=True, exist_ok=True)
    return path


def academy_db() -> Path:
    return state_dir() / "academy.db"


def inbox_dir() -> Path:
    """Owner commands from HQ arrive here as JSON files (the engine is the only DB writer)."""
    path = state_dir() / "inbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def firm_db() -> Path:
    """The firm's database, read only: its daily bars are the class's market data."""
    override = os.environ.get("FIRM_STATE_ROOT", "").strip()
    return (Path(override) if override else project_root() / "state" / "firm") / "firm.db"
