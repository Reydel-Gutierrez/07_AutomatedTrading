"""SQLite persistence. One file, WAL mode, versioned schema.

The service process is the only writer. Every write goes through
``Database.tx()``, which serializes writers with a process lock and wraps the
work in one IMMEDIATE transaction.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS managers (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('INACTIVE','SIM','LIVE','PAUSED')),
    mode_changed_at TEXT NOT NULL,
    mode_changed_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OK',
    status_detail TEXT
);

CREATE TABLE IF NOT EXISTS mandates (
    id INTEGER PRIMARY KEY,
    manager_id TEXT NOT NULL REFERENCES managers(id),
    version INTEGER NOT NULL,
    params_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    note TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (manager_id, version)
);

-- Append-only. bucket: 'firm' (unallocated), 'suspense', or a manager id.
-- cash and quantity are signed decimal strings: + into the bucket, - out.
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY,
    txn_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    book TEXT NOT NULL CHECK (book IN ('LIVE','SIM')),
    kind TEXT NOT NULL,
    bucket TEXT NOT NULL,
    symbol TEXT,
    quantity TEXT NOT NULL DEFAULT '0',
    cash TEXT NOT NULL DEFAULT '0',
    price TEXT,
    order_id INTEGER,
    execution_id TEXT,
    memo TEXT,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_book_bucket ON ledger (book, bucket);
CREATE UNIQUE INDEX IF NOT EXISTS ledger_execution ON ledger (book, execution_id, bucket) WHERE execution_id IS NOT NULL;

-- FIFO lots, maintained in the same transaction as the fill entries.
CREATE TABLE IF NOT EXISTS lots (
    id INTEGER PRIMARY KEY,
    book TEXT NOT NULL,
    manager_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    quantity TEXT NOT NULL,
    open_quantity TEXT NOT NULL,
    cost_basis TEXT NOT NULL,
    realized_pnl TEXT NOT NULL DEFAULT '0',
    source_ledger_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS lots_open ON lots (book, manager_id, symbol);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    client_ref TEXT NOT NULL UNIQUE,
    book TEXT NOT NULL,
    manager_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy','sell')),
    notional TEXT,
    quantity TEXT,
    order_type TEXT NOT NULL DEFAULT 'market',
    status TEXT NOT NULL,
    broker_order_id TEXT,
    broker_state TEXT,
    filled_quantity TEXT NOT NULL DEFAULT '0',
    average_price TEXT,
    fees TEXT NOT NULL DEFAULT '0',
    reason TEXT,
    decision_event_id INTEGER,
    block_reasons TEXT,
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    terminal_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_status ON orders (status);

CREATE TABLE IF NOT EXISTS broker_snaps (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    nav TEXT NOT NULL,
    cash TEXT NOT NULL,
    buying_power TEXT,
    pending_deposits TEXT,
    positions_json TEXT NOT NULL,
    open_orders INTEGER NOT NULL DEFAULT 0,
    reconciled INTEGER NOT NULL,
    diff_json TEXT
);

CREATE TABLE IF NOT EXISTS marks (
    date TEXT NOT NULL,
    book TEXT NOT NULL,
    bucket TEXT NOT NULL,          -- manager id or 'firm' for the whole firm
    nav TEXT NOT NULL,
    cash TEXT NOT NULL,
    invested TEXT NOT NULL,
    net_flow TEXT NOT NULL,        -- capital in(+)/out(-) of the bucket since the prior mark
    benchmark_close TEXT,
    PRIMARY KEY (date, book, bucket)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    manager_id TEXT,
    symbol TEXT,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_manager ON events (manager_id, id);
CREATE INDEX IF NOT EXISTS events_type ON events (type, id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_import (
    key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    sha256 TEXT,
    imported_at TEXT NOT NULL,
    summary_json TEXT NOT NULL
);

-- Phase 2: CIO office
CREATE TABLE IF NOT EXISTS cio_conversations (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT,
    summarized_through INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cio_messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES cio_conversations(id),
    role TEXT NOT NULL CHECK (role IN ('user','assistant','notice')),
    content TEXT NOT NULL,
    citations_json TEXT NOT NULL DEFAULT '[]',
    tools_json TEXT NOT NULL DEFAULT '[]',
    proposals_json TEXT NOT NULL DEFAULT '[]',
    flags_json TEXT NOT NULL DEFAULT '[]',
    model TEXT,
    cost TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cio_messages_conv ON cio_messages (conversation_id, id);
CREATE TABLE IF NOT EXISTS cio_memory (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    created_by TEXT NOT NULL,
    conversation_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- Structured proposals: the only way the CIO's advice becomes an action, and only on owner approval.
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('PENDING','APPLIED','REJECTED','EXPIRED','FAILED')),
    created_by TEXT NOT NULL,
    conversation_id INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT,
    result_json TEXT
);
CREATE INDEX IF NOT EXISTS proposals_status ON proposals (status, id);
CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    month TEXT NOT NULL,
    agent TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost TEXT NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ai_usage_month ON ai_usage (month, agent);

CREATE TABLE IF NOT EXISTS market_bars (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open TEXT, high TEXT, low TEXT,
    close TEXT NOT NULL,
    volume TEXT,
    PRIMARY KEY (symbol, date)
);
-- Alex's research desk: one row per AI due-diligence note (newest per symbol counts).
CREATE TABLE IF NOT EXISTS research_notes (
    id INTEGER PRIMARY KEY,
    manager_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('approve','reject')),
    conviction INTEGER NOT NULL,
    thesis TEXT NOT NULL,
    risks_json TEXT NOT NULL DEFAULT '[]',
    sources_json TEXT NOT NULL DEFAULT '[]',
    rank INTEGER,
    model TEXT,
    searches INTEGER NOT NULL DEFAULT 0,
    cost TEXT NOT NULL DEFAULT '0',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_notes_symbol ON research_notes (manager_id, symbol, id);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        with self._lock:
            # executescript manages its own transaction (it COMMITs first), so it
            # cannot run inside tx(). Every statement is idempotent.
            conn = self._connect()
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        try:
            os.chmod(self.path, 0o660)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One serialized write transaction. Nested calls join the outer one."""
        with self._lock:
            conn = self._connect()
            if conn.in_transaction:
                yield conn
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def read(self) -> sqlite3.Connection:
        return self._connect()

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return list(self.read().execute(sql, params))

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.read().execute(sql, params).fetchone()

    def backup_to(self, target: Path) -> None:
        """Consistent online copy (SQLite backup API)."""
        dest = sqlite3.connect(target)
        try:
            with self._lock:
                self._connect().backup(dest)
        finally:
            dest.close()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
