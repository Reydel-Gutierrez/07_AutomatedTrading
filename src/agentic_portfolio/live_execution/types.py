"""Live execution types. Human-approved actions only."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agentic_portfolio.schemas import to_dict


class ExecutionIntentStatus(str, Enum):
    CREATED = "CREATED"
    LOCKED = "LOCKED"
    REVALIDATING = "REVALIDATING"
    REVIEWING = "REVIEWING"
    PENDING_SUBMISSION = "PENDING_SUBMISSION"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    BLOCKED_DISABLED = "BLOCKED_DISABLED"
    REVALIDATION_REQUIRED = "REVALIDATION_REQUIRED"
    BLOCKED = "BLOCKED"
    UNKNOWN_RECONCILIATION_REQUIRED = "UNKNOWN_RECONCILIATION_REQUIRED"


class BrokerOrderStatus(str, Enum):
    PENDING_SUBMISSION = "PENDING_SUBMISSION"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    UNKNOWN_RECONCILIATION_REQUIRED = "UNKNOWN_RECONCILIATION_REQUIRED"


TERMINAL_INTENT = {
    ExecutionIntentStatus.FILLED,
    ExecutionIntentStatus.REJECTED,
    ExecutionIntentStatus.CANCELED,
    ExecutionIntentStatus.BLOCKED_DISABLED,
    ExecutionIntentStatus.BLOCKED,
    ExecutionIntentStatus.REVALIDATION_REQUIRED,
}

IN_FLIGHT = {
    ExecutionIntentStatus.LOCKED,
    ExecutionIntentStatus.REVALIDATING,
    ExecutionIntentStatus.REVIEWING,
    ExecutionIntentStatus.PENDING_SUBMISSION,
    ExecutionIntentStatus.SUBMITTED,
    ExecutionIntentStatus.OPEN,
    ExecutionIntentStatus.PARTIALLY_FILLED,
}


@dataclass
class ExecutionIntent:
    intent_id: str
    approval_id: str
    proposal_id: str | None = None
    thesis_id: str | None = None
    symbol: str = ""
    side: str = "buy"
    action: str = "BUY"
    notional: float | None = None
    quantity: float | None = None
    allocation_pct: float | None = None
    order_type: str = "market"
    time_in_force: str = "gfd"
    limit_price: float | None = None
    status: ExecutionIntentStatus = ExecutionIntentStatus.CREATED
    broker_order_id: str | None = None
    ref_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    submitted_at: str | None = None
    block_reasons: list[str] = field(default_factory=list)
    nav_at_approval: float | None = None
    quote_at_approval: float | None = None
    runtime_mode: str = "LIVE"
    LIVE_ORDER_PLACEMENT: bool = False
    place_attempted: bool = False
    review_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = to_dict(self)
        data["status"] = self.status.value if isinstance(self.status, ExecutionIntentStatus) else str(self.status)
        return data


def intent_from_dict(raw: dict[str, Any]) -> ExecutionIntent:
    data = dict(raw)
    status = ExecutionIntentStatus(str(data.pop("status", ExecutionIntentStatus.CREATED.value)))
    intent_id = str(data.pop("intent_id"))
    approval_id = str(data.pop("approval_id"))
    allowed = ExecutionIntent.__dataclass_fields__.keys() - {"intent_id", "approval_id", "status"}
    kwargs = {key: data[key] for key in allowed if key in data}
    return ExecutionIntent(intent_id=intent_id, approval_id=approval_id, status=status, **kwargs)


@dataclass
class BrokerOrderRecord:
    order_id: str
    intent_id: str
    approval_id: str
    proposal_id: str | None = None
    thesis_id: str | None = None
    symbol: str = ""
    side: str = "buy"
    quantity: float | None = None
    notional: float | None = None
    order_type: str = "market"
    limit_price: float | None = None
    broker_order_id: str | None = None
    status: BrokerOrderStatus = BrokerOrderStatus.PENDING_SUBMISSION
    broker_status: str | None = None
    submitted_at: str | None = None
    updated_at: str = ""
    filled_quantity: float | None = None
    average_fill_price: float | None = None
    rejection_reason: str | None = None
    raw_broker: dict[str, Any] = field(default_factory=dict)
    ref_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = to_dict(self)
        data["status"] = self.status.value if isinstance(self.status, BrokerOrderStatus) else str(self.status)
        return data


def order_from_dict(raw: dict[str, Any]) -> BrokerOrderRecord:
    data = dict(raw)
    status = BrokerOrderStatus(str(data.pop("status", BrokerOrderStatus.PENDING_SUBMISSION.value)))
    order_id = str(data.pop("order_id"))
    intent_id = str(data.pop("intent_id"))
    approval_id = str(data.pop("approval_id"))
    allowed = BrokerOrderRecord.__dataclass_fields__.keys() - {"order_id", "intent_id", "approval_id", "status"}
    kwargs = {key: data[key] for key in allowed if key in data}
    return BrokerOrderRecord(order_id=order_id, intent_id=intent_id, approval_id=approval_id, status=status, **kwargs)
