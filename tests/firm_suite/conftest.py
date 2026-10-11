from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from firm.db import Database
from firm.events import EventLog
from firm.ledger import Ledger
from firm.settings import Settings


class FakeClock:
    """Settable UTC clock. Default: Tuesday 2026-10-13 11:00 ET (15:00 UTC), regular hours."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> datetime:
        self.now = self.now + timedelta(**kwargs)
        return self.now

    def set(self, dt: datetime) -> None:
        self.now = dt


@pytest.fixture(autouse=True)
def _no_live_env(monkeypatch, tmp_path):
    for name in ("AGENTIC_LIVE_ORDER_PLACEMENT", "AGENTIC_KILL_SWITCH", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FIRM_STATE_ROOT", str(tmp_path / "firm-state"))
    # Never let a test reach the real backup repository.
    monkeypatch.setattr("firm.backup._configured_remote", lambda: "")
    monkeypatch.setenv("AGENTIC_READONLY_MCP_HOME", str(tmp_path / "mcp-home"))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "firm.db")
    yield database
    database.close()


@pytest.fixture
def ledger(db, clock) -> Ledger:
    return Ledger(db, clock=clock)


@pytest.fixture
def events(db, clock) -> EventLog:
    return EventLog(db, clock=clock)


@pytest.fixture
def settings(db, clock) -> Settings:
    return Settings(db, clock=clock)
