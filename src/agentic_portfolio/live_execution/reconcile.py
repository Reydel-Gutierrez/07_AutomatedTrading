"""Reconcile local broker order records against the live broker."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from agentic_portfolio.live_approval.types import LiveApprovalStatus
from agentic_portfolio.live_execution.audit import record_audit
from agentic_portfolio.live_execution.broker import BrokerClient
from agentic_portfolio.live_execution.store import ExecutionStore
from agentic_portfolio.live_execution.types import (
    BrokerOrderRecord,
    BrokerOrderStatus,
    ExecutionIntentStatus,
)
from agentic_portfolio.notify import NotificationEngine, NotificationKind


BROKER_TO_LOCAL = {
    "new": BrokerOrderStatus.OPEN,
    "queued": BrokerOrderStatus.OPEN,
    "confirmed": BrokerOrderStatus.OPEN,
    "unconfirmed": BrokerOrderStatus.OPEN,
    "partially_filled": BrokerOrderStatus.PARTIALLY_FILLED,
    "filled": BrokerOrderStatus.FILLED,
    "cancelled": BrokerOrderStatus.CANCELED,
    "canceled": BrokerOrderStatus.CANCELED,
    "rejected": BrokerOrderStatus.REJECTED,
    "failed": BrokerOrderStatus.REJECTED,
    "voided": BrokerOrderStatus.REJECTED,
}


def reconcile_orders(
    store: ExecutionStore,
    broker: BrokerClient,
    *,
    account_number: str,
    root,
    now: datetime | None = None,
    notify: NotificationEngine | None = None,
    refresh_fn: Callable[[], Any] | None = None,
    approval_store=None,
) -> dict[str, Any]:
    stamp = now or datetime.now(timezone.utc)
    updated = 0
    filled = 0
    rejected = 0
    unknown = 0
    for order in store.orders():
        if order.status in {BrokerOrderStatus.FILLED, BrokerOrderStatus.CANCELED, BrokerOrderStatus.REJECTED}:
            continue
        remote = _fetch(broker, account_number, order)
        if remote is None and order.status is BrokerOrderStatus.UNKNOWN_RECONCILIATION_REQUIRED:
            unknown += 1
            continue
        if remote is None:
            continue
        state = str(remote.get("state") or remote.get("status") or "").lower()
        mapped = BROKER_TO_LOCAL.get(state)
        if mapped is None:
            order.status = BrokerOrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
            order.broker_status = state
            store.save_order(order)
            unknown += 1
            continue
        prior = order.status
        order.status = mapped
        order.broker_status = state
        order.filled_quantity = _f(remote.get("filled_quantity") or order.filled_quantity)
        order.average_fill_price = _f(remote.get("average_fill_price") or order.average_fill_price)
        order.rejection_reason = remote.get("reject_reason") or order.rejection_reason
        order.updated_at = stamp.isoformat()
        if remote.get("id") and not order.broker_order_id:
            order.broker_order_id = str(remote.get("id"))
        store.save_order(order)
        intent = store.get_intent(order.intent_id)
        if intent is not None:
            intent.status = ExecutionIntentStatus(mapped.value)
            intent.broker_order_id = order.broker_order_id
            intent.updated_at = order.updated_at
            store.save_intent(intent)
        updated += 1
        if mapped is BrokerOrderStatus.FILLED and prior is not BrokerOrderStatus.FILLED:
            filled += 1
            record_audit("ORDER_FILLED", root=root, now=stamp, approval_id=order.approval_id, broker_order_id=order.broker_order_id)
            try:
                from agentic_portfolio.live_execution.positions import upsert_from_fill

                upsert_from_fill(root, symbol=order.symbol, store=store, mode=store.runtime_mode)
            except Exception:  # noqa: BLE001
                pass
            if refresh_fn:
                try:
                    refresh_fn()
                except Exception:  # noqa: BLE001
                    pass
            if approval_store is not None:
                item = approval_store.get(order.approval_id)
                if item is not None:
                    item.status = LiveApprovalStatus.EXECUTED
                    approval_store.save(item)
            if notify:
                notify.emit(
                    NotificationKind.ORDER_FILLED,
                    title=f"ORDER FILLED — {order.symbol}",
                    body=f"{order.symbol} filled.",
                    payload={
                        "broker_order_id": order.broker_order_id,
                        "approval_id": order.approval_id,
                        "ticker": order.symbol,
                    },
                )
        elif mapped is BrokerOrderStatus.REJECTED and prior is not BrokerOrderStatus.REJECTED:
            rejected += 1
            record_audit("ORDER_REJECTED", root=root, now=stamp, approval_id=order.approval_id, reason=order.rejection_reason)
            if notify:
                notify.emit(
                    NotificationKind.ORDER_REJECTED,
                    title=f"Broker rejected {order.symbol}",
                    body=order.rejection_reason or "rejected",
                    payload={"approval_id": order.approval_id, "ticker": order.symbol},
                )
        elif mapped is BrokerOrderStatus.CANCELED and prior is not BrokerOrderStatus.CANCELED:
            record_audit("ORDER_CANCELED", root=root, now=stamp, approval_id=order.approval_id)
            if notify:
                notify.emit(
                    NotificationKind.ORDER_CANCELED,
                    title=f"Order canceled — {order.symbol}",
                    body=f"{order.symbol} canceled.",
                    payload={"approval_id": order.approval_id},
                )
        elif mapped is BrokerOrderStatus.PARTIALLY_FILLED:
            record_audit("ORDER_PARTIAL_FILL", root=root, now=stamp, approval_id=order.approval_id, filled_quantity=order.filled_quantity)
    return {"updated": updated, "filled": filled, "rejected": rejected, "unknown": unknown}


def _fetch(broker: BrokerClient, account_number: str, order: BrokerOrderRecord) -> dict[str, Any] | None:
    try:
        if order.broker_order_id:
            payload = broker.get_equity_orders(account_number, order_id=order.broker_order_id)
        elif order.ref_id:
            payload = broker.get_equity_orders(account_number, symbol=order.symbol)
        else:
            payload = broker.get_equity_orders(account_number, symbol=order.symbol)
    except Exception:  # noqa: BLE001
        return None
    rows = ((payload or {}).get("data") or {}).get("orders") or []
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if order.broker_order_id and str(row.get("id") or "") == order.broker_order_id:
            return row
        if order.ref_id and str(row.get("ref_id") or "") == order.ref_id:
            return row
    return rows[0] if len(rows) == 1 else None


def _f(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
