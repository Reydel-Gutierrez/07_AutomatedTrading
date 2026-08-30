"""Read-only dashboard views over existing stores. No portfolio math rewrite."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_portfolio.approval.engine import record_human_decision
from agentic_portfolio.approval.report import render_packet
from agentic_portfolio.approval.store import ApprovalStore
from agentic_portfolio.approval.types import ApprovalPacket, ApprovalStatus
from agentic_portfolio.approval.validate import ApprovalValidationError
from agentic_portfolio.dashboard.history import (
    chart_ready,
    record_nav_snapshot,
    spy_return,
    total_return,
)
from agentic_portfolio.dashboard.labels import (
    ALLOCATION_ORDER,
    HISTORY_COLLECTING,
    SLEEVE_LABELS,
    UNAVAILABLE,
    friendly_enum,
    friendly_reason,
)
from agentic_portfolio.dashboard.settings import LIVE_ACCOUNT_LABEL, PAPER_BOOK_LABEL, resolve_ui_flags
from agentic_portfolio.decision.store import DecisionStore
from agentic_portfolio.discovery.store import CandidateStore, DiscoveryRunStore, ResearchQueue
from agentic_portfolio.execution.store import OrderPlanStore
from agentic_portfolio.journal import read_jsonl
from agentic_portfolio.monitoring.store import MonitoringStore
from agentic_portfolio.paper_fill.store import PaperFillStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules, load_pipeline_config, load_policy
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.review.store import ReviewStore
from agentic_portfolio.schemas import ThesisRecord, to_dict
from agentic_portfolio.session import load_session_state
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.state_store import load_hwm_state
from agentic_portfolio.thesis_registry import ThesisRegistry

JOURNAL_FILES = (
    ("decision", "thesis_decision.jsonl"),
    ("research", "research.jsonl"),
    ("risk", "risk_gate.jsonl"),
    ("approval", "approval.jsonl"),
    ("review", "robinhood_review.jsonl"),
    ("execution", "order_plan.jsonl"),
    ("paper_fill", "paper_fill.jsonl"),
    ("monitoring", "position_monitor.jsonl"),
)

ACTIVE_THESIS = {"ACTIVE", "STRENGTHENED", "UNCHANGED", "WEAKENED", "DRAFT"}
STALE_PACKET = {ApprovalStatus.EXPIRED, ApprovalStatus.SUPERSEDED}
BOOK_LABELS = {
    "paper": PAPER_BOOK_LABEL,
    "paper_monitor": f"{PAPER_BOOK_LABEL} (monitor)",
    "live": LIVE_ACCOUNT_LABEL,
}


@dataclass
class DashboardState:
    """Existing module handles pointed at one project root."""

    root: Path
    approvals: ApprovalStore
    order_plans: OrderPlanStore
    fills: PaperFillStore
    reviews: ReviewStore
    monitoring: MonitoringStore
    decisions: DecisionStore
    research: ResearchStore
    candidates: CandidateStore
    queue: ResearchQueue
    discovery: DiscoveryRunStore
    theses_live: ThesisRegistry
    theses_paper: ThesisRegistry
    theses_monitor: ThesisRegistry
    sleeves_live: SleeveRegistry
    sleeves_paper: SleeveRegistry


def dashboard_state(root: Path | None = None) -> DashboardState:
    base = root or project_root()
    state_dir = base / "state"
    return DashboardState(
        root=base,
        approvals=ApprovalStore(base),
        order_plans=OrderPlanStore(base),
        fills=PaperFillStore(base),
        reviews=ReviewStore(base),
        monitoring=MonitoringStore(base),
        decisions=DecisionStore(base),
        research=ResearchStore(base),
        candidates=CandidateStore(state_dir / "candidates.json"),
        queue=ResearchQueue(state_dir / "research_queue.json"),
        discovery=DiscoveryRunStore(state_dir / "discovery_runs.json"),
        theses_live=ThesisRegistry(state_dir / "thesis_registry.json"),
        theses_paper=ThesisRegistry(state_dir / "paper_book" / "theses.json"),
        theses_monitor=ThesisRegistry(state_dir / "paper_monitor" / "theses.json"),
        sleeves_live=SleeveRegistry(state_dir / "sleeve_registry.json"),
        sleeves_paper=SleeveRegistry(state_dir / "paper_book" / "sleeves.json"),
    )


def _pct_fraction(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100.0:.2f}%"


def _pct_points(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{float(value):.2f}%"


def _usd(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${float(value):,.2f}"


def _signed_usd(value: float | None) -> str:
    if value is None:
        return "—"
    number = float(value)
    if number > 0:
        return f"+${number:,.2f}"
    if number < 0:
        return f"-${abs(number):,.2f}"
    return f"${number:,.2f}"


def _signed_pct(value: float | None) -> str:
    if value is None:
        return "—"
    number = float(value) * 100.0
    if number > 0:
        return f"+{number:.2f}%"
    return f"{number:.2f}%"


def _metric(value: Any, display: str | None, *, available: bool | None = None) -> dict[str, Any]:
    ok = bool(available) if available is not None else value is not None
    return {
        "available": ok,
        "value": value,
        "display": display if ok and display else UNAVAILABLE,
    }


def execution_flags(rules: dict[str, Any] | None = None) -> dict[str, Any]:
    rules = rules or load_account_rules()
    exe = dict(rules.get("execution") or {})
    auto = bool(exe.get("auto_execution"))
    live = bool(exe.get("live_trade_actions_allowed"))
    return {
        "state": exe.get("state") or exe.get("mode"),
        "mode": exe.get("mode"),
        "auto_execution": auto,
        "require_human_approval": bool(exe.get("require_human_approval", True)),
        "live_trade_actions_allowed": live,
        "current_terminal_stage": exe.get("current_terminal_stage"),
        "autonomous_trading_disabled": (not auto) and (not live),
        "approved_does_not_place_order": True,
        "live_order_placement_enabled": False,
    }


def packet_kind(packet: ApprovalPacket) -> str:
    if packet.status in STALE_PACKET:
        return "stale"
    exec_status = str(packet.order_plan_summary.execution_status or "").upper()
    approval_id = str(packet.approval_id or "").lower()
    if exec_status == "DEMO" or approval_id.startswith("demo"):
        return "demo"
    if exec_status == "PAPER_ONLY":
        return "paper"
    return "unknown"


def decision_block_reason(kind: str, flags: dict[str, Any] | None = None) -> str | None:
    flags = flags or resolve_ui_flags()
    if kind == "stale" and not flags["allow_stale_packet_decisions"]:
        return "stale packets cannot be approved or rejected"
    if kind == "demo" and not flags["allow_demo_packet_decisions"]:
        return "demo packet decisions are disabled unless explicitly enabled"
    if kind == "paper" and not flags["allow_paper_packet_decisions"]:
        return "paper packet decisions are disabled unless explicitly enabled"
    return None


def paper_book(state: DashboardState) -> dict[str, Any]:
    return state.fills.current_book() or {}


def paper_context(state: DashboardState) -> dict[str, Any]:
    return dict((paper_book(state).get("context") or {}))


def spy_benchmark(ctx: dict[str, Any], reports: list[dict[str, Any]]) -> dict[str, Any]:
    spy = ctx.get("spy")
    if isinstance(spy, dict) and spy:
        return {
            "observed": True,
            "source": "paper_book_context",
            "payload": spy,
            "note": "Observed SPY benchmark from the paper book context. Not fabricated.",
        }
    spy_report = next((r for r in reports if str(r.get("symbol") or "").upper() == "SPY"), None)
    if spy_report:
        return {
            "observed": True,
            "source": "research_report",
            "symbol": "SPY",
            "research_id": spy_report.get("research_id"),
            "conclusion": spy_report.get("research_conclusion"),
            "summary": spy_report.get("executive_summary"),
            "price": spy_report.get("market_price"),
            "note": "No SPY series is stored on the paper book; showing the last SPY ResearchReport.",
        }
    return {
        "observed": False,
        "source": None,
        "note": "SPY benchmark is not on the current paper book snapshot. Not fabricated.",
    }


def _packet_row(packet: ApprovalPacket, *, flags: dict[str, Any] | None = None) -> dict[str, Any]:
    flags = flags or resolve_ui_flags()
    stale = packet.status in STALE_PACKET
    pending = packet.status == ApprovalStatus.PENDING_HUMAN_APPROVAL
    kind = packet_kind(packet)
    blocked = decision_block_reason(kind, flags)
    return {
        "approval_id": packet.approval_id,
        "symbol": packet.symbol,
        "action": packet.action.value,
        "status": packet.status.value,
        "sleeve": packet.sleeve.value if packet.sleeve else None,
        "current_allocation_pct": packet.current_allocation_pct,
        "desired_allocation_pct": packet.desired_allocation_pct,
        "current_allocation_display": _pct_points(packet.current_allocation_pct),
        "desired_allocation_display": _pct_points(packet.desired_allocation_pct),
        "order_notional": packet.order_notional,
        "order_notional_display": _usd(packet.order_notional),
        "order_quantity": packet.order_quantity,
        "current_price": packet.current_price,
        "risk_gate_verdict": packet.risk_gate_verdict,
        "created_at": packet.created_at,
        "decided_at": packet.decided_at,
        "expiry_reasons": list(packet.expiry_reasons),
        "superseded_by": packet.superseded_by,
        "pending": pending,
        "stale": stale,
        "packet_kind": kind,
        "book_label": PAPER_BOOK_LABEL if kind in {"paper", "demo", "stale"} else LIVE_ACCOUNT_LABEL,
        "decision_block_reason": blocked,
        "can_decide": pending and blocked is None,
        "approved_does_not_place_order": True,
        "broker_submitted": False,
        "live_execution_blocked": True,
        "live_order_placement_enabled": False,
    }


def packet_detail(packet: ApprovalPacket) -> dict[str, Any]:
    row = _packet_row(packet)
    row.update(
        {
            "thesis_summary": packet.thesis_summary,
            "why_now": packet.why_now,
            "why_not_cash": packet.why_not_cash,
            "why_not_spy": packet.why_not_spy,
            "bull_case": packet.bull_case,
            "base_case": packet.base_case,
            "bear_case": packet.bear_case,
            "key_risks": list(packet.key_risks),
            "invalidation_exit_policy": packet.invalidation_exit_policy,
            "expected_horizon": packet.expected_horizon,
            "portfolio_effect": packet.portfolio_effect,
            "sector_concentration_effect": packet.sector_concentration_effect,
            "enhanced_review_requirements": list(packet.enhanced_review_requirements),
            "order_plan_summary": to_dict(packet.order_plan_summary),
            "evidence_refs": to_dict(packet.evidence_refs),
            "snapshot": to_dict(packet.snapshot),
            "status_history": to_dict(packet.status_history),
            "human_note": packet.human_note,
            "monitoring_state": packet.monitoring_state,
            "sector": packet.sector,
            "explanation": render_packet(packet),
            "raw": to_dict(packet),
        }
    )
    return row


def list_approvals(state: DashboardState) -> dict[str, Any]:
    flags = resolve_ui_flags()
    packets = [_packet_row(p, flags=flags) for p in state.approvals.all_packets()]
    pending = [p for p in packets if p["pending"]]
    stale = [p for p in packets if p["stale"]]
    other = [p for p in packets if not p["pending"] and not p["stale"]]
    return {
        "pending": pending,
        "stale": stale,
        "other": other,
        "all": packets,
        "pending_count": len(pending),
        "approved_does_not_place_order": True,
        "book_label": PAPER_BOOK_LABEL,
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "live_order_placement_enabled": False,
        "allow_paper_packet_decisions": flags["allow_paper_packet_decisions"],
        "allow_demo_packet_decisions": flags["allow_demo_packet_decisions"],
        "allow_stale_packet_decisions": flags["allow_stale_packet_decisions"],
    }


def get_approval(state: DashboardState, approval_id: str) -> dict[str, Any] | None:
    packet = state.approvals.get_packet(approval_id)
    if packet is None:
        return None
    return packet_detail(packet)


def record_approval_decision(
    state: DashboardState,
    approval_id: str,
    status: ApprovalStatus,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    packet = state.approvals.get_packet(approval_id)
    if packet is None:
        raise KeyError(approval_id)
    blocked = decision_block_reason(packet_kind(packet))
    if blocked:
        raise ApprovalValidationError(blocked)
    updated = record_human_decision(
        packet,
        status,
        note=note or None,
        store=state.approvals,
        persist=True,
        journal=state.root / "logs" / "approval.jsonl",
    )
    if updated.broker_submitted or not updated.approved_does_not_place_order:
        raise ApprovalValidationError("dashboard approval must not place an order")
    return packet_detail(updated)


def _thesis_row(rec: ThesisRecord, *, book: str) -> dict[str, Any]:
    status = rec.status.value
    return {
        "thesis_id": rec.thesis_id,
        "symbol": rec.symbol,
        "sleeve": rec.sleeve.value,
        "status": status,
        "decision": rec.decision.value if rec.decision else None,
        "book": book,
        "book_label": BOOK_LABELS.get(book, book),
        "active_or_draft": status in ACTIVE_THESIS,
        "thesis_summary": rec.thesis_summary,
        "bull_case": rec.bull_case,
        "base_case": rec.base_case,
        "bear_case": rec.bear_case,
        "risks": list(rec.risks),
        "invalidation_conditions": list(rec.invalidation_conditions),
        "catalysts": list(rec.catalysts),
        "expected_horizon": rec.expected_horizon,
        "desired_allocation_pct": rec.desired_allocation_pct,
        "confidence": rec.confidence,
        "research_id": rec.research_id,
        "updated_at": rec.updated_at,
        "created_at": rec.created_at,
        "why_position_should_exist": rec.why_position_should_exist,
        "exit_policy": to_dict(rec.exit_policy) if rec.exit_policy else None,
    }


def _merge_theses(state: DashboardState) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for rec in state.theses_live.all_records():
        by_id[rec.thesis_id] = _thesis_row(rec, book="live")
    for rec in state.theses_monitor.all_records():
        by_id[rec.thesis_id] = _thesis_row(rec, book="paper_monitor")
    for rec in state.theses_paper.all_records():
        by_id[rec.thesis_id] = _thesis_row(rec, book="paper")
    rows = list(by_id.values())
    rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return rows


def research_view(state: DashboardState) -> dict[str, Any]:
    candidates = [to_dict(c) for c in state.candidates.all()]
    candidates.sort(key=lambda c: float(c.get("discovery_score") or 0), reverse=True)
    reports = [to_dict(r) for r in state.research.all_reports()]
    reports.sort(key=lambda r: str(r.get("completed_at") or r.get("started_at") or ""), reverse=True)
    theses = _merge_theses(state)
    queue = [to_dict(q) for q in state.queue.all()]
    return {
        "candidates": candidates,
        "queue": queue,
        "reports": reports,
        "theses": theses,
        "active_or_draft": [t for t in theses if t["active_or_draft"]],
        "book_label": PAPER_BOOK_LABEL,
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "live_order_placement_enabled": False,
    }


def get_thesis(state: DashboardState, thesis_id: str) -> dict[str, Any] | None:
    for rec, book in (
        (state.theses_paper.get(thesis_id), "paper"),
        (state.theses_monitor.get(thesis_id), "paper_monitor"),
        (state.theses_live.get(thesis_id), "live"),
    ):
        if rec is not None:
            return _thesis_row(rec, book=book)
    return None


def get_report(state: DashboardState, research_id: str) -> dict[str, Any] | None:
    report = state.research.get(research_id)
    return to_dict(report) if report else None


def orders_view(state: DashboardState) -> dict[str, Any]:
    plans: list[dict[str, Any]] = []
    for run in state.order_plans.all_runs():
        for plan in run.get("plans") or []:
            row = dict(plan)
            row["run_id"] = run.get("run_id")
            row["run_created_at"] = run.get("created_at")
            row["notional_display"] = _usd(plan.get("notional"))
            row["status"] = plan.get("execution_status")
            row["blocked"] = list(plan.get("blocked_reasons") or [])
            row["live_execution_blocked"] = True
            row["broker_submitted"] = False
            row["book_label"] = PAPER_BOOK_LABEL
            plans.append(row)
    fills: list[dict[str, Any]] = []
    for run in state.fills.all_runs():
        skipped = run.get("skipped") or []
        for fill in run.get("fills") or []:
            row = dict(fill)
            row["run_id"] = run.get("run_id")
            row["filled_notional_display"] = _usd(fill.get("filled_notional"))
            row["book_label"] = PAPER_BOOK_LABEL
            fills.append(row)
        for skip in skipped:
            if isinstance(skip, dict):
                fills.append(
                    {
                        "fill_id": None,
                        "symbol": skip.get("symbol"),
                        "status": skip.get("reason") or skip.get("status") or "SKIPPED",
                        "reject_reasons": [skip.get("reason")] if skip.get("reason") else [],
                        "run_id": run.get("run_id"),
                        "order_plan_id": skip.get("order_plan_id"),
                    }
                )
    reviews = []
    for result in state.reviews.all_results():
        row = to_dict(result)
        row["order_placed"] = False
        row["broker_submitted"] = False
        row["book_label"] = LIVE_ACCOUNT_LABEL
        row["live_order_placement_enabled"] = False
        reviews.append(row)
    return {
        "plans": plans,
        "fills": fills,
        "reviews": reviews,
        "book_label": PAPER_BOOK_LABEL,
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "live_order_placement_enabled": False,
    }


def journal_view(state: DashboardState, *, limit: int = 250) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    logs = state.root / "logs"
    for source, name in JOURNAL_FILES:
        for item in read_jsonl(logs / name):
            row = dict(item)
            row["_source"] = source
            row["_file"] = name
            row["_at"] = row.get("logged_at") or row.get("timestamp") or row.get("created_at") or row.get("reviewed_at")
            if "type" not in row:
                if source == "risk":
                    row["type"] = "RISK_GATE"
                elif source == "review":
                    row["type"] = row.get("status") or "REVIEW"
            rows.append(row)
    rows.sort(key=lambda r: str(r.get("_at") or ""), reverse=True)
    return {
        "entries": rows[:limit],
        "count": len(rows),
        "book_label": PAPER_BOOK_LABEL,
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "live_order_placement_enabled": False,
    }


def monitoring_alerts(state: DashboardState) -> list[dict[str, Any]]:
    latest = state.monitoring.latest()
    if not latest:
        return []
    alerts: list[dict[str, Any]] = []
    for pos in latest.get("positions") or []:
        state_name = pos.get("state") or pos.get("preliminary_state")
        triggers = pos.get("triggers") or []
        if state_name in {None, "HEALTHY"} and not triggers:
            continue
        if state_name == "HEALTHY" and not triggers:
            continue
        alerts.append(
            {
                "symbol": pos.get("symbol"),
                "state": state_name,
                "recommended_action": pos.get("recommended_action"),
                "triggers": triggers,
                "run_id": latest.get("run_id"),
                "created_at": latest.get("created_at"),
                "rationale": (pos.get("reassessment") or {}).get("rationale"),
            }
        )
    return alerts


def recent_decisions(state: DashboardState, *, limit: int = 12) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for run in state.decisions.all_runs()[:limit]:
        names = run.get("decisions") or []
        if isinstance(names, dict):
            items = [{"symbol": k, "decision": v} for k, v in names.items()]
        else:
            items = list(names)
        out.append(
            {
                "batch_id": run.get("batch_id") or run.get("run_id"),
                "created_at": run.get("created_at"),
                "symbols": run.get("symbols") or [],
                "decisions": items,
                "comparison": run.get("comparison"),
            }
        )
    return out


def health_status(state: DashboardState) -> list[dict[str, Any]]:
    flags = execution_flags()
    ui = resolve_ui_flags()
    latest_monitor = state.monitoring.latest() or {}
    reports = state.research.all_reports()
    decision_ids = state.decisions.all_ids()
    risk_rows = read_jsonl(state.root / "logs" / "risk_gate.jsonl")
    review_ids = state.reviews.all_ids()
    ctx = paper_context(state)
    return [
        {
            "id": "dashboard",
            "name": "dashboard",
            "ok": True,
            "status": "up",
            "detail": "localhost",
        },
        {
            "id": "monitoring",
            "name": "monitoring",
            "ok": bool(latest_monitor),
            "status": "up" if latest_monitor else "missing",
            "detail": latest_monitor.get("created_at") or "no run",
        },
        {
            "id": "research",
            "name": "research",
            "ok": bool(reports),
            "status": "up" if reports else "missing",
            "detail": f"{len(reports)} reports",
        },
        {
            "id": "decision",
            "name": "decision",
            "ok": bool(decision_ids),
            "status": "up" if decision_ids else "missing",
            "detail": f"{len(decision_ids)} batches",
        },
        {
            "id": "risk_gate",
            "name": "risk gate",
            "ok": bool(risk_rows) or bool(ctx.get("risk_state")),
            "status": "up" if (risk_rows or ctx.get("risk_state")) else "missing",
            "detail": ctx.get("risk_state") or (f"{len(risk_rows)} journal rows" if risk_rows else "no journal"),
        },
        {
            "id": "review_bridge",
            "name": "review bridge",
            "ok": bool(review_ids),
            "status": "up" if review_ids else "missing",
            "detail": "preflight only; does not place",
        },
        {
            "id": "live_placement",
            "name": "live placement disabled",
            "ok": True,
            "status": "disabled",
            "detail": ui["no_live_placement_banner"],
            "live_order_placement_enabled": False,
            "autonomous_trading_disabled": flags["autonomous_trading_disabled"],
        },
    ]


def system_view(state: DashboardState) -> dict[str, Any]:
    flags = execution_flags()
    ui = resolve_ui_flags()
    rules = load_account_rules()
    policy = load_policy()
    pipeline = load_pipeline_config()
    hwm = load_hwm_state(state.root / "state" / "hwm_state.json") or {}
    session = load_session_state(state.root / "state" / "session_state.json")
    book = paper_book(state)
    ctx = paper_context(state)
    discovery_runs = state.discovery.all()
    latest_discovery = max(discovery_runs, key=lambda r: r.started_at) if discovery_runs else None
    latest_monitor = state.monitoring.latest() or {}
    reports = [to_dict(r) for r in state.research.all_reports()]
    freshness = [
        {
            "label": "Paper book",
            "present": bool(book),
            "updated_at": book.get("created_at"),
            "detail": "isolated paper blotter",
        },
        {
            "label": "Live HWM state",
            "present": bool(hwm),
            "updated_at": hwm.get("updated_at"),
            "detail": hwm.get("risk_state"),
        },
        {
            "label": "Session SOD",
            "present": session is not None,
            "updated_at": session.last_observed_at if session else None,
            "detail": session.session_id if session else "missing",
        },
        {
            "label": "Discovery",
            "present": latest_discovery is not None,
            "updated_at": latest_discovery.completed_at if latest_discovery else None,
            "detail": latest_discovery.data_freshness if latest_discovery else None,
        },
        {
            "label": "Monitoring",
            "present": bool(latest_monitor),
            "updated_at": latest_monitor.get("created_at"),
            "detail": ",".join((latest_monitor.get("symbols") or [])[:6]),
        },
        {
            "label": "Research reports",
            "present": bool(reports),
            "updated_at": max((r.get("completed_at") or "") for r in reports) if reports else None,
            "detail": f"{len(reports)} reports",
        },
    ]
    services = [
        {"name": "Approval packets", "ok": bool(state.approvals.all_ids()), "count": len(state.approvals.all_ids())},
        {"name": "Order plans", "ok": bool(state.order_plans.all_ids()), "count": len(state.order_plans.all_ids())},
        {"name": "Paper fills", "ok": bool(state.fills.all_ids()), "count": len(state.fills.all_ids())},
        {"name": "Robinhood reviews", "ok": bool(state.reviews.all_ids()), "count": len(state.reviews.all_ids())},
        {"name": "Candidates", "ok": True, "count": len(state.candidates.all())},
        {"name": "Theses (paper)", "ok": True, "count": len(state.theses_paper.all_records())},
    ]
    return {
        "execution": flags,
        "account_nickname": (rules.get("account") or {}).get("nickname"),
        "live_observed": (rules.get("account") or {}).get("last_observed"),
        "hwm": hwm,
        "session": session.to_dict() if session else None,
        "paper_context": {
            "nav": ctx.get("current_nav"),
            "cash": ctx.get("cash"),
            "risk_state": ctx.get("risk_state"),
            "updated_at": book.get("created_at"),
        },
        "freshness": freshness,
        "services": services,
        "health": health_status(state),
        "environment": ui["environment"],
        "paper_book_label": PAPER_BOOK_LABEL,
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "live_order_placement_enabled": False,
        "pipeline_stop": pipeline.get("hard_stop_after"),
        "policy_version": policy.get("version"),
        "daily_halt_threshold": (policy.get("daily_risk_halt") or {}).get("threshold_fraction_of_start_of_day_nav"),
        "risk_limits_read_only": True,
        "writes_allowed": ["approve_packet", "reject_packet"],
    }


def _thesis_index(state: DashboardState) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in _merge_theses(state):
        by_id[str(row.get("thesis_id"))] = row
    return by_id


def _company_index(reports: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for report in reports:
        symbol = str(report.get("symbol") or "").upper()
        if not symbol or symbol in out:
            continue
        for key in ("company_name", "company", "issuer_name", "name"):
            value = report.get(key)
            if value and str(value).strip() and str(value).strip().upper() != symbol:
                out[symbol] = str(value).strip()
                break
    return out


def _qty_display(value: Any) -> str:
    if value is None:
        return "—"
    number = float(value)
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return text


def allocation_slices(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    sleeves = ctx.get("sleeve_allocation_pct") or {}
    cash_pct = ctx.get("cash_allocation_pct")
    slices: list[dict[str, Any]] = []
    for key in ALLOCATION_ORDER:
        raw = cash_pct if key == "CASH" else sleeves.get(key)
        pct = float(raw) if raw is not None else 0.0
        slices.append(
            {
                "key": key,
                "label": SLEEVE_LABELS[key],
                "pct": pct,
                "display": _pct_fraction(pct),
            }
        )
    return slices


def compact_activity(state: DashboardState) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for batch in recent_decisions(state, limit=4):
        created = batch.get("created_at")
        for decision in (batch.get("decisions") or [])[:4]:
            if not isinstance(decision, dict):
                continue
            symbol = decision.get("symbol")
            action = decision.get("decision")
            if not symbol:
                continue
            items.append(
                {
                    "kind": "decision",
                    "label": "Decision",
                    "title": f"{symbol} {friendly_enum(str(action) if action else '')}",
                    "meta": created,
                    "href": "/journal",
                }
            )
    for alert in monitoring_alerts(state)[:4]:
        items.append(
            {
                "kind": "alert",
                "label": "Monitor",
                "title": f"{alert.get('symbol')} {friendly_enum(alert.get('state'))}",
                "meta": alert.get("created_at"),
                "href": "/journal",
            }
        )
    reports = [to_dict(r) for r in state.research.all_reports()]
    reports.sort(key=lambda r: str(r.get("completed_at") or r.get("started_at") or ""), reverse=True)
    for report in reports[:4]:
        rid = report.get("research_id")
        items.append(
            {
                "kind": "research",
                "label": "Research",
                "title": f"{report.get('symbol')} {friendly_enum(report.get('research_conclusion') or report.get('research_status'))}",
                "meta": report.get("completed_at") or report.get("started_at"),
                "href": f"/research/reports/{rid}" if rid else "/research",
            }
        )
    approvals = list_approvals(state)
    for packet in (approvals.get("pending") or [])[:4]:
        items.append(
            {
                "kind": "approval",
                "label": "Approval",
                "title": f"{packet.get('symbol')} {packet.get('action')}",
                "meta": packet.get("created_at"),
                "href": f"/approvals/{packet.get('approval_id')}",
            }
        )
    items.sort(key=lambda row: str(row.get("meta") or ""), reverse=True)
    return items[:12]


def _queue_status_by_candidate(state: DashboardState) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in state.queue.all():
        status = entry.status.value if hasattr(entry.status, "value") else str(entry.status or "")
        cid = str(entry.candidate_id)
        if cid not in out:
            out[cid] = status
        symbol = str(entry.symbol or "").upper()
        out.setdefault(f"sym:{symbol}", status)
    return out


def candidate_rows(state: DashboardState) -> list[dict[str, Any]]:
    queue_status = _queue_status_by_candidate(state)
    rows: list[dict[str, Any]] = []
    for cand in state.candidates.all():
        raw = to_dict(cand)
        sources = list(raw.get("discovery_sources") or [])
        if not sources and raw.get("discovery_source"):
            sources = [part.strip() for part in str(raw["discovery_source"]).split(",") if part.strip()]
        signals = []
        for signal in raw.get("signals") or []:
            if isinstance(signal, dict):
                signals.append(friendly_reason(signal.get("name")))
            else:
                signals.append(friendly_reason(getattr(signal, "name", None)))
        research_status = queue_status.get(str(raw.get("candidate_id"))) or queue_status.get(
            f"sym:{str(raw.get('symbol') or '').upper()}"
        )
        rows.append(
            {
                **raw,
                "sleeve_label": friendly_enum(raw.get("provisional_sleeve")),
                "priority_label": friendly_enum(raw.get("priority")),
                "status_label": friendly_enum(raw.get("status")),
                "sector_label": friendly_enum(raw.get("sector")),
                "class_label": friendly_enum(raw.get("security_class")),
                "score_display": f"{float(raw.get('discovery_score') or 0):.1f}",
                "sources_display": sources,
                "reason_labels": [friendly_reason(r) for r in (raw.get("reasons") or [])[:6]],
                "signal_labels": signals[:6],
                "risk_labels": [friendly_reason(r) for r in (raw.get("known_risks") or [])[:6]],
                "event_labels": [friendly_enum(r) for r in (raw.get("event_flags") or [])[:6]],
                "research_status": research_status,
                "research_status_label": friendly_enum(research_status) if research_status else "—",
            }
        )
    rows.sort(key=lambda r: float(r.get("discovery_score") or 0), reverse=True)
    return rows


def discovery_view(state: DashboardState) -> dict[str, Any]:
    ui = resolve_ui_flags()
    rows = candidate_rows(state)
    return {
        "candidates": rows,
        "count": len(rows),
        "book_label": PAPER_BOOK_LABEL,
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "live_order_placement_enabled": False,
        "environment": ui["environment"],
        "note": "Read from stored discovery results. This page does not run discovery.",
    }


def dashboard_view(state: DashboardState) -> dict[str, Any]:
    flags = execution_flags()
    ui = resolve_ui_flags()
    book = paper_book(state)
    ctx = paper_context(state)
    hwm = load_hwm_state(state.root / "state" / "hwm_state.json") or {}
    reports = [to_dict(r) for r in state.research.all_reports()]
    sleeves = ctx.get("sleeve_allocation_pct") or {}
    theses = _thesis_index(state)
    companies = _company_index(reports)
    nav = ctx.get("current_nav")
    positions = []
    for pos in ctx.get("positions") or []:
        mv = pos.get("market_value")
        pct = (mv / nav) if nav and mv is not None else None
        thesis = theses.get(str(pos.get("thesis_id") or ""))
        pnl = pos.get("unrealized_pnl")
        symbol = str(pos.get("symbol") or "")
        positions.append(
            {
                **pos,
                "company": companies.get(symbol.upper()),
                "sleeve_label": friendly_enum(pos.get("sleeve")),
                "quantity_display": _qty_display(pos.get("quantity")),
                "price_display": pos.get("current_price") if pos.get("current_price") is not None else "—",
                "allocation_display": _pct_fraction(pct),
                "market_value_display": _usd(mv),
                "unrealized_pnl": pnl,
                "unrealized_display": _signed_usd(pnl) if pnl is not None else "—",
                "thesis_status": thesis.get("status") if thesis else None,
                "thesis_status_label": friendly_enum(thesis.get("status")) if thesis else "—",
            }
        )
    history = record_nav_snapshot(
        state.root,
        nav=float(nav) if nav is not None else None,
        spy=(ctx.get("spy") if isinstance(ctx.get("spy"), dict) else None),
        at=ctx.get("timestamp") or book.get("created_at"),
    )
    port_ret = total_return(history)
    spy_ret = spy_return(history)
    daily = ctx.get("daily_portfolio_return")
    sod = ctx.get("start_of_day_nav")
    today_dollars = None
    if daily is not None and sod is not None:
        today_dollars = float(daily) * float(sod)
    today_display = None
    if daily is not None:
        today_display = _signed_pct(daily) if today_dollars is None else f"{_signed_usd(today_dollars)} ({_signed_pct(daily)})"
    drawdown = ctx.get("current_drawdown") if ctx.get("current_drawdown") is not None else hwm.get("drawdown")
    risk_state = ctx.get("risk_state") or hwm.get("risk_state")
    excess = None if port_ret is None or spy_ret is None else port_ret - spy_ret
    slices = allocation_slices(ctx)
    candidates = candidate_rows(state)
    ready = chart_ready(history)
    return {
        "execution": flags,
        "book_kind": "paper",
        "environment": ui["environment"],
        "paper_book_label": PAPER_BOOK_LABEL,
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "live_order_placement_enabled": False,
        "health": health_status(state),
        "paper_environment": bool(book.get("paper_environment", True)),
        "live_book_untouched": bool(book.get("live_book_untouched", True)),
        "nav": ctx.get("current_nav"),
        "nav_display": _usd(ctx.get("current_nav")),
        "cash": ctx.get("cash"),
        "cash_display": _usd(ctx.get("cash")),
        "cash_pct": ctx.get("cash_allocation_pct"),
        "cash_pct_display": _pct_fraction(ctx.get("cash_allocation_pct")),
        "buying_power": ctx.get("buying_power"),
        "holdings_count": ctx.get("holdings_count") or len(positions),
        "risk_state": risk_state,
        "daily_risk_halt": bool(ctx.get("daily_risk_halt")),
        "high_water_mark": ctx.get("high_water_mark") or hwm.get("cash_flow_adjusted_hwm"),
        "hwm_display": _usd(ctx.get("high_water_mark") or hwm.get("cash_flow_adjusted_hwm")),
        "drawdown": drawdown,
        "drawdown_display": _pct_fraction(drawdown),
        "start_of_day_nav": ctx.get("start_of_day_nav"),
        "daily_return": daily,
        "daily_return_display": _pct_fraction(daily),
        "updated_at": ctx.get("timestamp") or book.get("created_at"),
        "sleeves": [
            {
                "sleeve": name,
                "label": friendly_enum(name),
                "pct": value,
                "display": _pct_fraction(value),
                "market_value": (ctx.get("sleeve_market_values") or {}).get(name),
            }
            for name, value in sleeves.items()
        ],
        "positions": positions,
        "spy": spy_benchmark(ctx, reports),
        "live_hwm": hwm,
        "approvals": list_approvals(state),
        "alerts": monitoring_alerts(state),
        "recent_decisions": recent_decisions(state),
        "approved_does_not_place_order": True,
        "kpis": {
            "portfolio_value": _metric(nav, _usd(nav)),
            "cash": _metric(ctx.get("cash"), f"{_usd(ctx.get('cash'))} ({_pct_fraction(ctx.get('cash_allocation_pct'))})"),
            "today_pnl": _metric(daily, today_display),
            "total_return": _metric(port_ret, _signed_pct(port_ret)),
            "spy_return": _metric(spy_ret, _signed_pct(spy_ret)),
            "excess_return": _metric(excess, _signed_pct(excess)),
            "drawdown": _metric(drawdown, _pct_fraction(drawdown)),
            "risk_state": _metric(risk_state, risk_state, available=bool(risk_state)),
        },
        "allocation_chart": {
            "labels": [row["label"] for row in slices],
            "values": [round(row["pct"] * 100.0, 4) for row in slices],
            "keys": [row["key"] for row in slices],
            "slices": slices,
        },
        "performance": {
            "ready": ready,
            "message": None if ready else HISTORY_COLLECTING,
            "labels": [row["at"] for row in history],
            "nav": [row["nav"] for row in history],
            "spy": [row.get("spy") for row in history],
            "has_spy": any(row.get("spy") is not None for row in history),
        },
        "activity": compact_activity(state),
        "top_candidates": candidates[:5],
        "candidate_count": len(candidates),
        "unavailable": UNAVAILABLE,
        "history_collecting": HISTORY_COLLECTING,
    }
