"""CORE Portfolio Investment Committee.

One residual-allocation decision across cash, broad-market exposure, current
holdings, and qualified CORE research — not an isolated per-ticker hurdle.

Does not force BUYs. Does not bypass Risk Gate, human approval, or live execution.
Does not collect research. Does not touch PAPER state from the LIVE repair path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from agentic_portfolio.agent.activity import log_activity
from agentic_portfolio.context import build_context
from agentic_portfolio.decision.engine import run_portfolio_decision
from agentic_portfolio.decision.reasoner import DecisionReasoner
from agentic_portfolio.decision.residuals import (
    build_broad_market_residual,
    configured_residual_symbol,
    is_strategic_residual_report,
    residual_strategic_buy_eligible,
    spy_snapshot_usable,
)
from agentic_portfolio.decision.types import CASH_SYMBOL, GatedAction, RISK_UP, SPY_SYMBOL, DecisionResult
from agentic_portfolio.decision.validate import DecisionValidationError
from agentic_portfolio.discovery.store import CandidateStore
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.policy import load_decision_config, load_policy
from agentic_portfolio.research.operational import looks_like_operational_failure_report, report_is_still_fresh
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.types import ResearchConclusion, ResearchReport, ResearchStatus
from agentic_portfolio.risk_gate import evaluate
from agentic_portfolio.runtime import RuntimeMode, discovery_state_dir, live_placement_enabled
from agentic_portfolio.schemas import Candidate, CandidateStatus, ClassificationStatus, Decision, GateVerdict, PortfolioContext, Position, SecurityClass, Sleeve
from agentic_portfolio.watch.types import WatchItem, WatchStatus


LEDGER_NAME = "core_committee.json"
REEVALUATION_REASON = "live_core_committee_reevaluation"
CORE_SLEEVE = Sleeve.CORE_GROWTH
RISK_PERMIT = {GateVerdict.PASS, GateVerdict.REQUIRES_ENHANCED_REVIEW}
ACTIONABLE = {Decision.BUY, Decision.ADD, Decision.REDUCE, Decision.SELL}
MATERIAL_CASH_PCT = 0.05
SCHEDULED_REVIEW_DAYS = 1
DEFAULT_SCHEDULED_REVIEW_HOURS = 24
DEFAULT_NO_ACTION_MAX_AGE_HOURS = 24
NO_ACTION_STATUSES = {"NO_ACTION", "HOLD_CASH"}

COMMITTEE_EVENTS = (
    "CORE_COMMITTEE_STARTED",
    "CORE_COMMITTEE_ELIGIBLE_SET",
    "CORE_COMMITTEE_DECIDED",
    "CORE_COMMITTEE_NO_ACTION",
    "CORE_COMMITTEE_PROPOSAL_CREATED",
    "CORE_COMMITTEE_BLOCKED_BY_RISK",
    "CORE_COMMITTEE_SKIPPED_UNCHANGED",
    "CORE_COMMITTEE_REEVALUATION",
    "CORE_COMMITTEE_OUTPUT_TRUNCATED_RETRY",
    "CORE_COMMITTEE_NO_ACTION_EXPIRED_REVIEW",
)


@dataclass
class EligibleAlternative:
    symbol: str
    report: ResearchReport
    source: str
    watch: WatchItem | None = None
    candidate: Candidate | None = None
    skipped_reason: str | None = None


@dataclass
class CommitteeInput:
    reports: list[ResearchReport] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    eligible: list[EligibleAlternative] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    cash_included: bool = True
    spy_included: bool = False
    spy_evidence_report: ResearchReport | None = None

    @property
    def symbols(self) -> list[str]:
        return [item.symbol for item in self.eligible]


@dataclass
class CommitteeResult:
    status: str = "OK"
    trigger: str | None = None
    skipped_reason: str | None = None
    fingerprint: str | None = None
    eligible_symbols: list[str] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    selected_symbols: list[str] = field(default_factory=list)
    target_allocations: dict[str, float | None] = field(default_factory=dict)
    decisions: dict[str, str] = field(default_factory=dict)
    alternatives_considered: list[str] = field(default_factory=list)
    reports_in_packet: int = 0
    ai_calls: int = 0
    ai_stages_called: list[str] = field(default_factory=list)
    watches_created: int = 0
    theses_created: int = 0
    proposals_created: int = 0
    approvals_created: int = 0
    risk_blocked: int = 0
    symbol_rows: list[dict[str, Any]] = field(default_factory=list)
    batch_id: str | None = None
    reason: str | None = None
    ai_role: str | None = None
    ai_model: str | None = None
    research_called: bool = False
    terra_called: bool = False
    forced_buy: bool = False
    auto_execution: bool = False
    paper_state_touched: bool = False
    LIVE_ORDER_PLACEMENT: bool = False
    reevaluation: bool = False
    spy_included: bool = False
    previous_portfolio_action: str | None = None
    portfolio_action: str | None = None
    cash_target_weight: float | None = None
    broad_market_target_weight: float | None = None
    cash_reason: str | None = None
    broad_market_reason: str | None = None
    prior_decision_age_hours: float | None = None
    next_eligible_scheduled_review_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "trigger": self.trigger,
            "skipped_reason": self.skipped_reason,
            "fingerprint": self.fingerprint,
            "eligible_symbols": list(self.eligible_symbols),
            "skipped": list(self.skipped),
            "selected_symbols": list(self.selected_symbols),
            "target_allocations": dict(self.target_allocations),
            "decisions": dict(self.decisions),
            "alternatives_considered": list(self.alternatives_considered),
            "reports_in_packet": self.reports_in_packet,
            "ai_calls": self.ai_calls,
            "ai_stages_called": list(self.ai_stages_called),
            "watches_created": self.watches_created,
            "theses_created": self.theses_created,
            "proposals_created": self.proposals_created,
            "approvals_created": self.approvals_created,
            "risk_blocked": self.risk_blocked,
            "symbol_rows": list(self.symbol_rows),
            "batch_id": self.batch_id,
            "reason": self.reason,
            "ai_role": self.ai_role,
            "ai_model": self.ai_model,
            "research_called": False,
            "terra_called": False,
            "forced_buy": False,
            "auto_execution": False,
            "paper_state_touched": self.paper_state_touched,
            "LIVE_ORDER_PLACEMENT": live_placement_enabled(),
            "reevaluation": self.reevaluation,
            "spy_included": self.spy_included,
            "previous_portfolio_action": self.previous_portfolio_action,
            "portfolio_action": self.portfolio_action,
            "cash_target_weight": self.cash_target_weight,
            "broad_market_target_weight": self.broad_market_target_weight,
            "cash_reason": self.cash_reason,
            "broad_market_reason": self.broad_market_reason,
            "prior_decision_age_hours": self.prior_decision_age_hours,
            "next_eligible_scheduled_review_at": self.next_eligible_scheduled_review_at,
            "placement_attempted": False,
        }


def committee_ledger_path(root: Path, *, runtime_mode: RuntimeMode | str) -> Path:
    mode = runtime_mode if isinstance(runtime_mode, RuntimeMode) else RuntimeMode(str(runtime_mode).upper())
    return discovery_state_dir(Path(root), mode=mode) / LEDGER_NAME


def load_committee_ledger(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            data.setdefault("runs", [])
            data.setdefault("reevaluation_runs", [])
            return data
    return {"runs": [], "reevaluation_runs": [], "last_fingerprint": None}


def save_committee_ledger(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def is_core_sleeve(value: Any) -> bool:
    if value is None:
        return False
    raw = getattr(value, "value", value)
    return str(raw).upper() == CORE_SLEEVE.value


def is_core_report(report: ResearchReport) -> bool:
    return is_core_sleeve(report.provisional_sleeve)


def committee_report_eligible(report: ResearchReport | None, *, now: datetime) -> bool:
    if report is None:
        return False
    if not is_core_report(report):
        return False
    if report.research_conclusion is not ResearchConclusion.ADVANCE_TO_THESIS:
        return False
    if report.research_status is not ResearchStatus.RESEARCH_COMPLETE:
        return False
    if looks_like_operational_failure_report(report):
        return False
    if not report_is_still_fresh(report, now=now):
        return False
    return True


POST_BUY_CONFIRMATION = {
    WatchStatus.WAITING_FOR_OPEN,
    WatchStatus.WAITING_FOR_PRICE,
    WatchStatus.WAITING_FOR_LIQUIDITY,
    WatchStatus.WAITING_FOR_CATALYST,
    WatchStatus.READY_FOR_RISK_GATE,
}


def _watch_is_ordinary_core_watch(item: WatchItem) -> bool:
    if item.paper_environment:
        return False
    if str(getattr(item, "runtime_mode", "") or "").upper() == RuntimeMode.PAPER.value:
        return False
    if item.status in POST_BUY_CONFIRMATION:
        return False
    if item.status not in {WatchStatus.WATCH, WatchStatus.APPROVAL_REQUIRED}:
        return False
    if item.sleeve and not is_core_sleeve(item.sleeve):
        return False
    return True


def collect_committee_input(
    *,
    research_store: ResearchStore,
    watch_store=None,
    candidates: CandidateStore | None = None,
    theses=None,
    approvals=None,
    now: datetime,
    extra_reports: list[ResearchReport] | None = None,
) -> CommitteeInput:
    """Deterministic eligibility: fresh CORE ADVANCE_TO_THESIS only. No stale dump."""
    skipped: list[dict[str, Any]] = []
    by_symbol: dict[str, EligibleAlternative] = {}

    def consider(report: ResearchReport, source: str, watch: WatchItem | None = None, candidate: Candidate | None = None) -> None:
        sym = str(report.symbol or "").upper()
        if not sym or sym in {CASH_SYMBOL}:
            return
        if is_strategic_residual_report(report):
            skipped.append({"symbol": sym, "reason": "strategic_residual_not_individual", "source": source})
            return
        if not is_core_report(report) and not (watch is not None and is_core_sleeve(watch.sleeve)):
            skipped.append({"symbol": sym, "reason": "not_core", "source": source})
            return
        if looks_like_operational_failure_report(report):
            skipped.append({"symbol": sym, "reason": "operational_failure", "research_id": report.research_id})
            return
        if report.research_conclusion is not ResearchConclusion.ADVANCE_TO_THESIS:
            skipped.append({"symbol": sym, "reason": "not_advance_to_thesis", "research_id": report.research_id})
            return
        if report.research_status is not ResearchStatus.RESEARCH_COMPLETE:
            skipped.append({"symbol": sym, "reason": "research_incomplete", "research_id": report.research_id})
            return
        if not report_is_still_fresh(report, now=now):
            skipped.append({"symbol": sym, "reason": "stale_research", "research_id": report.research_id})
            return
        existing = by_symbol.get(sym)
        if existing is not None and (existing.report.started_at or "") >= (report.started_at or ""):
            return
        by_symbol[sym] = EligibleAlternative(symbol=sym, report=report, source=source, watch=watch, candidate=candidate)

    for report in extra_reports or []:
        consider(report, "fresh_advance_to_thesis")

    if hasattr(research_store, "all_reports"):
        for report in research_store.all_reports():
            consider(report, "research_store")

    if watch_store is not None:
        blocked: set[str] = set()
        for item in watch_store.all():
            if item.status in POST_BUY_CONFIRMATION:
                blocked.add(str(item.ticker).upper())
                skipped.append({"symbol": str(item.ticker).upper(), "reason": "trade_confirmation_excluded", "status": item.status.value})
                continue
            if not _watch_is_ordinary_core_watch(item):
                continue
            report = _latest_fresh_core_report(research_store, item.ticker, now=now)
            if report is None:
                skipped.append({"symbol": str(item.ticker).upper(), "reason": "watch_research_not_fresh"})
                continue
            cand = None
            if candidates is not None:
                cand = candidates.active_for_symbol(item.ticker) or candidates.current_for_symbol(item.ticker)
            consider(report, "named_core_watch", watch=item, candidate=cand)
        for sym in blocked:
            by_symbol.pop(sym, None)

    if theses is not None:
        for rec in theses.all_records():
            if not is_core_sleeve(rec.sleeve):
                continue
            report = _latest_fresh_core_report(research_store, rec.symbol, now=now)
            if report is None:
                skipped.append({"symbol": str(rec.symbol).upper(), "reason": "draft_thesis_research_not_fresh"})
                continue
            consider(report, "draft_core_thesis")

    if approvals is not None:
        pending_syms = {str(item.ticker).upper() for item in getattr(approvals, "pending", lambda: [])()}
        for sym in pending_syms:
            if sym in by_symbol:
                by_symbol[sym].source = by_symbol[sym].source + "+pending_approval"

    eligible = sorted(by_symbol.values(), key=lambda item: item.symbol)
    reports = [item.report for item in eligible]
    residual_symbol = configured_residual_symbol()
    alternatives = [CASH_SYMBOL, residual_symbol] + [
        item.symbol for item in eligible if item.symbol not in {CASH_SYMBOL, residual_symbol}
    ]
    if residual_symbol != SPY_SYMBOL and SPY_SYMBOL not in alternatives:
        alternatives.insert(2, SPY_SYMBOL)
    spy_evidence = by_symbol[residual_symbol].report if residual_symbol in by_symbol else None
    if spy_evidence is None and SPY_SYMBOL in by_symbol:
        spy_evidence = by_symbol[SPY_SYMBOL].report
    if spy_evidence is None and hasattr(research_store, "latest_for_symbol"):
        spy_evidence = research_store.latest_for_symbol(residual_symbol) or (
            research_store.latest_for_symbol(SPY_SYMBOL) if residual_symbol != SPY_SYMBOL else None
        )
    if is_strategic_residual_report(spy_evidence):
        spy_evidence = None
    return CommitteeInput(
        reports=reports,
        alternatives=alternatives,
        eligible=eligible,
        skipped=skipped,
        cash_included=True,
        spy_included=False,
        spy_evidence_report=spy_evidence,
    )


def _latest_fresh_core_report(store: ResearchStore, symbol: str, *, now: datetime) -> ResearchReport | None:
    reports = store.by_symbol(symbol)
    usable = [r for r in reports if committee_report_eligible(r, now=now) or (
        r.research_conclusion is ResearchConclusion.ADVANCE_TO_THESIS
        and r.research_status is ResearchStatus.RESEARCH_COMPLETE
        and not looks_like_operational_failure_report(r)
        and report_is_still_fresh(r, now=now)
        and (is_core_report(r) or is_core_sleeve(r.provisional_sleeve))
    )]
    if not usable:
        return None
    return sorted(usable, key=lambda r: r.started_at or "", reverse=True)[0]


def committee_fingerprint(committee_input: CommitteeInput, context: PortfolioContext) -> str:
    cash_pol = dict(load_policy().get("cash") or {})
    payload = {
        "research_ids": sorted(r.research_id for r in committee_input.reports),
        "symbols": sorted(committee_input.symbols),
        "holdings_symbols": sorted(p.symbol.upper() for p in context.positions),
        "cash_pct": round(float(context.cash_allocation_pct or 0.0), 2),
        "risk_state": context.risk_state.value if context.risk_state else None,
        "daily_risk_halt": bool(context.daily_risk_halt),
        # Schema/config identity only — not live SPY ticks — so this packet
        # change reevaluates once without burning the $10 cap on every quote.
        "packet_schema": "strategic_core_residual_v1",
        "cash_yield": {
            "current_yield": cash_pol.get("current_yield"),
            "yield_source": cash_pol.get("yield_source"),
            "yield_as_of": cash_pol.get("yield_as_of"),
        },
        "residual_symbol": configured_residual_symbol(),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def material_portfolio_change(context: PortfolioContext, ledger: dict[str, Any]) -> bool:
    last_holdings = {str(s).upper() for s in (ledger.get("last_holdings_symbols") or [])}
    now_holdings = {p.symbol.upper() for p in context.positions}
    if now_holdings != last_holdings:
        return True
    last_risk = ledger.get("last_risk_state")
    now_risk = context.risk_state.value if context.risk_state else None
    if last_risk is not None and last_risk != now_risk:
        return True
    last_halt = ledger.get("last_daily_risk_halt")
    if last_halt is not None and bool(last_halt) != bool(context.daily_risk_halt):
        return True
    last_cash = ledger.get("last_cash_allocation_pct")
    if last_cash is None:
        return False
    cfg = (load_decision_config().get("core_committee") or {})
    threshold = float(cfg.get("material_cash_allocation_change") or MATERIAL_CASH_PCT)
    return abs(float(context.cash_allocation_pct or 0.0) - float(last_cash)) >= threshold


def _committee_hours() -> tuple[float, float]:
    cfg = load_decision_config().get("core_committee") or {}
    if cfg.get("scheduled_review_hours") is not None:
        scheduled = float(cfg.get("scheduled_review_hours"))
    else:
        days = float(cfg.get("scheduled_review_days") or SCHEDULED_REVIEW_DAYS)
        scheduled = days * 24.0
    no_action = cfg.get("no_action_max_age_hours")
    no_action_hours = float(no_action) if no_action is not None else float(DEFAULT_NO_ACTION_MAX_AGE_HOURS)
    return scheduled, no_action_hours


def _parse_ledger_time(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _age_hours(last_at: Any, now: datetime) -> float | None:
    prev = _parse_ledger_time(last_at)
    if prev is None:
        return None
    return (now - prev).total_seconds() / 3600.0


def last_was_no_action(ledger: dict[str, Any]) -> bool:
    status = str(ledger.get("last_status") or "").upper()
    action = str(ledger.get("last_portfolio_action") or "").upper()
    return status in NO_ACTION_STATUSES or action in NO_ACTION_STATUSES


def needs_periodic_review(context: PortfolioContext, ledger: dict[str, Any]) -> bool:
    cash_pct = float(context.cash_allocation_pct or 0.0)
    empty = int(context.holdings_count or 0) == 0 or not list(context.positions or [])
    deployable = cash_pct >= MATERIAL_CASH_PCT or (empty and float(context.cash or 0.0) > 0)
    return deployable or last_was_no_action(ledger)


def next_scheduled_review_at(ledger: dict[str, Any], *, now: datetime) -> datetime:
    scheduled_h, _ = _committee_hours()
    prev = _parse_ledger_time(ledger.get("last_run_at"))
    if prev is None:
        return now
    return prev + timedelta(hours=scheduled_h)


def should_run_committee(
    committee_input: CommitteeInput,
    context: PortfolioContext,
    ledger: dict[str, Any],
    *,
    now: datetime,
    trigger: str,
    force: bool = False,
    residual_eligible: bool = False,
) -> tuple[bool, str]:
    if not committee_input.reports and not residual_eligible:
        return False, "empty_eligible_set"
    fingerprint = committee_fingerprint(committee_input, context)
    last_fp = ledger.get("last_fingerprint")
    same_fp = last_fp == fingerprint
    last_ids = set(ledger.get("last_research_ids") or [])
    now_ids = {r.research_id for r in committee_input.reports}
    if now_ids - last_ids:
        return True, "new_advance_to_thesis" if trigger == "new_advance_to_thesis" else trigger or "new_eligible_research"
    if material_portfolio_change(context, ledger):
        return True, "portfolio_or_cash_change"
    if trigger == "material_thesis_change":
        return True, trigger

    age = _age_hours(ledger.get("last_run_at"), now)
    scheduled_h, no_action_h = _committee_hours()
    periodic = needs_periodic_review(context, ledger)
    expired_no_action = last_was_no_action(ledger) and (age is None or age >= no_action_h)
    expired_schedule = age is None or age >= scheduled_h

    # Expiry is checked before unchanged_fingerprint so a stale NO_ACTION
    # cannot skip scheduled reviews across multiple days.
    if periodic and expired_no_action:
        return True, "no_action_expired_review"
    if periodic and expired_schedule:
        if trigger in {"scheduled_review", "weekend_review"}:
            return True, trigger
        return True, "scheduled_review"
    if force and not same_fp:
        return True, trigger or "forced_reevaluation"
    if same_fp:
        return False, "unchanged_fingerprint"
    if force:
        return True, trigger or "forced_reevaluation"
    if trigger == "new_advance_to_thesis":
        return True, trigger
    return False, "no_material_change"


def simulate_after_risk_up(context: PortfolioContext, action) -> PortfolioContext:
    """Apply a permitted BUY/ADD to a copy of context so later names see combined book."""
    notional = float(action.proposed_notional or 0.0)
    cash = max(0.0, float(context.cash) - notional)
    buying_power = max(0.0, float(context.buying_power) - notional)
    positions: list[Position] = []
    found = False
    for pos in context.positions:
        if pos.symbol.upper() == str(action.symbol).upper():
            found = True
            positions.append(replace(pos, market_value=float(pos.market_value) + notional))
        else:
            positions.append(pos)
    if not found:
        positions.append(
            Position(
                symbol=str(action.symbol).upper(),
                market_value=notional,
                sleeve=action.sleeve,
                security_class=action.security_class,
                classification_status=action.classification_status,
                sector=action.sector,
                thesis_id=action.thesis_id,
            )
        )
    out = build_context(
        account_number=context.account_number,
        current_nav=context.current_nav,
        cash=cash,
        buying_power=buying_power,
        positions=positions,
        open_orders=list(context.open_orders or []),
        realized_pnl=context.realized_pnl,
        start_of_day_nav=context.start_of_day_nav,
        prior_nav=context.current_nav,
        prior_hwm=context.high_water_mark,
        spy=context.spy,
        timestamp=context.timestamp,
        trading_session_id=context.trading_session_id,
        session_fail_safe=context.session_fail_safe,
        correlation=context.correlation,
    )
    out.risk_state = context.risk_state
    out.daily_risk_halt = context.daily_risk_halt
    return out


def sequential_risk_gate(
    decided: DecisionResult,
    context: PortfolioContext,
    *,
    sleeves=None,
    theses=None,
) -> list[GatedAction]:
    """Re-evaluate risk-up actions against an accumulating book. No bypass."""
    gated_by = {g.proposed_action.symbol.upper(): g for g in decided.gated_actions}
    ranking = list((decided.comparison.ranking if decided.comparison else []) or [])
    order = []
    seen: set[str] = set()
    for sym in ranking:
        key = str(sym).upper()
        if key in gated_by and key not in seen:
            order.append(key)
            seen.add(key)
    for nd in decided.decisions:
        key = nd.symbol.upper()
        if key in gated_by and key not in seen:
            order.append(key)
            seen.add(key)
    sim = context
    out: list[GatedAction] = []
    for key in order:
        gated = gated_by[key]
        action = gated.proposed_action
        if action.decision in RISK_UP:
            risk = evaluate(sim, action, sleeves=sleeves, theses=theses)
            gated = GatedAction(proposed_action=action, risk=risk, thesis_id=gated.thesis_id)
            if risk.verdict in RISK_PERMIT:
                sim = simulate_after_risk_up(sim, action)
        out.append(gated)
    return out


def ensure_strategic_residual_candidate(worker, symbol: str, snapshot: dict[str, Any], now: datetime) -> Candidate | None:
    store = getattr(worker, "candidates", None)
    if store is None:
        return None
    existing = store.active_for_symbol(symbol) or store.current_for_symbol(symbol)
    price = snapshot.get("current_price") if isinstance(snapshot, dict) else None
    if existing is not None:
        if price is not None:
            existing.current_price = price
        if existing.security_class is None:
            existing.security_class = SecurityClass.BROAD_MARKET_INDEX_ETF
        if existing.classification_status is None:
            existing.classification_status = ClassificationStatus.VALIDATED
        if not existing.provisional_sleeve:
            existing.provisional_sleeve = Sleeve.CORE_GROWTH
        return store.upsert(existing)
    cand = Candidate(
        candidate_id=f"strategic-residual-{symbol.lower()}",
        symbol=symbol,
        discovered_at=now.isoformat(),
        discovery_source="strategic_core_residual",
        provisional_sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
        classification_status=ClassificationStatus.VALIDATED,
        current_price=price,
        status=CandidateStatus.RESEARCH_COMPLETE,
        reasons=["configured_core_benchmark_vehicle"],
    )
    return store.upsert(cand, allow_status_transition=True)


def _observability_from_decided(decided: DecisionResult, residual_symbol: str) -> dict[str, Any]:
    residual = str(residual_symbol or SPY_SYMBOL).upper()
    cash = next((d for d in decided.decisions if d.symbol == CASH_SYMBOL), None)
    market = next((d for d in decided.decisions if d.symbol == residual), None)
    selected = [d.symbol for d in decided.decisions if d.decision in RISK_UP and d.symbol not in {CASH_SYMBOL, residual}]
    return {
        "portfolio_action": "ALLOCATE" if any(d.decision in RISK_UP for d in decided.decisions) else "HOLD_CASH",
        "cash_target_weight": cash.desired_allocation_pct if cash else None,
        "broad_market_target_weight": market.desired_allocation_pct if market else None,
        "selected_individual_names": selected,
        "cash_reason": (decided.comparison.vs_cash if decided.comparison else None) or (cash.rationale if cash else None),
        "broad_market_reason": (decided.comparison.vs_spy if decided.comparison else None)
        or (market.why_preferable_to_cash if market else None),
    }


def _committee_meta(reasoner: DecisionReasoner | None) -> tuple[str | None, str | None]:
    result = getattr(reasoner, "last_result", None)
    if result is None:
        return None, None
    role = getattr(result, "role", None) or getattr(reasoner, "role", None)
    model = getattr(result, "model", None)
    if hasattr(role, "value"):
        role = role.value
    return (str(role) if role else None), (str(model) if model else None)


def _event(
    root: Path,
    kind: str,
    *,
    persist: bool,
    journal: Path | None,
    **fields: Any,
) -> None:
    payload = {
        "type": kind,
        "auto_execution": False,
        "forced_buy": False,
        "research_called": False,
        "LIVE_ORDER_PLACEMENT": live_placement_enabled(),
        **fields,
    }
    if persist:
        append_jsonl(payload, journal or (Path(root) / "logs" / "core_committee.jsonl"))
        log_activity(root, kind, **{k: v for k, v in fields.items() if k != "type"})


def run_core_committee(
    *,
    worker,
    context: PortfolioContext,
    trigger: str,
    force: bool = False,
    persist: bool = True,
    extra_reports: list[ResearchReport] | None = None,
    reevaluation: bool = False,
) -> CommitteeResult:
    """Run one CORE committee pass using existing artifacts + current LIVE context."""
    result = CommitteeResult(trigger=trigger, reevaluation=reevaluation, LIVE_ORDER_PLACEMENT=live_placement_enabled())
    now = worker.now()
    root = Path(worker.root)
    ledger_path = committee_ledger_path(root, runtime_mode=worker.runtime_mode)
    ledger = load_committee_ledger(ledger_path)
    journal = root / "logs" / "core_committee.jsonl"
    watch_store = worker.watch.store if worker.watch is not None else None
    approvals_store = worker.approvals.store if worker.approvals is not None else None

    committee_input = collect_committee_input(
        research_store=worker.research_store,
        watch_store=watch_store,
        candidates=worker.candidates,
        theses=worker.theses,
        approvals=approvals_store,
        now=now,
        extra_reports=extra_reports,
    )
    result.eligible_symbols = list(committee_input.symbols)
    result.skipped = list(committee_input.skipped)
    result.alternatives_considered = list(committee_input.alternatives)
    result.reports_in_packet = len(committee_input.reports)
    residual_symbol = configured_residual_symbol()
    residual_snapshot = build_broad_market_residual(
        context,
        spy_report=committee_input.spy_evidence_report,
        now=now,
    )
    residual_eligible = residual_strategic_buy_eligible(residual_snapshot)
    result.spy_included = spy_snapshot_usable(residual_snapshot)
    committee_input.spy_included = result.spy_included
    result.fingerprint = committee_fingerprint(committee_input, context)
    result.previous_portfolio_action = ledger.get("last_portfolio_action") or ledger.get("last_status")
    result.prior_decision_age_hours = _age_hours(ledger.get("last_run_at"), now)
    result.next_eligible_scheduled_review_at = next_scheduled_review_at(ledger, now=now).isoformat()

    _event(
        root,
        "CORE_COMMITTEE_STARTED",
        persist=persist,
        journal=journal,
        trigger=trigger,
        candidate_symbols=result.eligible_symbols,
        portfolio_cash=context.cash,
        holdings_count=context.holdings_count,
        reevaluation=reevaluation,
        previous_portfolio_action=result.previous_portfolio_action,
        prior_decision_age_hours=result.prior_decision_age_hours,
        next_eligible_scheduled_review_at=result.next_eligible_scheduled_review_at,
        spy_included=result.spy_included,
    )
    _event(
        root,
        "CORE_COMMITTEE_ELIGIBLE_SET",
        persist=persist,
        journal=journal,
        candidate_symbols=result.eligible_symbols,
        alternatives_considered=result.alternatives_considered,
        skipped=[row.get("symbol") for row in result.skipped],
        reports_in_packet=result.reports_in_packet,
        portfolio_cash=context.cash,
        holdings_count=context.holdings_count,
        spy_included=result.spy_included,
        residual_eligible=residual_eligible,
    )

    run, why = should_run_committee(
        committee_input,
        context,
        ledger,
        now=now,
        trigger=trigger,
        force=force,
        residual_eligible=residual_eligible,
    )
    if why == "no_action_expired_review":
        _event(
            root,
            "CORE_COMMITTEE_NO_ACTION_EXPIRED_REVIEW",
            persist=persist,
            journal=journal,
            reason=why,
            previous_portfolio_action=result.previous_portfolio_action,
            prior_decision_age_hours=result.prior_decision_age_hours,
            next_eligible_scheduled_review_at=result.next_eligible_scheduled_review_at,
            candidate_symbols=result.eligible_symbols,
            portfolio_cash=context.cash,
            holdings_count=context.holdings_count,
        )
    if not run:
        result.status = "SKIPPED_UNCHANGED" if why == "unchanged_fingerprint" else "SKIPPED"
        result.skipped_reason = why
        skip_kind = "CORE_COMMITTEE_SKIPPED_UNCHANGED"
        _event(
            root,
            skip_kind,
            persist=persist,
            journal=journal,
            reason=why,
            previous_portfolio_action=result.previous_portfolio_action,
            prior_decision_age_hours=result.prior_decision_age_hours,
            next_eligible_scheduled_review_at=result.next_eligible_scheduled_review_at,
            candidate_symbols=result.eligible_symbols,
            portfolio_cash=context.cash,
            holdings_count=context.holdings_count,
            spy_included=result.spy_included,
        )
        return result
    if not committee_input.reports and not residual_eligible:
        result.status = "SKIPPED"
        result.skipped_reason = "empty_eligible_set"
        return result

    blocked, budget_why = worker._budget_blocked()
    if blocked:
        result.status = "BLOCKED"
        result.skipped_reason = budget_why
        result.reason = budget_why
        return result

    try:
        reasoner = worker._decision()
    except RuntimeError as exc:
        result.status = "DEGRADED"
        result.reason = str(exc)
        result.skipped_reason = "no_decision_reasoner"
        return result

    try:
        decided = run_portfolio_decision(
            committee_input.reports,
            context,
            reasoner,
            theses=worker.theses,
            sleeves=worker.sleeves,
            store=worker.decision_store,
            persist=persist,
            now=now,
            journal=root / "logs" / "thesis_decision.jsonl",
            committee=True,
            market_evidence_reports=[committee_input.spy_evidence_report]
            if committee_input.spy_evidence_report is not None
            else None,
        )
    except DecisionValidationError as exc:
        result.status = "DEGRADED"
        result.reason = str(exc)
        result.ai_calls = int(getattr(reasoner, "call_count", 1) or 1)
        result.ai_stages_called = ["portfolio_decision"]
        result.ai_role, result.ai_model = _committee_meta(reasoner)
        if getattr(reasoner, "truncation_retry_used", False):
            _event(
                root,
                "CORE_COMMITTEE_OUTPUT_TRUNCATED_RETRY",
                persist=persist,
                journal=journal,
                reason=str(exc),
                retried=True,
                succeeded=False,
                ai_calls=result.ai_calls,
                candidate_symbols=result.eligible_symbols,
            )
        return result

    result.ai_calls = int(getattr(reasoner, "call_count", 1) or 1)
    result.ai_stages_called = ["portfolio_decision"]
    result.ai_role, result.ai_model = _committee_meta(reasoner)
    residual = ((getattr(decided.packet, "policy_context", None) or {}).get("broad_market_residual") or {})
    result.spy_included = bool(
        (getattr(decided.packet, "policy_context", None) or {}).get("spy_included")
        or residual.get("usable_for_comparison")
    )
    committee_input.spy_included = result.spy_included
    if getattr(reasoner, "truncation_retry_used", False):
        failed = bool(decided.validation_errors)
        _event(
            root,
            "CORE_COMMITTEE_OUTPUT_TRUNCATED_RETRY",
            persist=persist,
            journal=journal,
            reason="; ".join(str(item) for item in decided.validation_errors) if failed else "max_output_tokens",
            retried=True,
            succeeded=not failed,
            ai_calls=result.ai_calls,
            candidate_symbols=result.eligible_symbols,
        )
    result.batch_id = decided.batch_id
    if decided.validation_errors:
        result.status = "DEGRADED"
        result.reason = "; ".join(str(item) for item in decided.validation_errors)
        return result

    decided.gated_actions = sequential_risk_gate(
        decided,
        context,
        sleeves=worker.sleeves,
        theses=worker.theses,
    )
    by_report = {item.symbol: item for item in committee_input.eligible}
    residual_report = next(
        (
            r
            for r in decided.reports
            if is_strategic_residual_report(r) or str(r.symbol or "").upper() == residual_symbol
        ),
        None,
    )
    selected_residual = next(
        (d for d in decided.decisions if d.symbol.upper() == residual_symbol and d.decision in ACTIONABLE),
        None,
    )
    if selected_residual is not None and residual_report is not None and residual_symbol not in by_report:
        cand = ensure_strategic_residual_candidate(worker, residual_symbol, residual_snapshot, now)
        by_report[residual_symbol] = EligibleAlternative(
            symbol=residual_symbol,
            report=residual_report,
            source="strategic_residual",
            candidate=cand,
        )
    symbol_rows = worker.apply_committee_decisions(decided, by_report, context)
    result.symbol_rows = symbol_rows
    result.theses_created = len(decided.theses)
    result.watches_created = sum(int(row.get("watches_created") or 0) for row in symbol_rows)
    result.proposals_created = sum(int(row.get("proposals_created") or 0) for row in symbol_rows)
    result.approvals_created = result.proposals_created
    result.risk_blocked = sum(1 for row in symbol_rows if row.get("risk_verdict") and row.get("proposals_created") in {0, None} and row.get("decision") in {d.value for d in RISK_UP})
    result.decisions = {d.symbol: d.decision.value for d in decided.decisions}
    result.selected_symbols = [d.symbol for d in decided.decisions if d.decision in RISK_UP]
    result.target_allocations = {d.symbol: d.desired_allocation_pct for d in decided.decisions if d.decision in RISK_UP | {Decision.HOLD, Decision.NO_ACTION, Decision.WATCH}}
    obs = _observability_from_decided(decided, residual_symbol)
    result.portfolio_action = obs["portfolio_action"]
    result.cash_target_weight = obs["cash_target_weight"]
    result.broad_market_target_weight = obs["broad_market_target_weight"]
    result.cash_reason = obs["cash_reason"]
    result.broad_market_reason = obs["broad_market_reason"]
    if not result.selected_symbols:
        result.status = "NO_ACTION"
        result.reason = result.cash_reason or "cash_retained"
        _event(
            root,
            "CORE_COMMITTEE_NO_ACTION",
            persist=persist,
            journal=journal,
            candidate_symbols=result.eligible_symbols,
            selected_symbols=[],
            target_allocation=result.target_allocations,
            alternatives_considered=result.alternatives_considered,
            reason=result.reason,
            previous_portfolio_action=result.previous_portfolio_action,
            portfolio_action=result.portfolio_action,
            cash_target_weight=result.cash_target_weight,
            broad_market_target_weight=result.broad_market_target_weight,
            selected_individual_names=obs["selected_individual_names"],
            cash_reason=result.cash_reason,
            broad_market_reason=result.broad_market_reason,
            prior_decision_age_hours=result.prior_decision_age_hours,
            next_eligible_scheduled_review_at=now.isoformat(),
            portfolio_cash=context.cash,
            holdings_count=context.holdings_count,
            ai_role=result.ai_role,
            ai_model=result.ai_model,
        )
    else:
        result.status = "OK"
        _event(
            root,
            "CORE_COMMITTEE_DECIDED",
            persist=persist,
            journal=journal,
            candidate_symbols=result.eligible_symbols,
            selected_symbols=result.selected_symbols,
            target_allocation=result.target_allocations,
            alternatives_considered=result.alternatives_considered,
            reason=(decided.comparison.notes if decided.comparison else None),
            previous_portfolio_action=result.previous_portfolio_action,
            portfolio_action=result.portfolio_action,
            cash_target_weight=result.cash_target_weight,
            broad_market_target_weight=result.broad_market_target_weight,
            selected_individual_names=obs["selected_individual_names"],
            cash_reason=result.cash_reason,
            broad_market_reason=result.broad_market_reason,
            prior_decision_age_hours=result.prior_decision_age_hours,
            portfolio_cash=context.cash,
            holdings_count=context.holdings_count,
            ai_role=result.ai_role,
            ai_model=result.ai_model,
            batch_id=result.batch_id,
        )
    for row in symbol_rows:
        if int(row.get("proposals_created") or 0):
            _event(
                root,
                "CORE_COMMITTEE_PROPOSAL_CREATED",
                persist=persist,
                journal=journal,
                selected_symbols=[row.get("symbol")],
                target_allocation=row.get("desired_allocation_pct") or row.get("decision"),
                approval_id=row.get("approval_id"),
                portfolio_cash=context.cash,
                holdings_count=context.holdings_count,
                auto_execution=False,
            )
        if row.get("risk_verdict") and int(row.get("proposals_created") or 0) == 0 and row.get("decision") in {d.value for d in RISK_UP}:
            _event(
                root,
                "CORE_COMMITTEE_BLOCKED_BY_RISK",
                persist=persist,
                journal=journal,
                selected_symbols=[row.get("symbol")],
                reason=row.get("risk_verdict"),
                portfolio_cash=context.cash,
                holdings_count=context.holdings_count,
            )

    if persist:
        ledger["last_fingerprint"] = result.fingerprint
        ledger["last_run_at"] = now.isoformat()
        ledger["last_research_ids"] = [r.research_id for r in committee_input.reports]
        ledger["last_holdings_symbols"] = [p.symbol.upper() for p in context.positions]
        ledger["last_cash_allocation_pct"] = context.cash_allocation_pct
        ledger["last_risk_state"] = context.risk_state.value if context.risk_state else None
        ledger["last_daily_risk_halt"] = bool(context.daily_risk_halt)
        ledger["last_batch_id"] = result.batch_id
        ledger["last_decisions"] = result.decisions
        ledger["last_status"] = result.status
        ledger["last_portfolio_action"] = result.portfolio_action
        ledger["last_cash_target_weight"] = result.cash_target_weight
        ledger["last_broad_market_target_weight"] = result.broad_market_target_weight
        run_row = {
            "at": now.isoformat(),
            "trigger": trigger if why != "no_action_expired_review" else why,
            "fingerprint": result.fingerprint,
            "status": result.status,
            "portfolio_action": result.portfolio_action,
            "symbols": result.eligible_symbols,
            "selected": result.selected_symbols,
            "cash_target_weight": result.cash_target_weight,
            "broad_market_target_weight": result.broad_market_target_weight,
            "batch_id": result.batch_id,
            "proposals_created": result.proposals_created,
            "reevaluation": reevaluation,
            "research_called": False,
            "auto_execution": False,
        }
        ledger.setdefault("runs", []).append(run_row)
        if reevaluation:
            ledger.setdefault("reevaluation_runs", []).append(run_row)
        save_committee_ledger(ledger_path, ledger)
    return result


def reevaluate_live_core_committee(
    *,
    root: Path,
    runtime_mode: RuntimeMode | str,
    decision_reasoner: DecisionReasoner | None = None,
    context_fn: Callable[[], Any] | None = None,
    now: datetime | None = None,
    persist: bool = True,
    journal: Path | None = None,
    watch=None,
    approvals=None,
    notify=None,
    payload_fn: Callable[..., Any] | None = None,
    research_reasoner: Any = None,
    fetcher: Any = None,
    force: bool = True,
) -> CommitteeResult:
    """LIVE-only committee reevaluation using existing fresh artifacts.

    Never calls research collection / Terra. Never touches PAPER. Never forces BUY.
    Human approval remains mandatory. Auto-execution remains false.
    """
    mode = runtime_mode if isinstance(runtime_mode, RuntimeMode) else RuntimeMode(str(runtime_mode).upper())
    result = CommitteeResult(trigger="reevaluation", reevaluation=True)
    if mode is not RuntimeMode.LIVE:
        result.status = "SKIPPED"
        result.skipped_reason = "not_live"
        return result
    if payload_fn is not None or research_reasoner is not None or fetcher is not None:
        raise RuntimeError("CORE committee reevaluation must not collect research or call Terra")
    if decision_reasoner is None:
        raise RuntimeError("CORE committee reevaluation requires a portfolio decision reasoner")

    base = Path(root)
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)

    from agentic_portfolio.agent.pipeline import ResearchQueueWorker, load_live_context
    from agentic_portfolio.live_approval import LiveApprovalEngine, LiveApprovalStore
    from agentic_portfolio.notify import NotificationEngine, NotificationStore
    from agentic_portfolio.watch.engine import WatchEngine
    from agentic_portfolio.watch.store import WatchStore

    def _refuse_research(candidate):
        raise RuntimeError("CORE committee reevaluation must not collect research or call Terra")

    context_provider = context_fn or (lambda: load_live_context(base, runtime_mode=RuntimeMode.LIVE))
    watch_engine = watch or WatchEngine(
        WatchStore(base, runtime_mode=RuntimeMode.LIVE),
        journal=base / "logs" / "agent.jsonl",
        now_fn=lambda: stamp,
    )
    approval_engine = approvals
    if approval_engine is None:
        approval_engine = LiveApprovalEngine(
            LiveApprovalStore(base, runtime_mode=RuntimeMode.LIVE),
            journal=base / "logs" / "approval.jsonl",
            now_fn=lambda: stamp,
        )
    notify_engine = notify or NotificationEngine(NotificationStore(base), now_fn=lambda: stamp)
    worker = ResearchQueueWorker(
        base,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=decision_reasoner,
        payload_fn=_refuse_research,
        context_fn=context_provider,
        watch=watch_engine,
        approvals=approval_engine,
        notify=notify_engine,
        now_fn=lambda: stamp,
    )
    context = context_provider()
    if context is None:
        result.status = "BLOCKED"
        result.skipped_reason = "missing_live_context"
        return result

    out = run_core_committee(
        worker=worker,
        context=context,
        trigger="reevaluation",
        force=force,
        persist=persist,
        reevaluation=True,
    )
    out.reevaluation = True
    out.research_called = False
    out.terra_called = False
    out.paper_state_touched = False
    _event(
        base,
        "CORE_COMMITTEE_REEVALUATION",
        persist=persist,
        journal=journal or (base / "logs" / "core_committee.jsonl"),
        candidate_symbols=out.eligible_symbols,
        skipped=out.skipped,
        selected_symbols=out.selected_symbols,
        target_allocation=out.target_allocations,
        alternatives_considered=out.alternatives_considered,
        reason=out.reason or out.skipped_reason or out.status,
        portfolio_cash=context.cash,
        holdings_count=context.holdings_count,
        proposals_created=out.proposals_created,
        approvals_created=out.approvals_created,
        ai_stages_called=out.ai_stages_called,
        ai_role=out.ai_role,
        ai_model=out.ai_model,
        research_called=False,
        terra_called=False,
        auto_execution=False,
        paper_state_touched=False,
    )
    return out
