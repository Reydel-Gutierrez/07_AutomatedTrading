"""Internal notification events. Dashboard first; email/push can be added later."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from agentic_portfolio.schemas import to_dict


class NotificationKind(str, Enum):
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    RISK_ALERT = "RISK_ALERT"
    BROKER_CONNECTION_LOST = "BROKER_CONNECTION_LOST"
    BROKER_CONNECTION_RESTORED = "BROKER_CONNECTION_RESTORED"
    AI_BUDGET_CRITICAL = "AI_BUDGET_CRITICAL"
    AI_BUDGET_EXHAUSTED = "AI_BUDGET_EXHAUSTED"
    AI_BUDGET_WARNING = "AI_BUDGET_WARNING"
    SERVICE_ERROR = "SERVICE_ERROR"
    CANDIDATE_PROMOTED = "CANDIDATE_PROMOTED"
    RESEARCH_COMPLETED = "RESEARCH_COMPLETED"
    RESEARCH_REJECTED = "RESEARCH_REJECTED"
    WATCH_CREATED = "WATCH_CREATED"
    THESIS_CHANGED = "THESIS_CHANGED"
    TRADE_PROPOSAL = "TRADE_PROPOSAL"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_SUPERSEDED = "APPROVAL_SUPERSEDED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_CANCELED = "ORDER_CANCELED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    RISK_GATE_BLOCKED = "RISK_GATE_BLOCKED"


@dataclass
class Notification:
    notification_id: str
    kind: NotificationKind
    title: str
    body: str
    created_at: str
    read: bool = False
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = to_dict(self)
        data["kind"] = self.kind.value if isinstance(self.kind, NotificationKind) else str(self.kind)
        return data


def notification_from_dict(raw: dict[str, Any]) -> Notification:
    data = dict(raw)
    return Notification(
        notification_id=str(data["notification_id"]),
        kind=NotificationKind(str(data["kind"])),
        title=str(data.get("title") or data["kind"]),
        body=str(data.get("body") or ""),
        created_at=str(data.get("created_at") or ""),
        read=bool(data.get("read")),
        payload=dict(data.get("payload") or {}),
    )


class NotificationSink(Protocol):
    """Future email/push sinks implement this. Trading logic never imports a sink."""

    def emit(self, notification: Notification) -> None: ...
