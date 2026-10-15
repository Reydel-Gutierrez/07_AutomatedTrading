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

ACTIONS = {"pause", "resume", "validate", "sprint", "auto_on", "auto_off", "to_demo", "to_research", "retire"}
START_CAPITAL = 10_000.0
# Floor 2 (paper) and Floor 3 (OANDA demo): at least four weeks and enough trades for the result to mean something
QUALIFY = {"PAPER": {"days": 28, "trades": {"day": 40, "swing": 15}}, "DEMO": {"days": 28, "trades": {"day": 30, "swing": 10}}}
# Graham may propose a Demo Desk seat after this many profitable days on paper (the owner usually decides at 4 weeks)
PROPOSE_AFTER_DAYS = 14
FLOORS = {"RESEARCH": "Floor 1 · Research", "PAPER": "Floor 2 · Paper trading", "DEMO": "Floor 3 · OANDA Demo Desk", "RETIRED": "Alumni"}
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


def _epoch(iso_ts: str | None) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(iso_ts).timestamp()) if iso_ts else 0


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
        "SELECT * FROM validations WHERE agent_id = ? AND version = ? AND finished_at IS NOT NULL AND report_json IS NOT NULL ORDER BY id DESC LIMIT 1", (agent_id, version)
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


def _readiness(agent: sqlite3.Row, paper: dict[str, Any], validation: dict[str, Any] | None, dd: float, paused: str | None, sprint: dict[str, Any] | None) -> dict[str, Any]:
    """The gates for the agent's next floor. Floor 1 -> 2 is automatic; every move after that is the owner's."""
    from datetime import datetime, timezone

    stage = agent["stage"]
    if stage == "RESEARCH":
        s = (sprint or {}).get("summary") or {}
        d1 = s.get("day1") or {}
        v = s.get("validation") or validation or {}
        checks = [
            {"name": "Passes the Examiner's validation", "passed": bool(v.get("passed")), "detail": ", ".join(c["name"] for c in v.get("checks", []) if not c["passed"]) or (v.get("status") or "pending").lower()},
            {"name": "Passes the day-1 test (random start dates)", "passed": bool(d1.get("passed")),
             "detail": f"{d1['profitable_share']:.0%} of {d1.get('runs')} starts profitable, median {d1['median_return']:+.1%}" if d1.get("runs") else "runs with the nightly sprint"},
        ]
        ready = all(c["passed"] for c in checks)
        return {"next": "Floor 2 · Paper trading", "automatic": True, "eligible": ready, "checks": checks,
                "label": "Passing: moves to Floor 2 tonight" if ready else f"In research ({sum(c['passed'] for c in checks)}/2 gates)"}
    if stage in ("PAPER", "DEMO"):
        rule = QUALIFY[stage]
        since = agent["stage_since"] or agent["created_at"]
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(since)).days
        need = rule["trades"][agent["style"]]
        unseen = (validation or {}).get("unseen") or {}
        expected, sd, n = unseen.get("avg_r"), paper.get("r_stdev"), paper.get("trades") or 0
        band_ok = None
        if expected is not None and paper.get("avg_r") is not None and sd and n > 1:
            band_ok = paper["avg_r"] >= expected - 2 * sd / math.sqrt(n)
        where = "paper" if stage == "PAPER" else "demo"
        ret = paper.get("return_pct")
        checks = [
            {"name": "4+ weeks on this floor", "passed": days >= rule["days"], "detail": f"{days} day{'' if days == 1 else 's'} ({days // 7} full week{'' if days // 7 == 1 else 's'})"},
            {"name": f"Profitable over its {where} weeks", "passed": ret is not None and ret > 0, "detail": "no result yet" if ret is None else f"{ret:+.2%} since it arrived"},
            {"name": "Drawdown under 10% and not paused", "passed": dd < 0.10 and not paused, "detail": f"max {dd:.1%}" + (f", paused: {paused}" if paused else "")},
            {"name": f"{need}+ {where} trades", "passed": n >= need, "detail": f"{n} trades", "advisory": True},
            {"name": f"{where.title()} results in line with the backtest", "passed": bool(band_ok), "detail": "not enough trades yet" if band_ok is None else ("within band" if band_ok else "below band"), "advisory": True},
        ]
        nxt = "Floor 3 · Demo Desk (your approval)" if stage == "PAPER" else "Forex Division live chair (Graham's memo + your authorization)"
        gates = [c for c in checks if not c.get("advisory")]
        checks.append({"name": "Owner decision", "passed": False, "detail": "you evaluate and decide; Graham may propose it" if stage == "PAPER" else "you decide; nothing moves automatically"})
        ready = all(c["passed"] for c in gates)
        return {"next": nxt, "automatic": False, "eligible": ready, "checks": checks, "days_on_floor": days,
                "label": f"Ready for your review: {nxt}" if ready else f"Proving itself ({sum(c['passed'] for c in gates)}/{len(gates)} gates)"}
    return {"next": None, "automatic": False, "eligible": False, "checks": [], "label": "Retired"}


def _latest_sprint(conn: sqlite3.Connection, agent_id: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM sprints WHERE agent_id = ? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", (agent_id,)).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "day": r["day"], "outcome": r["outcome"], "passed": bool(r["passed"]), "from_version": r["from_version"], "to_version": r["to_version"],
            "finished_at": r["finished_at"], "summary": json.loads(r["summary_json"] or "{}")}


def _card(conn: sqlite3.Connection, a: sqlite3.Row, engine: dict[str, Any]) -> dict[str, Any]:
    acct = conn.execute("SELECT * FROM accounts WHERE agent_id = ?", (a["id"],)).fetchone()
    ver = conn.execute("SELECT * FROM versions WHERE agent_id = ? AND version = ?", (a["id"], a["current_version"])).fetchone()
    all_trades = conn.execute("SELECT * FROM trades WHERE agent_id = ? ORDER BY closed_at", (a["id"],)).fetchall()
    from datetime import datetime

    floor_since = int(datetime.fromisoformat(a["stage_since"]).timestamp()) if a["stage_since"] else 0
    cur_trades = [t for t in all_trades if t["version"] == a["current_version"] and t["closed_at"] >= floor_since]
    series = _equity(conn, a["id"])
    equity = series[-1][1] if series else (acct["cash"] if acct else START_CAPITAL)
    positions = conn.execute("SELECT * FROM positions WHERE agent_id = ?", (a["id"],)).fetchall()
    last = conn.execute("SELECT * FROM events WHERE agent_id = ? ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
    val = _validation(conn, a["id"], a["current_version"])
    series = [(t, e) for t, e in series if t >= floor_since] if a["stage"] in ("PAPER", "DEMO") else series
    dd = _drawdown(series)
    sprint = _latest_sprint(conn, a["id"])
    sprinting = _kv(conn, f"sprint:{a['id']}", {}) or {}
    live = (engine.get("agents") or {}).get(a["id"], {})
    paper_all, paper_cur = _stats(all_trades), _stats(cur_trades)
    return {
        "id": a["id"], "name": a["name"], "style": a["style"], "strategy": a["strategy"], "persona": json.loads(a["persona_json"]),
        "status": a["status"], "status_reason": a["status_reason"], "paused": acct["paused"] if acct else None,
        "stage": a["stage"], "floor": FLOORS.get(a["stage"], a["stage"]), "stage_since": a["stage_since"], "klass": a["klass"],
        "sprint": {k: v for k, v in (sprint or {}).items() if k != "summary"} | {"verdict": ((sprint or {}).get("summary") or {}).get("verdict")} if sprint else None,
        "sprinting": sprinting if sprinting.get("running") else None,
        "version": a["current_version"], "version_note": ver["note"] if ver else None, "auto_revise": bool(a["auto_revise"]), "trials": a["trials"],
        "equity": round(equity, 2), "cash": round(acct["cash"], 2) if acct else None, "return_pct": equity / START_CAPITAL - 1, "max_drawdown": dd,
        "open_positions": len(positions), "paper": paper_all, "paper_current_version": paper_cur,
        "validation": val, "activity": live.get("activity"), "last_event": dict(last) if last else None,
        "readiness": _readiness(a, {**paper_cur, "return_pct": equity / START_CAPITAL - 1 if a["stage"] in ("PAPER", "DEMO") else None}, val, dd, acct["paused"] if acct else None, sprint),
        "weekly": weekly(series, cur_trades, floor_since) if a["stage"] in ("PAPER", "DEMO") else [],
    }


def weekly(series: list[tuple[int, float]], trades: list[Any], since: int) -> list[dict[str, Any]]:
    """Week-by-week results on the current floor: each 7-day period from the day the agent arrived."""
    if not since:
        return []
    now = int(time.time())
    out = []
    week = 0
    while since + week * 7 * 86400 <= now:
        a, b = since + week * 7 * 86400, since + (week + 1) * 7 * 86400
        before = [e for t, e in series if t < a]
        inside = [e for t, e in series if a <= t < b]
        start = before[-1] if before else START_CAPITAL
        end = inside[-1] if inside else start
        tr = [t for t in trades if a <= t["closed_at"] < b]
        out.append({"week": week + 1, "start": a, "end": min(b, now), "complete": b <= now, "start_equity": round(start, 2), "end_equity": round(end, 2),
                    "return_pct": end / start - 1 if start else 0.0, "trades": len(tr), "net_usd": round(sum(t["pnl_usd"] + t["financing_usd"] for t in tr), 2)})
        week += 1
    return out


def overview() -> dict[str, Any]:
    conn = _connect()
    if conn is None:
        return {"available": False, "reason": "The Forex engine has not created the Academy yet.", "agents": [], "prices": [], "events": []}
    with closing(conn):
        eng = _engine(conn)
        agents = [_card(conn, a, eng) for a in conn.execute("SELECT * FROM agents WHERE status != 'RETIRED' ORDER BY created_at, id")]
        prices = [dict(r) for r in conn.execute("SELECT * FROM prices ORDER BY instrument")]
        events = [dict(r) for r in conn.execute("SELECT id, ts, agent_id, type, summary FROM events ORDER BY id DESC LIMIT 40")]
        report = _kv(conn, "sprint_report_latest")
        sprint_day = _kv(conn, "sprint_day")
    ranked = sorted(agents, key=lambda c: (c["validation"] or {}).get("score") or -1e9, reverse=True)
    return {"available": True, "engine": eng, "agents": agents, "leaderboard": [c["id"] for c in ranked], "prices": prices, "events": events, "start_capital": START_CAPITAL,
            "latest_report": report, "sprint_day": sprint_day, "floors": FLOORS}


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
        sprints = [{**dict(r), "summary": json.loads(r["summary_json"] or "{}")} for r in conn.execute("SELECT * FROM sprints WHERE agent_id = ? ORDER BY id DESC LIMIT 30", (agent_id,))]
        for sp in sprints:
            sp.pop("summary_json", None)
        transitions = [dict(r) for r in conn.execute("SELECT * FROM stage_transitions WHERE agent_id = ? ORDER BY id DESC", (agent_id,))]
        validation = _validation(conn, agent_id, a["current_version"], full=True)
        prices = {r["instrument"]: dict(r) for r in conn.execute("SELECT * FROM prices")}
        strategy_info = _strategy_catalog(conn).get(a["strategy"])
    for p in positions:
        q = prices.get(p["instrument"])
        p["mark"] = (q["bid"] if p["side"] > 0 else q["ask"]) if q else None
    return {
        **card, "equity_series": series[::step] + (series[-1:] if series and (len(series) - 1) % step else []),
        "positions": positions, "trades": trades, "versions": versions, "revisions": revisions, "events": events,
        "validation": validation, "decision": decision, "diagnostics": diagnostics(all_trades), "sprints": sprints, "transitions": transitions,
        "strategy_info": strategy_info,
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
            for a in conn.execute("SELECT * FROM agents ORDER BY created_at, id"):
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
                    "validation": val["status"] if val else None, "version": a["current_version"], "stage": a["stage"], "stage_since": a["stage_since"],
                    "floor_trades": conn.execute("SELECT count(*) AS n FROM trades WHERE agent_id = ? AND version = ? AND closed_at >= ?",
                                                 (a["id"], a["current_version"], _epoch(a["stage_since"]))).fetchone()["n"],
                    "sprinting": ((_kv(conn, f"sprint:{a['id']}", {}) or {}).get("step") if (_kv(conn, f"sprint:{a['id']}", {}) or {}).get("running") else None),
                    "last_sprint": (lambda r: {"day": r["day"], "outcome": r["outcome"]} if r else None)(
                        conn.execute("SELECT day, outcome FROM sprints WHERE agent_id = ? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()),
                })
            running = conn.execute("SELECT agent_id, status FROM validations WHERE finished_at IS NULL").fetchall()
            running = [dict(r) for r in running] + [
                {"agent_id": a["id"], "status": f"RUNNING: sprint · {(_kv(conn, 'sprint:' + a['id'], {}) or {}).get('step', '')}"}
                for a in conn.execute("SELECT id FROM agents") if (_kv(conn, f"sprint:{a['id']}", {}) or {}).get("running")
            ]
            prices = [dict(r) for r in conn.execute("SELECT instrument, bid, ask, time, tradeable FROM prices ORDER BY instrument")]
            latest = _kv(conn, "sprint_report_latest") or {}
        report_brief = {k: latest.get(k) for k in ("day", "passed", "improved", "floor2")} if latest else None
        return {"available": True, "latest_report": report_brief, "engine": {k: eng.get(k) for k in ("online", "heartbeat", "market_open", "data_ready", "job", "last_error", "demo")}, "agents": agents, "validations_running": running, "prices": prices}
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


def reports(limit: int = 30) -> list[dict[str, Any]]:
    conn = _connect()
    if conn is None:
        return []
    with closing(conn):
        rows = conn.execute("SELECT key, value_json FROM kv WHERE key LIKE 'sprint_report:%' ORDER BY key DESC LIMIT ?", (limit,)).fetchall()
    return [json.loads(r["value_json"]) for r in rows]


def report(day: str) -> dict[str, Any] | None:
    conn = _connect()
    if conn is None:
        return None
    with closing(conn):
        head = _kv(conn, f"sprint_report:{day}")
        rows = conn.execute("SELECT * FROM sprints WHERE day = ? ORDER BY id", (day,)).fetchall()
    if head is None and not rows:
        return None
    sprints = [{**{k: r[k] for k in r.keys() if k != "summary_json"}, "summary": json.loads(r["summary_json"] or "{}")} for r in rows]
    return {**(head or {"day": day, "agents": []}), "sprints": sprints}


def morning_message(day: str) -> str | None:
    """One short line for Telegram: the report is ready, who was tested, who passed."""
    conn = _connect()
    if conn is None:
        return None
    with closing(conn):
        rep_ = _kv(conn, f"sprint_report:{day}")
        research = [r["name"] for r in conn.execute("SELECT name FROM agents WHERE stage = 'RESEARCH' ORDER BY created_at, id")]
    if rep_ is None:
        return f"Academy: no research sprint report for {day} yet (the Forex engine may be offline)." if research else None
    agents = rep_.get("agents") or []
    if not agents:
        return None
    improved = [f"{a['name']} (v{a['to_version']})" for a in agents if a.get("outcome") == "IMPROVED" and a.get("to_version")]
    retire = [a["name"] for a in agents if a.get("recommend_retire")]
    parts = [
        f"📚 Academy sprint report ready for review ({day}).",
        f"Tested: {', '.join(a['name'] for a in agents)}.",
        f"Passed and moved to Floor 2: {', '.join(rep_.get('passed') or []) or 'none'}.",
    ]
    if improved:
        parts.append(f"Improved but not passing yet: {', '.join(improved)}.")
    if retire:
        parts.append(f"Retirement suggested: {', '.join(retire)}.")
    return " ".join(parts)


# -- Graham's read access and the analytics room ---------------------------------------------------

def _strategy_catalog(conn: sqlite3.Connection) -> dict[str, Any]:
    return _kv(conn, "strategy_catalog", {}) or {}


def _brief_validation(v: dict[str, Any] | None) -> dict[str, Any] | None:
    if not v:
        return None
    rep = v.get("report") or {}
    return {
        "status": v.get("status"), "passed": v.get("passed"), "finished_at": v.get("finished_at"),
        "failing": [c["name"] for c in (v.get("checks") or []) if not c.get("passed")],
        "unseen": v.get("unseen"), "holdout": (rep.get("metrics") or {}).get("holdout"),
        "trial_adjusted_sharpe": v.get("trial_adjusted_sharpe"),
    }


def cio_overview() -> dict[str, Any]:
    """Everything happening in the Academy, compact enough for one tool result."""
    ov = overview()
    if not ov.get("available"):
        return ov
    agents = []
    for a in ov["agents"]:
        sp = a.get("sprint") or {}
        agents.append({
            "id": a["id"], "name": a["name"], "style": a["style"], "class": a["klass"], "floor": a["floor"], "stage": a["stage"], "since": a["stage_since"],
            "strategy": a["strategy"], "version": a["version"], "version_note": a["version_note"], "variants_tried": a["trials"],
            "paused": a["paused"], "nightly_sprints": a["auto_revise"],
            "paper_account": {"equity": a["equity"], "return_pct": a["return_pct"], "max_drawdown": a["max_drawdown"], "open_positions": a["open_positions"], "this_floor": a["paper_current_version"]},
            "validation": _brief_validation(a.get("validation")),
            "last_sprint": {k: sp.get(k) for k in ("day", "outcome", "verdict", "to_version")} if sp else None,
            "next_gate": a["readiness"]["next"], "readiness": a["readiness"]["label"],
        })
    eng = ov.get("engine") or {}
    return {
        "available": True,
        "how_it_works": "Floor 1 research (no trading; a sprint every night at 01:00 NY polishes each strategy on OANDA history; promotion to Floor 2 is automatic when validation and the day-1 test pass). Floor 2 paper trading ($10,000 virtual, 4+ weeks; the owner evaluates the weekly results and gives a Demo Desk seat when it is profitable; you may propose a seat with an academy_promotion proposal after 2+ profitable weeks). Floor 3 OANDA practice account (owner decides on a live chair after Graham's memo). No live forex trading exists.",
        "engine": {k: eng.get(k) for k in ("online", "market_open", "data_ready", "job", "queue", "last_error", "demo")},
        "agents": agents,
        "latest_report": ov.get("latest_report"),
        "recent_events": [{"academy_event_id": e["id"], "ts": e["ts"], "agent": e["agent_id"], "type": e["type"], "summary": e["summary"]} for e in ov.get("events", [])[:25]],
        "rates": ov.get("prices"),
    }


def cio_agent(agent_id: str) -> dict[str, Any] | None:
    a = agent(agent_id)
    if a is None:
        return None
    conn = _connect()
    with closing(conn):  # type: ignore[arg-type]
        cat = _strategy_catalog(conn).get(a["strategy"], {})  # type: ignore[arg-type]
    v = a.get("validation") or {}
    rep = v.get("report") or {}
    return {
        "id": a["id"], "name": a["name"], "persona": a["persona"].get("about"), "floor": a["floor"], "stage": a["stage"], "since": a["stage_since"],
        "strategy": {"key": a["strategy"], "title": cat.get("title"), "description": cat.get("description"), "tunable_settings": cat.get("tunables")},
        "current_version": a["version"],
        "versions": [{"version": x["version"], "note": x["note"], "created_at": x["created_at"], "by": x["created_by"], "validation": x["validation_status"], "params": x["params"], "risk_envelope": x["envelope"]} for x in a["versions"][:6]],
        "validation": {
            **(_brief_validation(v) or {}), "checks": rep.get("checks"), "windows": rep.get("windows"), "metrics": rep.get("metrics"),
            "random_baseline_p": (rep.get("baseline") or {}).get("p_value"), "cost_stress": rep.get("stress"), "mc_dd95": rep.get("monte_carlo_dd95"),
            "by_instrument": rep.get("by_instrument"), "day1": rep.get("day1"),
        } if v else None,
        "sprints": [{"day": s["day"], "outcome": s["outcome"], "from_version": s["from_version"], "to_version": s["to_version"], "verdict": s["summary"].get("verdict"),
                     "change": s["summary"].get("change"), "current": s["summary"].get("current"), "best": s["summary"].get("best"), "day1": s["summary"].get("day1")} for s in a["sprints"][:10]],
        "paper_account": {"equity": a["equity"], "return_pct": a["return_pct"], "max_drawdown": a["max_drawdown"], "all_time": a["paper"], "this_floor": a["paper_current_version"]},
        "open_positions": a["positions"], "recent_trades": a["trades"][:25], "diagnostics": a["diagnostics"],
        "floor_history": a["transitions"], "readiness": a["readiness"], "weeks_on_this_floor": a["weekly"],
        "demo_desk_proposal": promotion_check(a["id"]),
        "recent_events": [{"academy_event_id": e["id"], "ts": e["ts"], "type": e["type"], "summary": e["summary"]} for e in a["events"][:20]],
    }


def cio_report(day: str | None = None) -> dict[str, Any] | None:
    if day is None:
        rs = reports(1)
        if not rs:
            return None
        day = rs[0]["day"]
    rep = report(day)
    if rep is None:
        return None
    for s in rep.get("sprints", []):
        s["summary"].pop("top_candidates", None)
    return rep


def event_label(event_id: int) -> str | None:
    conn = _connect()
    if conn is None:
        return None
    with closing(conn):
        row = conn.execute("SELECT summary FROM events WHERE id = ?", (event_id,)).fetchone()
    return row["summary"] if row else None


def analytics_block() -> dict[str, Any]:
    """The Academy at a glance, for the Boardroom & Treasury."""
    ov = overview()
    if not ov.get("available"):
        return {"available": False}
    agents = ov["agents"]
    floors: dict[str, list[dict[str, Any]]] = {s: [] for s in ("RESEARCH", "PAPER", "DEMO", "RETIRED")}
    for a in agents:
        unseen = ((a.get("validation") or {}).get("unseen")) or {}
        floors.setdefault(a["stage"], []).append({
            "id": a["id"], "name": a["name"], "color": a["persona"]["color"], "version": a["version"], "style": a["style"],
            "equity": a["equity"], "return_pct": a["return_pct"], "trades": a["paper_current_version"]["trades"], "avg_r": a["paper_current_version"]["avg_r"],
            "validation": (a.get("validation") or {}).get("status"), "unseen_avg_r": unseen.get("avg_r"), "unseen_trades": unseen.get("trades"),
            "last_sprint": (a.get("sprint") or {}).get("outcome"), "readiness": a["readiness"]["label"],
        })
    conn = _connect()
    with closing(conn):  # type: ignore[arg-type]
        hist = [dict(r) for r in conn.execute(  # type: ignore[union-attr]
            "SELECT day, count(*) AS tested, sum(passed) AS passed, sum(outcome = 'IMPROVED') AS improved FROM sprints WHERE finished_at IS NOT NULL GROUP BY day ORDER BY day DESC LIMIT 14"
        )]
    paper = floors["PAPER"] + floors["DEMO"]
    return {
        "available": True, "engine_online": (ov.get("engine") or {}).get("online"), "floors": floors,
        "counts": {k: len(v) for k, v in floors.items()},
        "paper_equity": round(sum(p["equity"] for p in paper), 2), "paper_capital": 10_000.0 * len(paper),
        "latest_report": ov.get("latest_report"), "sprint_history": hist,
    }


def promotion_check(agent_id: str) -> dict[str, Any]:
    """Whether Graham may propose a Demo Desk seat for this agent now, and why not."""
    ov = overview()
    if not ov.get("available"):
        return {"allowed": False, "reason": "the Academy is not running"}
    a = next((x for x in ov["agents"] if x["id"] == agent_id), None)
    if a is None:
        return {"allowed": False, "reason": f"no Academy agent '{agent_id}'"}
    if a["stage"] != "PAPER":
        return {"allowed": False, "reason": f"{a['name']} is on {a['floor']}; only Floor 2 agents can take a Demo Desk seat"}
    days = a["readiness"].get("days_on_floor", 0)
    demo = (ov.get("engine") or {}).get("demo") or {}
    seats_taken = sum(1 for x in ov["agents"] if x["stage"] == "DEMO")
    reasons = []
    if days < PROPOSE_AFTER_DAYS:
        reasons.append(f"only {days} days on paper (propose after {PROPOSE_AFTER_DAYS}+)")
    if a["return_pct"] <= 0:
        reasons.append(f"not profitable on paper ({a['return_pct']:+.2%})")
    if a["max_drawdown"] >= 0.10:
        reasons.append(f"drawdown {a['max_drawdown']:.1%} is 10% or more")
    if not demo.get("configured"):
        reasons.append("the OANDA practice account for the Demo Desk is not set up yet")
    if seats_taken >= 2:
        reasons.append("both Demo Desk seats are taken")
    return {"allowed": not reasons, "reason": "; ".join(reasons) or None, "agent": a["name"], "days_on_paper": days, "return_pct": a["return_pct"],
            "max_drawdown": a["max_drawdown"], "weekly": a.get("weekly", [])}


def floor_analytics(stage: str) -> dict[str, Any]:
    """One Academy floor in depth, for that floor's boardroom."""
    ov = overview()
    if not ov.get("available"):
        return {"available": False}
    agents = [a for a in ov["agents"] if a["stage"] == stage]
    rows = []
    for a in agents:
        v = a.get("validation") or {}
        rows.append({
            "id": a["id"], "name": a["name"], "color": a["persona"]["color"], "style": a["style"], "version": a["version"], "since": a["stage_since"],
            "equity": a["equity"], "return_pct": a["return_pct"], "max_drawdown": a["max_drawdown"], "open_positions": a["open_positions"],
            "this_floor": a["paper_current_version"], "weekly": a.get("weekly", []), "readiness": a["readiness"],
            "validation": {"status": v.get("status"), "passed": v.get("passed"), "unseen": v.get("unseen"), "failing": [c["name"] for c in v.get("checks", []) if not c["passed"]]} if v else None,
            "sprint": a.get("sprint"), "auto_revise": a["auto_revise"], "trials": a["trials"],
        })
    out: dict[str, Any] = {"available": True, "stage": stage, "floor": FLOORS.get(stage, stage), "agents": rows, "engine": {k: (ov.get("engine") or {}).get(k) for k in ("online", "market_open", "data_ready", "demo")}}
    if stage == "RESEARCH":
        conn = _connect()
        with closing(conn):  # type: ignore[arg-type]
            out["sprint_history"] = [dict(r) for r in conn.execute(  # type: ignore[union-attr]
                "SELECT day, count(*) AS tested, sum(passed) AS passed, sum(outcome = 'IMPROVED') AS improved, sum(outcome = 'PROMOTE') AS promoted"
                " FROM sprints WHERE finished_at IS NOT NULL GROUP BY day ORDER BY day DESC LIMIT 30")]
        out["latest_report"] = ov.get("latest_report")
    return out
