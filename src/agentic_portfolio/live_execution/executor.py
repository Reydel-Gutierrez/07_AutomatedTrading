"""LiveOrderExecutor — the only production path that may place a broker order."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from agentic_portfolio.live_approval.types import LiveApproval, LiveApprovalStatus
from agentic_portfolio.live_execution.audit import record_audit
from agentic_portfolio.live_execution.broker import BrokerClient
from agentic_portfolio.live_execution.revalidate import revalidate_for_send
from agentic_portfolio.live_execution.safety import LiveExecutionSafetyError
from agentic_portfolio.live_execution.store import ExecutionStore
from agentic_portfolio.live_execution.types import (
    BrokerOrderRecord,
    BrokerOrderStatus,
    ExecutionIntent,
    ExecutionIntentStatus,
)
from agentic_portfolio.notify import NotificationEngine, NotificationKind
from agentic_portfolio.policy import load_account_rules, load_live_execution_config
from agentic_portfolio.review.validate import parse_review_response, quantity_str
from agentic_portfolio.runtime import RuntimeMode, live_placement_enabled
from agentic_portfolio.schemas import PortfolioContext


APPROVED_OK = {
    LiveApprovalStatus.APPROVED,
    LiveApprovalStatus.APPROVED_EXECUTION_DISABLED,
    LiveApprovalStatus.APPROVED_AWAITING_EXECUTION_IMPLEMENTATION,
}


@dataclass
class ExecutionOutcome:
    intent: ExecutionIntent
    order: BrokerOrderRecord | None = None
    placed: bool = False
    reasons: list[str] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent.intent_id,
            "approval_id": self.intent.approval_id,
            "status": self.intent.status.value,
            "placed": self.placed,
            "broker_order_id": self.intent.broker_order_id,
            "reasons": list(self.reasons),
            "review": dict(self.review),
        }


class LiveOrderExecutor:
    """Construct → review → place. No other module should call place_equity_order."""

    def __init__(
        self,
        store: ExecutionStore,
        broker: BrokerClient | None,
        *,
        root,
        runtime_mode: RuntimeMode | str = RuntimeMode.LIVE,
        context_fn: Callable[[], PortfolioContext | None] | None = None,
        regular_hours_fn: Callable[[], bool] | None = None,
        notify: NotificationEngine | None = None,
        now_fn: Callable[[], datetime] | None = None,
        account_number: str | None = None,
        thesis_status_fn: Callable[[str], str | None] | None = None,
        refresh_fn: Callable[[], Any] | None = None,
    ) -> None:
        self.store = store
        self.broker = broker
        self.root = root
        self.runtime_mode = runtime_mode if isinstance(runtime_mode, RuntimeMode) else RuntimeMode(str(runtime_mode).upper())
        self.context_fn = context_fn
        self.regular_hours_fn = regular_hours_fn or (lambda: False)
        self.notify = notify
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.account_number = account_number or str(load_account_rules()["account"]["account_number"])
        self.thesis_status_fn = thesis_status_fn
        self.refresh_fn = refresh_fn
        self.cfg = load_live_execution_config()

    def now(self) -> datetime:
        stamp = self._now()
        if stamp.tzinfo is None:
            return stamp.replace(tzinfo=timezone.utc)
        return stamp

    def execute_approved(self, approval: LiveApproval) -> ExecutionOutcome:
        """Idempotent: one intent, at most one broker place per approval."""
        existing = self.store.intent_for_approval(approval.approval_id)
        if existing is not None and (
            existing.broker_order_id
            or existing.status
            in {
                ExecutionIntentStatus.SUBMITTED,
                ExecutionIntentStatus.OPEN,
                ExecutionIntentStatus.PARTIALLY_FILLED,
                ExecutionIntentStatus.FILLED,
            }
        ):
            return ExecutionOutcome(intent=existing, placed=False, reasons=["already_submitted"])
        if existing is not None and existing.status is ExecutionIntentStatus.UNKNOWN_RECONCILIATION_REQUIRED:
            return ExecutionOutcome(intent=existing, placed=False, reasons=["ambiguous_reconcile_required"])
        if existing is not None and existing.status is ExecutionIntentStatus.REJECTED:
            return ExecutionOutcome(intent=existing, placed=False, reasons=["already_rejected"])
        if approval.status not in APPROVED_OK:
            existing = self.store.intent_for_approval(approval.approval_id)
            if existing is not None:
                return ExecutionOutcome(intent=existing, placed=False, reasons=["approval_not_approved"])
            stamp = self.now().isoformat()
            dummy = ExecutionIntent(
                intent_id=self.store.intent_id_for(approval.approval_id),
                approval_id=approval.approval_id,
                proposal_id=approval.approval_id,
                thesis_id=approval.thesis_id,
                symbol=approval.ticker.upper(),
                side="buy",
                action=str(approval.proposed_action).upper(),
                status=ExecutionIntentStatus.BLOCKED,
                block_reasons=["approval_not_approved"],
                created_at=stamp,
                updated_at=stamp,
                runtime_mode=self.runtime_mode.value,
                LIVE_ORDER_PLACEMENT=live_placement_enabled(),
            )
            return ExecutionOutcome(intent=dummy, placed=False, reasons=["approval_not_approved"])
        intent = self._get_or_create_intent(approval)
        if intent.broker_order_id or intent.status in {
            ExecutionIntentStatus.SUBMITTED,
            ExecutionIntentStatus.OPEN,
            ExecutionIntentStatus.PARTIALLY_FILLED,
            ExecutionIntentStatus.FILLED,
        }:
            return ExecutionOutcome(intent=intent, placed=False, reasons=["already_submitted"])
        if intent.status is ExecutionIntentStatus.UNKNOWN_RECONCILIATION_REQUIRED:
            return ExecutionOutcome(intent=intent, placed=False, reasons=["ambiguous_reconcile_required"])
        if intent.status is ExecutionIntentStatus.REJECTED:
            return ExecutionOutcome(intent=intent, placed=False, reasons=["already_rejected"])
        lock = self.store.lock_for(intent.intent_id, stale_seconds=int((self.cfg.get("idempotency") or {}).get("lock_stale_seconds") or 120))
        if not lock.acquire():
            existing = self.store.get_intent(intent.intent_id) or intent
            return ExecutionOutcome(intent=existing, placed=False, reasons=["lock_held"])
        try:
            return self._run_locked(approval, intent)
        finally:
            lock.release()

    def _get_or_create_intent(self, approval: LiveApproval) -> ExecutionIntent:
        existing = self.store.intent_for_approval(approval.approval_id)
        if existing is not None:
            return existing
        stamp = self.now().isoformat()
        side = "sell" if str(approval.proposed_action).upper() in {"SELL", "REDUCE"} else "buy"
        intent = ExecutionIntent(
            intent_id=self.store.intent_id_for(approval.approval_id),
            approval_id=approval.approval_id,
            proposal_id=approval.approval_id,
            thesis_id=approval.thesis_id,
            symbol=approval.ticker.upper(),
            side=side,
            action=str(approval.proposed_action).upper(),
            notional=approval.proposed_dollar_amount,
            allocation_pct=approval.proposed_allocation_pct,
            order_type=str(self.cfg.get("default_order_type") or "market"),
            time_in_force=str(self.cfg.get("default_time_in_force") or "gfd"),
            status=ExecutionIntentStatus.CREATED,
            ref_id=self.store.intent_id_for(approval.approval_id),
            created_at=stamp,
            updated_at=stamp,
            nav_at_approval=approval.nav_at_proposal,
            quote_at_approval=approval.quote_at_proposal or approval.current_quote,
            runtime_mode=self.runtime_mode.value,
            LIVE_ORDER_PLACEMENT=live_placement_enabled(),
        )
        return self.store.save_intent(intent)

    def _run_locked(self, approval: LiveApproval, intent: ExecutionIntent) -> ExecutionOutcome:
        stamp = self.now()
        placement = live_placement_enabled()
        intent.LIVE_ORDER_PLACEMENT = placement
        if not placement:
            intent.status = ExecutionIntentStatus.BLOCKED_DISABLED
            intent.block_reasons = ["LIVE_ORDER_PLACEMENT_false"]
            intent.updated_at = stamp.isoformat()
            self.store.save_intent(intent)
            approval.status = LiveApprovalStatus.APPROVED_EXECUTION_DISABLED
            approval.live_execution_blocked = True
            approval.approved_does_not_place_order = True
            record_audit(
                "ORDER_SUBMISSION_SKIPPED",
                root=self.root,
                now=stamp,
                approval_id=approval.approval_id,
                intent_id=intent.intent_id,
                reason="LIVE_ORDER_PLACEMENT_false",
            )
            return ExecutionOutcome(intent=intent, placed=False, reasons=["LIVE_ORDER_PLACEMENT_false"])

        if bool(self.cfg.get("auto_execution")):
            return self._block(intent, approval, ["auto_execution_true"], LiveApprovalStatus.EXECUTION_FAILED)

        if self.runtime_mode is not RuntimeMode.LIVE:
            intent.status = ExecutionIntentStatus.BLOCKED
            intent.block_reasons = ["runtime_not_live"]
            self.store.save_intent(intent)
            return ExecutionOutcome(intent=intent, placed=False, reasons=["runtime_not_live"])
        if self.broker is None:
            intent.status = ExecutionIntentStatus.BLOCKED
            intent.block_reasons = ["no_broker"]
            self.store.save_intent(intent)
            return ExecutionOutcome(intent=intent, placed=False, reasons=["no_broker"])

        if self.refresh_fn is not None:
            try:
                self.refresh_fn()
            except Exception as exc:  # noqa: BLE001
                return self._block(intent, approval, [f"refresh_failed:{type(exc).__name__}"], LiveApprovalStatus.REVALIDATION_REQUIRED)

        context = self.context_fn() if self.context_fn else None
        if context is None:
            return self._block(intent, approval, ["missing_live_context"], LiveApprovalStatus.REVALIDATION_REQUIRED)

        quote, tradable = self._quote_and_tradability(intent.symbol)
        hours = bool(self.regular_hours_fn())
        thesis = self.thesis_status_fn(intent.symbol) if self.thesis_status_fn else None
        intent.status = ExecutionIntentStatus.REVALIDATING
        self.store.save_intent(intent)
        record_audit("SEND_TIME_REVALIDATION", root=self.root, now=stamp, approval_id=approval.approval_id, intent_id=intent.intent_id, quote=quote, nav=context.current_nav)
        codes = revalidate_for_send(
            approval,
            context=context,
            quote=quote,
            tradable=tradable,
            regular_hours_open=hours,
            thesis_status=thesis,
            now=stamp,
            config=self.cfg,
            connected=True,
        )
        if codes:
            return self._block(intent, approval, codes, LiveApprovalStatus.REVALIDATION_REQUIRED)

        payload = self._build_payload(approval, context, quote)
        if payload is None:
            return self._block(intent, approval, ["order_too_small_or_unsized"], LiveApprovalStatus.EXECUTION_FAILED)

        intent.status = ExecutionIntentStatus.REVIEWING
        self.store.save_intent(intent)
        try:
            raw_review = self.broker.review_equity_order(payload)
        except Exception as exc:  # noqa: BLE001
            return self._block(intent, approval, [f"review_failed:{type(exc).__name__}:{exc}"], LiveApprovalStatus.EXECUTION_FAILED)
        parsed = parse_review_response(raw_review)
        nested = raw_review.get("data", raw_review) if isinstance(raw_review, dict) else {}
        if parsed.get("errors") or nested.get("ok") is False:
            record_audit("ORDER_REVIEW_REJECTED", root=self.root, now=stamp, approval_id=approval.approval_id, review=parsed)
            return self._block(intent, approval, ["broker_review_rejected"] + list(parsed.get("errors") or []), LiveApprovalStatus.EXECUTION_FAILED)
        record_audit("ORDER_REVIEW_ACCEPTED", root=self.root, now=stamp, approval_id=approval.approval_id, intent_id=intent.intent_id)

        order = BrokerOrderRecord(
            order_id=str(uuid4()),
            intent_id=intent.intent_id,
            approval_id=approval.approval_id,
            thesis_id=approval.thesis_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=_f(payload.get("quantity")),
            notional=_f(payload.get("dollar_amount")) or approval.proposed_dollar_amount,
            order_type=payload.get("type") or "market",
            status=BrokerOrderStatus.PENDING_SUBMISSION,
            submitted_at=None,
            updated_at=stamp.isoformat(),
            ref_id=intent.ref_id,
            raw_broker={"review": raw_review, "payload": payload},
        )
        self.store.save_order(order)
        intent.status = ExecutionIntentStatus.PENDING_SUBMISSION
        intent.place_attempted = True
        intent.updated_at = stamp.isoformat()
        self.store.save_intent(intent)
        record_audit("ORDER_SUBMISSION_ATTEMPT", root=self.root, now=stamp, approval_id=approval.approval_id, intent_id=intent.intent_id, payload=payload)

        try:
            raw_place = self.broker.place_equity_order({**payload, "ref_id": intent.ref_id})
        except TimeoutError:
            intent.status = ExecutionIntentStatus.UNKNOWN_RECONCILIATION_REQUIRED
            order.status = BrokerOrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
            intent.updated_at = self.now().isoformat()
            self.store.save_intent(intent)
            self.store.save_order(order)
            record_audit("ORDER_SUBMISSION_AMBIGUOUS", root=self.root, now=self.now(), approval_id=approval.approval_id, intent_id=intent.intent_id)
            if self.notify:
                self.notify.emit(
                    NotificationKind.SERVICE_ERROR,
                    title=f"Order submission ambiguous — {intent.symbol}",
                    body="Broker acknowledgement was not confirmed. Reconcile before retrying.",
                    payload={"intent_id": intent.intent_id, "approval_id": approval.approval_id},
                )
            return ExecutionOutcome(intent=intent, order=order, placed=False, reasons=["ambiguous_submission"], review=parsed)
        except Exception as exc:  # noqa: BLE001
            return self._block(intent, approval, [f"place_failed:{type(exc).__name__}:{exc}"], LiveApprovalStatus.EXECUTION_FAILED)

        broker_id, broker_state, reject = _parse_place(raw_place)
        order.raw_broker["place"] = raw_place
        order.broker_order_id = broker_id
        order.broker_status = broker_state
        order.submitted_at = self.now().isoformat()
        order.updated_at = order.submitted_at
        intent.broker_order_id = broker_id
        intent.submitted_at = order.submitted_at
        intent.updated_at = order.submitted_at
        if reject or str(broker_state or "").lower() in {"rejected", "failed"}:
            order.status = BrokerOrderStatus.REJECTED
            order.rejection_reason = reject or "broker_rejected"
            intent.status = ExecutionIntentStatus.REJECTED
            approval.status = LiveApprovalStatus.EXECUTION_FAILED
            approval.execution_attempted = True
            self.store.save_order(order)
            self.store.save_intent(intent)
            record_audit("ORDER_REJECTED", root=self.root, now=self.now(), approval_id=approval.approval_id, reason=order.rejection_reason)
            if self.notify:
                self.notify.emit(
                    NotificationKind.ORDER_REJECTED,
                    title=f"Broker rejected {intent.symbol}",
                    body=order.rejection_reason or "rejected",
                    payload={
                        "approval_id": approval.approval_id,
                        "broker_order_id": broker_id,
                        "ticker": intent.symbol,
                        "action": intent.action,
                    },
                )
            return ExecutionOutcome(intent=intent, order=order, placed=False, reasons=["broker_rejected"], review=parsed)

        order.status = _order_status(broker_state)
        intent.status = _intent_status(order.status)
        approval.status = LiveApprovalStatus.APPROVED
        approval.broker_submitted = True
        approval.placed_order = True
        approval.execution_attempted = True
        approval.live_execution_blocked = False
        approval.approved_does_not_place_order = False
        self.store.save_order(order)
        self.store.save_intent(intent)
        record_audit(
            "ORDER_SUBMITTED",
            root=self.root,
            now=self.now(),
            approval_id=approval.approval_id,
            intent_id=intent.intent_id,
            broker_order_id=broker_id,
            broker_status=broker_state,
        )
        if order.status is BrokerOrderStatus.FILLED:
            record_audit("ORDER_FILLED", root=self.root, now=self.now(), approval_id=approval.approval_id, broker_order_id=broker_id)
            approval.status = LiveApprovalStatus.EXECUTED
            try:
                from agentic_portfolio.live_execution.positions import upsert_from_fill

                upsert_from_fill(
                    self.root,
                    symbol=intent.symbol,
                    store=self.store,
                    sleeve=approval.sleeve,
                    rationale=approval.reason or approval.supporting_thesis,
                    invalidation=list(approval.invalidation or []),
                    mode=self.runtime_mode,
                )
            except Exception:  # noqa: BLE001
                pass
            if self.notify:
                self.notify.emit(
                    NotificationKind.ORDER_FILLED,
                    title=f"ORDER FILLED — {intent.symbol}",
                    body=f"{intent.symbol} filled.",
                    payload={
                        "broker_order_id": broker_id,
                        "approval_id": approval.approval_id,
                        "ticker": intent.symbol,
                        "action": intent.action,
                    },
                )
        elif self.notify:
            self.notify.emit(
                NotificationKind.ORDER_SUBMITTED,
                title=f"ORDER SUBMITTED — {intent.symbol}",
                body=f"{intent.symbol} submitted to broker ({order.status.value}).",
                payload={
                    "broker_order_id": broker_id,
                    "approval_id": approval.approval_id,
                    "ticker": intent.symbol,
                    "action": intent.action,
                },
            )
        return ExecutionOutcome(intent=intent, order=order, placed=True, review=parsed)

    def _block(
        self,
        intent: ExecutionIntent,
        approval: LiveApproval,
        reasons: list[str],
        approval_status: LiveApprovalStatus,
    ) -> ExecutionOutcome:
        intent.block_reasons = list(reasons)
        intent.status = (
            ExecutionIntentStatus.REVALIDATION_REQUIRED
            if approval_status is LiveApprovalStatus.REVALIDATION_REQUIRED
            else ExecutionIntentStatus.BLOCKED
        )
        intent.updated_at = self.now().isoformat()
        self.store.save_intent(intent)
        approval.status = approval_status
        approval.execution_attempted = True
        approval.placed_order = False
        record_audit(
            "EXECUTION_BLOCKED",
            root=self.root,
            now=self.now(),
            approval_id=approval.approval_id,
            intent_id=intent.intent_id,
            reasons=reasons,
        )
        return ExecutionOutcome(intent=intent, placed=False, reasons=reasons)

    def _quote_and_tradability(self, symbol: str) -> tuple[float | None, bool | None]:
        if self.broker is None:
            return None, None
        quote = None
        tradable = None
        try:
            payload = self.broker.get_equity_quotes([symbol])
            results = (payload.get("data") or {}).get("results") or []
            for row in results:
                q = row.get("quote") if isinstance(row.get("quote"), dict) else row
                if str((q or {}).get("symbol") or "").upper() == symbol.upper():
                    quote = _f((q or {}).get("last_trade_price"))
                    break
        except Exception:  # noqa: BLE001
            quote = None
        try:
            trad = self.broker.get_equity_tradability([symbol])
            rows = (trad.get("data") or {}).get("results") or []
            for row in rows:
                if str(row.get("symbol") or "").upper() == symbol.upper():
                    tradable = bool(row.get("tradeable"))
        except Exception:  # noqa: BLE001
            tradable = None
        return quote, tradable

    def _build_payload(self, approval: LiveApproval, context: PortfolioContext, quote: float | None) -> dict[str, Any] | None:
        cfg = self.cfg
        decimals = int(cfg.get("quantity_decimal_places") or 6)
        side = "sell" if str(approval.proposed_action).upper() in {"SELL", "REDUCE"} else "buy"
        order_type = str(cfg.get("default_order_type") or "market")
        payload: dict[str, Any] = {
            "account_number": self.account_number,
            "symbol": approval.ticker.upper(),
            "side": side,
            "type": order_type,
            "time_in_force": str(cfg.get("default_time_in_force") or "gfd"),
            "market_hours": str(cfg.get("market_hours") or "regular_hours"),
        }
        nav = float(context.current_nav or 0)
        if side == "buy":
            notional = _f(approval.proposed_dollar_amount) or 0.0
            pct = _f(approval.proposed_allocation_pct)
            if pct and nav:
                from_pct = nav * (pct / 100.0)
                notional = min(notional, from_pct) if notional else from_pct
            notional = min(notional, float(context.cash or 0), float(context.buying_power or 0))
            if notional < float(cfg.get("min_order_notional_usd") or 1.0):
                return None
            if cfg.get("prefer_dollar_amount_for_buy", True) and order_type == "market":
                payload["dollar_amount"] = f"{notional:.2f}"
            elif quote:
                qty = notional / float(quote)
                payload["quantity"] = quantity_str(qty, decimals=decimals)
            else:
                return None
        else:
            held = 0.0
            for pos in context.positions or []:
                if str(getattr(pos, "symbol", "")).upper() == approval.ticker.upper():
                    held = float(getattr(pos, "quantity", None) or 0)
                    if not held:
                        mv = float(getattr(pos, "market_value", None) or 0)
                        if quote and mv:
                            held = mv / float(quote)
            if held <= 0:
                return None
            payload["quantity"] = quantity_str(held, decimals=decimals)
        return payload


def _parse_place(raw: Any) -> tuple[str | None, str | None, str | None]:
    data = raw if isinstance(raw, dict) else {}
    nested = data.get("data") if isinstance(data.get("data"), dict) else data
    orders = nested.get("orders") if isinstance(nested.get("orders"), list) else []
    first = orders[0] if orders and isinstance(orders[0], dict) else {}
    broker_id = nested.get("order_id") or first.get("id") or nested.get("id")
    state = nested.get("state") or first.get("state") or nested.get("status")
    reject = None
    if nested.get("ok") is False or str(state or "").lower() in {"rejected", "failed"}:
        reject = nested.get("reject_reason") or first.get("reject_reason") or "broker_rejected"
    return (str(broker_id) if broker_id else None), (str(state) if state else None), reject


def _order_status(state: str | None) -> BrokerOrderStatus:
    text = str(state or "").lower()
    if text == "filled":
        return BrokerOrderStatus.FILLED
    if text in {"partially_filled"}:
        return BrokerOrderStatus.PARTIALLY_FILLED
    if text in {"cancelled", "canceled"}:
        return BrokerOrderStatus.CANCELED
    if text in {"rejected", "failed", "voided"}:
        return BrokerOrderStatus.REJECTED
    if text in {"new", "queued", "confirmed", "unconfirmed"}:
        return BrokerOrderStatus.OPEN
    return BrokerOrderStatus.SUBMITTED


def _intent_status(status: BrokerOrderStatus) -> ExecutionIntentStatus:
    return ExecutionIntentStatus(status.value)


def _f(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
