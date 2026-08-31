"""LIVE approval queue. APPROVE never submits an order."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agentic_portfolio.schemas import to_dict


class LiveApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    APPROVED_EXECUTION_DISABLED = "APPROVED_EXECUTION_DISABLED"
    APPROVED_AWAITING_EXECUTION_IMPLEMENTATION = "APPROVED_AWAITING_EXECUTION_IMPLEMENTATION"
    REVALIDATION_REQUIRED = "REVALIDATION_REQUIRED"
    EXECUTED = "EXECUTED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


OPEN_LIVE = {LiveApprovalStatus.PENDING}
APPROVED_STATES = {
    LiveApprovalStatus.APPROVED,
    LiveApprovalStatus.APPROVED_EXECUTION_DISABLED,
    LiveApprovalStatus.APPROVED_AWAITING_EXECUTION_IMPLEMENTATION,
    LiveApprovalStatus.EXECUTED,
    LiveApprovalStatus.REVALIDATION_REQUIRED,
    LiveApprovalStatus.EXECUTION_FAILED,
}
TERMINAL_LIVE = {
    LiveApprovalStatus.REJECTED,
    LiveApprovalStatus.EXPIRED,
    LiveApprovalStatus.CANCELLED,
    LiveApprovalStatus.EXECUTED,
}


@dataclass
class LiveApproval:
    approval_id: str
    ticker: str
    proposed_action: str
    proposed_dollar_amount: float | None = None
    proposed_allocation_pct: float | None = None
    reason: str | None = None
    ai_rationale: str | None = None
    supporting_thesis: str | None = None
    current_quote: float | None = None
    current_spread_bps: float | None = None
    risk_gate_result: dict[str, Any] = field(default_factory=dict)
    portfolio_impact: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    expires_at: str | None = None
    status: LiveApprovalStatus = LiveApprovalStatus.PENDING
    watch_id: str | None = None
    decided_at: str | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    runtime_mode: str = "LIVE"
    queue_kind: str = "live"
    broker_submitted: bool = False
    placed_order: bool = False
    approved_does_not_place_order: bool = True
    live_execution_blocked: bool = True
    live_trade_actions_allowed: bool = False
    auto_execution: bool = False
    LIVE_ORDER_PLACEMENT: bool = False
    execution_attempted: bool = False
    paper_environment: bool = False
    sleeve: str | None = None
    research_id: str | None = None
    thesis_id: str | None = None
    research_summary: str | None = None
    catalysts: list[str] = field(default_factory=list)
    key_risks: list[str] = field(default_factory=list)
    invalidation: list[str] = field(default_factory=list)
    expected_horizon: str | None = None
    evidence_freshness: str | None = None
    provider: str | None = None
    model: str | None = None
    quote_at_proposal: float | None = None
    nav_at_proposal: float | None = None
    bull_case: str | None = None
    base_case: str | None = None
    bear_case: str | None = None
    expected_order_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = to_dict(self)
        data["status"] = self.status.value if isinstance(self.status, LiveApprovalStatus) else str(self.status)
        return data


def live_approval_from_dict(raw: dict[str, Any]) -> LiveApproval:
    data = dict(raw)
    status = LiveApprovalStatus(str(data.pop("status", LiveApprovalStatus.PENDING.value)))
    approval_id = str(data.pop("approval_id"))
    ticker = str(data.pop("ticker")).upper()
    proposed_action = str(data.pop("proposed_action"))
    allowed = LiveApproval.__dataclass_fields__.keys() - {"approval_id", "ticker", "proposed_action", "status"}
    kwargs = {key: data[key] for key in allowed if key in data}
    return LiveApproval(approval_id=approval_id, ticker=ticker, proposed_action=proposed_action, status=status, **kwargs)
