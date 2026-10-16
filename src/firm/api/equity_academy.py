"""HQ's read-only window into the Stock Swing Class (equity_academy/academy.db, written by its
own engine). Like the forex Academy, the firm never imports the engine: it reads the SQLite file
read-only and drops owner commands into the engine's inbox as JSON files. Every number comes from
stored records: validation reports, sprint results, and the Floor 2 paper record.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from firm.paths import state_root

ACTIONS = {"sprint", "pause", "resume", "to_research", "retire"}
START_CAPITAL = 10_000.0
STALE_SECONDS = 180
# Floor 2: at least four weeks (20 sessions) and enough trades before the owner judges a live seat
QUALIFY = {"sessions": 20, "trades": 10}
FLOORS = {"RESEARCH": "Floor 1 · Research", "PAPER": "Floor 2 · Paper trading", "RETIRED": "Alumni"}


def state_dir() -> Path:
    override = os.environ.get("EQUITY_STATE_ROOT", "").strip()
    return Path(override) if override else state_root().parent / "equity_academy"


def _connect() -> sqlite3.Connection | None:
    path = state_dir() / "academy.db"
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _kv(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value_json FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value_json"]) if row else default


def wanted_symbols() -> list[str]:
    conn = _connect()
    if conn is None:
        return []
    with closing(conn):
        return [s for s in (_kv(conn, "wanted_symbols") or []) if isinstance(s, str) and s.isalpha() and s.isupper()]


def _engine(conn: sqlite3.Connection) -> dict[str, Any]:
    e = _kv(conn, "engine", {}) or {}
    e["online"] = bool(e.get("heartbeat") and time.time() - e["heartbeat"] < STALE_SECONDS)
    d = _kv(conn, "data", {}) or {}
    e["data"] = {k: d.get(k) for k in ("ready", "reason", "first_date", "last_date", "sessions")}
    return e


def _desk_return(firm: Any, since: str) -> float | None:
    """Max's desk over the same days: compounded daily returns after `since` (flows excluded)."""
    if firm is None:
        return None
    try:
        reg = firm.registry.get("max")
        marks = [m for m in firm.performance.marks(reg.book or "SIM", "max") if m.date >= since]
    except Exception:  # noqa: BLE001 - the comparison is context, never a reason to fail
        return None
    if len(marks) < 2:
        return None
    g = 1.0
    for prev, m in zip(marks, marks[1:]):
        if prev.nav > 0:
            g *= 1 + float((m.nav - m.net_flow) / prev.nav - 1)
    return g - 1


def _paper(conn: sqlite3.Connection, agent_id: str, firm: Any) -> dict[str, Any] | None:
    eq = conn.execute("SELECT date, equity FROM paper_equity WHERE agent_id = ? ORDER BY date", (agent_id,)).fetchall()
    if not eq:
        return None
    trades = conn.execute("SELECT * FROM paper_trades WHERE agent_id = ? ORDER BY closed", (agent_id,)).fetchall()
    first, last = eq[0]["date"], eq[-1]
    wins = [t["net_usd"] for t in trades if t["net_usd"] > 0]
    losses = [-t["net_usd"] for t in trades if t["net_usd"] < 0]
    ret = last["equity"] / START_CAPITAL - 1
    desk = _desk_return(firm, first)
    weeks: dict[str, float] = {}
    prev = START_CAPITAL
    for r in eq:
        wk = date.fromisoformat(r["date"]).strftime("%G-W%V")
        weeks.setdefault(wk, prev)
        prev = r["equity"]
    week_rows = []
    keys = list(weeks)
    for i, wk in enumerate(keys):
        end = weeks[keys[i + 1]] if i + 1 < len(keys) else last["equity"]
        week_rows.append({"week": wk, "return": end / weeks[wk] - 1})
    sessions = len(eq) - 1
    return {
        "since": first, "through": last["date"], "sessions": sessions, "equity": round(last["equity"], 2), "return": ret,
        "trades": len(trades), "win_rate": len(wins) / len(trades) if trades else None,
        "profit_factor": sum(wins) / sum(losses) if losses else None, "avg_r": sum(t["r"] for t in trades) / len(trades) if trades else None,
        "max_desk_return": desk, "vs_max": (ret - desk) if desk is not None else None, "weeks": week_rows,
        "profitable_weeks": sum(1 for w in week_rows if w["return"] > 0),
        "qualified": sessions >= QUALIFY["sessions"] and len(trades) >= QUALIFY["trades"],
        "positions": [dict(r) for r in conn.execute("SELECT symbol, shares, entry, stop, opened FROM paper_positions WHERE agent_id = ? ORDER BY symbol", (agent_id,))],
    }


def _card(conn: sqlite3.Connection, a: sqlite3.Row, firm: Any, eng: dict[str, Any]) -> dict[str, Any]:
    v = conn.execute("SELECT params_json, envelope_json, validation_status FROM versions WHERE agent_id = ? AND version = ?", (a["id"], a["current_version"])).fetchone()
    params = json.loads(v["params_json"]) if v else {}
    val = conn.execute("SELECT report_json, version, finished_at FROM validations WHERE agent_id = ? ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
    rep = json.loads(val["report_json"]) if val and val["report_json"] else None
    sp = conn.execute("SELECT day, outcome, summary_json FROM sprints WHERE agent_id = ? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
    sprint_kv = _kv(conn, f"sprint:{a['id']}", {}) or {}
    unseen = ((rep or {}).get("metrics") or {}).get("unseen") or {}
    return {
        "id": a["id"], "name": a["name"], "persona": json.loads(a["persona_json"]), "strategy": a["strategy"], "stage": a["stage"],
        "floor": FLOORS.get(a["stage"], a["stage"]), "stage_since": a["stage_since"], "status": a["status"], "version": a["current_version"],
        "trials": a["trials"], "symbols": params.get("symbols") or [], "params": {k: x for k, x in params.items() if k != "symbols"},
        "validation": {"status": rep.get("status"), "version": val["version"], "at": val["finished_at"], "checks": rep.get("checks"),
                       "unseen": {k: unseen.get(k) for k in ("trades", "avg_r", "return_pct", "sharpe", "max_drawdown", "win_rate")},
                       "day1": {k: (rep.get("day1") or {}).get(k) for k in ("passed", "profitable_share", "median_return", "p10_return")}} if rep else None,
        "last_sprint": {"day": sp["day"], "outcome": sp["outcome"], "verdict": json.loads(sp["summary_json"] or "{}").get("verdict")} if sp else None,
        "sprinting": sprint_kv.get("step") if sprint_kv.get("running") else None,
        "activity": ((eng.get("activity") or {}).get(a["id"])) or ("paper trading" if a["stage"] == "PAPER" else "researching" if a["stage"] == "RESEARCH" else "retired"),
        "paper": _paper(conn, a["id"], firm) if a["stage"] == "PAPER" else None,
    }


def overview(firm: Any = None) -> dict[str, Any]:
    conn = _connect()
    if conn is None:
        return {"available": False, "reason": "The Stock Swing Class engine has not started yet.", "agents": []}
    with closing(conn):
        eng = _engine(conn)
        agents = [_card(conn, a, firm, eng) for a in conn.execute("SELECT * FROM agents ORDER BY rowid")]
        events = [dict(r) for r in conn.execute("SELECT id, ts, agent_id, type, summary FROM events ORDER BY id DESC LIMIT 30")]
        latest = _kv(conn, "sprint_report_latest")
    return {"available": True, "engine": {k: eng.get(k) for k in ("online", "heartbeat", "job", "queue", "last_error", "data_ready", "data")},
            "agents": agents, "events": events, "latest_report": latest, "qualify": QUALIFY}


def agent(agent_id: str, firm: Any = None) -> dict[str, Any] | None:
    conn = _connect()
    if conn is None:
        return None
    with closing(conn):
        a = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if a is None:
            return None
        eng = _engine(conn)
        card = _card(conn, a, firm, eng)
        val = conn.execute("SELECT report_json FROM validations WHERE agent_id = ? ORDER BY id DESC LIMIT 1", (agent_id,)).fetchone()
        rep = json.loads(val["report_json"]) if val and val["report_json"] else {}
        sprints = [{"day": r["day"], "outcome": r["outcome"], "from": r["from_version"], "to": r["to_version"],
                    "verdict": json.loads(r["summary_json"] or "{}").get("verdict")}
                   for r in conn.execute("SELECT * FROM sprints WHERE agent_id = ? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 30", (agent_id,))]
        trades = [dict(r) for r in conn.execute("SELECT symbol, shares, entry, exit, opened, closed, net_usd, r, exit_reason FROM paper_trades WHERE agent_id = ? ORDER BY closed DESC LIMIT 100", (agent_id,))]
        equity = [[r["date"], r["equity"]] for r in conn.execute("SELECT date, equity FROM paper_equity WHERE agent_id = ? ORDER BY date", (agent_id,))]
        events = [dict(r) for r in conn.execute("SELECT id, ts, type, summary FROM events WHERE agent_id = ? ORDER BY id DESC LIMIT 30", (agent_id,))]
    return {**card, "backtest": {"equity": rep.get("equity"), "metrics": rep.get("metrics"), "windows": rep.get("windows"), "by_symbol": rep.get("by_symbol"),
                                 "baseline": rep.get("baseline"), "refused": rep.get("refused")},
            "sprints": sprints, "paper_trades": trades, "paper_equity": equity, "events": events}


def space_block() -> dict[str, Any]:
    """Compact facts for the Firm map's Stock Swing Class room (cheap; never raises)."""
    try:
        conn = _connect()
        if conn is None:
            return {"available": False}
        with closing(conn):
            eng = _engine(conn)
            out = []
            for a in conn.execute("SELECT * FROM agents ORDER BY rowid"):
                last = conn.execute("SELECT equity FROM paper_equity WHERE agent_id = ? ORDER BY date DESC LIMIT 1", (a["id"],)).fetchone()
                sp = conn.execute("SELECT day, outcome FROM sprints WHERE agent_id = ? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
                kv = _kv(conn, f"sprint:{a['id']}", {}) or {}
                eq = last["equity"] if last else START_CAPITAL
                out.append({"id": a["id"], "name": a["name"], "persona": json.loads(a["persona_json"]), "stage": a["stage"], "status": a["status"],
                            "version": a["current_version"], "equity": round(eq, 2), "return_pct": eq / START_CAPITAL - 1,
                            "sprinting": kv.get("step") if kv.get("running") else None,
                            "last_sprint": {"day": sp["day"], "outcome": sp["outcome"]} if sp else None,
                            "activity": (eng.get("activity") or {}).get(a["id"])})
        return {"available": True, "online": eng.get("online"), "data_ready": eng.get("data_ready"), "agents": out}
    except sqlite3.Error as exc:
        return {"available": False, "error": str(exc)}


def send_command(agent_id: str, action: str, *, by: str, note: str = "") -> None:
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {sorted(ACTIONS)}")
    conn = _connect()
    if conn is None:
        raise LookupError("the Stock Swing Class is not running yet")
    with closing(conn):
        if conn.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone() is None:
            raise LookupError(f"no agent {agent_id}")
    inbox = state_dir() / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"
    tmp = inbox / f".{name}.tmp"
    tmp.write_text(json.dumps({"agent_id": agent_id, "action": action, "by": by, "note": note[:200]}), encoding="utf-8")
    tmp.replace(inbox / f"{name}.json")


def morning_line(day: str) -> str | None:
    """One sentence for the 08:00 message, or None when there is nothing to say."""
    conn = _connect()
    if conn is None:
        return None
    with closing(conn):
        rep = _kv(conn, f"sprint_report:{day}")
    if not rep or not rep.get("agents"):
        return None
    passed = ", ".join(rep.get("passed") or []) or "none"
    return f"📈 Stock Swing Class: tested {', '.join(a['name'] for a in rep['agents'])}; passed to Floor 2: {passed}."


def cio_brief(firm: Any = None) -> dict[str, Any]:
    """What Graham may read about the class: stages, gates, and paper results against Max."""
    data = overview(firm)
    if not data.get("available"):
        return data
    return {"engine_online": data["engine"]["online"], "agents": [
        {k: a.get(k) for k in ("id", "name", "strategy", "stage", "version", "symbols", "last_sprint", "validation", "paper")} for a in data["agents"]]}
