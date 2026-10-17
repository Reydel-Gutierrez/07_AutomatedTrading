"""The Academy's stock class (Stock Swing Class), read for HQ in the Academy's own shapes.

The class's engine writes equity_academy/academy.db; the firm never imports it. This module reads
that file read-only and returns agents exactly as firm.api.academy returns forex agents (cards,
details, map agents, sprint reports), tagged market="stocks", so every page, the map, and Graham
treat both classes the same way. Owner commands go to the class engine's inbox as JSON files.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
from typing import Any

from firm.paths import state_root

ACTIONS = {"sprint", "pause", "resume", "to_paper", "to_research", "retire"}
START_CAPITAL = 10_000.0
STALE_SECONDS = 180
QUALIFY = {"sessions": 20, "trades": 10}  # Floor 2: about four weeks and enough trades for the owner to judge
FLOORS = {"RESEARCH": "Floor 1 · Research", "PAPER": "Floor 2 · Paper trading", "RETIRED": "Alumni"}
EVENT_OFFSET = 1_000_000_000  # stock-class event ids, kept apart from the forex Academy's in shared lists

_firm: Any = None


def bind(firm: Any) -> None:
    """The firm, for Max's desk record (the comparison) and last closes (open-position marks)."""
    global _firm
    _firm = firm


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


def _epoch(day: str) -> int:
    return int(datetime.combine(date.fromisoformat(day), dtime(21, 0), tzinfo=timezone.utc).timestamp())


def _iso_epoch(ts: str | None) -> int:
    return int(datetime.fromisoformat(ts).timestamp()) if ts else 0


def wanted_symbols() -> list[str]:
    conn = _connect()
    if conn is None:
        return []
    with closing(conn):
        return [s for s in (_kv(conn, "wanted_symbols") or []) if isinstance(s, str) and s.isalpha() and s.isupper()]


def has(agent_id: str) -> bool:
    conn = _connect()
    if conn is None:
        return False
    with closing(conn):
        return conn.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone() is not None


def engine() -> dict[str, Any] | None:
    conn = _connect()
    if conn is None:
        return None
    with closing(conn):
        return _engine(conn)


def _engine(conn: sqlite3.Connection) -> dict[str, Any]:
    e = _kv(conn, "engine", {}) or {}
    e["online"] = bool(e.get("heartbeat") and time.time() - e["heartbeat"] < STALE_SECONDS)
    d = _kv(conn, "data", {}) or {}
    e["data"] = {k: d.get(k) for k in ("ready", "reason", "first_date", "last_date", "sessions")}
    return e


# -- records in the Academy's shapes ------------------------------------------------------------

def _stats(trades: list[sqlite3.Row]) -> dict[str, Any]:
    rs = [t["r"] for t in trades]
    net = [t["net_usd"] for t in trades]
    wins, losses = [x for x in net if x > 0], [-x for x in net if x < 0]
    sd = math.sqrt(sum((r - sum(rs) / len(rs)) ** 2 for r in rs) / (len(rs) - 1)) if len(rs) > 1 else None
    return {"trades": len(trades), "win_rate": len(wins) / len(trades) if trades else None, "avg_r": sum(rs) / len(rs) if rs else None,
            "r_stdev": sd, "profit_factor": (sum(wins) / sum(losses)) if losses else None, "net_usd": round(sum(net), 2)}


def _series(conn: sqlite3.Connection, agent_id: str) -> list[tuple[int, float]]:
    return [(_epoch(r["date"]), r["equity"]) for r in conn.execute("SELECT date, equity FROM paper_equity WHERE agent_id = ? ORDER BY date", (agent_id,))]


def _drawdown(series: list[tuple[int, float]]) -> float:
    peak, mdd = START_CAPITAL, 0.0
    for _, e in series:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak) if peak > 0 else mdd
    return mdd


def _weekly(series: list[tuple[int, float]], trades: list[sqlite3.Row], since: int) -> list[dict[str, Any]]:
    if not since:
        return []
    now, out, week = int(time.time()), [], 0
    while since + week * 7 * 86400 <= now:
        a, b = since + week * 7 * 86400, since + (week + 1) * 7 * 86400
        before = [e for t, e in series if t < a]
        inside = [e for t, e in series if a <= t < b]
        start = before[-1] if before else START_CAPITAL
        end = inside[-1] if inside else start
        tr = [t for t in trades if a <= _epoch(t["closed"]) < b]
        out.append({"week": week + 1, "start": a, "end": min(b, now), "complete": b <= now, "start_equity": round(start, 2), "end_equity": round(end, 2),
                    "return_pct": end / start - 1 if start else 0.0, "trades": len(tr), "net_usd": round(sum(t["net_usd"] for t in tr), 2)})
        week += 1
    return out


def _desk_return(since_day: str) -> float | None:
    """Max's live desk over the same days: compounded daily returns after `since_day` (flows excluded)."""
    if _firm is None:
        return None
    try:
        reg = _firm.registry.get("max")
        marks = [m for m in _firm.performance.marks(reg.book or "SIM", "max") if m.date >= since_day]
    except Exception:  # noqa: BLE001 - the comparison is context, never a reason to fail
        return None
    if len(marks) < 2:
        return None
    g = 1.0
    for prev, m in zip(marks, marks[1:]):
        if prev.nav > 0:
            g *= 1 + float((m.nav - m.net_flow) / prev.nav - 1)
    return g - 1


def _report(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """A stored validation report, with windows as epochs (the Academy's format)."""
    if row is None or not row["report_json"]:
        return None
    rep = json.loads(row["report_json"])
    rep["windows"] = {k: [_epoch(v[0]), _epoch(v[1])] for k, v in (rep.get("windows") or {}).items()}
    rep["baseline"] = {"avg_r": (rep.get("baseline") or {}).get("return_pct", []), "p_value": (rep.get("baseline") or {}).get("p_value", 1.0)}
    rep["by_instrument"] = rep.pop("by_symbol", {}) or {}
    rep.setdefault("monte_carlo_dd95", None)
    rep["market"] = "stocks"
    return rep


def _validation(conn: sqlite3.Connection, agent_id: str, version: int, *, full: bool = False) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM validations WHERE agent_id = ? AND version = ? ORDER BY id DESC LIMIT 1", (agent_id, version)).fetchone()
    if row is None:  # a new version gets its validation with the sprint that made it; show the latest otherwise
        row = conn.execute("SELECT * FROM validations WHERE agent_id = ? ORDER BY id DESC LIMIT 1", (agent_id,)).fetchone()
    out: dict[str, Any] = {"running": None}
    rep = _report(row)
    if rep:
        out.update({"id": row["id"], "version": row["version"], "kind": row["kind"], "status": row["status"], "passed": bool(row["passed"]), "score": row["score"],
                    "finished_at": row["finished_at"], "checks": rep.get("checks", []), "unseen": (rep.get("metrics") or {}).get("unseen"),
                    "trial_adjusted_sharpe": rep.get("trial_adjusted_sharpe")})
        if full:
            out["report"] = rep
    return out


def _sprint_rows(conn: sqlite3.Connection, agent_id: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM sprints WHERE agent_id = ? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT ?", (agent_id, limit)).fetchall()
    out = []
    for r in rows:
        s = json.loads(r["summary_json"] or "{}")
        s["validation"] = {"checks": s.pop("validation_checks", None), "unseen": s.get("unseen"), "passed": bool(r["passed"])}
        out.append({**{k: r[k] for k in r.keys() if k != "summary_json"}, "summary": s})
    return out


def _readiness(a: sqlite3.Row, paper: dict[str, Any], dd: float, sprint: dict[str, Any] | None, sessions: int, vs_max: float | None, desk: float | None) -> dict[str, Any]:
    if a["stage"] == "RESEARCH":
        s = (sprint or {}).get("summary") or {}
        d1 = s.get("day1") or {}
        checks_v = (s.get("validation") or {}).get("checks") or []
        passed_v = bool(checks_v) and all(c["passed"] for c in checks_v)
        checks = [
            {"name": "Passes the Examiner's validation", "passed": passed_v, "detail": ", ".join(c["name"] for c in checks_v if not c["passed"]) or ("pending" if not checks_v else "passed")},
            {"name": "Passes the day-1 test (random start dates)", "passed": bool(d1.get("passed")),
             "detail": f"{d1['profitable_share']:.0%} of {d1.get('runs')} starts profitable, median {d1['median_return']:+.1%}" if d1.get("runs") else "runs with the nightly sprint"},
        ]
        ready = all(c["passed"] for c in checks)
        return {"next": "Floor 2 · Paper trading", "automatic": True, "eligible": ready, "checks": checks,
                "label": "Passing: moves to Floor 2 tonight" if ready else f"In research ({sum(c['passed'] for c in checks)}/2 gates)"}
    if a["stage"] == "PAPER":
        ret, n = paper.get("return_pct"), paper.get("trades") or 0
        checks = [
            {"name": f"{QUALIFY['sessions']}+ sessions on paper (about 4 weeks)", "passed": sessions >= QUALIFY["sessions"], "detail": f"{sessions} sessions"},
            {"name": "Profitable over its paper weeks", "passed": ret is not None and ret > 0, "detail": "no result yet" if ret is None else f"{ret:+.2%} since it arrived"},
            {"name": "Drawdown under 10% and not paused", "passed": dd < 0.10 and a["status"] == "ACTIVE", "detail": f"max {dd:.1%}" + ("" if a["status"] == "ACTIVE" else f", {a['status'].lower()}")},
            {"name": f"{QUALIFY['trades']}+ paper trades", "passed": n >= QUALIFY["trades"], "detail": f"{n} trades", "advisory": True},
            {"name": "Ahead of Max's live desk over the same days", "passed": bool(vs_max is not None and vs_max > 0),
             "detail": "no comparison yet" if vs_max is None else f"{vs_max:+.2%} vs Max ({desk:+.2%})", "advisory": True},
        ]
        gates = [c for c in checks if not c.get("advisory")]
        checks.append({"name": "Owner decision", "passed": False, "detail": "a live swing seat on L3, or more tests; you decide"})
        ready = all(c["passed"] for c in gates)
        nxt = "Live swing seat on L3 (your approval)"
        return {"next": nxt, "automatic": False, "eligible": ready, "checks": checks, "days_on_floor": sessions,
                "label": f"Ready for your review: {nxt}" if ready else f"Proving itself ({sum(c['passed'] for c in gates)}/{len(gates)} gates)"}
    return {"next": None, "automatic": False, "eligible": False, "checks": [], "label": "Retired"}


def _card(conn: sqlite3.Connection, a: sqlite3.Row, eng: dict[str, Any]) -> dict[str, Any]:
    ver = conn.execute("SELECT * FROM versions WHERE agent_id = ? AND version = ?", (a["id"], a["current_version"])).fetchone()
    trades = conn.execute("SELECT * FROM paper_trades WHERE agent_id = ? ORDER BY closed", (a["id"],)).fetchall()
    series = _series(conn, a["id"])
    paper = a["stage"] == "PAPER"
    equity = series[-1][1] if series and paper else START_CAPITAL
    dd = _drawdown(series) if paper else 0.0
    sprints = _sprint_rows(conn, a["id"], 1)
    sprint = sprints[0] if sprints else None
    kv = _kv(conn, f"sprint:{a['id']}", {}) or {}
    last = conn.execute("SELECT * FROM events WHERE agent_id = ? ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
    first_day = conn.execute("SELECT min(date) AS d FROM paper_equity WHERE agent_id = ?", (a["id"],)).fetchone()["d"] if paper else None
    desk = _desk_return(first_day) if first_day else None
    ret = equity / START_CAPITAL - 1
    stats = _stats(trades)
    n_pos = conn.execute("SELECT count(*) AS n FROM paper_positions WHERE agent_id = ?", (a["id"],)).fetchone()["n"]
    since = _iso_epoch(a["stage_since"])
    return {
        "id": a["id"], "name": a["name"], "market": "stocks", "style": "swing", "strategy": a["strategy"], "persona": json.loads(a["persona_json"]),
        "status": a["status"], "status_reason": a["status_reason"], "paused": a["status_reason"] if a["status"] == "PAUSED" else None,
        "stage": a["stage"], "floor": FLOORS.get(a["stage"], a["stage"]), "stage_since": a["stage_since"], "klass": 1,
        "sprint": {**{k: v for k, v in sprint.items() if k != "summary"}, "passed": bool(sprint["passed"]), "verdict": sprint["summary"].get("verdict")} if sprint else None,
        "sprinting": {"running": True, "since": None, "step": kv.get("step")} if kv.get("running") else None,
        "version": a["current_version"], "version_note": ver["note"] if ver else None, "auto_revise": True, "trials": a["trials"],
        "equity": round(equity, 2), "cash": None, "return_pct": ret, "max_drawdown": dd, "open_positions": n_pos,
        "paper": stats, "paper_current_version": stats,
        "validation": _validation(conn, a["id"], a["current_version"]),
        "activity": (eng.get("activity") or {}).get(a["id"]), "last_event": dict(last) if last else None,
        "readiness": _readiness(a, {**stats, "return_pct": ret if paper and series else None}, dd, sprint, max(len(series) - 1, 0), (ret - desk) if desk is not None else None, desk),
        "weekly": _weekly(series, trades, since) if paper else [],
        "desk_comparison": {"since": first_day, "max_return": desk, "vs_max": (ret - desk) if desk is not None else None} if paper else None,
    }


def cards() -> list[dict[str, Any]]:
    conn = _connect()
    if conn is None:
        return []
    with closing(conn):
        eng = _engine(conn)
        return [_card(conn, a, eng) for a in conn.execute("SELECT * FROM agents WHERE status != 'RETIRED' ORDER BY rowid")]


def events(limit: int = 40) -> list[dict[str, Any]]:
    conn = _connect()
    if conn is None:
        return []
    with closing(conn):
        return [{**dict(r), "id": r["id"] + EVENT_OFFSET} for r in conn.execute("SELECT id, ts, agent_id, type, summary FROM events ORDER BY id DESC LIMIT ?", (limit,))]


def event_label(event_id: int) -> str | None:
    conn = _connect()
    if conn is None:
        return None
    with closing(conn):
        row = conn.execute("SELECT summary FROM events WHERE id = ?", (event_id - EVENT_OFFSET,)).fetchone()
    return row["summary"] if row else None


def detail(agent_id: str) -> dict[str, Any] | None:
    conn = _connect()
    if conn is None:
        return None
    with closing(conn):
        a = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if a is None:
            return None
        eng = _engine(conn)
        card = _card(conn, a, eng)
        series = _series(conn, agent_id) if a["stage"] == "PAPER" else []
        trades = conn.execute("SELECT * FROM paper_trades WHERE agent_id = ? ORDER BY closed DESC LIMIT 100", (agent_id,)).fetchall()
        positions = conn.execute("SELECT * FROM paper_positions WHERE agent_id = ? ORDER BY opened", (agent_id,)).fetchall()
        versions = [{**{k: v[k] for k in v.keys() if k not in ("params_json", "envelope_json")}, "params": json.loads(v["params_json"]), "envelope": json.loads(v["envelope_json"])}
                    for v in conn.execute("SELECT * FROM versions WHERE agent_id = ? ORDER BY version DESC", (agent_id,))]
        evs = [{**dict(e), "id": e["id"] + EVENT_OFFSET} for e in conn.execute("SELECT id, ts, type, summary FROM events WHERE agent_id = ? ORDER BY id DESC LIMIT 60", (agent_id,))]
        transitions = [dict(r) for r in conn.execute("SELECT * FROM stage_transitions WHERE agent_id = ? ORDER BY id DESC", (agent_id,))]
        validation = _validation(conn, agent_id, a["current_version"], full=True)
        sprints = _sprint_rows(conn, agent_id, 30)
        info = (_kv(conn, "strategy_catalog", {}) or {}).get(a["strategy"])
    marks: dict[str, float] = {}
    if _firm is not None:
        for p in positions:
            try:
                px = _firm.market.close_on(p["symbol"], date.today())
                if px is not None:
                    marks[p["symbol"]] = float(px)
            except Exception:  # noqa: BLE001
                pass
    pos = [{"id": f"{agent_id}-{p['symbol']}", "instrument": p["symbol"], "side": 1, "units": round(p["shares"], 4), "entry": round(p["entry"], 2), "stop": round(p["stop"], 2),
            "target": None, "opened_at": _epoch(p["opened"]), "risk_usd": round(p["shares"] * max(p["entry"] - p["stop"], 0), 2), "tag": "", "version": card["version"],
            "financing_usd": 0.0, "mark": marks.get(p["symbol"])} for p in positions]
    trs = [{"id": t["id"], "version": t["version"], "instrument": t["symbol"], "side": 1, "units": round(t["shares"], 4), "entry": round(t["entry"], 2), "exit": round(t["exit"], 2),
            "opened_at": _epoch(t["opened"]), "closed_at": _epoch(t["closed"]), "pnl_usd": t["net_usd"], "financing_usd": 0.0, "r": t["r"], "exit_reason": t["exit_reason"], "tag": ""} for t in trades]
    by = lambda key: _group([t for t in trades], key)  # noqa: E731
    return {
        **card, "equity_series": series, "positions": pos, "trades": trs, "versions": versions, "revisions": [], "events": evs,
        "validation": validation, "decision": None, "sprints": sprints, "transitions": transitions, "strategy_info": info,
        "diagnostics": {"by_instrument": by(lambda t: t["symbol"]), "by_exit": by(lambda t: (t["exit_reason"] or "").split(":")[0]), "by_session": {}, "by_side": {}},
    }


def _group(trades: list[sqlite3.Row], key) -> dict[str, Any]:
    g: dict[str, list] = {}
    for t in trades:
        g.setdefault(key(t), []).append(t)
    return {k: _stats(v) for k, v in sorted(g.items())}


def space_agents() -> list[dict[str, Any]]:
    """Map figures in the Academy's AcademySpaceAgent shape (cheap; never raises)."""
    try:
        conn = _connect()
        if conn is None:
            return []
        with closing(conn):
            eng = _engine(conn)
            out = []
            for a in conn.execute("SELECT * FROM agents ORDER BY rowid"):
                last = conn.execute("SELECT equity FROM paper_equity WHERE agent_id = ? ORDER BY date DESC LIMIT 1", (a["id"],)).fetchone()
                sp = conn.execute("SELECT day, outcome FROM sprints WHERE agent_id = ? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
                val = conn.execute("SELECT status FROM validations WHERE agent_id = ? ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
                ev = conn.execute("SELECT ts, type, summary FROM events WHERE agent_id = ? ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
                kv = _kv(conn, f"sprint:{a['id']}", {}) or {}
                eq = last["equity"] if last and a["stage"] == "PAPER" else START_CAPITAL
                out.append({
                    "id": a["id"], "name": a["name"], "market": "stocks", "style": "swing", "persona": json.loads(a["persona_json"]), "status": a["status"],
                    "paused": a["status_reason"] if a["status"] == "PAUSED" else None, "equity": round(eq, 2), "return_pct": eq / START_CAPITAL - 1,
                    "positions": conn.execute("SELECT count(*) AS n FROM paper_positions WHERE agent_id = ?", (a["id"],)).fetchone()["n"],
                    "activity": (eng.get("activity") or {}).get(a["id"]), "last_event": dict(ev) if ev else None, "validation": val["status"] if val else None,
                    "version": a["current_version"], "stage": a["stage"], "stage_since": a["stage_since"],
                    "floor_trades": conn.execute("SELECT count(*) AS n FROM paper_trades WHERE agent_id = ?", (a["id"],)).fetchone()["n"],
                    "sprinting": kv.get("step") if kv.get("running") else None,
                    "last_sprint": {"day": sp["day"], "outcome": sp["outcome"]} if sp else None,
                })
            return out
    except sqlite3.Error:
        return []


def sprint_history(limit: int = 30) -> list[dict[str, Any]]:
    conn = _connect()
    if conn is None:
        return []
    with closing(conn):
        return [dict(r) for r in conn.execute(
            "SELECT day, count(*) AS tested, sum(passed) AS passed, sum(outcome = 'IMPROVED') AS improved, sum(outcome = 'PROMOTE') AS promoted"
            " FROM sprints WHERE finished_at IS NOT NULL AND outcome NOT IN ('NO_DATA', 'INTERRUPTED') GROUP BY day ORDER BY day DESC LIMIT ?", (limit,))]


def report_part(day: str) -> dict[str, Any] | None:
    """The stock class's part of a night's sprint report, in the Academy's report shape."""
    conn = _connect()
    if conn is None:
        return None
    with closing(conn):
        rows = conn.execute("SELECT s.*, a.name FROM sprints s JOIN agents a ON a.id = s.agent_id WHERE s.day = ? AND s.finished_at IS NOT NULL"
                            " AND s.outcome NOT IN ('NO_DATA', 'INTERRUPTED') ORDER BY s.id", (day,)).fetchall()
    if not rows:
        return None
    agents, sprints = [], []
    for r in rows:
        s = json.loads(r["summary_json"] or "{}")
        s["validation"] = {"checks": s.pop("validation_checks", None), "unseen": s.get("unseen"), "passed": bool(r["passed"])}
        agents.append({"agent_id": r["agent_id"], "name": r["name"], "market": "stocks", "outcome": r["outcome"], "passed": bool(r["passed"]), "from_version": r["from_version"],
                       "to_version": r["to_version"], "verdict": s.get("verdict"), "tested": s.get("tested"), "unseen": s.get("unseen"), "day1": s.get("day1")})
        sprints.append({**{k: r[k] for k in r.keys() if k not in ("summary_json", "name")}, "summary": s})
    return {"agents": agents, "sprints": sprints, "passed": [a["name"] for a in agents if a["passed"]], "improved": [a["name"] for a in agents if a["outcome"] == "IMPROVED"]}


def report_days(limit: int = 30) -> list[str]:
    conn = _connect()
    if conn is None:
        return []
    with closing(conn):
        return [r["day"] for r in conn.execute("SELECT DISTINCT day FROM sprints WHERE finished_at IS NOT NULL AND outcome NOT IN ('NO_DATA', 'INTERRUPTED') ORDER BY day DESC LIMIT ?", (limit,))]


def send_command(agent_id: str, action: str, *, by: str, note: str = "") -> None:
    if action not in ACTIONS:
        raise ValueError(f"stock agents take {', '.join(sorted(ACTIONS))}")
    if not has(agent_id):
        raise LookupError(f"no agent {agent_id}")
    inbox = state_dir() / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"
    tmp = inbox / f".{name}.tmp"
    tmp.write_text(json.dumps({"agent_id": agent_id, "action": action, "by": by, "note": note[:200]}), encoding="utf-8")
    tmp.replace(inbox / f"{name}.json")


def morning_line(day: str) -> str | None:
    part = report_part(day)
    if not part:
        return None
    return f"📈 Stock class: tested {', '.join(a['name'] for a in part['agents'])}; passed to Floor 2: {', '.join(part['passed']) or 'none'}."
