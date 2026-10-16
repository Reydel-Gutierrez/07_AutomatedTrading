"""equity_academy/academy.db: the Stock Swing Class's agents, versions, validations, sprints,
and Floor 2 paper records. The class's engine is the only writer; HQ reads it read-only."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from equity.sim import CAPITAL, Envelope, Result

STAGES = ("RESEARCH", "PAPER", "RETIRED")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, strategy TEXT NOT NULL, persona_json TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT 'RESEARCH', stage_since TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE', status_reason TEXT,
  current_version INTEGER NOT NULL DEFAULT 1, trials INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
  agent_id TEXT NOT NULL, version INTEGER NOT NULL, params_json TEXT NOT NULL, envelope_json TEXT NOT NULL, note TEXT,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, parent INTEGER, validation_status TEXT NOT NULL DEFAULT 'PENDING',
  PRIMARY KEY (agent_id, version)
);
CREATE TABLE IF NOT EXISTS validations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, version INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, passed INTEGER, score REAL, report_json TEXT
);
CREATE TABLE IF NOT EXISTS sprints (
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, day TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  from_version INTEGER NOT NULL, to_version INTEGER, outcome TEXT, passed INTEGER, summary_json TEXT
);
CREATE INDEX IF NOT EXISTS sprints_day ON sprints (day, agent_id);
CREATE TABLE IF NOT EXISTS tested (agent_id TEXT NOT NULL, params_hash TEXT NOT NULL, first_seen TEXT NOT NULL, score REAL, PRIMARY KEY (agent_id, params_hash));
CREATE TABLE IF NOT EXISTS stage_transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, from_stage TEXT, to_stage TEXT NOT NULL, by TEXT NOT NULL, reason TEXT, ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, agent_id TEXT, type TEXT NOT NULL, summary TEXT NOT NULL, payload_json TEXT);
CREATE TABLE IF NOT EXISTS paper_trades (
  agent_id TEXT NOT NULL, version INTEGER NOT NULL, id TEXT NOT NULL, symbol TEXT NOT NULL, shares REAL NOT NULL, entry REAL NOT NULL,
  exit REAL NOT NULL, opened TEXT NOT NULL, closed TEXT NOT NULL, net_usd REAL NOT NULL, r REAL NOT NULL, exit_reason TEXT, PRIMARY KEY (agent_id, id)
);
CREATE TABLE IF NOT EXISTS paper_equity (agent_id TEXT NOT NULL, date TEXT NOT NULL, equity REAL NOT NULL, PRIMARY KEY (agent_id, date));
CREATE TABLE IF NOT EXISTS paper_positions (agent_id TEXT NOT NULL, symbol TEXT NOT NULL, shares REAL NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL, opened TEXT NOT NULL, PRIMARY KEY (agent_id, symbol));
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AcademyDB:
    def __init__(self, path: Path) -> None:
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def write(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self.lock, self.conn:
            return self.conn.execute(sql, tuple(args)).lastrowid or 0

    # -- agents and versions ----------------------------------------------------------------
    def seed(self, klass: list[dict[str, Any]], default_params) -> list[str]:
        created = []
        for a in klass:
            if self.one("SELECT 1 FROM agents WHERE id = ?", (a["id"],)):
                continue
            ts = now_iso()
            with self.lock, self.conn:
                self.conn.execute("INSERT INTO agents (id, name, strategy, persona_json, stage, stage_since, created_at) VALUES (?,?,?,?,?,?,?)",
                                  (a["id"], a["name"], a["strategy"], json.dumps(a["persona"]), "RESEARCH", ts, ts))
                self.conn.execute("INSERT INTO versions (agent_id, version, params_json, envelope_json, note, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                                  (a["id"], 1, json.dumps(default_params(a["strategy"])), json.dumps(a["envelope"].as_dict()), "founding version", "system", ts))
            self.event(a["id"], "AGENT_ENROLLED", f"{a['name']} enrolled in the Stock Swing Class (Floor 1, research).")
            created.append(a["id"])
        return created

    def agents(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM agents ORDER BY rowid")

    def agent(self, agent_id: str) -> sqlite3.Row:
        row = self.one("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if row is None:
            raise KeyError(agent_id)
        return row

    def version(self, agent_id: str, version: int | None = None) -> tuple[int, dict[str, Any], Envelope]:
        v = version or self.agent(agent_id)["current_version"]
        row = self.one("SELECT * FROM versions WHERE agent_id = ? AND version = ?", (agent_id, v))
        if row is None:
            raise KeyError(f"{agent_id} v{v}")
        return v, json.loads(row["params_json"]), Envelope.from_dict(json.loads(row["envelope_json"]))

    def add_version(self, agent_id: str, params: Mapping[str, Any], envelope: Envelope, *, note: str, by: str, validation_status: str = "PENDING") -> int:
        cur = self.agent(agent_id)["current_version"]
        new = (self.one("SELECT max(version) AS v FROM versions WHERE agent_id = ?", (agent_id,))["v"] or 0) + 1
        ts = now_iso()
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO versions (agent_id, version, params_json, envelope_json, note, created_by, created_at, parent, validation_status) VALUES (?,?,?,?,?,?,?,?,?)",
                              (agent_id, new, json.dumps(dict(params)), json.dumps(envelope.as_dict()), note, by, ts, cur, validation_status))
            self.conn.execute("UPDATE agents SET current_version = ? WHERE id = ?", (new, agent_id))
        return new

    def set_validation_status(self, agent_id: str, version: int, status: str) -> None:
        self.write("UPDATE versions SET validation_status = ? WHERE agent_id = ? AND version = ?", (status, agent_id, version))

    def set_status(self, agent_id: str, status: str, reason: str | None = None) -> None:
        self.write("UPDATE agents SET status = ?, status_reason = ? WHERE id = ?", (status, reason, agent_id))

    def add_trials(self, agent_id: str, n: int) -> None:
        self.write("UPDATE agents SET trials = trials + ? WHERE id = ?", (n, agent_id))

    def set_stage(self, agent_id: str, stage: str, *, by: str, reason: str) -> str:
        """Move between floors. Entering paper trading starts a fresh $10,000 record."""
        if stage not in STAGES:
            raise ValueError(f"stage must be one of {STAGES}")
        old = self.agent(agent_id)["stage"]
        ts = now_iso()
        with self.lock, self.conn:
            self.conn.execute("UPDATE agents SET stage = ?, stage_since = ? WHERE id = ?", (stage, ts, agent_id))
            self.conn.execute("INSERT INTO stage_transitions (agent_id, from_stage, to_stage, by, reason, ts) VALUES (?,?,?,?,?,?)", (agent_id, old, stage, by, reason, ts))
            for table in ("paper_trades", "paper_equity", "paper_positions"):
                self.conn.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent_id,))
        return old

    def tested_hashes(self, agent_id: str) -> set[str]:
        return {r["params_hash"] for r in self.q("SELECT params_hash FROM tested WHERE agent_id = ?", (agent_id,))}

    def mark_tested(self, agent_id: str, items: Iterable[tuple[str, float]]) -> None:
        ts = now_iso()
        with self.lock, self.conn:
            self.conn.executemany("INSERT OR IGNORE INTO tested VALUES (?,?,?,?)", [(agent_id, h, ts, sc) for h, sc in items])

    # -- sprints and validations ----------------------------------------------------------------
    def start_sprint(self, agent_id: str, day: str, version: int) -> int:
        return self.write("INSERT INTO sprints (agent_id, day, started_at, from_version) VALUES (?,?,?,?)", (agent_id, day, now_iso(), version))

    def finish_sprint(self, sid: int, *, outcome: str, to_version: int | None, passed: bool, summary: Mapping[str, Any]) -> None:
        self.write("UPDATE sprints SET finished_at=?, outcome=?, to_version=?, passed=?, summary_json=? WHERE id=?",
                   (now_iso(), outcome, to_version, 1 if passed else 0, json.dumps(summary, default=str), sid))

    def save_validation(self, agent_id: str, version: int, kind: str, report: Mapping[str, Any]) -> int:
        ts = now_iso()
        return self.write("INSERT INTO validations (agent_id, version, kind, status, started_at, finished_at, passed, score, report_json) VALUES (?,?,?,?,?,?,?,?,?)",
                          (agent_id, version, kind, report.get("status", "DONE"), ts, ts, 1 if report.get("passed") else 0, report.get("score"), json.dumps(report, default=str)))

    def close_unfinished(self) -> int:
        with self.lock, self.conn:
            return self.conn.execute("UPDATE sprints SET finished_at = ?, outcome = 'INTERRUPTED' WHERE finished_at IS NULL", (now_iso(),)).rowcount

    def latest_validation(self, agent_id: str) -> dict[str, Any] | None:
        row = self.one("SELECT * FROM validations WHERE agent_id = ? ORDER BY id DESC LIMIT 1", (agent_id,))
        return {**json.loads(row["report_json"]), "version": row["version"]} if row and row["report_json"] else None

    def sprints_since_pass(self, agent_id: str) -> int:
        rows = self.q("SELECT passed FROM sprints WHERE agent_id = ? AND finished_at IS NOT NULL AND outcome != 'INTERRUPTED' ORDER BY id DESC", (agent_id,))
        n = 0
        for r in rows:
            if r["passed"]:
                break
            n += 1
        return n

    # -- Floor 2 paper record (rebuilt from bars on every new day) ------------------------------
    def save_paper(self, agent_id: str, version: int, res: Result, dates: list[str], date_of) -> None:
        with self.lock, self.conn:
            for table in ("paper_trades", "paper_equity", "paper_positions"):
                self.conn.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent_id,))
            self.conn.executemany("INSERT INTO paper_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                (agent_id, version, t.id, t.instrument, t.shares, t.entry, t.exit, date_of(t.opened_at), date_of(t.closed_at), t.net_usd, t.r, t.exit_reason) for t in res.trades])
            self.conn.executemany("INSERT INTO paper_equity VALUES (?,?,?)", [(agent_id, date_of(ts), round(e, 2)) for ts, e in res.equity])
            self.conn.executemany("INSERT INTO paper_positions VALUES (?,?,?,?,?,?)", [
                (agent_id, p.symbol, p.shares, p.entry, p.stop, dates[p.opened]) for p in res.positions])

    # -- events and status ---------------------------------------------------------------------
    def event(self, agent_id: str | None, kind: str, summary: str, payload: Mapping[str, Any] | None = None) -> int:
        return self.write("INSERT INTO events (ts, agent_id, type, summary, payload_json) VALUES (?,?,?,?,?)", (now_iso(), agent_id, kind, summary, json.dumps(payload or {}, default=str)))

    def put(self, key: str, value: Any) -> None:
        self.write("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, json.dumps(value, default=str), now_iso()))

    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value_json FROM kv WHERE key = ?", (key,))
        return json.loads(row["value_json"]) if row else default


START_CAPITAL = CAPITAL
