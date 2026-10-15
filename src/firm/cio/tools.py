"""The CIO's tools: read-only views of the firm, plus proposals, memory, his pool, and the final answer.

Everything returned carries database ids (events, orders, proposals, memory)
so the CIO can cite its sources; citations are verified before the owner sees
them. No tool here can trade or change a mandate or mode. The only capital the
CIO can move is the pool the owner gave him, through firm.capital_pool, whose
rules (caps, LIVE-only, recall only his own grants) are enforced in code.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Callable

from firm.ai.providers import ToolSpec
from firm.api import academy, views
from firm.broker.errors import BrokerUnavailable
from firm.capital_pool import CapitalPool
from firm.cio.memory import KINDS as MEMORY_KINDS
from firm.cio.memory import Memory
from firm.cio.proposals import KINDS as PROPOSAL_KINDS
from firm.cio.proposals import ProposalError, Proposals
from firm.clock import et
from firm.core import Firm
from firm.money import D

MAX_RESULT_CHARS = 14000
CITATION_TYPES = ("event", "order", "proposal", "memory", "academy")


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


SPECS = [
    ToolSpec("get_firm_overview", "Firm value, performance vs S&P 500, allocation, each manager's mode/status/latest decision, holdings, and safety controls. Call first when unsure.", _obj({})),
    ToolSpec("get_manager", "One manager in depth: mandate (with version history), live and simulated books, positions vs targets, recent decisions, and recent orders.", _obj({"manager_id": {"type": "string", "enum": ["tim", "alex", "max"]}}, ["manager_id"])),
    ToolSpec(
        "get_activity",
        "The audit trail: decisions, orders, fills, owner actions, alerts. Filter by manager or event type; newest first.",
        _obj({"manager_id": {"type": "string"}, "event_type": {"type": "string"}, "limit": {"type": "integer"}, "before_id": {"type": "integer"}}),
    ),
    ToolSpec("get_performance", "Daily marks and time-weighted return vs SPY for the firm ('_firm') or a manager, in the LIVE or SIM book.", _obj({"bucket": {"type": "string"}, "book": {"type": "string", "enum": ["LIVE", "SIM"]}}, ["bucket"])),
    ToolSpec("get_capital", "Unallocated cash, each manager's cash and free cash, deposit split, items awaiting the owner's review, recent capital flows.", _obj({})),
    ToolSpec("get_risk_controls", "Kill switch, reconcile hold, drawdown brake and its scope, firm limits, and each sleeve's current drawdown.", _obj({})),
    ToolSpec("get_quotes", "Current Robinhood quotes (bid/ask/last/previous close) for up to 10 symbols. Read-only.", _obj({"symbols": {"type": "array", "items": {"type": "string"}}}, ["symbols"])),
    ToolSpec("get_price_history", "Stored daily closes for a symbol (VTI, VXUS, BND, SPY are always kept).", _obj({"symbol": {"type": "string"}, "days": {"type": "integer"}}, ["symbol"])),
    ToolSpec(
        "get_firm_analytics",
        "The firm's money over time: total contributed (deposits minus withdrawals), dividends, every capital flow, daily marks and "
        "daily returns vs SPY, best/worst day, up/down days, max and current drawdown. Combine with get_firm_overview for today's value.",
        _obj({}),
    ),
    ToolSpec(
        "get_academy",
        "The 537 Academy (the firm's Forex Division training ground, on OANDA data): every agent's floor (1 research, 2 paper trading, "
        "3 OANDA demo desk), strategy, version, validation result (failing checks, unseen-data and locked-year results), last nightly "
        "sprint verdict, paper account, the latest sprint report, engine status, live rates, and recent Academy events (cite as type 'academy').",
        _obj({}),
    ),
    ToolSpec(
        "get_academy_agent",
        "One Academy agent in depth: the strategy's full description and tunable settings (the algorithm), every recent version with its "
        "parameters and risk envelope, the full validation report (windows, baseline, cost stress, Monte Carlo, day-1 test, by pair), "
        "sprint history, paper trades and positions, diagnostics, floor history, and readiness for the next floor.",
        _obj({"agent_id": {"type": "string"}}, ["agent_id"]),
    ),
    ToolSpec(
        "get_sprint_report",
        "A night's research sprint report (latest if no day is given, format YYYY-MM-DD): per agent, the candidates tried, the change "
        "adopted, validation and day-1 results, and the verdict.",
        _obj({"day": {"type": "string"}}),
    ),
    ToolSpec("search_memory", "Search what you have been told to remember (objectives, preferences, decisions, concerns).", _obj({"query": {"type": "string"}}, ["query"])),
    ToolSpec("list_proposals", "Proposals and their status (PENDING, APPLIED, REJECTED, EXPIRED, FAILED).", _obj({"status": {"type": "string"}})),
    ToolSpec(
        "create_proposal",
        "File a structured proposal for the owner to approve or decline in HQ. Nothing changes until they approve. "
        "kinds: reallocate {moves:[{from,to,amount}]} with 'firm' meaning unallocated; mandate_change {manager_id, changes:{key:value}}; "
        "mode_change {manager_id, mode: PAUSED|SIM|INACTIVE} (never LIVE); deposit_split {split:{manager: fraction}}; "
        "academy_promotion {agent_id} gives a Floor 2 Academy agent an OANDA Demo Desk seat (practice account, fake money) "
        "once it has 2+ profitable weeks on paper (see demo_desk_proposal in get_academy_agent).",
        _obj(
            {
                "kind": {"type": "string", "enum": list(PROPOSAL_KINDS)},
                "title": {"type": "string"},
                "rationale": {"type": "string"},
                "payload": {"type": "object"},
                "evidence_event_ids": {"type": "array", "items": {"type": "integer"}},
            },
            ["kind", "title", "rationale", "payload"],
        ),
    ),
    ToolSpec(
        "get_capital_pool",
        "Your capital pool (money the owner gave you to distribute): cash, the owner's per-manager caps (max share of the firm), "
        "each manager's share, headroom under the cap, what you granted, and what you can recall.",
        _obj({}),
    ),
    ToolSpec(
        "allocate_from_pool",
        "Move money from your pool to a manager trading real money (LIVE). Takes effect immediately and is logged and alerted. "
        "Refused if it would push the manager above the owner's cap. Give an evidence-based reason the owner can read.",
        _obj({"manager_id": {"type": "string", "enum": ["tim", "alex", "max"]}, "amount": {"type": "number"}, "reason": {"type": "string"}}, ["manager_id", "amount", "reason"]),
    ),
    ToolSpec(
        "recall_to_pool",
        "Return money you previously granted a manager back to your pool (only your own grants, only their free cash). Takes effect immediately.",
        _obj({"manager_id": {"type": "string", "enum": ["tim", "alex", "max"]}, "amount": {"type": "number"}, "reason": {"type": "string"}}, ["manager_id", "amount", "reason"]),
    ),
    ToolSpec(
        "remember",
        "Save something durable the owner said or decided (objective, preference, decision, concern, fact). The owner sees and can edit every memory.",
        _obj({"kind": {"type": "string", "enum": list(MEMORY_KINDS)}, "text": {"type": "string"}}, ["kind", "text"]),
    ),
    ToolSpec(
        "respond",
        "Your final answer to the owner. Cite the records behind every factual statement about the portfolio, managers, orders, proposals, or memories.",
        _obj(
            {
                "message": {"type": "string"},
                "citations": {
                    "type": "array",
                    "items": _obj({"type": {"type": "string", "enum": list(CITATION_TYPES)}, "id": {"type": "integer"}}, ["type", "id"]),
                },
            },
            ["message", "citations"],
        ),
    ),
]


class CIOTools:
    def __init__(self, firm: Firm, proposals: Proposals, memory: Memory, *, health: Callable[[], dict[str, Any]], conversation_id: int | None = None) -> None:
        self.firm = firm
        self.proposals = proposals
        self.memory = memory
        self.health = health
        self.conversation_id = conversation_id
        self.created_proposals: list[int] = []
        self.remembered: list[int] = []
        self.pool_moves: list[int] = []
        self.final: dict[str, Any] | None = None

    def call(self, name: str, args: dict[str, Any]) -> str:
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return json.dumps({"error": f"unknown tool {name}"})
        try:
            result = fn(**args)
        except TypeError as exc:
            result = {"error": f"bad arguments for {name}: {exc}"}
        except (ProposalError, ValueError, KeyError) as exc:
            result = {"error": str(exc)}
        except BrokerUnavailable as exc:
            result = {"error": f"market data unavailable right now: {exc}"}
        text = json.dumps(result, default=str)
        return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + '…"(truncated)"'

    # -- read-only views -------------------------------------------------------
    def t_get_firm_overview(self) -> dict[str, Any]:
        o = views.overview(self.firm, self.health())
        o["firm"]["performance"].pop("series", None)
        for m in o["managers"]:
            ev = self.firm.events.last_of("MANAGER_RUN", manager_id=m["id"])
            if m["last_run"] and ev:
                m["last_run"]["event_id"] = ev.id
                m["last_run"].pop("detail", None)
        o["phase_status"] = {"tim": "Phase 1: built", "alex": "Phase 3: not built yet", "max": "Phase 4: not built yet"}
        o.pop("broker", None)
        return o

    def t_get_manager(self, manager_id: str) -> dict[str, Any]:
        d = views.manager_detail(self.firm, manager_id)
        for b in d["books"].values():
            b["performance"].pop("series", None)
        d["recent_decisions"] = [{"event_id": e["id"], "ts": e["ts"], "summary": e["summary"], "book": e["payload"].get("book"), "detail": e["payload"].get("detail")} for e in d.pop("runs")[:8]]
        d["recent_orders"] = [
            {k: o.get(k) for k in ("id", "created_at", "book", "side", "symbol", "notional", "quantity", "status", "filled_quantity", "average_price", "reason", "block_reasons")}
            for o in d.pop("orders")[:15]
        ]
        d.pop("activity_feed", None)
        d["recent_orders_note"] = "cite orders as {type: order, id}"
        return d

    def t_get_activity(self, manager_id: str | None = None, event_type: str | None = None, limit: int = 25, before_id: int | None = None) -> dict[str, Any]:
        rows = self.firm.events.recent(manager_id=manager_id or None, types=[event_type] if event_type else None, limit=max(1, min(int(limit), 50)), before_id=before_id)
        return {"events": [{"id": e.id, "ts": e.ts, "type": e.type, "actor": e.actor, "manager_id": e.manager_id, "summary": e.summary} for e in rows]}

    def t_get_performance(self, bucket: str, book: str = "LIVE") -> dict[str, Any]:
        s = self.firm.performance.summary(book, bucket)
        series = s.pop("series")
        step = max(1, len(series) // 40)
        s["series_sampled"] = series[::step] + ([series[-1]] if series and (len(series) - 1) % step else [])
        s["drawdown"] = self.firm.performance.drawdown(self.firm.performance.marks(book, bucket))
        s["note"] = "Returns are time-weighted (deposits and allocations removed). Fewer than 2 marks means not enough history yet."
        return s

    def t_get_capital(self) -> dict[str, Any]:
        c = views.capital(self.firm)
        c["flows"] = [{"event_id": e["id"], "ts": e["ts"], "summary": e["summary"]} for e in c["flows"][:20]]
        return c

    def t_get_risk_controls(self) -> dict[str, Any]:
        out = views.controls(self.firm, self.health())
        out["firm_limits"] = self.firm.settings.get("firm_limits")
        out["drawdowns"] = {
            m.id: {b: self.firm.performance.drawdown(self.firm.performance.marks(b, m.id)) for b in ("LIVE", "SIM")} for m in self.firm.registry.all()
        }
        out["drawdowns"]["_firm"] = {"LIVE": self.firm.performance.drawdown(self.firm.performance.marks("LIVE", "_firm"))}
        return out

    def t_get_quotes(self, symbols: list[str]) -> dict[str, Any]:
        wanted = [str(s).upper() for s in symbols][:10]
        q = self.firm.market.quotes(wanted)
        return {s: {"bid": str(v.bid), "ask": str(v.ask), "last": str(v.last), "previous_close": str(v.close), "as_of": v.observed_at.isoformat() if v.observed_at else None} for s, v in q.items()}

    def t_get_price_history(self, symbol: str, days: int = 120) -> dict[str, Any]:
        start = et(self.firm.clock()).date() - timedelta(days=max(5, min(int(days), 730)))
        bars = self.firm.market.bars(str(symbol).upper(), start=start)
        step = max(1, len(bars) // 60)
        return {"symbol": symbol.upper(), "closes": [{"date": b.date, "close": str(b.close)} for b in bars[::step]], "count": len(bars)}

    def t_get_firm_analytics(self) -> dict[str, Any]:
        return views.money_analytics(self.firm)

    def t_get_academy(self) -> dict[str, Any]:
        return academy.cio_overview()

    def t_get_academy_agent(self, agent_id: str) -> dict[str, Any]:
        out = academy.cio_agent(agent_id)
        return out if out is not None else {"error": f"no Academy agent '{agent_id}'"}

    def t_get_sprint_report(self, day: str | None = None) -> dict[str, Any]:
        out = academy.cio_report(day)
        return out if out is not None else {"error": "no sprint report for that day yet"}

    def t_search_memory(self, query: str) -> dict[str, Any]:
        return {"memories": [{"id": m["id"], "kind": m["kind"], "text": m["text"], "active": bool(m["active"]), "created_at": m["created_at"]} for m in self.memory.search(query)]}

    def t_list_proposals(self, status: str | None = None) -> dict[str, Any]:
        return {"proposals": [{k: p[k] for k in ("id", "kind", "title", "status", "created_at", "decided_by", "decision_note", "payload")} for p in self.proposals.list(status or None, limit=20)]}

    def t_get_capital_pool(self) -> dict[str, Any]:
        return CapitalPool(self.firm).status()

    # -- side effects: proposals, memory, the owner's pool, final answer ----------
    def t_allocate_from_pool(self, manager_id: str, amount: float, reason: str) -> dict[str, Any]:
        out = CapitalPool(self.firm).grant(manager_id, D(str(amount)), reason=reason, by="cio")
        self.pool_moves.append(out["event_id"])
        return {**out, "note": "Done. Cite this event in your answer."}

    def t_recall_to_pool(self, manager_id: str, amount: float, reason: str) -> dict[str, Any]:
        out = CapitalPool(self.firm).recall(manager_id, D(str(amount)), reason=reason, by="cio")
        self.pool_moves.append(out["event_id"])
        return {**out, "note": "Done. Cite this event in your answer."}

    def t_create_proposal(self, kind: str, title: str, rationale: str, payload: dict[str, Any], evidence_event_ids: list[int] | None = None) -> dict[str, Any]:
        p = self.proposals.create(kind, title, payload, rationale, evidence=evidence_event_ids or [], created_by="cio", conversation_id=self.conversation_id)
        self.created_proposals.append(p["id"])
        return {"proposal_id": p["id"], "status": p["status"], "payload": p["payload"], "note": "Filed. Nothing changes unless the owner approves it in HQ."}

    def t_remember(self, kind: str, text: str) -> dict[str, Any]:
        m = self.memory.add(kind, text, by="cio", conversation_id=self.conversation_id)
        self.remembered.append(m["id"])
        return {"memory_id": m["id"], "saved": True}

    def t_respond(self, message: str, citations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self.final = {"message": message, "citations": citations or []}
        return {"ok": True}

    # -- citation verification -----------------------------------------------
    def verify_citations(self, citations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split citations into (verified with a label, rejected)."""
        tables = {"event": ("events", "summary"), "order": ("orders", "symbol"), "proposal": ("proposals", "title"), "memory": ("cio_memory", "text")}
        good, bad, seen = [], [], set()
        for c in citations:
            kind, cid = str(c.get("type", "")), c.get("id")
            if (kind, cid) in seen:
                continue
            seen.add((kind, cid))
            if kind == "academy" and isinstance(cid, int):
                label = academy.event_label(cid)
                (good if label else bad).append({"type": kind, "id": cid, **({"label": label[:140]} if label else {})})
                continue
            if kind not in tables or not isinstance(cid, int):
                bad.append(c)
                continue
            table, col = tables[kind]
            row = self.firm.db.one(f"SELECT {col} AS label FROM {table} WHERE id = ?", (cid,))
            (good if row else bad).append({"type": kind, "id": cid, **({"label": str(row["label"])[:140]} if row else {})})
        return good, bad
