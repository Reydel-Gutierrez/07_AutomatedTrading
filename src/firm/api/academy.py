"""HQ's read-only window into the 537 Academy (academy.db, written by the Forex engine).

The firm never imports the Forex engine: this module reads the Academy's SQLite file in
read-only mode and drops owner commands into the engine's inbox as JSON files. Every
number shown comes from stored records (trades, equity marks, validation reports).
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from firm.paths import state_root

ACTIONS = {"pause", "resume", "validate", "review", "auto_on", "auto_off"}
START_CAPITAL = 10_000.0
QUALIFY = {"day": {"days": 30, "trades": 100}, "swing": {"days": 60, "trades": 30}}
STALE_SECONDS = 120


def forex_dir() -> Path:
    override = os.environ.get("FOREX_STATE_ROOT", "").strip()
    return Path(override) if override else state_root().parent / "forex"


def _connect() -> sqlite3.Connection | None:
    path = forex_dir() / "academy.db"
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _kv(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value_json FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value_json"]) if row else default


def _engine(conn: sqlite3.Connection) -> dict[str, Any]:
    e = _kv(conn, "engine", {}) or {}
    hb = e.get("heartbeat")
    e["online"] = bool(hb and time.time() - hb < STALE_SECONDS)
    e["data"] = _kv(conn, "data", {}) or {}
    return e


def _stats(trades: list[sqlite3.Row]) -> dict[str, Any]:
    rs = [t["r"] for t in trades]
    net = [t["pnl_usd"] + t["financing_usd"] for t in trades]
    wins, losses = [x for x in net if x > 0], [-x for x in net if x < 0]
    sd = math.sqrt(sum((r - sum(rs) / len(rs)) ** 2 for r in rs) / (len(rs) - 1)) if len(rs) > 1 else None
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else None,
        "avg_r": sum(rs) / len(rs) if rs else None,
        "r_stdev": sd,
        "profit_factor": (sum(wins) / sum(losses)) if losses else None,
        "net_usd": round(sum(net), 2),
    }


def _equity(conn: sqlite3.Connection, agent_id: str) -> list[tuple[int, float]]:
    return [(r["ts"], r["equity"]) for r in conn.execute("SELECT ts, equity FROM equity WHERE agent_id = ? ORDER BY ts", (agent_id,))]


def _drawdown(series: list[tuple[int, float]]) -> float:
    peak, mdd = START_CAPITAL, 0.0
    for _, e in series:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak) if peak > 0 else mdd
    return mdd


def _validation(conn: sqlite3.Connection, agent_id: str, version: int, *, full: bool = False) -> dict[str, Any] | None:
    running = conn.execute("SELECT id, status, started_at FROM validations WHERE agent_id = ? AND finished_at IS NULL ORDER BY id DESC LIMIT 1", (agent_id,)).fetchone()
    row = conn.execute(
        "SELECT * FROM validations WHERE agent_id = ? AND version = ? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", (agent_id, version)
    ).fetchone()
    out: dict[str, Any] = {"running": dict(running) if running else None}
    if row:
        rep = json.loads(row["report_json"] or "{}")
        out.update({
            "id": row["id"], "version": row["version"], "kind": row["kind"], "status": row["status"], "passed": bool(row["passed"]),
            "score": row["score"], "finished_at": row["finished_at"], "checks": rep.get("checks", []),
            "unseen": rep.get("metrics", {}).get("unseen"), "trial_adjusted_sharpe": rep.get("trial_adjusted_sharpe"),
        })
        if full:
            out["report"] = rep
    return out


def _readiness(agent: sqlite3.Row, version_created: str, paper: dict[str, Any], validation: dict[str, Any] | None, dd: float, paused: str | None) -> dict[str, Any]:
    from datetime import datetime, timezone

    rule = QUALIFY[agent["style"]]
    days = (datetime.now(timezone.utc) - datetime.fromisoformat(version_created)).days
    unseen = (validation or {}).get("unseen") or {}
    expected, sd, n = unseen.get("avg_r"), paper.get("r_stdev"), paper.get("trades") or 0
    band_ok = None
    if expected is not None and paper.get("avg_r") is not None and sd and n > 1:
        band_ok = paper["avg_r"] >= expected - 2 * sd / math.sqrt(n)
    checks = [
        {"name": "Validated by the Examiner", "passed": bool(validation and validation.get("passed")), "detail": (validation or {}).get("status") or "pending"},
        {"name": f"{rule['days']}+ days of paper trading on this version", "passed": days >= rule["days"], "detail": f"{days} days"},
        {"name": f"{rule['trades']}+ paper trades on this version", "passed": n >= rule["trades"], "detail": f"{n} trades"},
        {"name": "Paper results within the validated expectation", "passed": bool(band_ok), "detail": "not enough trades yet" if band_ok is None else ("within band" if band_ok else "below band")},
        {"name": "Drawdown under 10% and not paused", "passed": dd < 0.10 and not paused, "detail": f"max {dd:.1%}" + (f", paused: {paused}" if paused else "")},
        {"name": "CIO memo and owner authorization", "passed": False, "detail": "required for any live capital"},
    ]
    ready = all(c["passed"] for c in checks[:-1])
    return {"eligible_for_review": ready, "checks": checks, "label": "Eligible for CIO review" if ready else f"In training ({sum(c['passed'] for c in checks[:-1])}/5 gates)"}


def _card(conn: sqlite3.Connection, a: sqlite3.Row, engine: dict[str, Any]) -> dict[str, Any]:
    acct = conn.execute("SELECT * FROM accounts WHERE agent_id = ?", (a["id"],)).fetchone()
    ver = conn.execute("SELECT * FROM versions WHERE agent_id = ? AND version = ?", (a["id"], a["current_version"])).fetchone()
    all_trades = conn.execute("SELECT * FROM trades WHERE agent_id = ? ORDER BY closed_at", (a["id"],)).fetchall()
    cur_trades = [t for t in all_trades if t["version"] == a["current_version"]]
    series = _equity(conn, a["id"])
    equity = series[-1][1] if series else (acct["cash"] if acct else START_CAPITAL)
    positions = conn.execute("SELECT * FROM positions WHERE agent_id = ?", (a["id"],)).fetchall()
    last = conn.execute("SELECT * FROM events WHERE agent_id = ? ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
    val = _validation(conn, a["id"], a["current_version"])
    dd = _drawdown(series)
    live = (engine.get("agents") or {}).get(a["id"], {})
    paper_all, paper_cur = _stats(all_trades), _stats(cur_trades)
    return {
        "id": a["id"], "name": a["name"], "style": a["style"], "strategy": a["strategy"], "persona": json.loads(a["persona_json"]),
        "status": a["status"], "status_reason": a["status_reason"], "paused": acct["paused"] if acct else None,
        "version": a["current_version"], "version_note": ver["note"] if ver else None, "auto_revise": bool(a["auto_revise"]), "trials": a["trials"],
        "equity": round(equity, 2), "cash": round(acct["cash"], 2) if acct else None, "return_pct": equity / START_CAPITAL - 1, "max_drawdown": dd,
        "open_positions": len(positions), "paper": paper_all, "paper_current_version": paper_cur,
        "validation": val, "activity": live.get("activity"), "last_event": dict(last) if last else None,
        "readiness": _readiness(a, ver["created_at"] if ver else a["created_at"], paper_cur, val, dd, acct["paused"] if acct else None),
    }


def overview() -> dict[str, Any]:
    conn = _connect()
    if conn is None:
        return {"available": False, "reason": "The Forex engine has not created the Academy yet.", "agents": [], "prices": [], "events": []}
    with closing(conn):
        eng = _engine(conn)
        agents = [_card(conn, a, eng) for a in conn.execute("SELECT * FROM agents WHERE status != 'RETIRED' ORDER BY created_at, id")]
        prices = [dict(r) for r in conn.execute("SELECT * FROM prices ORDER BY instrument")]
        events = [dict(r) for r in conn.execute("SELECT id, ts, agent_id, type, summary FROM events ORDER BY id DESC LIMIT 40")]
    ranked = sorted(agents, key=lambda c: (c["validation"] or {}).get("score") or -1e9, reverse=True)
    return {"available": True, "engine": eng, "agents": agents, "leaderboard": [c["id"] for c in ranked], "prices": prices, "events": events, "start_capital": START_CAPITAL}


def agent(agent_id: str) -> dict[str, Any] | None:
    conn = _connect()
    if conn is None:
        return None
    with closing(conn):
        a = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if a is None:
            return None
        eng = _engine(conn)
        card = _card(conn, a, eng)
        series = _equity(conn, agent_id)
        step = max(1, len(series) // 500)
        trades = [dict(t) for t in conn.execute("SELECT * FROM trades WHERE agent_id = ? ORDER BY closed_at DESC LIMIT 100", (agent_id,))]
        positions = [dict(p) for p in conn.execute("SELECT * FROM positions WHERE agent_id = ? ORDER BY opened_at", (agent_id,))]
        versions = [{**dict(v), "params": json.loads(v["params_json"]), "envelope": json.loads(v["envelope_json"])} for v in conn.execute("SELECT * FROM versions WHERE agent_id = ? ORDER BY version DESC", (agent_id,))]
        for v in versions:
            v.pop("params_json", None)
            v.pop("envelope_json", None)
        revisions = [{**dict(r), "report": json.loads(r["report_json"] or "{}")} for r in conn.execute("SELECT * FROM revisions WHERE agent_id = ? ORDER BY id DESC LIMIT 20", (agent_id,))]
        for r in revisions:
            r.pop("report_json", None)
        events = [dict(e) for e in conn.execute("SELECT id, ts, type, summary FROM events WHERE agent_id = ? ORDER BY id DESC LIMIT 60", (agent_id,))]
        all_trades = conn.execute("SELECT * FROM trades WHERE agent_id = ?", (agent_id,)).fetchall()
        decision = _kv(conn, f"decision:{agent_id}")
        validation = _validation(conn, agent_id, a["current_version"], full=True)
        prices = {r["instrument"]: dict(r) for r in conn.execute("SELECT * FROM prices")}
    for p in positions:
        q = prices.get(p["instrument"])
        p["mark"] = (q["bid"] if p["side"] > 0 else q["ask"]) if q else None
    return {
        **card, "equity_series": series[::step] + (series[-1:] if series and (len(series) - 1) % step else []),
        "positions": positions, "trades": trades, "versions": versions, "revisions": revisions, "events": events,
        "validation": validation, "decision": decision, "diagnostics": diagnostics(all_trades),
    }


def diagnostics(trades) -> dict[str, Any]:
    from datetime import datetime, timezone

    def group(key) -> dict[str, Any]:
        g: dict[str, list] = {}
        for t in trades:
            g.setdefault(key(t), []).append(t)
        return {k: _stats(v) for k, v in sorted(g.items())}

    def session(t) -> str:
        h = datetime.fromtimestamp(t["opened_at"], tz=timezone.utc).hour
        return "Asia (00-07 UTC)" if h < 7 else "London (07-12 UTC)" if h < 12 else "New York (12-17 UTC)" if h < 17 else "Late (17-24 UTC)"

    return {
        "by_instrument": group(lambda t: t["instrument"]),
        "by_exit": group(lambda t: (t["exit_reason"] or "").split(" (")[0]),
        "by_session": group(session),
        "by_side": group(lambda t: "long" if t["side"] > 0 else "short"),
    }


def space_block() -> dict[str, Any]:
    """Compact Academy and forex facts for the Firm Space (cheap; never raises)."""
    try:
        conn = _connect()
        if conn is None:
            return {"available": False}
        with closing(conn):
            eng = _engine(conn)
            agents = []
            for a in conn.execute("SELECT * FROM agents WHERE status != 'RETIRED' ORDER BY created_at, id"):
                acct = conn.execute("SELECT cash, paused FROM accounts WHERE agent_id = ?", (a["id"],)).fetchone()
                last = conn.execute("SELECT equity FROM equity WHERE agent_id = ? ORDER BY ts DESC LIMIT 1", (a["id"],)).fetchone()
                n_pos = conn.execute("SELECT count(*) AS n FROM positions WHERE agent_id = ?", (a["id"],)).fetchone()["n"]
                ev = conn.execute("SELECT ts, type, summary FROM events WHERE agent_id = ? ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
                val = conn.execute("SELECT status FROM validations WHERE agent_id = ? ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
                eq = last["equity"] if last else (acct["cash"] if acct else START_CAPITAL)
                agents.append({
                    "id": a["id"], "name": a["name"], "style": a["style"], "persona": json.loads(a["persona_json"]), "status": a["status"],
                    "paused": acct["paused"] if acct else None, "equity": round(eq, 2), "return_pct": eq / START_CAPITAL - 1, "positions": n_pos,
                    "activity": ((eng.get("agents") or {}).get(a["id"]) or {}).get("activity"), "last_event": dict(ev) if ev else None,
                    "validation": val["status"] if val else None, "version": a["current_version"],
                })
            running = conn.execute("SELECT agent_id, status FROM validations WHERE finished_at IS NULL").fetchall()
            prices = [dict(r) for r in conn.execute("SELECT instrument, bid, ask, time, tradeable FROM prices ORDER BY instrument")]
        return {"available": True, "engine": {k: eng.get(k) for k in ("online", "heartbeat", "market_open", "data_ready", "job", "last_error")}, "agents": agents, "validations_running": [dict(r) for r in running], "prices": prices}
    except sqlite3.Error as exc:
        return {"available": False, "error": str(exc)}


def send_command(agent_id: str, action: str, *, by: str, note: str = "") -> None:
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {sorted(ACTIONS)}")
    conn = _connect()
    if conn is None:
        raise LookupError("the Academy is not running yet")
    with closing(conn):
        if conn.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone() is None:
            raise LookupError(f"no agent {agent_id}")
    inbox = forex_dir() / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"
    tmp = inbox / f".{name}.tmp"
    tmp.write_text(json.dumps({"agent_id": agent_id, "action": action, "by": by, "note": note[:200]}), encoding="utf-8")
    tmp.replace(inbox / f"{name}.json")
