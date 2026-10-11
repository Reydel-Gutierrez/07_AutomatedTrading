"""Execution Desk — the only path from a manager's intent to an order.

Gates, in order (the first failure decides):
  1 firm      kill switch; for BUYs also reconcile hold and drawdown brake
  2 manager   mode matches the book; symbol in mandate universe; orders/day; minimum size
  3 money     BUY ≤ manager free cash (and LIVE: ≤ broker unleveraged cash); SELL ≤ manager shares
  4 market    regular hours inside the firm window; fresh quote; spread ≤ mandate max; tradable
Then: persist the order (idempotency ref) BEFORE any broker call, route, and record fills.

Deferrable outcomes (closed, outside window, stale quote, wide spread, broker
unreachable) create no order; the manager simply tries again next run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable
from uuid import uuid4

from firm.broker.errors import AmbiguousSubmission, BrokerAccountMismatch, BrokerUnavailable
from firm.broker.types import BrokerOrder, BrokerWriter, Quote
from firm.clock import Clock, et, in_window, iso, is_regular_hours, parse, parse_hhmm, utc_now
from firm.controls import Controls
from firm.db import Database
from firm.events import EventLog
from firm.ledger import Ledger
from firm.managers.base import OrderIntent
from firm.managers.registry import Registry
from firm.market import MarketData
from firm.money import QTY_TOLERANCE, ZERO, D, cents, s, shares_floor
from firm.settings import Settings

DEFERRABLE = frozenset({"MARKET_CLOSED", "OUTSIDE_WINDOW", "QUOTE_STALE", "QUOTE_UNAVAILABLE", "SPREAD_WIDE", "BROKER_UNAVAILABLE"})
OPEN_STATUSES = ("PENDING", "SUBMITTED", "UNKNOWN")
UNKNOWN_GIVE_UP = timedelta(hours=24)


@dataclass
class DeskResult:
    status: str  # FILLED | SUBMITTED | DEFERRED | REJECTED | BLOCKED | UNKNOWN
    reasons: list[str] = field(default_factory=list)
    order_id: int | None = None

    @property
    def deferred(self) -> bool:
        return self.status == "DEFERRED"


class Desk:
    def __init__(
        self,
        *,
        db: Database,
        ledger: Ledger,
        events: EventLog,
        settings: Settings,
        controls: Controls,
        registry: Registry,
        market: MarketData,
        writer: Callable[[], BrokerWriter | None],
        clock: Clock = utc_now,
    ) -> None:
        self.db = db
        self.ledger = ledger
        self.events = events
        self.settings = settings
        self.controls = controls
        self.registry = registry
        self.market = market
        self._writer = writer
        self.clock = clock

    # -- entry point -----------------------------------------------------------
    def submit(self, intent: OrderIntent) -> DeskResult:
        try:
            reasons, quote = self._gates(intent)
        except BrokerUnavailable as exc:
            reasons, quote = [f"BROKER_UNAVAILABLE: {exc}"], None
        if reasons:
            codes = [r.split(":")[0] for r in reasons]
            status = "DEFERRED" if all(c in DEFERRABLE for c in codes) else "REJECTED"
            self.events.emit(
                "ORDER_DEFERRED" if status == "DEFERRED" else "ORDER_BLOCKED",
                f"{intent.side.upper()} {intent.symbol} {'deferred' if status == 'DEFERRED' else 'blocked'}: {', '.join(codes)}",
                actor="desk",
                manager_id=intent.manager_id,
                symbol=intent.symbol,
                severity="info" if status == "DEFERRED" else "warning",
                payload={"reasons": reasons, "intent": _intent_dict(intent)},
            )
            return DeskResult(status, reasons)
        assert quote is not None
        order_id = self._persist(intent)
        if intent.book == "SIM":
            return self._fill_sim(order_id, intent, quote)
        return self._send_live(order_id, intent, quote)

    # -- gates ---------------------------------------------------------------
    def _gates(self, intent: OrderIntent) -> tuple[list[str], Quote | None]:
        reasons: list[str] = []
        limits = self.settings.get("firm_limits")
        now = self.clock()
        if intent.side not in ("buy", "sell"):
            return ["BAD_SIDE"], None
        # 1 firm
        if self.controls.kill_switch().engaged:
            return ["KILL_SWITCH"], None
        if intent.side == "buy" and intent.book == "LIVE":
            blocked = [r for r in self.controls.buys_blocked() if r != "KILL_SWITCH"]
            if blocked:
                return blocked, None
        # 2 manager
        manager = self.registry.get(intent.manager_id)
        if manager.book != intent.book:
            return [f"MODE_MISMATCH: manager is {manager.mode}, intent is {intent.book}"], None
        _, mandate = self.registry.mandate(intent.manager_id)
        universe = set((mandate.get("targets") or {}).keys()) | set(mandate.get("universe") or [])
        if intent.symbol not in universe:
            return [f"NOT_IN_MANDATE: {intent.symbol}"], None
        if self._orders_today(intent.manager_id, intent.book) >= int(mandate.get("max_orders_per_day") or 10):
            return ["MAX_ORDERS_PER_DAY"], None
        # 3 money
        if intent.side == "buy":
            if not intent.notional or intent.notional <= 0:
                return ["BAD_NOTIONAL"], None
            if intent.notional < D(mandate.get("min_trade_usd") or 1):
                return ["BELOW_MIN_TRADE"], None
            free = self.ledger.free_cash(intent.book, intent.manager_id)
            if intent.notional > free:
                return [f"INSUFFICIENT_MANAGER_CASH: free {cents(free)}"], None
        else:
            if not intent.quantity or intent.quantity <= 0:
                return ["BAD_QUANTITY"], None
            held = self.ledger.holdings(intent.book, intent.manager_id).get(intent.manager_id, {}).get(intent.symbol, ZERO)
            if intent.quantity > held + QTY_TOLERANCE:
                return [f"INSUFFICIENT_MANAGER_SHARES: holds {held}"], None
        # 4 market
        if limits.get("regular_hours_only", True) and not is_regular_hours(now):
            reasons.append("MARKET_CLOSED")
            return reasons, None
        if not in_window(now, parse_hhmm(limits["window_start_et"]), parse_hhmm(limits["window_end_et"])):
            return ["OUTSIDE_WINDOW"], None
        quote = self.market.quotes([intent.symbol]).get(intent.symbol)
        if quote is None or quote.ask is None or quote.bid is None:
            return ["QUOTE_UNAVAILABLE"], None
        if quote.tradable_state != "active":
            return [f"NOT_TRADABLE: {quote.tradable_state}"], None
        age = self.market.quote_age_seconds(quote)
        if age is None or age > float(limits.get("max_quote_age_seconds") or 15):
            return [f"QUOTE_STALE: {age}"], None
        spread = quote.spread_bps
        if spread is None or spread > D(mandate.get("max_spread_bps") or 50):
            return [f"SPREAD_WIDE: {spread and round(float(spread), 1)} bps"], None
        if intent.side == "buy" and intent.notional and intent.notional / quote.ask < D("0.000001"):
            return ["BELOW_MIN_TRADE"], None
        return [], quote

    def _orders_today(self, manager_id: str, book: str) -> int:
        start = et(self.clock()).replace(hour=0, minute=0, second=0, microsecond=0)
        row = self.db.one(
            "SELECT COUNT(*) AS n FROM orders WHERE manager_id = ? AND book = ? AND created_at >= ? AND status NOT IN ('BLOCKED','REJECTED')",
            (manager_id, book, iso(start)),
        )
        return int(row["n"])

    # -- persistence -----------------------------------------------------------
    def _persist(self, intent: OrderIntent) -> int:
        now = iso(self.clock())
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO orders (client_ref, book, manager_id, symbol, side, notional, quantity, status, reason, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    intent.book,
                    intent.manager_id,
                    intent.symbol,
                    intent.side,
                    s(cents(intent.notional)) if intent.notional is not None else None,
                    s(intent.quantity) if intent.quantity is not None else None,
                    "PENDING",
                    intent.reason,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def _update(self, order_id: int, **fields: Any) -> None:
        fields["updated_at"] = iso(self.clock())
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.db.tx() as conn:
            conn.execute(f"UPDATE orders SET {cols} WHERE id = ?", (*fields.values(), order_id))

    def order(self, order_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM orders WHERE id = ?", (order_id,))
        return dict(row) if row else {}

    # -- SIM route -------------------------------------------------------------
    def _fill_sim(self, order_id: int, intent: OrderIntent, quote: Quote) -> DeskResult:
        price = quote.ask if intent.side == "buy" else quote.bid
        assert price is not None
        qty = shares_floor(intent.notional / price) if intent.side == "buy" else shares_floor(intent.quantity)
        if qty <= 0:
            self._update(order_id, status="REJECTED", block_reasons=json.dumps(["ZERO_QUANTITY"]), terminal_at=iso(self.clock()))
            return DeskResult("REJECTED", ["ZERO_QUANTITY"], order_id)
        ts = iso(self.clock())
        self.ledger.record_fill(
            book="SIM", manager=intent.manager_id, order_id=order_id, execution_id=f"sim-{order_id}",
            side=intent.side, symbol=intent.symbol, quantity=qty, price=price, ts=ts,
        )
        self._update(order_id, status="FILLED", filled_quantity=s(qty), average_price=s(price), submitted_at=ts, terminal_at=ts, broker_state="filled")
        self.events.emit(
            "ORDER_FILLED",
            f"[SIM] {intent.side.upper()} {qty} {intent.symbol} @ ${price} (${cents(qty * price)})",
            actor="desk",
            manager_id=intent.manager_id,
            symbol=intent.symbol,
            severity="info",
            payload={"book": "SIM", "order_id": order_id, "quantity": str(qty), "price": str(price), "reason": intent.reason},
        )
        return DeskResult("FILLED", [], order_id)

    # -- LIVE route ------------------------------------------------------------
    def _block(self, order_id: int, intent: OrderIntent, reasons: list[str], *, status: str = "BLOCKED", severity: str = "warning") -> DeskResult:
        self._update(order_id, status=status, block_reasons=json.dumps(reasons), terminal_at=iso(self.clock()))
        self.events.emit(
            "ORDER_REJECTED" if status == "REJECTED" else "ORDER_BLOCKED",
            f"{intent.side.upper()} {intent.symbol} {status.lower()}: {'; '.join(reasons)}",
            actor="desk",
            manager_id=intent.manager_id,
            symbol=intent.symbol,
            severity=severity,
            payload={"order_id": order_id, "reasons": reasons},
        )
        return DeskResult(status, reasons, order_id)

    def _send_live(self, order_id: int, intent: OrderIntent, quote: Quote) -> DeskResult:
        try:
            writer = self._writer()
        except BrokerUnavailable as exc:
            return self._block(order_id, intent, [f"BROKER_UNAVAILABLE: {exc}"])
        if writer is None:
            return self._block(order_id, intent, ["BROKER_WRITES_DISABLED (AGENTIC_LIVE_ORDER_PLACEMENT is off)"])
        try:
            writer.confirm_account()  # type: ignore[attr-defined]
        except BrokerAccountMismatch as exc:
            self.controls.engage(f"account check failed: {exc}", by="desk")
            return self._block(order_id, intent, [f"ACCOUNT_MISMATCH: {exc}"], severity="critical")
        try:
            if writer.trade_approval_required():
                self.events.emit(
                    "BROKER_APPROVAL_REQUIRED",
                    "Robinhood is set to require your approval for agent trades. Orders are paused until you turn it off in the app.",
                    actor="desk",
                    severity="critical",
                )
                return self._block(order_id, intent, ["ROBINHOOD_APPROVAL_REQUIRED"])
            portfolio = writer.portfolio()
            if intent.side == "buy":
                spendable = portfolio.unleveraged_buying_power if portfolio.unleveraged_buying_power is not None else portfolio.cash
                spendable = min(spendable, portfolio.cash)  # cash only, never margin
                if intent.notional > spendable:
                    return self._block(order_id, intent, [f"BROKER_CASH_SHORT: broker cash {spendable}"])
            else:
                broker_qty = writer.positions().get(intent.symbol, ZERO)
                if intent.quantity > broker_qty + QTY_TOLERANCE:
                    return self._block(order_id, intent, [f"BROKER_SHARES_SHORT: broker holds {broker_qty}"])
            tradability = writer.tradability([intent.symbol]).get(intent.symbol)
            if tradability is None or not tradability.tradable or tradability.halted:
                return self._block(order_id, intent, ["NOT_TRADABLE"])
        except BrokerUnavailable as exc:
            return self._block(order_id, intent, [f"BROKER_UNAVAILABLE: {exc}"])

        order_row = self.order(order_id)
        payload: dict[str, Any] = {
            "account_number": writer.account_number,
            "symbol": intent.symbol,
            "side": intent.side,
            "type": "market",
            "time_in_force": "gfd",
            "market_hours": "regular_hours",
        }
        if intent.side == "buy":
            if tradability.fractional:
                payload["dollar_amount"] = f"{cents(intent.notional):.2f}"
            else:
                whole = (intent.notional / quote.ask).to_integral_value(rounding="ROUND_DOWN")
                if whole < 1:
                    return self._block(order_id, intent, ["WHOLE_SHARES_ONLY: notional below one share"])
                payload["quantity"] = str(whole)
        else:
            qty = shares_floor(intent.quantity)
            if not tradability.fractional:
                qty = qty.to_integral_value(rounding="ROUND_DOWN")
            if qty <= 0:
                return self._block(order_id, intent, ["ZERO_QUANTITY"])
            payload["quantity"] = format(qty, "f")

        try:
            review = writer.review(payload)
        except BrokerUnavailable as exc:
            return self._block(order_id, intent, [f"REVIEW_FAILED: {exc}"])
        checks = review.get("order_checks")
        if checks:
            return self._block(order_id, intent, [f"BROKER_REVIEW_ALERT: {json.dumps(checks, default=str)[:400]}"], status="REJECTED")
        if review.get("customer_approval_required"):
            return self._block(order_id, intent, ["ROBINHOOD_APPROVAL_REQUIRED"])

        self._update(order_id, status="SUBMITTED", submitted_at=iso(self.clock()))
        try:
            placed = writer.place({**payload, "ref_id": order_row["client_ref"]})
        except AmbiguousSubmission as exc:
            self._update(order_id, status="UNKNOWN", block_reasons=json.dumps([str(exc)]))
            self.controls.engage(f"ambiguous submission for {intent.symbol} (order {order_id}); reconcile before trading", by="desk")
            self.events.emit(
                "ORDER_UNKNOWN",
                f"{intent.side.upper()} {intent.symbol}: broker response was ambiguous. Trading stopped until it is reconciled.",
                actor="desk",
                manager_id=intent.manager_id,
                symbol=intent.symbol,
                severity="critical",
                payload={"order_id": order_id, "error": str(exc), "ref_id": order_row["client_ref"]},
            )
            return DeskResult("UNKNOWN", [str(exc)], order_id)
        except BrokerUnavailable as exc:
            return self._block(order_id, intent, [f"BROKER_REJECTED: {exc}"], status="REJECTED")
        self._update(order_id, broker_order_id=placed.id, broker_state=placed.state)
        self.events.emit(
            "ORDER_SUBMITTED",
            f"{intent.side.upper()} {intent.symbol} sent to Robinhood ({payload.get('dollar_amount') and '$' + payload['dollar_amount'] or payload.get('quantity') + ' sh'})",
            actor="desk",
            manager_id=intent.manager_id,
            symbol=intent.symbol,
            payload={"order_id": order_id, "broker_order_id": placed.id, "reason": intent.reason},
        )
        return self.apply_broker_order(order_id, placed)

    # -- fills -----------------------------------------------------------------
    def apply_broker_order(self, order_id: int, remote: BrokerOrder) -> DeskResult:
        """Post new executions to the ledger and advance the order's status. Idempotent."""
        row = self.order(order_id)
        for ex in remote.executions:
            self.ledger.record_fill(
                book="LIVE", manager=row["manager_id"], order_id=order_id, execution_id=ex.id,
                side=row["side"], symbol=row["symbol"], quantity=ex.quantity, price=ex.price, fees=ex.fees, ts=ex.timestamp or None,
            )
        fields: dict[str, Any] = {
            "broker_order_id": remote.id or row["broker_order_id"],
            "broker_state": remote.state,
            "filled_quantity": s(remote.cumulative_quantity),
            "average_price": s(remote.average_price) if remote.average_price is not None else None,
            "fees": s(remote.fees),
        }
        status = row["status"]
        if remote.state == "filled":
            status = "FILLED"
        elif remote.state in {"cancelled", "canceled", "partially_filled_rest_cancelled"}:
            status = "CANCELED"
        elif remote.state in {"rejected", "failed", "voided"}:
            status = "REJECTED"
        elif remote.is_open:
            status = "SUBMITTED"
        if status != row["status"] or row["broker_state"] != remote.state:
            if status in {"FILLED", "CANCELED", "REJECTED"}:
                fields["terminal_at"] = iso(self.clock())
            self._update(order_id, status=status, **fields)
            if status == "FILLED":
                self.events.emit(
                    "ORDER_FILLED",
                    f"{row['side'].upper()} {remote.cumulative_quantity} {row['symbol']} @ ${remote.average_price} filled",
                    actor="desk", manager_id=row["manager_id"], symbol=row["symbol"],
                    payload={"book": "LIVE", "order_id": order_id, "broker_order_id": remote.id, "fees": str(remote.fees)},
                )
            elif status in {"CANCELED", "REJECTED"}:
                self.events.emit(
                    "ORDER_REJECTED",
                    f"{row['side'].upper()} {row['symbol']} {remote.state}" + (f": {remote.reject_reason}" if remote.reject_reason else ""),
                    actor="desk", manager_id=row["manager_id"], symbol=row["symbol"], severity="warning",
                    payload={"order_id": order_id, "filled_quantity": str(remote.cumulative_quantity)},
                )
        else:
            self._update(order_id, **fields)
        return DeskResult(status, [], order_id)

    def open_live_orders(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM orders WHERE book = 'LIVE' AND status IN ('SUBMITTED','UNKNOWN','PENDING') ORDER BY id")]

    def poll(self, reader) -> int:
        """Advance every open LIVE order from the broker. Returns orders updated."""
        updated = 0
        for row in self.open_live_orders():
            try:
                if row["broker_order_id"]:
                    found = reader.orders(order_id=row["broker_order_id"])
                    remote = next((o for o in found if o.id == row["broker_order_id"]), None)
                else:
                    since = iso(parse(row["created_at"]) - timedelta(minutes=5))
                    remote = next((o for o in reader.orders(symbol=row["symbol"], since=since) if o.ref_id == row["client_ref"]), None)
            except BrokerUnavailable:
                continue
            if remote is None:
                if row["status"] == "UNKNOWN" and self.clock() - parse(row["created_at"]) > UNKNOWN_GIVE_UP:
                    self._update(row["id"], status="NOT_FOUND", terminal_at=iso(self.clock()))
                    self.events.emit(
                        "ORDER_NOT_FOUND",
                        f"Order {row['id']} ({row['symbol']}) was never found at Robinhood after 24h; marked not placed. Review before releasing the kill switch.",
                        actor="desk", manager_id=row["manager_id"], symbol=row["symbol"], severity="warning",
                    )
                    updated += 1
                elif row["status"] == "PENDING" and self.clock() - parse(row["updated_at"]) > timedelta(minutes=10):
                    # Persisted but the process died before sending: nothing reached the broker.
                    self._update(row["id"], status="ABANDONED", terminal_at=iso(self.clock()))
                    updated += 1
                elif row["status"] == "SUBMITTED" and not row["broker_order_id"] and self.clock() - parse(row["updated_at"]) > timedelta(minutes=10):
                    # Marked as sent but no broker id and not visible under its ref: the process
                    # died mid-submission. It may still appear, so treat it as ambiguous.
                    self._update(row["id"], status="UNKNOWN")
                    self.controls.engage(f"order {row['id']} ({row['symbol']}) has no broker record after submission", by="desk")
                    updated += 1
                continue
            if row["status"] == "UNKNOWN":
                self.events.emit(
                    "ORDER_RECOVERED",
                    f"Ambiguous order {row['id']} found at Robinhood ({remote.state}). Books updated; the kill switch stays on until you release it.",
                    actor="desk", manager_id=row["manager_id"], symbol=row["symbol"], severity="warning",
                )
                self._update(row["id"], status="SUBMITTED")
            self.apply_broker_order(row["id"], remote)
            updated += 1
        return updated


def _intent_dict(intent: OrderIntent) -> dict[str, Any]:
    return {
        "manager_id": intent.manager_id,
        "book": intent.book,
        "symbol": intent.symbol,
        "side": intent.side,
        "notional": str(intent.notional) if intent.notional is not None else None,
        "quantity": str(intent.quantity) if intent.quantity is not None else None,
        "reason": intent.reason,
    }
