"""Robinhood review-only types. Preflight only; never places a live order."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from agentic_portfolio.approval.types import ApprovalPacket
from agentic_portfolio.execution.types import OrderPlan, QuoteSnapshot
from agentic_portfolio.research.types import ResearchReport
from agentic_portfolio.schemas import PortfolioContext, ProposedAction, ThesisRecord


class ReviewStatus(str, Enum):
    REVIEW_READY = "REVIEW_READY"
    REVIEW_ACCEPTED = "REVIEW_ACCEPTED"
    REVIEW_REJECTED = "REVIEW_REJECTED"
    REVIEW_EXPIRED = "REVIEW_EXPIRED"
    REVIEW_FAILED = "REVIEW_FAILED"


SUCCESS_STATUSES = {ReviewStatus.REVIEW_READY, ReviewStatus.REVIEW_ACCEPTED}
EXPIRED_CODES = {
    "STALE_QUOTE",
    "STALE_RESEARCH",
    "STALE_THESIS",
    "SUPERSEDED_BY_NEWER_DECISION",
    "APPROVAL_EXPIRED",
    "APPROVAL_SUPERSEDED",
}


class ReviewClient(Protocol):
    """Broker preflight. Must implement review only. Engine never places."""

    def review_equity_order(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class StaticReviewClient:
    """Deterministic client for tests and scripted MCP responses."""

    response: dict[str, Any] | None = None
    error: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def review_equity_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(payload))
        if self.error is not None:
            raise self.error
        return dict(self.response or {})


@dataclass
class ReviewRequest:
    packet: ApprovalPacket
    plan: OrderPlan
    action: ProposedAction
    context: PortfolioContext
    quote: QuoteSnapshot | None = None
    thesis: ThesisRecord | None = None
    research: ResearchReport | None = None
    newer_decision_id: str | None = None


@dataclass
class ReviewResult:
    review_id: str
    approval_id: str
    order_plan_id: str
    symbol: str
    side: str | None
    quantity: float | None
    notional: float | None
    requested_order_type: str | None
    robinhood_response: dict[str, Any]
    estimated_cost: float | None
    estimated_proceeds: float | None
    warnings: list[str]
    errors: list[str]
    reviewed_at: str
    status: ReviewStatus
    fail_reasons: list[str] = field(default_factory=list)
    account_number: str | None = None
    time_in_force: str | None = None
    market_hours: str = "regular_hours"
    review_payload: dict[str, Any] = field(default_factory=dict)
    risk_gate_verdict: str | None = None
    broker_submitted: bool = False
    order_placed: bool = False
    execution_attempted: bool = False
    live_execution_blocked: bool = True
    live_trade_actions_allowed: bool = False
    auto_execution: bool = False
    review_accepted_does_not_execute: bool = True


@dataclass
class ReviewRun:
    run_id: str
    results: list[ReviewResult] = field(default_factory=list)
    broker_orders_submitted: int = 0
    order_placed: bool = False
    execution_attempted: bool = False
    live_execution_attempted: bool = False
    live_trade_actions_allowed: bool = False
    auto_execution: bool = False
    review_accepted_does_not_execute: bool = True


def result_from_dict(raw: dict[str, Any]) -> ReviewResult:
    return ReviewResult(
        review_id=str(raw["review_id"]),
        approval_id=str(raw["approval_id"]),
        order_plan_id=str(raw.get("order_plan_id") or ""),
        symbol=str(raw["symbol"]).upper(),
        side=raw.get("side"),
        quantity=raw.get("quantity"),
        notional=raw.get("notional"),
        requested_order_type=raw.get("requested_order_type"),
        robinhood_response=dict(raw.get("robinhood_response") or {}),
        estimated_cost=raw.get("estimated_cost"),
        estimated_proceeds=raw.get("estimated_proceeds"),
        warnings=list(raw.get("warnings") or []),
        errors=list(raw.get("errors") or []),
        reviewed_at=str(raw["reviewed_at"]),
        status=ReviewStatus(raw["status"]),
        fail_reasons=list(raw.get("fail_reasons") or []),
        account_number=raw.get("account_number"),
        time_in_force=raw.get("time_in_force"),
        market_hours=str(raw.get("market_hours") or "regular_hours"),
        review_payload=dict(raw.get("review_payload") or {}),
        risk_gate_verdict=raw.get("risk_gate_verdict"),
        broker_submitted=False,
        order_placed=False,
        execution_attempted=False,
        live_execution_blocked=True,
        live_trade_actions_allowed=False,
        auto_execution=False,
        review_accepted_does_not_execute=True,
    )
