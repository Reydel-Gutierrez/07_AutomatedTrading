"""Persistent LIVE watch / thesis items. Survive market close and process restart."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from agentic_portfolio.schemas import to_dict


class WatchStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    SCREENING = "SCREENING"
    RESEARCHING = "RESEARCHING"
    WATCH = "WATCH"
    WAITING_FOR_OPEN = "WAITING_FOR_OPEN"
    WAITING_FOR_PRICE = "WAITING_FOR_PRICE"
    WAITING_FOR_LIQUIDITY = "WAITING_FOR_LIQUIDITY"
    WAITING_FOR_CATALYST = "WAITING_FOR_CATALYST"
    READY_FOR_RISK_GATE = "READY_FOR_RISK_GATE"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


TERMINAL_WATCH = {WatchStatus.REJECTED, WatchStatus.EXPIRED, WatchStatus.INVALIDATED}
ACTIVE_WATCH = {
    WatchStatus.DISCOVERED,
    WatchStatus.SCREENING,
    WatchStatus.RESEARCHING,
    WatchStatus.WATCH,
    WatchStatus.WAITING_FOR_OPEN,
    WatchStatus.WAITING_FOR_PRICE,
    WatchStatus.WAITING_FOR_LIQUIDITY,
    WatchStatus.WAITING_FOR_CATALYST,
    WatchStatus.READY_FOR_RISK_GATE,
    WatchStatus.APPROVAL_REQUIRED,
}


class ReassessTrigger(str, Enum):
    PRICE_MOVE = "PRICE_MOVE"
    NEWS_CATALYST = "NEWS_CATALYST"
    EARNINGS_UPDATE = "EARNINGS_UPDATE"
    FUNDAMENTAL_UPDATE = "FUNDAMENTAL_UPDATE"
    THESIS_EXPIRED = "THESIS_EXPIRED"
    ENTRY_APPROACHED = "ENTRY_APPROACHED"
    MARKET_OPEN_AFTER_OFFHOURS = "MARKET_OPEN_AFTER_OFFHOURS"
    RISK_STATE_CHANGE = "RISK_STATE_CHANGE"
    MANUAL = "MANUAL"


@dataclass
class ConditionalPlan:
    """Off-hours plan. Never assumes Friday liquidity is executable Monday."""

    max_price: float | None = None
    max_spread_bps: float | None = None
    min_dollar_volume: float | None = None
    require_no_adverse_catalyst: bool = True
    require_cash_available: bool = True
    require_risk_gate_pass: bool = True
    require_regular_hours_quotes: bool = True
    notes: str | None = None
    prepared_session_id: str | None = None
    target_session_id: str | None = None
    proposed_notional: float | None = None
    desired_allocation_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)


@dataclass
class WatchItem:
    watch_id: str
    ticker: str
    security_identity: str | None = None
    security_type: str | None = None
    created_at: str = ""
    last_updated: str = ""
    source_candidate_score: float | None = None
    screening_result: dict[str, Any] = field(default_factory=dict)
    research_thesis: str | None = None
    confidence: str | None = None
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    entry_conditions: list[str] = field(default_factory=list)
    invalidating_conditions: list[str] = field(default_factory=list)
    expiration: str | None = None
    next_review_at: str | None = None
    required_market_confirmation: bool = True
    ai_context_ids: list[str] = field(default_factory=list)
    status: WatchStatus = WatchStatus.DISCOVERED
    runtime_mode: str = "LIVE"
    last_reassessed_at: str | None = None
    last_context_hash: str | None = None
    last_ai_at: str | None = None
    last_ai_cost: float = 0.0
    last_price: float | None = None
    last_news_hash: str | None = None
    last_risk_state: str | None = None
    conditional_plan: ConditionalPlan | None = None
    approval_id: str | None = None
    paper_environment: bool = False
    LIVE_ORDER_PLACEMENT: bool = False
    candidate_id: str | None = None
    research_id: str | None = None
    thesis_id: str | None = None
    sleeve: str | None = None
    catalysts: list[str] = field(default_factory=list)
    price_levels: dict[str, Any] = field(default_factory=dict)
    reason_for_watch: str | None = None
    proposed_notional: float | None = None
    desired_allocation_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data = to_dict(self)
        data["status"] = self.status.value if isinstance(self.status, WatchStatus) else str(self.status)
        if self.conditional_plan is not None:
            data["conditional_plan"] = self.conditional_plan.to_dict()
        return data


def watch_from_dict(raw: dict[str, Any]) -> WatchItem:
    data = dict(raw)
    plan_raw = data.pop("conditional_plan", None)
    status = WatchStatus(str(data.pop("status", WatchStatus.DISCOVERED.value)))
    plan = None
    if isinstance(plan_raw, dict):
        plan = ConditionalPlan(
            max_price=_opt_float(plan_raw.get("max_price")),
            max_spread_bps=_opt_float(plan_raw.get("max_spread_bps")),
            min_dollar_volume=_opt_float(plan_raw.get("min_dollar_volume")),
            require_no_adverse_catalyst=bool(plan_raw.get("require_no_adverse_catalyst", True)),
            require_cash_available=bool(plan_raw.get("require_cash_available", True)),
            require_risk_gate_pass=bool(plan_raw.get("require_risk_gate_pass", True)),
            require_regular_hours_quotes=bool(plan_raw.get("require_regular_hours_quotes", True)),
            notes=plan_raw.get("notes"),
            prepared_session_id=plan_raw.get("prepared_session_id"),
            target_session_id=plan_raw.get("target_session_id"),
            proposed_notional=_opt_float(plan_raw.get("proposed_notional")),
            desired_allocation_pct=_opt_float(plan_raw.get("desired_allocation_pct")),
        )
    item = WatchItem(watch_id=str(data.pop("watch_id")), ticker=str(data.pop("ticker")).upper(), status=status, **_watch_kwargs(data))
    item.conditional_plan = plan
    return item


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _watch_kwargs(data: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "security_identity",
        "security_type",
        "created_at",
        "last_updated",
        "source_candidate_score",
        "screening_result",
        "research_thesis",
        "confidence",
        "reasons",
        "risks",
        "entry_conditions",
        "invalidating_conditions",
        "expiration",
        "next_review_at",
        "required_market_confirmation",
        "ai_context_ids",
        "runtime_mode",
        "last_reassessed_at",
        "last_context_hash",
        "last_ai_at",
        "last_ai_cost",
        "last_price",
        "last_news_hash",
        "last_risk_state",
        "approval_id",
        "paper_environment",
        "LIVE_ORDER_PLACEMENT",
        "candidate_id",
        "research_id",
        "thesis_id",
        "sleeve",
        "catalysts",
        "price_levels",
        "reason_for_watch",
        "proposed_notional",
        "desired_allocation_pct",
    }
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in allowed:
            out[key] = value
    for key in ("proposed_notional", "desired_allocation_pct"):
        if key in out:
            out[key] = _opt_float(out[key])
    return out


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp
