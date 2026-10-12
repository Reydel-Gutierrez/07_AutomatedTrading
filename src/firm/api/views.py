"""Read models for Investment HQ (The 537 Firm). Every number comes from the ledger, marks, or the broker."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from firm.broker.errors import BrokerUnavailable
from firm.clock import CALENDAR, EASTERN, et, iso, parse_hhmm
from firm.core import Firm
from firm.ledger import FIRM, SUSPENSE
from firm.money import ZERO, cents

ACTIVITY_EVENTS = ("MANAGER_RUN", "ORDER_FILLED", "ORDER_SUBMITTED", "ORDER_BLOCKED", "ORDER_DEFERRED", "ORDER_REJECTED", "MANAGER_DEGRADED", "MANAGER_WAITING", "MANAGER_MODE", "MANDATE_CHANGED", "CAPITAL_ALLOCATED", "CAPITAL_DEALLOCATED")


def f(x: Decimal | None) -> float | None:
    return float(cents(x)) if x is not None else None


def prices(firm: Firm, symbols: list[str]) -> tuple[dict[str, Decimal], str]:
    """Live mid prices, else the latest stored close. Returns (prices, source)."""
    if not symbols:
        return {}, "none"
    try:
        q = firm.market.quotes(symbols)
        out = {s: q[s].mid for s in symbols if s in q and q[s].mid}
        if len(out) == len(symbols):
            return out, "live"
    except BrokerUnavailable:
        out = {}
    today = et(firm.clock()).date()
    for s in symbols:
        if s not in out:
            close = firm.market.close_on(s, today)
            if close:
                out[s] = close
    return out, "last close"


def _sleeve(firm: Firm, book: str, bucket: str, px: dict[str, Decimal]) -> dict[str, Any]:
    cash = firm.ledger.cash(book, bucket)
    holdings = firm.ledger.holdings(book, bucket).get(bucket, {})
    invested = sum((q * px.get(s, ZERO) for s, q in holdings.items()), ZERO)
    return {"cash": cash, "invested": invested, "nav": cash + invested, "holdings": holdings}


def _next_run(firm: Firm, manager_id: str) -> str | None:
    req = (firm.settings.get("manager_requests") or {}).get(manager_id)
    if req:
        return req["at"]
    impl = firm.managers.get(manager_id)
    if impl is None or firm.registry.get(manager_id).book is None:
        return None
    _, mandate = firm.registry.mandate(manager_id)
    run_at = parse_hhmm(str(mandate.get("run_time_et", "10:05")))
    last = (firm.settings.get("manager_scheduled_runs") or {}).get(manager_id)
    day = et(firm.clock()).date()
    for _ in range(10):
        session = CALENDAR.session_on(day)
        if session is not None:
            at = datetime.combine(day, run_at, tzinfo=EASTERN)
            if at > firm.clock() - timedelta(minutes=1) and (not last or et(datetime.fromisoformat(last)).date() != day):
                return at.isoformat()
        day += timedelta(days=1)
    return None


def _last_run(firm: Firm, manager_id: str) -> dict[str, Any] | None:
    ev = firm.events.last_of("MANAGER_RUN", manager_id=manager_id)
    if ev is None:
        return None
    return {"at": ev.ts, "summary": ev.summary, "activity": ev.payload.get("activity"), "book": ev.payload.get("book"), "detail": ev.payload.get("detail")}


def overview(firm: Firm, health: dict[str, Any]) -> dict[str, Any]:
    managers = firm.registry.all()
    symbols = sorted({s for book in ("LIVE", "SIM") for syms in firm.ledger.holdings(book).values() for s in syms if s.isalpha()})
    px, source = prices(firm, symbols)
    live = {m.id: _sleeve(firm, "LIVE", m.id, px) for m in managers}
    sim = {m.id: _sleeve(firm, "SIM", m.id, px) for m in managers}
    unallocated = firm.ledger.cash("LIVE", FIRM)
    suspense = _sleeve(firm, "LIVE", SUSPENSE, px)
    total = unallocated + suspense["nav"] + sum((v["nav"] for v in live.values()), ZERO)
    holdings = []
    for m in managers:
        for sym, qty in sorted(live[m.id]["holdings"].items()):
            lots = firm.ledger.open_lots("LIVE", m.id, sym)
            cost = sum((l.open_cost for l in lots), ZERO)
            value = qty * px.get(sym, ZERO)
            holdings.append({"manager": m.id, "symbol": sym, "quantity": float(qty), "price": f(px.get(sym)), "value": f(value), "cost": f(cost), "pnl": f(value - cost)})
    snap = firm.db.one("SELECT * FROM broker_snaps ORDER BY id DESC LIMIT 1")
    return {
        "as_of": firm.clock().isoformat(),
        "price_source": source,
        "books_open": health.get("books_open"),
        "firm": {
            "nav": f(total),
            "unallocated": f(unallocated),
            "suspense": f(suspense["nav"]),
            "invested": f(sum((v["invested"] for v in live.values()), ZERO)),
            "performance": firm.performance.summary("LIVE", "_firm"),
        },
        "allocation": [
            *({"id": m.id, "name": m.display_name, "nav": f(live[m.id]["nav"]), "pct": float(live[m.id]["nav"] / total) if total > 0 else 0.0} for m in managers),
            {"id": "unallocated", "name": "Unallocated", "nav": f(unallocated), "pct": float(unallocated / total) if total > 0 else 0.0},
        ],
        "managers": [
            {
                "id": m.id,
                "name": m.display_name,
                "role": m.role,
                "mode": m.mode,
                "status": m.status,
                "activity": m.status_detail,
                "last_run": _last_run(firm, m.id),
                "next_run": _next_run(firm, m.id),
                "live": {"nav": f(live[m.id]["nav"]), "cash": f(live[m.id]["cash"]), "invested": f(live[m.id]["invested"])},
                "sim": {"nav": f(sim[m.id]["nav"]), "cash": f(sim[m.id]["cash"]), "invested": f(sim[m.id]["invested"])},
                "performance": {"LIVE": _perf_brief(firm, "LIVE", m.id), "SIM": _perf_brief(firm, "SIM", m.id)},
            }
            for m in managers
        ],
        "holdings": holdings,
        "controls": controls(firm, health),
        "broker": {
            "connected": health.get("broker_connected"),
            "last_snapshot": dict(snap) if snap else None,
        },
    }


def _perf_brief(firm: Firm, book: str, bucket: str) -> dict[str, Any]:
    s = firm.performance.summary(book, bucket)
    return {k: s[k] for k in ("twr", "benchmark", "excess", "since", "marks")}


def controls(firm: Firm, health: dict[str, Any]) -> dict[str, Any]:
    ks = firm.controls.kill_switch()
    return {
        "kill_switch": {"engaged": ks.engaged, "reason": ks.reason, "by": ks.by, "at": ks.at, "source": ks.source},
        "reconcile_hold": firm.controls.reconcile_hold(),
        "drawdown_brake": firm.controls.brake(),
        "live_writes_enabled": health.get("live_writes_enabled"),
    }


def manager_detail(firm: Firm, manager_id: str) -> dict[str, Any]:
    row = firm.registry.get(manager_id)
    version, mandate = firm.registry.mandate(manager_id)
    held = set()
    for b in ("LIVE", "SIM"):
        held |= set(firm.ledger.holdings(b, manager_id).get(manager_id, {}))
    targets = mandate.get("targets") or {}
    px, source = prices(firm, sorted(held | set(targets)))
    books = {}
    for b in ("LIVE", "SIM"):
        sl = _sleeve(firm, b, manager_id, px)
        nav = sl["nav"]
        positions = []
        for sym in sorted(set(sl["holdings"]) | set(targets)):
            qty = sl["holdings"].get(sym, ZERO)
            lots = firm.ledger.open_lots(b, manager_id, sym)
            cost = sum((l.open_cost for l in lots), ZERO)
            value = qty * px.get(sym, ZERO)
            positions.append({
                "symbol": sym, "quantity": float(qty), "price": f(px.get(sym)), "value": f(value), "cost": f(cost), "pnl": f(value - cost),
                "weight": float(value / nav) if nav > 0 else 0.0, "target": float(targets.get(sym, 0)),
            })
        books[b] = {
            "nav": f(nav), "cash": f(sl["cash"]), "free_cash": f(firm.ledger.free_cash(b, manager_id)), "invested": f(sl["invested"]),
            "positions": positions, "realized_pnl": f(firm.ledger.realized_pnl(b, manager_id)),
            "performance": firm.performance.summary(b, manager_id),
        }
    orders = [dict(r) for r in firm.db.query("SELECT * FROM orders WHERE manager_id = ? ORDER BY id DESC LIMIT 100", (manager_id,))]
    runs = [e.as_dict() for e in firm.events.recent(manager_id=manager_id, types=["MANAGER_RUN"], limit=30)]
    activity = [e.as_dict() for e in firm.events.recent(manager_id=manager_id, types=list(ACTIVITY_EVENTS), limit=60)]
    return {
        "id": row.id, "name": row.display_name, "role": row.role, "mode": row.mode, "active_book": row.book,
        "status": row.status, "activity": row.status_detail, "mode_changed_at": row.mode_changed_at, "mode_changed_by": row.mode_changed_by,
        "mandate": {"version": version, "params": mandate, "history": [{k: h[k] for k in ("version", "created_at", "created_by", "note", "params")} for h in firm.registry.mandate_history(manager_id)[:10]]},
        "books": books, "price_source": source, "orders": orders, "runs": runs, "activity_feed": activity,
        "last_run": _last_run(firm, manager_id), "next_run": _next_run(firm, manager_id),
        "implemented": manager_id in firm.managers,
    }


SPACE_EVENTS = (*ACTIVITY_EVENTS, "ORDER_UNKNOWN", "ORDER_RECOVERED", "ORDER_NOT_FOUND", "SLEEVE_PAUSED", "TURNOVER_LIMIT")
CIO_EVENTS = ("PROPOSAL_CREATED", "PROPOSAL_APPLIED", "PROPOSAL_REJECTED", "PROPOSAL_FAILED", "CIO_MEMORY", "AI_PROVIDER_UNAVAILABLE")


def space(firm: Firm, health: dict[str, Any], jobs: dict[str, Any], cio=None) -> dict[str, Any]:
    """Who is doing what right now, for the Firm Space. Cheap: no quotes, no performance math.

    Every field is a fact from the scheduler, the ledger's orders, the event log, or the
    CIO's in-memory turn state; the browser animates it but never invents it.
    """
    agents = []
    for m in firm.registry.all():
        job = jobs.get(f"manager:{m.id}") or {}
        running = bool(job.get("running"))
        open_orders = [
            {k: r[k] for k in ("id", "book", "symbol", "side", "notional", "quantity", "status", "created_at", "submitted_at")}
            for r in firm.db.query(
                "SELECT * FROM orders WHERE manager_id = ? AND status IN ('PENDING','SUBMITTED','UNKNOWN') ORDER BY id DESC LIMIT 10", (m.id,)
            )
        ]
        last = _last_run(firm, m.id)
        if last:
            last.pop("detail", None)
        agents.append({
            "id": m.id, "name": m.display_name, "role": m.role, "mode": m.mode, "book": m.book,
            "status": m.status, "activity": m.status_detail, "implemented": m.id in firm.managers,
            "running": running, "run_started": job.get("last_started") if running else None,
            "last_run": last, "next_run": _next_run(firm, m.id), "open_orders": open_orders,
            "recent": [e.as_dict() for e in firm.events.recent(manager_id=m.id, types=list(SPACE_EVENTS), limit=8)],
        })
    now = iso(firm.clock())
    pending = [
        dict(r) for r in firm.db.query(
            "SELECT id, kind, title, created_at, expires_at FROM proposals WHERE status = 'PENDING' AND expires_at > ? ORDER BY id DESC LIMIT 5", (now,)
        )
    ]
    ks = firm.controls.kill_switch()
    market = dict(health.get("market") or {})
    nxt = CALENDAR.next_regular_open(firm.clock())
    market["next_open"] = iso(nxt) if nxt else None
    benchmarks = []
    for sym, label in (("SPY", "S&P 500 (SPY)"), ("QQQ", "Nasdaq-100 (QQQ)")):
        rows = list(reversed(firm.db.query("SELECT date, close FROM market_bars WHERE symbol = ? ORDER BY date DESC LIMIT 30", (sym,))))
        if len(rows) >= 2:
            benchmarks.append({"symbol": sym, "label": label, "closes": [{"date": r["date"], "close": float(r["close"])} for r in rows]})
    return {
        "as_of": firm.clock().isoformat(),
        "market": market,
        "benchmarks": benchmarks,
        "broker_connected": health.get("broker_connected"),
        "controls": {
            "kill_switch": ks.engaged,
            "reconcile_hold": bool(firm.controls.reconcile_hold().get("active")),
            "drawdown_brake": bool(firm.controls.brake().get("engaged")),
            "live_writes_enabled": bool(health.get("live_writes_enabled")),
        },
        "cio": {
            "configured": cio is not None,
            "provider_ready": bool(cio is not None and cio.provider.available()),
            "working": cio.working() if cio is not None else None,
            "pending_proposals": pending,
            "recent": [e.as_dict() for e in firm.events.recent(types=list(CIO_EVENTS), limit=6)],
        },
        "agents": agents,
    }


def capital(firm: Firm) -> dict[str, Any]:
    flows = [e.as_dict() for e in firm.events.recent(types=["EXTERNAL_FLOW", "DIVIDEND", "CAPITAL_ALLOCATED", "CAPITAL_DEALLOCATED", "SUSPENSE", "SUSPENSE_RESOLVED", "BOOKS_OPENED"], limit=100)]
    return {
        "unallocated": f(firm.ledger.free_cash("LIVE", FIRM)),
        "suspense_cash": f(firm.ledger.cash("LIVE", SUSPENSE)),
        "suspense_positions": {k: float(v) for k, v in firm.ledger.holdings("LIVE", SUSPENSE).get(SUSPENSE, {}).items()},
        "deposit_split": firm.settings.get("deposit_split"),
        "foreign_orders": (firm.settings.get("foreign_orders") or {}).get("open", {}),
        "managers": [{"id": m.id, "name": m.display_name, "mode": m.mode, "cash": f(firm.ledger.cash("LIVE", m.id)), "free_cash": f(firm.ledger.free_cash("LIVE", m.id))} for m in firm.registry.all()],
        "flows": flows,
    }


def system(firm: Firm, health: dict[str, Any], jobs: dict[str, Any]) -> dict[str, Any]:
    snaps = [dict(r) for r in firm.db.query("SELECT id, ts, nav, cash, reconciled, diff_json FROM broker_snaps ORDER BY id DESC LIMIT 20")]
    for s in snaps:
        s["diff"] = json.loads(s.pop("diff_json") or "{}")
    return {
        "health": health,
        "controls": controls(firm, health),
        "jobs": jobs,
        "backup": firm.settings.get("backup_status"),
        "firm_limits": firm.settings.get("firm_limits"),
        "ai_budget": firm.settings.get("ai_budget"),
        "reconciliations": snaps,
    }
