"""Read-only dashboard views over existing stores. No portfolio math rewrite."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_portfolio.agent.activity import read_activity
from agentic_portfolio.agent.heartbeat import load_health
from agentic_portfolio.agent.pipeline import primary_queue_stores
from agentic_portfolio.approval.engine import record_human_decision
from agentic_portfolio.approval.report import render_packet
from agentic_portfolio.approval.store import ApprovalStore
from agentic_portfolio.approval.types import ApprovalPacket, ApprovalStatus
from agentic_portfolio.approval.validate import ApprovalValidationError
from agentic_portfolio.live_approval import LiveApproval, LiveApprovalEngine, LiveApprovalStatus, LiveApprovalStore
from agentic_portfolio.notify import NotificationStore
from agentic_portfolio.watch import WatchStore
from agentic_portfolio.dashboard.display import newest_first, ts_sort_key
from agentic_portfolio.dashboard.history import (
    chart_ready,
    record_nav_snapshot,
    spy_return,
    total_return,
)
from agentic_portfolio.dashboard.labels import (
    ALLOCATION_ORDER,
    HISTORY_COLLECTING,
    LIVE_DATA_UNAVAILABLE,
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
from agentic_portfolio.lifecycle import latest_by_symbol
from agentic_portfolio.research.operational import looks_like_schema_failure_report
from agentic_portfolio.live.isolation import detect_paper_contamination
from agentic_portfolio.live.store import LivePortfolioStore
from agentic_portfolio.monitoring.store import MonitoringStore
from agentic_portfolio.paper_fill.store import PaperFillStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules, load_pipeline_config, load_policy
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.review.store import ReviewStore
from agentic_portfolio.runtime import (
    RuntimeMode,
    artifact_environment,
    discovery_state_dir,
    get_active_artifact_environment,
    get_active_portfolio_source,
    get_active_runtime,
    live_execution_authority,
    live_placement_enabled,
)
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
    runtime: RuntimeMode
    artifact_environment: str
    approvals: ApprovalStore
    order_plans: OrderPlanStore
    fills: PaperFillStore
    reviews: ReviewStore
    monitoring: MonitoringStore
    decisions: DecisionStore
    research: ResearchStore
    candidates: CandidateStore
    candidates_paper: CandidateStore
    queue: ResearchQueue
    queue_paper: ResearchQueue
    discovery: DiscoveryRunStore
    discovery_paper: DiscoveryRunStore
    theses_live: ThesisRegistry
    theses_paper: ThesisRegistry
    theses_monitor: ThesisRegistry
    sleeves_live: SleeveRegistry
    sleeves_paper: SleeveRegistry


def dashboard_state(root: Path | None = None) -> DashboardState:
    base = root or project_root()
    state_dir = base / "state"
    runtime = get_active_runtime()
    env = get_active_artifact_environment()
    discovery_dir = discovery_state_dir(base, mode=runtime)
    paper_candidates = CandidateStore(state_dir / "candidates.json", runtime_mode=RuntimeMode.PAPER.value)
    paper_queue = ResearchQueue(state_dir / "research_queue.json", runtime_mode=RuntimeMode.PAPER.value)
    paper_discovery = DiscoveryRunStore(state_dir / "discovery_runs.json", runtime_mode=RuntimeMode.PAPER.value)
    if runtime is RuntimeMode.LIVE:
        active_candidates, active_queue = primary_queue_stores(base, runtime_mode=runtime)
        live_runs = DiscoveryRunStore(discovery_dir / "discovery_runs.json", runtime_mode=RuntimeMode.LIVE.value)
        active_discovery = live_runs
    else:
        active_candidates = paper_candidates
        active_queue = paper_queue
        active_discovery = paper_discovery
    return DashboardState(
        root=base,
        runtime=runtime,
        artifact_environment=env,
        approvals=ApprovalStore(base),
        order_plans=OrderPlanStore(base),
        fills=PaperFillStore(base),
        reviews=ReviewStore(base),
        monitoring=MonitoringStore(base),
        decisions=DecisionStore(base),
        research=ResearchStore(base),
        candidates=active_candidates,
        candidates_paper=paper_candidates,
        queue=active_queue,
        queue_paper=paper_queue,
        discovery=active_discovery,
        discovery_paper=paper_discovery,
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
    if not ok:
        return {
            "available": False,
            "value": value,
            "display": display if display == LIVE_DATA_UNAVAILABLE else UNAVAILABLE,
        }
    return {
        "available": True,
        "value": value,
        "display": display if display else UNAVAILABLE,
    }


def execution_flags(rules: dict[str, Any] | None = None) -> dict[str, Any]:
    rules = rules or load_account_rules()
    exe = dict(rules.get("execution") or {})
    auth = live_execution_authority()
    return {
        "state": exe.get("state") or exe.get("mode") or "HUMAN_APPROVAL",
        "mode": exe.get("mode") or "HUMAN_APPROVAL",
        "auto_execution": False,
        "require_human_approval": True,
        "live_trade_actions_allowed": auth.live_trade_actions_allowed,
        "current_terminal_stage": exe.get("current_terminal_stage"),
        "autonomous_trading_disabled": True,
        "approved_does_not_place_order": not auth.LIVE_ORDER_PLACEMENT,
        "live_order_placement_enabled": auth.LIVE_ORDER_PLACEMENT,
        "execution_mode": auth.execution_mode,
        "observation_mode": auth.observation_mode,
        "placement_requested": auth.placement_requested,
        "write_transport_ready": auth.write_transport_ready,
    }


def _dashboard_executor(state: DashboardState):
    from agentic_portfolio.live.store import LivePortfolioStore
    from agentic_portfolio.live_execution import ExecutionStore, LiveOrderExecutor, bind_live_write_broker
    from agentic_portfolio.context import portfolio_context_from_dict
    from agentic_portfolio.agent.session import classify_market_phase
    from agentic_portfolio.policy import load_account_rules

    def context_fn():
        book = LivePortfolioStore(state.root).current_book() or {}
        raw = book.get("context")
        if isinstance(raw, dict):
            return portfolio_context_from_dict(raw)
        return None

    broker = None
    mode = state.runtime.value if hasattr(state.runtime, "value") else str(state.runtime)
    if live_placement_enabled() and str(mode).upper() == "LIVE":
        broker = bind_live_write_broker(account_number=str(load_account_rules()["account"]["account_number"]))
    return LiveOrderExecutor(
        ExecutionStore(state.root, runtime_mode=state.runtime),
        broker,
        root=state.root,
        runtime_mode=state.runtime,
        context_fn=context_fn,
        regular_hours_fn=lambda: classify_market_phase().regular_hours_open,
    )


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
    if flags.get("environment") == "LIVE" and kind in {"paper", "demo", "stale"}:
        return "paper artifacts are not LIVE decisions"
    if kind == "stale" and not flags["allow_stale_packet_decisions"]:
        return "stale packets cannot be approved or rejected"
    if kind == "demo" and not flags["allow_demo_packet_decisions"]:
        return "demo packet decisions are disabled unless explicitly enabled"
    if kind == "paper" and not flags["allow_paper_packet_decisions"]:
        return "paper packet decisions are disabled unless explicitly enabled"
    return None


def paper_book(state: DashboardState) -> dict[str, Any]:
    try:
        return state.fills.current_book() or {}
    except (OSError, json.JSONDecodeError):
        return {}


def paper_context(state: DashboardState) -> dict[str, Any]:
    return dict((paper_book(state).get("context") or {}))


def live_book(state: DashboardState) -> dict[str, Any]:
    try:
        return LivePortfolioStore(state.root).current_book() or {}
    except (OSError, json.JSONDecodeError):
        return {}


def live_context(state: DashboardState) -> dict[str, Any]:
    return dict((live_book(state).get("context") or {}))


def _ui(flags: dict[str, Any] | None = None) -> dict[str, Any]:
    return flags or resolve_ui_flags()


def live_data_unavailable(state: DashboardState, flags: dict[str, Any] | None = None) -> bool:
    flags = _ui(flags)
    if get_active_runtime() is not RuntimeMode.LIVE and flags.get("environment") != "LIVE":
        return False
    ctx = live_context(state)
    book = live_book(state)
    return not book or ctx.get("current_nav") is None


def live_error_state(state: DashboardState) -> dict[str, Any]:
    try:
        payload = LivePortfolioStore(state.root).last_error() or {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    health = load_health(state.root)
    if not payload:
        payload = dict(health.get("live_error") or {})
    broker = dict(health.get("robinhood") or {})
    code = payload.get("code") or broker.get("error_code")
    message = payload.get("message") or broker.get("error")
    return {
        "code": code,
        "message": message,
        "observed_at": payload.get("observed_at"),
        "job_skips": list(health.get("job_skips") or []),
    }


def active_book(state: DashboardState, flags: dict[str, Any] | None = None) -> dict[str, Any]:
    flags = _ui(flags)
    if get_active_runtime() is RuntimeMode.LIVE or flags.get("environment") == "LIVE":
        return live_book(state)
    return paper_book(state)


def active_context(state: DashboardState, flags: dict[str, Any] | None = None) -> dict[str, Any]:
    """PAPER → paper book. LIVE → Robinhood snapshot. LIVE never falls back to paper."""
    flags = _ui(flags)
    if get_active_runtime() is RuntimeMode.LIVE or flags.get("environment") == "LIVE":
        return live_context(state)
    return paper_context(state)


def live_hwm_state(state: DashboardState, flags: dict[str, Any] | None = None) -> dict[str, Any]:
    flags = _ui(flags)
    if get_active_runtime() is RuntimeMode.LIVE or flags.get("environment") == "LIVE":
        try:
            return load_hwm_state(LivePortfolioStore(state.root).hwm_path()) or {}
        except (OSError, json.JSONDecodeError):
            return {}
    return load_hwm_state(state.root / "state" / "hwm_state.json") or {}


def spy_benchmark(ctx: dict[str, Any], reports: list[dict[str, Any]], *, flags: dict[str, Any] | None = None) -> dict[str, Any]:
    flags = _ui(flags)
    live = flags.get("environment") == "LIVE" or get_active_runtime() is RuntimeMode.LIVE
    source_label = "live_robinhood_context" if live else "paper_book_context"
    book_note = "LIVE Robinhood snapshot" if live else "paper book"
    spy = ctx.get("spy")
    if isinstance(spy, dict) and spy:
        return {
            "observed": True,
            "source": source_label,
            "payload": spy,
            "note": f"Observed SPY benchmark from the {book_note} context. Not fabricated.",
        }
    spy_report = next((r for r in reports if str(r.get("symbol") or "").upper() == "SPY"), None)
    if spy_report and not live:
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
        "note": f"SPY benchmark is not on the current {book_note}. Not fabricated.",
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
        "runtime_mode": artifact_environment(packet),
        "decision_block_reason": blocked,
        "can_decide": pending and blocked is None,
        "approved_does_not_place_order": True,
        "broker_submitted": False,
        "live_execution_blocked": True,
        "live_order_placement_enabled": live_execution_authority().LIVE_ORDER_PLACEMENT,
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


def _packet_env(state: DashboardState, packet: ApprovalPacket) -> str:
    raw = state.approvals.get(packet.approval_id) or {}
    tagged = artifact_environment(raw)
    if tagged == RuntimeMode.LIVE.value:
        return RuntimeMode.LIVE.value
    return artifact_environment(packet)


def _operational_packets(state: DashboardState, flags: dict[str, Any] | None = None) -> list[ApprovalPacket]:
    flags = _ui(flags)
    env = get_active_artifact_environment()
    out: list[ApprovalPacket] = []
    for packet in state.approvals.all_packets():
        if _packet_env(state, packet) != env:
            continue
        if env == RuntimeMode.LIVE.value and packet_kind(packet) in {"paper", "demo"}:
            continue
        out.append(packet)
    return out


def _live_approval_store(state: DashboardState) -> LiveApprovalStore:
    return LiveApprovalStore(state.root, runtime_mode=state.runtime)


def _watch_store(state: DashboardState) -> WatchStore:
    return WatchStore(state.root, runtime_mode=state.runtime)


def _live_approval_row(item: LiveApproval) -> dict[str, Any]:
    pending = item.status == LiveApprovalStatus.PENDING
    stale = item.status == LiveApprovalStatus.EXPIRED
    return {
        "approval_id": item.approval_id,
        "symbol": item.ticker,
        "ticker": item.ticker,
        "action": item.proposed_action,
        "status": item.status.value,
        "sleeve": None,
        "current_allocation_pct": None,
        "desired_allocation_pct": item.proposed_allocation_pct,
        "current_allocation_display": "—",
        "desired_allocation_display": _pct_points(item.proposed_allocation_pct),
        "order_notional": item.proposed_dollar_amount,
        "order_notional_display": _usd(item.proposed_dollar_amount),
        "order_quantity": None,
        "approx_shares": (
            round(float(item.proposed_dollar_amount) / float(item.current_quote), 4)
            if item.proposed_dollar_amount and item.current_quote
            else None
        ),
        "nav_display": _usd((item.portfolio_impact or {}).get("nav") or item.nav_at_proposal),
        "cash_display": _usd((item.portfolio_impact or {}).get("cash")),
        "buying_power_display": _usd((item.portfolio_impact or {}).get("buying_power")),
        "existing_exposure": (item.portfolio_impact or {}).get("expected_resulting_position_pct"),
        "execution_status_label": (
            "APPROVED — LIVE EXECUTION DISABLED"
            if item.status.value == "APPROVED_EXECUTION_DISABLED"
            else ("ORDER SUBMITTED" if item.broker_submitted else item.status.value)
        ),
        "current_price": item.current_quote,
        "risk_gate_verdict": (item.risk_gate_result or {}).get("verdict"),
        "created_at": item.created_at,
        "expires_at": item.expires_at,
        "decided_at": item.decided_at,
        "expiry_reasons": ["expired"] if stale else [],
        "superseded_by": item.superseded_by,
        "pending": pending,
        "stale": stale,
        "packet_kind": "live",
        "queue_kind": "live",
        "book_label": LIVE_ACCOUNT_LABEL,
        "runtime_mode": item.runtime_mode,
        "decision_block_reason": None,
        "can_decide": pending,
        "approved_does_not_place_order": not live_execution_authority().LIVE_ORDER_PLACEMENT,
        "broker_submitted": bool(item.broker_submitted),
        "live_execution_blocked": bool(item.live_execution_blocked),
        "live_order_placement_enabled": live_execution_authority().LIVE_ORDER_PLACEMENT,
        "placed_order": bool(item.placed_order),
        "ai_rationale": item.ai_rationale,
        "supporting_thesis": item.supporting_thesis,
        "reason": item.reason,
        "current_spread_bps": item.current_spread_bps,
        "portfolio_impact": item.portfolio_impact,
        "risk_gate_result": item.risk_gate_result,
        "sleeve": item.sleeve or (item.portfolio_impact or {}).get("sleeve"),
        "research_summary": item.research_summary,
        "catalysts": list(item.catalysts or []),
        "key_risks": list(item.key_risks or []),
        "invalidation": list(item.invalidation or []),
        "expected_horizon": item.expected_horizon,
        "provider": item.provider,
        "model": item.model,
        "nav_at_proposal": item.nav_at_proposal,
        "quote_at_proposal": item.quote_at_proposal or item.current_quote,
        "evidence_freshness": item.evidence_freshness,
    }


def live_approval_detail(item: LiveApproval) -> dict[str, Any]:
    row = _live_approval_row(item)
    row.update(
        {
            "thesis_summary": item.supporting_thesis,
            "why_now": item.reason,
            "research_summary": item.research_summary or item.ai_rationale,
            "why_not_cash": None,
            "why_not_spy": None,
            "bull_case": None,
            "base_case": item.ai_rationale,
            "bear_case": None,
            "key_risks": list(item.key_risks or []),
            "catalysts": list(item.catalysts or []),
            "invalidation_exit_policy": "; ".join(item.invalidation or []) or None,
            "expected_horizon": item.expected_horizon,
            "ai_provenance": f"{item.provider or '—'} / {item.model or '—'}",
            "portfolio_effect": str(item.portfolio_impact or "—"),
            "sector_concentration_effect": None,
            "enhanced_review_requirements": [],
            "order_plan_summary": {
                "side": item.proposed_action,
                "order_type": "market",
                "time_in_force": "gfd",
                "execution_status": item.status.value,
                "live_execution_blocked": bool(item.live_execution_blocked),
                "broker_submitted": bool(item.broker_submitted),
                "stop_orders_created": 0,
            },
            "evidence_refs": {"watch_id": item.watch_id},
            "snapshot": {},
            "status_history": [],
            "human_note": item.decision_note,
            "monitoring_state": None,
            "sector": None,
            "explanation": item.ai_rationale or item.reason or item.supporting_thesis or "",
            "raw": item.to_dict(),
        }
    )
    return row


def watchlist_view(state: DashboardState) -> dict[str, Any]:
    items = []
    lifecycle = latest_by_symbol(state.root)
    for item in _watch_store(state).all():
        plan = item.conditional_plan
        last = lifecycle.get(str(item.ticker).upper()) or {}
        items.append(
            {
                "watch_id": item.watch_id,
                "ticker": item.ticker,
                "status": item.status.value,
                "thesis": item.research_thesis,
                "confidence": item.confidence,
                "score": item.source_candidate_score,
                "entry_conditions": list(item.entry_conditions),
                "invalidating_conditions": list(item.invalidating_conditions),
                "last_reassessed": item.last_reassessed_at,
                "next_review_at": item.next_review_at,
                "expiration": item.expiration,
                "required_market_confirmation": item.required_market_confirmation,
                "max_price": plan.max_price if plan else None,
                "max_spread_bps": plan.max_spread_bps if plan else None,
                "last_updated": item.last_updated,
                "approval_id": item.approval_id,
                "sleeve": item.sleeve,
                "sleeve_label": friendly_enum(item.sleeve),
                "catalysts": list(item.catalysts or []),
                "invalidation": list(item.invalidating_conditions or []),
                "reason_for_watch": item.reason_for_watch,
                "research_id": item.research_id,
                "thesis_id": item.thesis_id,
                "last_state_change": last.get("reason"),
                "last_state_from": last.get("from_status"),
                "last_state_to": last.get("to_status") or last.get("to_status"),
                "last_state_at": last.get("logged_at"),
                "operational_error": bool(last.get("operational_failure")),
            }
        )
    terminal = {"REJECTED", "EXPIRED", "INVALIDATED"}
    items = newest_first(items, "last_state_at", "last_updated", "last_reassessed", "next_review_at")
    active_rows = [row for row in items if row["status"] not in terminal]
    sleeve_order = [key for key in ALLOCATION_ORDER if key != "CASH"]
    groups = []
    for key in sleeve_order:
        rows = [row for row in active_rows if row.get("sleeve") == key]
        groups.append(
            {
                "id": key,
                "label": SLEEVE_LABELS.get(key, friendly_enum(key)),
                "rows": rows,
                "count": len(rows),
                "active": len(rows),
            }
        )
    unassigned = [row for row in active_rows if not row.get("sleeve") or row.get("sleeve") not in sleeve_order]
    if unassigned:
        groups.append({"id": "UNASSIGNED", "label": "Unassigned", "rows": unassigned, "count": len(unassigned), "active": len(unassigned)})
    groups.append(
        {
            "id": "ALL",
            "label": "All Watches",
            "rows": items,
            "count": len(items),
            "active": len(active_rows),
        }
    )
    return {
        "rows": items,
        "groups": groups,
        "count": len(items),
        "active": len(active_rows),
        "by_sleeve": {g["id"]: g["count"] for g in groups if g["id"] not in {"ALL", "UNASSIGNED"}},
        "live_order_placement_enabled": live_execution_authority().LIVE_ORDER_PLACEMENT,
    }


def notifications_view(state: DashboardState) -> dict[str, Any]:
    store = NotificationStore(state.root)
    items = newest_first([n.to_dict() for n in store.all()], "created_at")
    unread = [n for n in items if not n.get("read")]
    return {"rows": items, "unread": unread, "unread_count": len(unread)}


def _openai_card(health: dict[str, Any], state: DashboardState) -> dict[str, Any]:
    raw = dict(health.get("openai") or {})
    budget = dict(health.get("ai_budget") or {})
    try:
        summary = ai_summary(state)
        providers = dict(summary.get("providers") or {})
        if providers.get("openai"):
            raw["state"] = "READY"
            raw["calls_allowed"] = True
        spent = float(summary.get("spent") or budget.get("spent") or 0)
        calls = int(summary.get("calls_month") or budget.get("calls_month") or 0)
        if spent > 0 or calls > 0:
            if raw.get("state") in {None, "", "UNCONFIGURED"}:
                raw["state"] = "READY"
                raw["calls_allowed"] = True
        raw["spent"] = spent
        raw["calls_month"] = calls
    except Exception:  # noqa: BLE001
        if float(budget.get("spent") or 0) > 0 and raw.get("state") == "UNCONFIGURED":
            raw["state"] = "READY"
    return raw


def agent_runtime_view(state: DashboardState) -> dict[str, Any]:
    health = load_health(state.root)
    err = live_error_state(state)
    return {
        "agent": health.get("agent") or "OFFLINE",
        "alive": bool(health.get("alive")),
        "uptime_seconds": health.get("uptime_seconds") or 0,
        "runtime_mode": health.get("runtime_mode") or get_active_artifact_environment(),
        "market": health.get("market") or {},
        "last_cycle": health.get("last_cycle"),
        "next_jobs": health.get("next_jobs") or [],
        "robinhood": health.get("robinhood") or {},
        "openai": _openai_card(health, state),
        "ai_budget": health.get("ai_budget") or {},
        "cycles": health.get("cycles") or 0,
        "live_error_code": err.get("code"),
        "live_error_message": err.get("message"),
        "job_skips": err.get("job_skips") or [],
        "LIVE_ORDER_PLACEMENT": bool(health.get("LIVE_ORDER_PLACEMENT"))
        if health.get("execution_mode")
        else live_execution_authority().LIVE_ORDER_PLACEMENT,
        "execution_mode": health.get("execution_mode") or live_execution_authority().execution_mode,
        "live_trade_actions_allowed": bool(health.get("live_trade_actions_allowed"))
        if health.get("execution_mode")
        else live_execution_authority().live_trade_actions_allowed,
        "auto_execution": False,
    }


def chrome_status(state: DashboardState) -> dict[str, Any]:
    agent = agent_runtime_view(state)
    market = dict(agent.get("market") or {})
    robinhood = dict(agent.get("robinhood") or {})
    budget = dict(agent.get("ai_budget") or {})
    openai = dict(agent.get("openai") or {})
    spent = float(budget.get("spent") or openai.get("spent") or 0)
    cap = float(budget.get("cap") or 10)
    phase = str(market.get("phase") or "—")
    connected = bool(robinhood.get("connected"))
    last_cycle = dict(agent.get("last_cycle") or {})
    pct = round(min(100.0, (spent / cap) * 100.0), 0) if cap else 0
    return {
        "runtime": agent.get("runtime_mode") or get_active_artifact_environment(),
        "agent_alive": bool(agent.get("alive")),
        "market_phase": phase,
        "market_open": bool(market.get("regular_hours_open")) or phase.upper() in {"OPEN", "REGULAR"},
        "robinhood_connected": connected,
        "robinhood_label": "CONNECTED" if connected else "OFFLINE",
        "ai_spent": spent,
        "ai_cap": cap,
        "ai_spent_display": f"${spent:.2f}",
        "ai_cap_display": f"${cap:.0f}",
        "ai_pct": int(pct),
        "ai_state": openai.get("state") or "—",
        "last_data_at": last_cycle.get("at") or last_cycle.get("completed_at"),
    }


def upcoming_events(state: DashboardState) -> list[dict[str, Any]]:
    rows = []
    for item in watchlist_view(state).get("rows") or []:
        when = item.get("next_review_at")
        if not when:
            continue
        rows.append(
            {
                "ticker": item.get("ticker"),
                "title": f"{item.get('ticker')} review",
                "at": when,
                "status": item.get("status"),
            }
        )
    rows.sort(key=lambda row: ts_sort_key(row.get("at")) or "9999")
    return rows[:8]


def activity_log_view(state: DashboardState, *, limit: int = 200) -> dict[str, Any]:
    rows = read_activity(state.root, limit=limit)
    rows = newest_first(rows, "logged_at", "created_at")
    return {"entries": rows, "count": len(rows)}



def list_approvals(state: DashboardState) -> dict[str, Any]:
    flags = resolve_ui_flags()
    env = get_active_artifact_environment()
    packets = [_packet_row(p, flags=flags) for p in _operational_packets(state, flags)]
    live_rows = [_live_approval_row(item) for item in _live_approval_store(state).all()]
    pending = newest_first(
        [p for p in live_rows if p["pending"]] + [p for p in packets if p["pending"]],
        "created_at",
    )
    stale = newest_first(
        [p for p in live_rows if p["stale"]] + [p for p in packets if p["stale"]],
        "decided_at",
        "created_at",
    )
    other = newest_first(
        [p for p in live_rows if not p["pending"] and not p["stale"]]
        + [p for p in packets if not p["pending"] and not p["stale"]],
        "decided_at",
        "created_at",
    )
    return {
        "pending": pending,
        "stale": stale,
        "other": other,
        "all": live_rows + packets,
        "pending_count": len(pending),
        "live_pending_count": sum(1 for p in live_rows if p["pending"]),
        "approved_does_not_place_order": not live_execution_authority().LIVE_ORDER_PLACEMENT,
        "book_label": flags["active_book_label"],
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "runtime_mode": env,
        "live_order_placement_enabled": live_execution_authority().LIVE_ORDER_PLACEMENT,
        "allow_paper_packet_decisions": flags["allow_paper_packet_decisions"] and env != RuntimeMode.LIVE.value,
        "allow_demo_packet_decisions": flags["allow_demo_packet_decisions"] and env != RuntimeMode.LIVE.value,
        "allow_stale_packet_decisions": flags["allow_stale_packet_decisions"],
    }


def get_approval(state: DashboardState, approval_id: str) -> dict[str, Any] | None:
    live = _live_approval_store(state).get(approval_id)
    if live is not None:
        return live_approval_detail(live)
    packet = state.approvals.get_packet(approval_id)
    if packet is None:
        return None
    return packet_detail(packet)


def record_approval_decision(
    state: DashboardState,
    approval_id: str,
    status: ApprovalStatus | LiveApprovalStatus | str,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    live_store = _live_approval_store(state)
    live = live_store.get(approval_id)
    if live is not None:
        engine = LiveApprovalEngine(live_store, journal=state.root / "logs" / "approval.jsonl", executor=_dashboard_executor(state))
        wanted = LiveApprovalStatus.APPROVED if str(getattr(status, "value", status)).upper() in {"APPROVED", "APPROVED_AWAITING_EXECUTION_IMPLEMENTATION", "APPROVED_EXECUTION_DISABLED"} else LiveApprovalStatus.REJECTED
        updated = engine.record_decision(approval_id, wanted, note=note)
        if updated.placed_order and not live_placement_enabled():
            raise ApprovalValidationError("dashboard approval must not place an order")
        from agentic_portfolio.agent.activity import log_activity

        log_activity(state.root, "APPROVAL_APPROVED" if wanted is LiveApprovalStatus.APPROVED else "APPROVAL_REJECTED", ticker=updated.ticker, approval_id=updated.approval_id)
        return live_approval_detail(updated)
    packet = state.approvals.get_packet(approval_id)
    if packet is None:
        raise KeyError(approval_id)
    blocked = decision_block_reason(packet_kind(packet))
    if blocked:
        raise ApprovalValidationError(blocked)
    paper_status = status if isinstance(status, ApprovalStatus) else ApprovalStatus(str(getattr(status, "value", status)))
    updated_packet = record_human_decision(
        packet,
        paper_status,
        note=note or None,
        store=state.approvals,
        persist=True,
        journal=state.root / "logs" / "approval.jsonl",
    )
    if updated_packet.broker_submitted or not updated_packet.approved_does_not_place_order:
        raise ApprovalValidationError("dashboard approval must not place an order")
    return packet_detail(updated_packet)


def _thesis_row(rec: ThesisRecord, *, book: str) -> dict[str, Any]:
    status = rec.status.value
    env = RuntimeMode.LIVE.value if book == "live" else RuntimeMode.PAPER.value
    return {
        "thesis_id": rec.thesis_id,
        "symbol": rec.symbol,
        "sleeve": rec.sleeve.value,
        "status": status,
        "decision": rec.decision.value if rec.decision else None,
        "book": book,
        "book_label": BOOK_LABELS.get(book, book),
        "runtime_mode": env,
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


def _merge_theses(state: DashboardState, flags: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    flags = _ui(flags)
    env = get_active_artifact_environment()
    by_id: dict[str, dict[str, Any]] = {}
    if env == RuntimeMode.LIVE.value:
        for rec in state.theses_live.all_records():
            raw = to_dict(rec)
            if artifact_environment(raw) != RuntimeMode.LIVE.value:
                continue
            by_id[rec.thesis_id] = _thesis_row(rec, book="live")
    else:
        for rec in state.theses_live.all_records():
            by_id[rec.thesis_id] = _thesis_row(rec, book="live")
        for rec in state.theses_monitor.all_records():
            by_id[rec.thesis_id] = _thesis_row(rec, book="paper_monitor")
        for rec in state.theses_paper.all_records():
            by_id[rec.thesis_id] = _thesis_row(rec, book="paper")
    rows = list(by_id.values())
    rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return rows


def _live_research_rows(state: DashboardState) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    latest_by_key: dict[str, dict[str, Any]] = {}
    for row in _ai_store(state, RuntimeMode.LIVE.value).research_reports():
        if artifact_environment(row) != RuntimeMode.LIVE.value:
            continue
        if str(row.get("research_source") or "").lower() == "scripted":
            row = dict(row)
            row["production_artifact"] = False
        ticker = str(row.get("ticker") or row.get("symbol") or "").upper()
        rid = str(row.get("research_id") or "")
        seen.add(rid)
        full = state.research.get(rid)
        full_data = to_dict(full) if full is not None else {}
        poison = looks_like_schema_failure_report(row) or looks_like_schema_failure_report(full) or looks_like_schema_failure_report(full_data)
        if poison:
            continue
        built = {
            "research_id": row.get("research_id"),
            "symbol": ticker,
            "research_conclusion": row.get("recommended_action") or row.get("research_conclusion") or full_data.get("research_conclusion"),
            "research_status": full_data.get("research_status") or ("COMPLETE" if row.get("research_id") else "NONE"),
            "executive_summary": row.get("thesis") or row.get("executive_summary") or full_data.get("executive_summary"),
            "freshness": row.get("freshness") or full_data.get("freshness") or "FRESH",
            "confidence": row.get("confidence") or full_data.get("confidence"),
            "provisional_sleeve": row.get("provisional_sleeve") or full_data.get("provisional_sleeve"),
            "bull_case": {"summary": row.get("bull_case")} if row.get("bull_case") else full_data.get("bull_case"),
            "base_case": {"summary": row.get("base_case")} if row.get("base_case") else full_data.get("base_case"),
            "bear_case": {"summary": row.get("bear_case")} if row.get("bear_case") else full_data.get("bear_case"),
            "key_risks": list(row.get("risks") or row.get("key_risks") or full_data.get("key_risks") or []),
            "invalidation_candidates": list(row.get("invalidation_candidates") or full_data.get("invalidation_candidates") or []),
            "completed_at": row.get("created_at") or row.get("completed_at") or full_data.get("completed_at"),
            "started_at": row.get("created_at") or full_data.get("started_at"),
            "runtime_mode": RuntimeMode.LIVE.value,
            "source_of_truth": get_active_portfolio_source(),
            "research_source": row.get("research_source") or full_data.get("research_source"),
            "provider": row.get("provider") or full_data.get("provider"),
            "model": row.get("model") or full_data.get("model"),
            "ai_call_id": row.get("ai_call_id") or full_data.get("ai_call_id"),
            "estimated_cost": row.get("estimated_cost") if row.get("estimated_cost") is not None else full_data.get("estimated_cost"),
            "actual_cost": row.get("actual_cost") if row.get("actual_cost") is not None else full_data.get("actual_cost"),
            "originating_candidate_id": row.get("candidate_id") or full_data.get("candidate_id"),
            "evidence_fingerprint": row.get("evidence_fingerprint") or full_data.get("evidence_fingerprint"),
            "production_artifact": row.get("production_artifact", str(row.get("research_source") or "").lower() != "scripted"),
            "missing_information": list(full_data.get("missing_information") or []),
            "sources_observed": list(full_data.get("sources_observed") or []),
            "sources_unavailable": list(full_data.get("sources_unavailable") or []),
            "facts": list(full_data.get("facts") or []),
            "packet_id": full_data.get("packet_id"),
        }
        key = f"{ticker}:{(row.get('evidence_fingerprint') or rid)}"
        prior = latest_by_key.get(key)
        if prior is None or str(built.get("completed_at") or "") > str(prior.get("completed_at") or ""):
            latest_by_key[key] = built
    for report in state.research.all_reports():
        if report.research_id in seen:
            continue
        if getattr(report, "runtime_mode", None) == RuntimeMode.PAPER.value:
            continue
        if str(report.research_source or "").lower() == "scripted":
            continue
        seen.add(report.research_id)
        data = to_dict(report)
        ticker = str(data.get("symbol") or "").upper()
        if looks_like_schema_failure_report(report) or looks_like_schema_failure_report(data):
            continue
        key = f"{ticker}:{(data.get('evidence_fingerprint') or data.get('research_id'))}"
        prior = latest_by_key.get(key)
        if prior is None or str(data.get("completed_at") or "") > str(prior.get("completed_at") or ""):
            latest_by_key[key] = data
    rows = list(latest_by_key.values())
    rows.sort(key=lambda r: str(r.get("completed_at") or r.get("started_at") or ""), reverse=True)
    return rows


def research_view(state: DashboardState) -> dict[str, Any]:
    flags = resolve_ui_flags()
    env = get_active_artifact_environment()
    candidates = newest_first([to_dict(c) for c in state.candidates.all()], "discovered_at", "created_at")
    for row in candidates:
        row["runtime_mode"] = env
    if env == RuntimeMode.LIVE.value:
        reports = newest_first(_live_research_rows(state), "completed_at", "started_at", "created_at")
    else:
        reports = newest_first(
            [to_dict(r) for r in state.research.all_reports()],
            "completed_at",
            "started_at",
            "created_at",
        )
    theses = newest_first(_merge_theses(state, flags), "updated_at", "created_at")
    raw_queue = [to_dict(q) for q in state.queue.all()]
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in raw_queue:
        key = str(row.get("symbol") or "").upper()
        prior = by_symbol.get(key)
        if prior is None or str(row.get("enqueued_at") or "") >= str(prior.get("enqueued_at") or ""):
            by_symbol[key] = row
    queue = newest_first(list(by_symbol.values()), "enqueued_at", "claimed_at")
    counts = {
        "queued": sum(1 for q in queue if q.get("status") == "QUEUED"),
        "researching": sum(1 for q in queue if q.get("status") in {"RESEARCHING", "IN_PROGRESS"}),
        "completed": sum(1 for q in queue if q.get("status") == "COMPLETED"),
        "rejected": sum(1 for q in queue if q.get("status") in {"REJECTED", "EXPIRED", "DROPPED"}),
        "need_more_data": sum(1 for q in queue if q.get("status") in {"NEED_MORE_DATA", "INCONCLUSIVE"}),
    }
    return {
        "candidates": candidates,
        "queue": queue,
        "queue_counts": counts,
        "reports": reports,
        "theses": theses,
        "active_or_draft": [t for t in theses if t["active_or_draft"]],
        "book_label": flags["active_book_label"],
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "runtime_mode": env,
        "live_order_placement_enabled": live_execution_authority().LIVE_ORDER_PLACEMENT,
        "pipeline": pipeline_status(state),
    }


def get_thesis(state: DashboardState, thesis_id: str) -> dict[str, Any] | None:
    env = get_active_artifact_environment()
    lookups = [
        (state.theses_live.get(thesis_id), "live"),
        (state.theses_paper.get(thesis_id), "paper"),
        (state.theses_monitor.get(thesis_id), "paper_monitor"),
    ]
    if env == RuntimeMode.LIVE.value:
        live = lookups[0]
        if live[0] is not None and artifact_environment(to_dict(live[0])) == RuntimeMode.LIVE.value:
            return _thesis_row(live[0], book="live")
        return None
    for rec, book in lookups:
        if rec is not None:
            return _thesis_row(rec, book=book)
    return None


def get_report(state: DashboardState, research_id: str) -> dict[str, Any] | None:
    env = get_active_artifact_environment()
    if env == RuntimeMode.LIVE.value:
        for row in _live_research_rows(state):
            if str(row.get("research_id")) == str(research_id):
                return row
        return None
    report = state.research.get(research_id)
    return to_dict(report) if report else None


def orders_view(state: DashboardState) -> dict[str, Any]:
    flags = resolve_ui_flags()
    env = get_active_artifact_environment()
    plans: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    if env != RuntimeMode.LIVE.value:
        for run in state.order_plans.all_runs():
            if artifact_environment(run) != env:
                continue
            for plan in run.get("plans") or []:
                row = dict(plan)
                row["run_id"] = run.get("run_id")
                row["run_created_at"] = run.get("created_at")
                row["notional_display"] = _usd(plan.get("notional"))
                row["status"] = plan.get("execution_status")
                row["blocked"] = list(plan.get("blocked_reasons") or [])
                row["live_execution_blocked"] = True
                row["broker_submitted"] = False
                row["runtime_mode"] = RuntimeMode.PAPER.value
                row["book_label"] = PAPER_BOOK_LABEL
                plans.append(row)
        for run in state.fills.all_runs():
            skipped = run.get("skipped") or []
            for fill in run.get("fills") or []:
                row = dict(fill)
                row["run_id"] = run.get("run_id")
                row["filled_notional_display"] = _usd(fill.get("filled_notional"))
                row["book_label"] = PAPER_BOOK_LABEL
                row["runtime_mode"] = RuntimeMode.PAPER.value
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
                            "runtime_mode": RuntimeMode.PAPER.value,
                        }
                    )
    reviews = []
    for result in state.reviews.all_results():
        row = to_dict(result)
        if env == RuntimeMode.LIVE.value and artifact_environment(row) != RuntimeMode.LIVE.value:
            continue
        row["order_placed"] = False
        row["broker_submitted"] = False
        row["book_label"] = LIVE_ACCOUNT_LABEL
        row["live_order_placement_enabled"] = False
        reviews.append(row)
    from agentic_portfolio.live_execution.store import ExecutionStore

    broker_orders = []
    try:
        for order in ExecutionStore(state.root, runtime_mode=env).orders():
            broker_orders.append(order.to_dict())
    except Exception:  # noqa: BLE001
        broker_orders = []
    broker_orders = newest_first(broker_orders, "updated_at", "created_at")
    return {
        "plans": newest_first(plans, "run_created_at", "created_at"),
        "fills": newest_first(fills, "created_at", "filled_at"),
        "reviews": newest_first(reviews, "reviewed_at", "created_at"),
        "broker_orders": broker_orders,
        "book_label": flags["active_book_label"],
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "runtime_mode": env,
        "live_order_placement_enabled": live_execution_authority().LIVE_ORDER_PLACEMENT,
    }


def _broker_order_summary(state: DashboardState) -> dict[str, int]:
    from agentic_portfolio.live_execution.store import ExecutionStore

    counts = {"pending": 0, "submitted": 0, "filled": 0, "rejected": 0, "canceled": 0}
    try:
        orders = ExecutionStore(state.root, runtime_mode=state.runtime).orders()
    except Exception:  # noqa: BLE001
        return counts
    for order in orders:
        status = order.status.value if hasattr(order.status, "value") else str(order.status)
        if status in {"PENDING_SUBMISSION"}:
            counts["pending"] += 1
        elif status in {"SUBMITTED", "OPEN", "PARTIALLY_FILLED"}:
            counts["submitted"] += 1
        elif status == "FILLED":
            counts["filled"] += 1
        elif status == "REJECTED":
            counts["rejected"] += 1
        elif status in {"CANCELED"}:
            counts["canceled"] += 1
    return counts


def _live_position_count(state: DashboardState) -> dict[str, int]:
    ctx = live_context(state) if get_active_runtime() is RuntimeMode.LIVE else {}
    positions = ctx.get("positions") or []
    return {"count": len(positions) if isinstance(positions, list) else 0}


def journal_view(state: DashboardState, *, limit: int = 250) -> dict[str, Any]:
    flags = resolve_ui_flags()
    env = get_active_artifact_environment()
    rows: list[dict[str, Any]] = []
    logs = state.root / "logs"
    files = list(JOURNAL_FILES)
    if env == RuntimeMode.LIVE.value:
        files = [item for item in files if item[0] != "paper_fill"]
        files.append(("live", "live_portfolio.jsonl"))
        files.append(("ai", "ai_gateway.jsonl"))
    for source, name in files:
        for item in read_jsonl(logs / name):
            row = dict(item)
            row["_source"] = source
            row["_file"] = name
            row["_at"] = row.get("logged_at") or row.get("timestamp") or row.get("created_at") or row.get("reviewed_at")
            row_env = artifact_environment(row)
            if source == "paper_fill":
                row_env = RuntimeMode.PAPER.value
            if source == "live":
                row_env = RuntimeMode.LIVE.value
            if row_env != env:
                continue
            if "type" not in row:
                if source == "risk":
                    row["type"] = "RISK_GATE"
                elif source == "review":
                    row["type"] = row.get("status") or "REVIEW"
            row["runtime_mode"] = row_env
            rows.append(row)
    rows = newest_first(rows, "_at")
    return {
        "entries": rows[:limit],
        "count": len(rows),
        "book_label": flags["active_book_label"],
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "runtime_mode": env,
        "live_order_placement_enabled": live_execution_authority().LIVE_ORDER_PLACEMENT,
    }


def _latest_monitor(state: DashboardState, flags: dict[str, Any] | None = None) -> dict[str, Any]:
    flags = _ui(flags)
    env = get_active_artifact_environment()
    if env != RuntimeMode.LIVE.value:
        latest = state.monitoring.latest() or {}
        if latest and artifact_environment(latest) == RuntimeMode.LIVE.value:
            return {}
        return latest
    for run in state.monitoring.all_runs():
        if artifact_environment(run) == RuntimeMode.LIVE.value:
            return run
    return {
        "runtime_mode": RuntimeMode.LIVE.value,
        "symbols": [],
        "positions": [],
        "created_at": None,
        "note": "No live positions to monitor.",
        "synthetic_empty": True,
    }


def monitoring_alerts(state: DashboardState, flags: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    latest = _latest_monitor(state, flags)
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
    return newest_first(alerts, "created_at")


def recent_decisions(state: DashboardState, *, limit: int = 12) -> list[dict[str, Any]]:
    env = get_active_artifact_environment()
    out: list[dict[str, Any]] = []
    if env == RuntimeMode.LIVE.value:
        for row in _ai_store(state, RuntimeMode.LIVE.value).decisions()[:limit]:
            if artifact_environment(row) != RuntimeMode.LIVE.value:
                continue
            ticker = str(row.get("ticker") or "").upper()
            out.append(
                {
                    "batch_id": row.get("decision_id") or row.get("context_id"),
                    "created_at": row.get("created_at"),
                    "symbols": [ticker] if ticker else [],
                    "decisions": [{"symbol": ticker, "decision": row.get("action")}],
                    "runtime_mode": RuntimeMode.LIVE.value,
                    "comparison": None,
                }
            )
        return newest_first(out, "created_at")[:limit]
    for run in state.decisions.all_runs()[:limit]:
        if artifact_environment(run) == RuntimeMode.LIVE.value:
            continue
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
                "runtime_mode": RuntimeMode.PAPER.value,
                "comparison": run.get("comparison"),
            }
        )
    return newest_first(out, "created_at")[:limit]


def health_status(state: DashboardState) -> list[dict[str, Any]]:
    flags = execution_flags()
    ui = resolve_ui_flags()
    env = get_active_artifact_environment()
    latest_monitor = _latest_monitor(state, ui)
    if env == RuntimeMode.LIVE.value:
        reports = _live_research_rows(state)
        decision_count = len(_ai_store(state, RuntimeMode.LIVE.value).decisions())
        review_ids = [rid for rid in state.reviews.all_ids() if artifact_environment(state.reviews.get(rid) or {}) == RuntimeMode.LIVE.value]
    else:
        reports = state.research.all_reports()
        decision_count = len(state.decisions.all_ids())
        review_ids = state.reviews.all_ids()
    risk_rows = [row for row in read_jsonl(state.root / "logs" / "risk_gate.jsonl") if artifact_environment(row) == env]
    ctx = active_context(state, ui)
    monitor_ok = bool(latest_monitor) and not latest_monitor.get("synthetic_empty")
    if env == RuntimeMode.LIVE.value:
        monitor_ok = True
        monitor_detail = f"{len(latest_monitor.get('positions') or [])} positions"
    else:
        monitor_detail = latest_monitor.get("created_at") or "no run"
    unavailable = live_data_unavailable(state, ui)
    health = load_health(state.root)
    return [
        {
            "id": "agent_runtime",
            "name": "agent runtime",
            "ok": bool(health.get("alive")),
            "status": str(health.get("agent") or "OFFLINE"),
            "detail": (health.get("market") or {}).get("phase") or "not running",
        },
        {
            "id": "dashboard",
            "name": "dashboard",
            "ok": not unavailable,
            "status": "unavailable" if unavailable else "up",
            "detail": LIVE_DATA_UNAVAILABLE if unavailable else "localhost",
        },
        {
            "id": "monitoring",
            "name": "monitoring",
            "ok": monitor_ok,
            "status": "up" if monitor_ok else "missing",
            "detail": monitor_detail,
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
            "ok": bool(decision_count),
            "status": "up" if decision_count else "missing",
            "detail": f"{decision_count} batches",
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
            "id": "ai_gateway",
            "name": "AI gateway",
            "ok": True,
            "status": "advisory",
            "detail": "proposal-only; $10/month cap; never places",
        },
        {
            "id": "live_placement",
            "name": "live placement enabled" if flags["live_order_placement_enabled"] else "live placement disabled",
            "ok": True,
            "status": "enabled" if flags["live_order_placement_enabled"] else "disabled",
            "detail": ui["no_live_placement_banner"],
            "live_order_placement_enabled": flags["live_order_placement_enabled"],
            "autonomous_trading_disabled": flags["autonomous_trading_disabled"],
        },
    ]


def system_view(state: DashboardState) -> dict[str, Any]:
    flags = execution_flags()
    ui = resolve_ui_flags()
    runtime = get_active_runtime()
    env = get_active_artifact_environment()
    live = env == RuntimeMode.LIVE.value
    unavailable = live_data_unavailable(state, ui)
    rules = load_account_rules()
    policy = load_policy()
    pipeline = load_pipeline_config()
    book = active_book(state, ui)
    ctx = active_context(state, ui)
    paper = paper_book(state)
    live_snap = live_book(state)
    hwm = live_hwm_state(state, ui)
    session_path = LivePortfolioStore(state.root).session_path() if live else (state.root / "state" / "session_state.json")
    try:
        session = load_session_state(session_path)
    except (OSError, json.JSONDecodeError):
        session = None
    discovery_runs = state.discovery.all()
    latest_discovery = max(discovery_runs, key=lambda r: r.started_at) if discovery_runs else None
    latest_monitor = _latest_monitor(state, ui)
    live_reports = _live_research_rows(state) if live else []
    paper_reports = [to_dict(r) for r in state.research.all_reports()]
    reports = live_reports if live else paper_reports
    leaks = detect_paper_contamination(live_snap, paper, runtime_mode=env) if live else []
    paper_monitor = state.monitoring.latest() or {}
    if artifact_environment(paper_monitor) == RuntimeMode.LIVE.value:
        paper_monitor = {}
    paper_monitor_symbols = ",".join((paper_monitor.get("symbols") or [])[:6])
    live_monitor_count = 0 if latest_monitor.get("synthetic_empty") else len(latest_monitor.get("positions") or [])
    live_packet_count = len(_operational_packets(state, ui))
    paper_packet_count = sum(1 for p in state.approvals.all_packets() if _packet_env(state, p) != RuntimeMode.LIVE.value)
    paper_thesis_count = len(state.theses_paper.all_records()) + len(state.theses_monitor.all_records())
    untagged_registry = sum(
        1 for rec in state.theses_live.all_records() if artifact_environment(to_dict(rec)) != RuntimeMode.LIVE.value
    )
    live_thesis_count = sum(
        1 for rec in state.theses_live.all_records() if artifact_environment(to_dict(rec)) == RuntimeMode.LIVE.value
    )
    monitor_detail = f"{live_monitor_count} positions" if live else paper_monitor_symbols
    freshness = [
        {
            "label": "LIVE Robinhood snapshot" if live else "Paper book",
            "present": bool(book) and not unavailable,
            "updated_at": book.get("created_at"),
            "detail": LIVE_DATA_UNAVAILABLE if unavailable else ("agentic account source of truth" if live else "isolated paper blotter"),
        },
        {
            "label": "HWM state",
            "present": bool(hwm) and not unavailable,
            "updated_at": hwm.get("updated_at"),
            "detail": hwm.get("risk_state") if not unavailable else LIVE_DATA_UNAVAILABLE,
        },
        {
            "label": "Session SOD",
            "present": session is not None and not unavailable,
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
            "present": True if live else bool(latest_monitor),
            "updated_at": latest_monitor.get("created_at"),
            "detail": monitor_detail or "0 positions",
        },
        {
            "label": "Research reports",
            "present": bool(reports),
            "updated_at": max((r.get("completed_at") or "") for r in reports) if reports else None,
            "detail": f"{len(reports)} reports",
        },
    ]
    services = [
        {"name": "Approval packets", "ok": True, "count": live_packet_count if live else paper_packet_count},
        {"name": "Order plans", "ok": True, "count": 0 if live else len(state.order_plans.all_ids())},
        {"name": "Fills", "ok": True, "count": 0 if live else len(state.fills.all_ids())},
        {"name": "Robinhood reviews", "ok": True, "count": 0 if live else len(state.reviews.all_ids())},
        {"name": "Candidates", "ok": True, "count": len(state.candidates.all())},
        {"name": "Theses", "ok": True, "count": live_thesis_count if live else paper_thesis_count + untagged_registry},
        {"name": "LIVE AI proposals", "ok": True, "count": len(_ai_store(state, "LIVE").proposals())},
    ]
    paper_ctx = paper_context(state)
    paper_diagnostics = {
        "inactive": True,
        "label": "PAPER DIAGNOSTICS",
        "note": "Stored on disk for tests/dev. Not active LIVE operational state.",
        "nav": paper_ctx.get("current_nav"),
        "cash": paper_ctx.get("cash"),
        "risk_state": paper_ctx.get("risk_state"),
        "updated_at": paper.get("created_at"),
        "monitoring_symbols": paper_monitor_symbols,
        "counts": [
            {"name": "Research reports", "count": len(paper_reports)},
            {"name": "Approval packets", "count": paper_packet_count},
            {"name": "Order plans", "count": len(state.order_plans.all_ids())},
            {"name": "Paper fills", "count": len(state.fills.all_ids())},
            {"name": "Candidates", "count": len(state.candidates_paper.all())},
            {"name": "Theses (paper book)", "count": paper_thesis_count},
            {"name": "Theses (untagged registry)", "count": untagged_registry},
            {"name": "PAPER AI proposals", "count": len(_ai_store(state, "PAPER").proposals())},
        ],
    }
    risk_state = ctx.get("risk_state") if not unavailable else None
    return {
        "execution": flags,
        "account_nickname": (rules.get("account") or {}).get("nickname"),
        "live_observed": (rules.get("account") or {}).get("last_observed"),
        "hwm": hwm if not unavailable else {},
        "session": session.to_dict() if session else None,
        "active_runtime": env,
        "live_account_status": ui["live_account_status"],
        "paper_book_status": ui["paper_book_status"],
        "risk_book_label": ui["risk_book_label"],
        "risk_state_label": (
            LIVE_DATA_UNAVAILABLE
            if unavailable
            else (f"{risk_state} on {ui['risk_book_label']}" if risk_state else "—")
        ),
        "live_data_unavailable": unavailable,
        "live_error_code": live_error_state(state).get("code") if unavailable else None,
        "live_error_message": live_error_state(state).get("message") if unavailable else None,
        "paper_context": {
            "nav": paper_ctx.get("current_nav"),
            "cash": paper_ctx.get("cash"),
            "risk_state": paper_ctx.get("risk_state"),
            "updated_at": paper.get("created_at"),
        },
        "active_context": {
            "nav": None if unavailable else ctx.get("current_nav"),
            "cash": ctx.get("cash") if not unavailable else None,
            "buying_power": ctx.get("buying_power") if not unavailable else None,
            "risk_state": risk_state,
            "updated_at": book.get("created_at") if not unavailable else None,
            "source_of_truth": get_active_portfolio_source(),
        },
        "paper_leak_reasons": leaks,
        "freshness": freshness,
        "services": services,
        "paper_diagnostics": paper_diagnostics,
        "health": health_status(state),
        "environment": ui["environment"],
        "paper_book_label": PAPER_BOOK_LABEL,
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "active_book_label": ui["active_book_label"],
        "book_kind": ui["book_kind"],
        "live_order_placement_enabled": live_execution_authority().LIVE_ORDER_PLACEMENT,
        "pipeline_stop": pipeline.get("hard_stop_after"),
        "policy_version": policy.get("version"),
        "daily_halt_threshold": (policy.get("daily_risk_halt") or {}).get("threshold_fraction_of_start_of_day_nav"),
        "risk_limits_read_only": True,
        "writes_allowed": ["approve_packet", "reject_packet"],
        "runtime": runtime.value,
        "agent": agent_runtime_view(state),
        "watchlist": watchlist_view(state),
        "notifications": notifications_view(state),
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
    env = get_active_artifact_environment()
    items: list[dict[str, Any]] = []
    if env == RuntimeMode.LIVE.value:
        activity = ai_activity_view(state)
        for row in activity.get("rows") or []:
            ticker = row.get("ticker")
            if not ticker:
                continue
            items.append(
                {
                    "kind": "ai",
                    "label": "AI",
                    "title": f"{ticker} {friendly_enum(str(row.get('decision_action') or row.get('classification') or ''))}",
                    "meta": row.get("created_at"),
                    "href": "/ai/activity",
                    "runtime_mode": RuntimeMode.LIVE.value,
                }
            )
        for packet in (list_approvals(state).get("pending") or [])[:4]:
            items.append(
                {
                    "kind": "approval",
                    "label": "Approval",
                    "title": f"{packet.get('symbol')} {packet.get('action')}",
                    "meta": packet.get("created_at"),
                    "href": f"/approvals/{packet.get('approval_id')}",
                    "runtime_mode": RuntimeMode.LIVE.value,
                }
            )
        for row in read_activity(state.root, limit=20):
            items.append(
                {
                    "kind": str(row.get("type") or "activity").lower(),
                    "label": str(row.get("type") or "Activity").replace("_", " "),
                    "title": f"{row.get('ticker') or row.get('symbol') or row.get('job') or row.get('type')}",
                    "meta": row.get("logged_at"),
                    "href": "/journal",
                    "runtime_mode": RuntimeMode.LIVE.value,
                }
            )
        items.sort(key=lambda row: str(row.get("meta") or ""), reverse=True)
        return items[:12]
    for row in read_activity(state.root, limit=20):
        items.append(
            {
                "kind": str(row.get("type") or "activity").lower(),
                "label": str(row.get("type") or "Activity").replace("_", " "),
                "title": f"{row.get('ticker') or row.get('symbol') or row.get('job') or row.get('type')}",
                "meta": row.get("logged_at"),
                "href": "/journal",
                "runtime_mode": env,
            }
        )
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
        env = get_active_artifact_environment()
        rows.append(
            {
                **raw,
                "runtime_mode": env,
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
                "filter_bucket": _candidate_bucket(raw.get("status")),
            }
        )
    rows = newest_first(rows, "discovered_at", "created_at")
    return rows


def _candidate_bucket(status: Any) -> str:
    value = str(status or "").upper()
    if value in {"PROMOTED_TO_RESEARCH", "WATCHING", "RESEARCH_COMPLETE"}:
        return "promoted"
    if value in {"REJECTED", "EXPIRED"}:
        return "rejected"
    return "evaluated"


def discovery_view(state: DashboardState) -> dict[str, Any]:
    ui = resolve_ui_flags()
    env = get_active_artifact_environment()
    rows = candidate_rows(state)
    promoted = [row for row in rows if row.get("filter_bucket") == "promoted"]
    rejected = [row for row in rows if row.get("filter_bucket") == "rejected"]
    scored = sorted(rows, key=lambda r: float(r.get("discovery_score") or 0), reverse=True)
    top = scored[0] if scored else None
    avg = (
        round(sum(float(r.get("discovery_score") or 0) for r in rows) / len(rows), 2)
        if rows
        else None
    )
    latest = max(state.discovery.all(), key=lambda r: r.started_at) if state.discovery.all() else None
    return {
        "candidates": rows,
        "count": len(rows),
        "promoted_count": len(promoted),
        "rejected_count": len(rejected),
        "evaluated_count": len(rows),
        "top_score": top.get("discovery_score") if top else None,
        "top_symbol": top.get("symbol") if top else None,
        "avg_score": avg,
        "last_run": latest.completed_at if latest else (latest.started_at if latest else None),
        "book_label": ui["active_book_label"],
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "live_order_placement_enabled": live_execution_authority().LIVE_ORDER_PLACEMENT,
        "environment": env,
        "runtime_mode": env,
        "note": "Read from stored discovery results. This page does not run discovery. PAPER candidates are never reused as LIVE.",
    }


def dashboard_view(state: DashboardState) -> dict[str, Any]:
    flags = execution_flags()
    ui = resolve_ui_flags()
    live = ui["environment"] == "LIVE" or get_active_runtime() is RuntimeMode.LIVE
    unavailable = live_data_unavailable(state, ui)
    book = {} if unavailable else active_book(state, ui)
    ctx = {} if unavailable else active_context(state, ui)
    hwm = {} if unavailable else live_hwm_state(state, ui)
    reports = _live_research_rows(state) if live else [to_dict(r) for r in state.research.all_reports()]
    sleeves = ctx.get("sleeve_allocation_pct") or {}
    theses = _thesis_index(state)
    companies = _company_index(reports)
    nav = ctx.get("current_nav")
    missing = LIVE_DATA_UNAVAILABLE if unavailable else UNAVAILABLE
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
    history = [] if unavailable else record_nav_snapshot(
        state.root,
        nav=float(nav) if nav is not None else None,
        spy=(ctx.get("spy") if isinstance(ctx.get("spy"), dict) else None),
        at=ctx.get("timestamp") or book.get("created_at"),
        mode=ui["environment"],
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
    risk_label = None if unavailable else (f"{risk_state} on {ui['risk_book_label']}" if risk_state else None)
    excess = None if port_ret is None or spy_ret is None else port_ret - spy_ret
    slices = allocation_slices(ctx)
    candidates = candidate_rows(state)
    ready = chart_ready(history)
    latest_monitor = _latest_monitor(state, ui)
    return {
        "execution": flags,
        "book_kind": ui["book_kind"],
        "environment": ui["environment"],
        "active_runtime": env if (env := get_active_artifact_environment()) else ui["environment"],
        "paper_book_label": PAPER_BOOK_LABEL,
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "active_book_label": ui["active_book_label"],
        "risk_book_label": ui["risk_book_label"],
        "risk_state_label": risk_label,
        "live_account_status": ui["live_account_status"],
        "paper_book_status": ui["paper_book_status"],
        "live_order_placement_enabled": live_execution_authority().LIVE_ORDER_PLACEMENT,
        "live_data_unavailable": unavailable,
        "live_error_code": live_error_state(state).get("code") if unavailable else None,
        "live_error_message": live_error_state(state).get("message") if unavailable else None,
        "health": health_status(state),
        "paper_environment": False if live else bool(book.get("paper_environment", True)),
        "live_book_untouched": False if live else bool(book.get("live_book_untouched", True)),
        "source_of_truth": get_active_portfolio_source(),
        "empty_positions_label": LIVE_DATA_UNAVAILABLE if unavailable else ("No live positions." if live else "No paper positions."),
        "nav": None if unavailable else ctx.get("current_nav"),
        "nav_display": missing if unavailable else _usd(ctx.get("current_nav")),
        "cash": None if unavailable else ctx.get("cash"),
        "cash_display": missing if unavailable else _usd(ctx.get("cash")),
        "cash_pct": None if unavailable else ctx.get("cash_allocation_pct"),
        "cash_pct_display": missing if unavailable else _pct_fraction(ctx.get("cash_allocation_pct")),
        "buying_power": None if unavailable else ctx.get("buying_power"),
        "buying_power_display": missing if unavailable else _usd(ctx.get("buying_power")),
        "holdings_count": 0 if unavailable else (ctx.get("holdings_count") or len(positions)),
        "risk_state": None if unavailable else risk_state,
        "daily_risk_halt": False if unavailable else bool(ctx.get("daily_risk_halt")),
        "high_water_mark": None if unavailable else (ctx.get("high_water_mark") or hwm.get("cash_flow_adjusted_hwm")),
        "hwm_display": missing if unavailable else _usd(ctx.get("high_water_mark") or hwm.get("cash_flow_adjusted_hwm")),
        "drawdown": None if unavailable else drawdown,
        "drawdown_display": missing if unavailable else _pct_fraction(drawdown),
        "start_of_day_nav": None if unavailable else ctx.get("start_of_day_nav"),
        "daily_return": None if unavailable else daily,
        "daily_return_display": missing if unavailable else _pct_fraction(daily),
        "updated_at": None if unavailable else (ctx.get("timestamp") or book.get("created_at")),
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
        "spy": spy_benchmark(ctx, reports, flags=ui) if not unavailable else {"observed": False, "source": None, "note": LIVE_DATA_UNAVAILABLE},
        "live_hwm": hwm,
        "approvals": list_approvals(state),
        "alerts": [] if unavailable else monitoring_alerts(state),
        "recent_decisions": recent_decisions(state),
        "approved_does_not_place_order": True,
        "monitoring_positions_count": 0 if unavailable else len(latest_monitor.get("positions") or []),
        "kpis": {
            "portfolio_value": _metric(nav, _usd(nav) if not unavailable else missing, available=nav is not None and not unavailable),
            "cash": _metric(ctx.get("cash"), f"{_usd(ctx.get('cash'))} ({_pct_fraction(ctx.get('cash_allocation_pct'))})" if not unavailable else missing, available=ctx.get("cash") is not None and not unavailable),
            "buying_power": _metric(ctx.get("buying_power"), _usd(ctx.get("buying_power")) if not unavailable else missing, available=ctx.get("buying_power") is not None and not unavailable),
            "today_pnl": _metric(daily, today_display if not unavailable else missing, available=daily is not None and not unavailable),
            "total_return": _metric(port_ret, _signed_pct(port_ret) if not unavailable else missing, available=port_ret is not None and not unavailable),
            "spy_return": _metric(spy_ret, _signed_pct(spy_ret) if not unavailable else missing, available=spy_ret is not None and not unavailable),
            "excess_return": _metric(excess, _signed_pct(excess) if not unavailable else missing, available=excess is not None and not unavailable),
            "drawdown": _metric(drawdown, _pct_fraction(drawdown) if not unavailable else missing, available=drawdown is not None and not unavailable),
            "risk_state": _metric(risk_state, risk_label or missing, available=bool(risk_label) and not unavailable),
        },
        "allocation_chart": {
            "labels": [row["label"] for row in slices],
            "values": [round(row["pct"] * 100.0, 4) for row in slices],
            "keys": [row["key"] for row in slices],
            "slices": slices,
        },
        "performance": {
            "ready": ready and not unavailable,
            "message": LIVE_DATA_UNAVAILABLE if unavailable else (None if ready else HISTORY_COLLECTING),
            "labels": [row["at"] for row in history],
            "nav": [row["nav"] for row in history],
            "spy": [row.get("spy") for row in history],
            "has_spy": any(row.get("spy") is not None for row in history),
        },
        "activity": compact_activity(state),
        "top_candidates": candidates[:5],
        "candidate_count": len(candidates),
        "ai": ai_summary(state, ui),
        "unavailable": missing,
        "history_collecting": HISTORY_COLLECTING,
        "agent": agent_runtime_view(state),
        "watchlist": watchlist_view(state),
        "notifications": notifications_view(state),
        "upcoming": upcoming_events(state),
        "chrome": chrome_status(state),
        "approval_banner": (
            f"TRADE APPROVAL REQUIRED — {list_approvals(state)['pending_count']}"
            if list_approvals(state)["pending_count"]
            else None
        ),
        "autonomous_activity": activity_log_view(state, limit=40)["entries"][:12],
        "pipeline": pipeline_status(state),
    }


def pipeline_status(state: DashboardState) -> dict[str, Any]:
    """Compact autonomous pipeline snapshot for the dashboard."""
    flags = resolve_ui_flags()
    queue = list(state.queue.all())
    latest = max(state.discovery.all(), key=lambda r: r.started_at) if state.discovery.all() else None
    watches = watchlist_view(state)
    approvals = list_approvals(state)
    reports = _live_research_rows(state) if flags["environment"] == "LIVE" else [to_dict(r) for r in state.research.all_reports()]
    agent = agent_runtime_view(state)
    current = None
    for entry in queue:
        if entry.status.value in {"RESEARCHING", "IN_PROGRESS"}:
            current = entry.symbol
            break
    univ = dict((latest.market_session_context or {}) if latest else {})
    live_univ = univ.get("live_universe") if isinstance(univ.get("live_universe"), dict) else {}
    return {
        "discovery": {
            "last_run": latest.completed_at if latest else None,
            "symbols_evaluated": len(latest.symbols_evaluated) if latest else 0,
            "candidates_created": len(latest.candidates_created) if latest else 0,
            "promoted": len(latest.candidates_promoted) if latest else 0,
            "rejected": len(latest.candidates_rejected) if latest else 0,
            "conclusion": latest.conclusion if latest else None,
            "universe_size": univ.get("unique_universe_size") or live_univ.get("unique_universe_size"),
            "sources": list(latest.sources_queried or []) if latest else [],
            "sources_successful": univ.get("sources_successful") or live_univ.get("sources_successful") or [],
        },
        "research": {
            "queue_count": len(queue),
            "queued": sum(1 for q in queue if q.status.value == "QUEUED"),
            "researching": sum(1 for q in queue if q.status.value in {"RESEARCHING", "IN_PROGRESS"}),
            "completed": sum(1 for q in queue if q.status.value == "COMPLETED"),
            "rejected": sum(1 for q in queue if q.status.value in {"REJECTED", "EXPIRED", "DROPPED"}),
            "need_more_data": sum(1 for q in queue if q.status.value in {"NEED_MORE_DATA", "INCONCLUSIVE"}),
            "current_symbol": current,
            "reports": len(reports),
        },
        "watchlist": {
            "active": watches.get("active") or 0,
            "count": watches.get("count") or 0,
            "by_sleeve": watches.get("by_sleeve") or {},
        },
        "theses": {
            "count": len(_merge_theses(state, flags)),
            "draft_or_active": len([t for t in _merge_theses(state, flags) if t.get("active_or_draft")]),
        },
        "approvals": {
            "pending": approvals.get("pending_count") or 0,
        },
        "runtime": {
            "phase": (agent.get("market") or {}).get("phase") if agent else None,
            "alive": agent.get("alive") if agent else False,
            "cycles": agent.get("cycles") if agent else 0,
            "LIVE_ORDER_PLACEMENT": live_execution_authority().LIVE_ORDER_PLACEMENT,
        },
        "live_order_placement_enabled": live_execution_authority().LIVE_ORDER_PLACEMENT,
        "orders": _broker_order_summary(state),
        "positions": _live_position_count(state),
        "lifecycle": ["Discovery", "Research", "Thesis/Decision", "Approval", "Order", "Position"],
    }


def _ai_store(state: DashboardState, mode: str):
    from agentic_portfolio.ai.store import AIArtifactStore

    return AIArtifactStore(state.root, runtime_mode=mode)


def _ai_budget_status(state: DashboardState):
    from agentic_portfolio.ai.budget import BudgetManager
    from agentic_portfolio.ai.config import load_ai_config
    from agentic_portfolio.ai.ledger import UsageLedger

    cfg = load_ai_config()
    return BudgetManager(UsageLedger(state.root, config=cfg), cfg).status()


def ai_summary(state: DashboardState, ui: dict[str, Any] | None = None) -> dict[str, Any]:
    from agentic_portfolio.ai.config import load_ai_config
    from agentic_portfolio.ai.gateway import default_providers
    from agentic_portfolio.ai.safety import LIVE_AI_ALLOWED, LIVE_PROPOSALS_ALLOWED

    flags = ui or resolve_ui_flags()
    mode = flags["environment"]
    status = _ai_budget_status(state)
    cfg = load_ai_config()
    roles = {
        name: {"provider": spec.get("provider"), "model": spec.get("model")}
        for name, spec in dict(cfg.get("roles") or {}).items()
    }
    availability = {name: bool(adapter.available()) for name, adapter in default_providers(cfg).items() if name != "scripted"}
    store = _ai_store(state, mode)
    other = "PAPER" if mode == "LIVE" else "LIVE"
    return {
        "runtime_mode": mode,
        "LIVE_AI_ALLOWED": LIVE_AI_ALLOWED,
        "LIVE_PROPOSALS_ALLOWED": LIVE_PROPOSALS_ALLOWED,
        "LIVE_ORDER_PLACEMENT": live_execution_authority().LIVE_ORDER_PLACEMENT,
        "roles": roles,
        "providers": availability,
        "budget_mode": status.mode.value,
        "cap": float(status.cap),
        "spent": float(status.spent),
        "reserved": float(status.reserved),
        "accounted_usage": float(status.spent) + float(status.reserved),
        "remaining": float(status.remaining),
        "remaining_formula": "cap - spent - reserved",
        "pct_used": status.pct_used,
        "calls_month": status.calls_month,
        "calls_today": status.calls_today,
        "spend_by_provider": {k: float(v) for k, v in status.spend_by_provider.items()},
        "spend_by_model": {k: float(v) for k, v in status.spend_by_model.items()},
        "spent_display": f"${float(status.spent):.4f}",
        "remaining_display": f"${float(status.remaining):.4f}",
        "cap_display": f"${float(status.cap):.2f}",
        "proposals": len(store.proposals()),
        "screenings": len(store.screenings()),
        "other_book": other,
        "isolation_note": "PAPER AI artifacts never appear as LIVE decisions.",
    }


def ai_view(state: DashboardState) -> dict[str, Any]:
    ui = resolve_ui_flags()
    summary = ai_summary(state, ui)
    return {
        **summary,
        "environment": ui["environment"],
        "paper_book_label": PAPER_BOOK_LABEL,
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "active_book_label": ui["active_book_label"],
        "book_kind": ui["book_kind"],
        "live_order_placement_enabled": live_execution_authority().LIVE_ORDER_PLACEMENT,
        "note": "AI is advisory. Risk Gate is deterministic. Broker is the account source of truth. Cursor is the development agent; the Raspberry Pi runs this runtime.",
    }


def ai_activity_view(state: DashboardState) -> dict[str, Any]:
    ui = resolve_ui_flags()
    mode = ui["environment"]
    store = _ai_store(state, mode)
    other_store = _ai_store(state, "PAPER" if mode == "LIVE" else "LIVE")
    screenings = store.screenings()
    research = store.research_reports()
    decisions = store.decisions()
    proposals = store.proposals()
    scans = store.scans()
    other_ids = {str(r.get("proposal_id") or r.get("screening_id") or r.get("research_id")) for r in other_store.proposals() + other_store.screenings()}
    rows = []
    research_by = {str(r.get("ticker") or "").upper(): r for r in research}
    decision_by = {str(r.get("ticker") or "").upper(): r for r in decisions}
    proposal_by = {str(r.get("ticker") or "").upper(): r for r in proposals}
    for screen in screenings:
        ticker = str(screen.get("ticker") or "").upper()
        if str(screen.get("runtime_mode") or mode).upper() != mode:
            continue
        report = research_by.get(ticker) or {}
        decision = decision_by.get(ticker) or {}
        proposal = proposal_by.get(ticker) or {}
        rows.append(
            {
                "ticker": ticker,
                "runtime_mode": screen.get("runtime_mode") or mode,
                "screen_score": screen.get("score"),
                "classification": screen.get("classification"),
                "worth_deep_research": screen.get("worth_deep_research"),
                "research_action": report.get("recommended_action"),
                "research_status": "complete" if report else "none",
                "decision_action": decision.get("action"),
                "confidence": proposal.get("confidence") or decision.get("confidence") or screen.get("confidence"),
                "risk_verdict": proposal.get("risk_verdict"),
                "proposal_status": proposal.get("status"),
                "rejection_reason": proposal.get("rejection_reason") or screen.get("rejection_reason") or report.get("rejection_reason"),
                "provider": proposal.get("provider") or screen.get("provider"),
                "model": proposal.get("model") or screen.get("model"),
                "created_at": screen.get("created_at") or decision.get("created_at") or proposal.get("created_at"),
            }
        )
    rows = newest_first(rows, "created_at")
    return {
        "environment": mode,
        "book_kind": ui["book_kind"],
        "active_book_label": ui["active_book_label"],
        "paper_book_label": PAPER_BOOK_LABEL,
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "live_order_placement_enabled": live_execution_authority().LIVE_ORDER_PLACEMENT,
        "scans": newest_first(scans, "created_at")[:10],
        "rows": rows,
        "proposals": proposals,
        "other_book_count": len(other_ids),
        "contamination": False,
        "note": f"Showing {mode} AI activity only. The other book has {len(other_ids)} artifacts that are not mixed in.",
    }
