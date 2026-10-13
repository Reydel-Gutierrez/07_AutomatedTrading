"""academy.db: agents, versions, virtual accounts, positions, trades, equity, events,
validations, revisions, engine status, and latest prices. The Forex engine is the only
writer; HQ reads it read-only."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from forex.sim import Book, Position, RiskEnvelope, Trade

START_CAPITAL = 10_000.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, style TEXT NOT NULL, strategy TEXT NOT NULL, persona_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ACTIVE', status_reason TEXT, current_version INTEGER NOT NULL DEFAULT 1,
  auto_revise INTEGER NOT NULL DEFAULT 1, trials INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, last_revision_at TEXT
);
CREATE TABLE IF NOT EXISTS versions (
  agent_id TEXT NOT NULL, version INTEGER NOT NULL, params_json TEXT NOT NULL, envelope_json TEXT NOT NULL,
  note TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL, parent INTEGER, validation_status TEXT NOT NULL DEFAULT 'PENDING',
  PRIMARY KEY (agent_id, version)
);
CREATE TABLE IF NOT EXISTS accounts (
  agent_id TEXT PRIMARY KEY, cash REAL NOT NULL, start_capital REAL NOT NULL, peak_equity REAL NOT NULL, day_key TEXT,
  day_start_equity REAL, paused TEXT, last_financing_day TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS positions (
  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, version INTEGER NOT NULL, instrument TEXT NOT NULL, side INTEGER NOT NULL,
  units INTEGER NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL, target REAL, opened_at INTEGER NOT NULL,
  initial_risk REAL NOT NULL, risk_usd REAL NOT NULL, tag TEXT, max_hold_until INTEGER, financing_usd REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trades (
  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, version INTEGER NOT NULL, instrument TEXT NOT NULL, side INTEGER NOT NULL,
  units INTEGER NOT NULL, entry REAL NOT NULL, exit REAL NOT NULL, opened_at INTEGER NOT NULL, closed_at INTEGER NOT NULL,
  pnl_usd REAL NOT NULL, financing_usd REAL NOT NULL, r REAL NOT NULL, exit_reason TEXT, tag TEXT
);
CREATE INDEX IF NOT EXISTS trades_agent ON trades (agent_id, closed_at);
CREATE TABLE IF NOT EXISTS equity (agent_id TEXT NOT NULL, ts INTEGER NOT NULL, equity REAL NOT NULL, cash REAL NOT NULL, open_pnl REAL NOT NULL, PRIMARY KEY (agent_id, ts));
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, agent_id TEXT, type TEXT NOT NULL, summary TEXT NOT NULL, payload_json TEXT);
CREATE INDEX IF NOT EXISTS events_agent ON events (agent_id, id);
CREATE TABLE IF NOT EXISTS validations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, version INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, passed INTEGER, score REAL, report_json TEXT
);
CREATE TABLE IF NOT EXISTS revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, ts TEXT NOT NULL, from_version INTEGER NOT NULL, to_version INTEGER,
  outcome TEXT NOT NULL, chosen TEXT, reason TEXT, report_json TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prices (instrument TEXT PRIMARY KEY, bid REAL, ask REAL, time INTEGER, tradeable INTEGER, updated_at TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AcademyDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def write(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self.lock, self.conn:
            return self.conn.execute(sql, tuple(args)).lastrowid or 0

    # -- agents and versions -------------------------------------------------------------
    def seed(self, founding: list[dict[str, Any]], default_params) -> list[str]:
        """Create the founding agents (version 1, $10,000 each) if they do not exist."""
        created = []
        for a in founding:
            if self.one("SELECT 1 FROM agents WHERE id = ?", (a["id"],)):
                continue
            ts = now_iso()
            with self.lock, self.conn:
                self.conn.execute(
                    "INSERT INTO agents (id, name, style, strategy, persona_json, created_at) VALUES (?,?,?,?,?,?)",
                    (a["id"], a["name"], a["style"], a["strategy"], json.dumps(a["persona"]), ts),
                )
                self.conn.execute(
                    "INSERT INTO versions (agent_id, version, params_json, envelope_json, note, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                    (a["id"], 1, json.dumps(default_params(a["strategy"])), json.dumps(a["envelope"].as_dict()), "founding version", "system", ts),
                )
                self.conn.execute(
                    "INSERT INTO accounts (agent_id, cash, start_capital, peak_equity, day_start_equity, updated_at) VALUES (?,?,?,?,?,?)",
                    (a["id"], START_CAPITAL, START_CAPITAL, START_CAPITAL, START_CAPITAL, ts),
                )
            self.event(a["id"], "AGENT_ENROLLED", f"{a['name']} enrolled in the Academy with $10,000 of virtual capital.")
            created.append(a["id"])
        return created

    def agents(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM agents ORDER BY created_at, id")

    def agent(self, agent_id: str) -> sqlite3.Row:
        row = self.one("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if row is None:
            raise KeyError(agent_id)
        return row

    def version(self, agent_id: str, version: int | None = None) -> tuple[int, dict[str, Any], RiskEnvelope]:
        v = version or self.agent(agent_id)["current_version"]
        row = self.one("SELECT * FROM versions WHERE agent_id = ? AND version = ?", (agent_id, v))
        if row is None:
            raise KeyError(f"{agent_id} v{v}")
        return v, json.loads(row["params_json"]), RiskEnvelope.from_dict(json.loads(row["envelope_json"]))

    def add_version(self, agent_id: str, params: Mapping[str, Any], envelope: RiskEnvelope, *, note: str, by: str, validation_status: str = "PENDING") -> int:
        cur = self.agent(agent_id)["current_version"]
        new = (self.one("SELECT max(version) AS v FROM versions WHERE agent_id = ?", (agent_id,))["v"] or 0) + 1
        ts = now_iso()
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO versions (agent_id, version, params_json, envelope_json, note, created_by, created_at, parent, validation_status) VALUES (?,?,?,?,?,?,?,?,?)",
                (agent_id, new, json.dumps(dict(params)), json.dumps(envelope.as_dict()), note, by, ts, cur, validation_status),
            )
            self.conn.execute("UPDATE agents SET current_version = ?, last_revision_at = ? WHERE id = ?", (new, ts, agent_id))
        return new

    def set_validation_status(self, agent_id: str, version: int, status: str) -> None:
        self.write("UPDATE versions SET validation_status = ? WHERE agent_id = ? AND version = ?", (status, agent_id, version))

    def set_status(self, agent_id: str, status: str, reason: str | None = None) -> None:
        self.write("UPDATE agents SET status = ?, status_reason = ? WHERE id = ?", (status, reason, agent_id))

    def set_auto_revise(self, agent_id: str, on: bool) -> None:
        self.write("UPDATE agents SET auto_revise = ? WHERE id = ?", (1 if on else 0, agent_id))

    def add_trials(self, agent_id: str, n: int) -> None:
        self.write("UPDATE agents SET trials = trials + ? WHERE id = ?", (n, agent_id))

    # -- books ------------------------------------------------------------------------------
    def load_book(self, agent_id: str) -> Book:
        a = self.one("SELECT * FROM accounts WHERE agent_id = ?", (agent_id,))
        if a is None:
            raise KeyError(agent_id)
        book = Book(agent_id, a["cash"], a["start_capital"], a["peak_equity"], a["day_key"] or "", a["day_start_equity"] or a["cash"], a["paused"], last_financing_day=a["last_financing_day"] or "")
        for r in self.q("SELECT * FROM positions WHERE agent_id = ?", (agent_id,)):
            book.positions[r["id"]] = Position(
                r["id"], r["instrument"], r["side"], r["units"], r["entry"], r["stop"], r["target"], r["opened_at"], r["initial_risk"],
                r["risk_usd"], r["tag"] or "", r["version"], r["max_hold_until"], r["financing_usd"],
            )
        return book

    def save_book(self, book: Book) -> list[Trade]:
        """Persist the account, open positions, and any newly closed trades. Returns those trades."""
        closed, book.closed = book.closed, []
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE accounts SET cash=?, peak_equity=?, day_key=?, day_start_equity=?, paused=?, last_financing_day=?, updated_at=? WHERE agent_id=?",
                (book.cash, book.peak_equity, book.day_key, book.day_start_equity, book.paused, book.last_financing_day, now_iso(), book.agent_id),
            )
            ids = list(book.positions)
            if ids:
                self.conn.execute(f"DELETE FROM positions WHERE agent_id = ? AND id NOT IN ({','.join('?' * len(ids))})", (book.agent_id, *ids))
            else:
                self.conn.execute("DELETE FROM positions WHERE agent_id = ?", (book.agent_id,))
            for p in book.positions.values():
                self.conn.execute(
                    "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (p.id, book.agent_id, p.version, p.instrument, p.side, p.units, p.entry, p.stop, p.target, p.opened_at, p.initial_risk, p.risk_usd, p.tag, p.max_hold_until, p.financing_usd),
                )
            for t in closed:
                self.conn.execute(
                    "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t.id, book.agent_id, t.version, t.instrument, t.side, t.units, t.entry, t.exit, t.opened_at, t.closed_at, t.pnl_usd, t.financing_usd, t.r, t.exit_reason, t.tag),
                )
        return closed

    def trades(self, agent_id: str, *, since: int = 0) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM trades WHERE agent_id = ? AND closed_at >= ? ORDER BY closed_at", (agent_id, since))

    def mark(self, agent_id: str, ts: int, equity: float, cash: float) -> None:
        self.write("INSERT OR REPLACE INTO equity VALUES (?,?,?,?,?)", (agent_id, ts, round(equity, 4), round(cash, 4), round(equity - cash, 4)))

    # -- events, validations, revisions ---------------------------------------------------------
    def event(self, agent_id: str | None, kind: str, summary: str, payload: Mapping[str, Any] | None = None) -> int:
        return self.write("INSERT INTO events (ts, agent_id, type, summary, payload_json) VALUES (?,?,?,?,?)", (now_iso(), agent_id, kind, summary, json.dumps(payload or {}, default=str)))

    def start_validation(self, agent_id: str, version: int, kind: str) -> int:
        return self.write("INSERT INTO validations (agent_id, version, kind, status, started_at) VALUES (?,?,?,?,?)", (agent_id, version, kind, "RUNNING", now_iso()))

    def progress_validation(self, vid: int, status: str) -> None:
        self.write("UPDATE validations SET status = ? WHERE id = ?", (status[:120], vid))

    def finish_validation(self, vid: int, report: Mapping[str, Any]) -> None:
        self.write(
            "UPDATE validations SET status=?, finished_at=?, passed=?, score=?, report_json=? WHERE id=?",
            (report.get("status", "DONE"), now_iso(), 1 if report.get("passed") else 0, report.get("score"), json.dumps(report, default=str), vid),
        )

    def close_unfinished_validations(self, status: str) -> int:
        """Runs cut off by a restart or crash never finish on their own; close them so nothing shows them as running."""
        with self.lock, self.conn:
            return self.conn.execute("UPDATE validations SET status = ?, finished_at = ?, passed = 0 WHERE finished_at IS NULL", (status, now_iso())).rowcount

    def latest_validation(self, agent_id: str, version: int | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM validations WHERE agent_id = ? AND finished_at IS NOT NULL" + (" AND version = ?" if version else "") + " ORDER BY id DESC LIMIT 1"
        row = self.one(sql, (agent_id, version) if version else (agent_id,))
        if row is None or not row["report_json"]:
            return None
        return {**json.loads(row["report_json"]), "id": row["id"], "version": row["version"], "finished_at": row["finished_at"]}

    def add_revision(self, agent_id: str, from_version: int, to_version: int | None, outcome: str, chosen: str | None, reason: str, report: Mapping[str, Any]) -> int:
        slim = {k: v for k, v in report.items() if k != "validation"}
        return self.write(
            "INSERT INTO revisions (agent_id, ts, from_version, to_version, outcome, chosen, reason, report_json) VALUES (?,?,?,?,?,?,?,?)",
            (agent_id, now_iso(), from_version, to_version, outcome, chosen, reason, json.dumps(slim, default=str)),
        )

    # -- engine status and prices -----------------------------------------------------------------
    def put(self, key: str, value: Any) -> None:
        self.write("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, json.dumps(value, default=str), now_iso()))

    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value_json FROM kv WHERE key = ?", (key,))
        return json.loads(row["value_json"]) if row else default

    def put_prices(self, quotes: Mapping[str, Any], tradeable: Mapping[str, bool]) -> None:
        ts = now_iso()
        with self.lock, self.conn:
            for inst, qt in quotes.items():
                self.conn.execute("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?)", (inst, qt.bid, qt.ask, qt.t, 1 if tradeable.get(inst, True) else 0, ts))
