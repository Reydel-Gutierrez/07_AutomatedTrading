"""Broker ⇄ ledger reconciliation for the LIVE book.

Invariants (checked when no LIVE order is in flight):
  broker position[s] == Σ ledger shares[s] over every bucket (managers + suspense)
  broker cash        == Σ ledger cash over every bucket

Robinhood exposes no transfer or dividend feed, so an unexplained cash
difference is classified:
  deposit     pending_deposits rose by the same amount, or ≥ $1 and not dividend-shaped
  dividend    small relative to holdings (≤ firm_limits.dividend_max_pct_of_position)
  withdrawal  cash fell with no fills → from unallocated (shortfall → suspense)
  rounding    |diff| ≤ firm_limits.rounding_trueup_usd → booked to unallocated
Anything that cannot be explained lands in suspense, which holds BUYs until
the owner classifies it on HQ. Orders not placed by the firm (manual trades,
DRIP, recurring buys, any other tool) also hold BUYs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from firm.broker.errors import BrokerAccountMismatch, BrokerUnavailable
from firm.broker.types import BrokerReader
from firm.clock import Clock, iso, utc_now
from firm.controls import Controls
from firm.db import Database
from firm.desk import Desk
from firm.events import EventLog
from firm.ledger import FIRM, SUSPENSE, Entry, Ledger, LedgerError
from firm.money import CASH_TOLERANCE, QTY_TOLERANCE, ZERO, D, cents, s
from firm.settings import Settings


@dataclass
class ReconcileResult:
    status: str  # OK | HOLD | IN_FLIGHT | UNAVAILABLE | BOOTSTRAPPED
    reasons: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    diff: dict[str, Any] = field(default_factory=dict)


class Reconciler:
    def __init__(
        self,
        *,
        db: Database,
        ledger: Ledger,
        events: EventLog,
        settings: Settings,
        controls: Controls,
        desk: Desk,
        clock: Clock = utc_now,
    ) -> None:
        self.db = db
        self.ledger = ledger
        self.events = events
        self.settings = settings
        self.controls = controls
        self.desk = desk
        self.clock = clock

    # -- bootstrap -------------------------------------------------------------
    def bootstrapped(self) -> bool:
        return self.db.one("SELECT 1 FROM ledger WHERE kind = 'OPENING_BALANCE' AND book = 'LIVE' LIMIT 1") is not None

    def bootstrap(self, reader: BrokerReader) -> ReconcileResult:
        """Open the books from a fresh broker snapshot. Positions found at the broker go to suspense."""
        if self.bootstrapped():
            return ReconcileResult("OK", actions=["already bootstrapped"])
        reader.confirm_account()  # type: ignore[attr-defined]
        open_orders = [o for o in reader.orders(since=None) if o.is_open]
        if open_orders:
            raise LedgerError(f"cannot open the books with {len(open_orders)} open broker orders")
        facts = reader.portfolio()
        positions = reader.positions()
        self.ledger.opening_balance(facts.cash, by="reconciler", memo=f"broker cash at opening; NAV {facts.nav}")
        for symbol, qty in positions.items():
            with self.db.tx() as conn:
                self.ledger._post(conn, book="LIVE", kind="SUSPENSE_IN", entries=[Entry(SUSPENSE, ZERO, symbol=symbol, quantity=qty)], by="reconciler", memo="position held at opening; owner must assign")
        self.settings.put("reconcile_watermark", iso(self.clock()), by="reconciler")
        last = self.db.one("SELECT MAX(id) AS id FROM ledger WHERE book = 'LIVE'")
        self.settings.put("hwm", {"value": s(facts.nav), "ledger_id": int(last["id"] or 0)}, by="reconciler")
        self.events.emit(
            "BOOKS_OPENED",
            f"Books opened from Robinhood: cash ${cents(facts.cash)}, {len(positions)} position(s).",
            actor="reconciler",
            payload={"cash": str(facts.cash), "nav": str(facts.nav), "positions": {k: str(v) for k, v in positions.items()}},
        )
        result = ReconcileResult("BOOTSTRAPPED", actions=["opening balance"])
        if positions:
            self.controls.set_reconcile_hold(["POSITIONS_AT_OPENING_UNASSIGNED"], {k: str(v) for k, v in positions.items()})
        return result

    # -- the periodic check ------------------------------------------------
    def run(self, reader: BrokerReader) -> ReconcileResult:
        try:
            reader.confirm_account()  # type: ignore[attr-defined]
        except BrokerAccountMismatch as exc:
            self.controls.engage(f"account check failed: {exc}", by="reconciler")
            return ReconcileResult("HOLD", [f"ACCOUNT_MISMATCH: {exc}"])
        except BrokerUnavailable as exc:
            return ReconcileResult("UNAVAILABLE", [str(exc)])
        try:
            self.desk.poll(reader)
            foreign = self._foreign_orders(reader)
            if self.desk.open_live_orders():
                return ReconcileResult("IN_FLIGHT", foreign)
            facts = reader.portfolio()
            broker_pos = reader.positions()
        except BrokerUnavailable as exc:
            return ReconcileResult("UNAVAILABLE", [str(exc)])

        result = ReconcileResult("OK")
        ledger_pos = self.ledger.firm_positions("LIVE")
        pos_diff = {
            sym: {"broker": str(broker_pos.get(sym, ZERO)), "ledger": str(ledger_pos.get(sym, ZERO))}
            for sym in set(broker_pos) | set(ledger_pos)
            if abs(broker_pos.get(sym, ZERO) - ledger_pos.get(sym, ZERO)) > QTY_TOLERANCE
        }
        if pos_diff:
            result.reasons.append("POSITION_MISMATCH: " + ", ".join(sorted(pos_diff)))
            result.diff["positions"] = pos_diff
        else:
            self._classify_cash(facts, broker_pos, result)
        self._drawdown(facts.nav)

        suspense_cash = self.ledger.cash("LIVE", SUSPENSE)
        suspense_pos = self.ledger.holdings("LIVE", SUSPENSE).get(SUSPENSE, {})
        if abs(suspense_cash) > CASH_TOLERANCE or suspense_pos:
            result.reasons.append(f"SUSPENSE: ${cents(suspense_cash)} cash, {len(suspense_pos)} position(s) awaiting your classification")
        result.reasons.extend(foreign)
        if result.reasons:
            result.status = "HOLD"
            self.controls.set_reconcile_hold(result.reasons, result.diff)
        else:
            self.controls.clear_reconcile_hold()
        self._snapshot(facts, broker_pos, result)
        return result

    def _foreign_orders(self, reader: BrokerReader) -> list[str]:
        """Orders at the broker the firm did not place. Remembered until the owner acknowledges them."""
        since = self.settings.get("reconcile_watermark")
        if not since:
            return []
        rows = self.db.query("SELECT client_ref, broker_order_id FROM orders WHERE book = 'LIVE'")
        known = {r["client_ref"] for r in rows}
        # Robinhood does not always echo our ref_id on its order list; the broker id we stored at submission is authoritative.
        ours = {r["broker_order_id"] for r in rows if r["broker_order_id"]}
        state = self.settings.get("foreign_orders") or {"open": {}, "acknowledged": []}
        changed = False
        for oid in [k for k in state["open"] if k in ours]:
            info = state["open"].pop(oid)
            changed = True
            self.events.emit(
                "FOREIGN_ORDER_CLEARED",
                f"The {info['side']} of {info['symbol']} flagged as an outside order was the firm's own order; flag cleared.",
                actor="reconciler", symbol=info["symbol"], payload={"broker_order_id": oid, **info},
            )
        for o in reader.orders(since=since):
            if (o.ref_id and o.ref_id in known) or o.id in ours or o.id in state["open"] or o.id in state["acknowledged"]:
                continue
            state["open"][o.id] = {"symbol": o.symbol, "side": o.side, "state": o.state, "placed_agent": o.placed_agent, "created_at": o.created_at, "quantity": str(o.cumulative_quantity)}
            changed = True
            self.events.emit(
                "FOREIGN_ORDER",
                f"Robinhood shows a {o.side} of {o.symbol} the firm did not place (source: {o.placed_agent or 'unknown'}). Buying paused until you resolve it.",
                actor="reconciler",
                symbol=o.symbol,
                severity="critical",
                payload=state["open"][o.id],
            )
        if changed:
            self.settings.put("foreign_orders", state, by="reconciler")
        # Keep the scan window short; anything older is already known or recorded.
        from datetime import timedelta

        self.settings.put("reconcile_watermark", iso(self.clock() - timedelta(days=2)), by="reconciler")
        return [f"FOREIGN_ORDER: {v['side']} {v['symbol']} ({v['placed_agent']})" for v in state["open"].values()]

    def _classify_cash(self, facts, broker_pos: dict[str, Decimal], result: ReconcileResult) -> None:
        limits = self.settings.get("firm_limits")
        ledger_cash = sum(self.ledger.balances("LIVE").values(), ZERO)
        diff = cents(facts.cash - ledger_cash)
        if abs(diff) <= CASH_TOLERANCE:
            if diff:
                self.ledger.adjust("LIVE", FIRM, diff, kind="ROUNDING", by="reconciler", memo="sub-cent true-up")
            self._remember_pending(facts.pending_deposits)
            return
        trueup = D(limits.get("rounding_trueup_usd") or "0.50")
        result.diff["cash"] = {"broker": str(facts.cash), "ledger": str(ledger_cash), "diff": str(diff)}
        prior_pending = D((self.settings.get("pending_deposits") or {}).get("value") or 0)
        pending_rise = cents(facts.pending_deposits - prior_pending)
        self._remember_pending(facts.pending_deposits)

        fills_since_last = self._fills_since_last_snapshot()
        if diff > 0:
            if pending_rise > 0 and abs(pending_rise - diff) <= CASH_TOLERANCE:
                self._deposit(diff, "deposit (pending deposit at Robinhood)")
                result.actions.append(f"deposit ${diff}")
            elif diff <= trueup and fills_since_last:
                # Tiny differences right after fills are the broker's rounding, not income.
                self.ledger.adjust("LIVE", FIRM, diff, kind="ROUNDING", by="reconciler", memo="rounding true-up after fills")
                result.actions.append(f"rounding +${diff}")
            elif self._dividend_shaped(diff, broker_pos, limits):
                self._dividend(diff)
                result.actions.append(f"dividend ${diff}")
            elif diff >= D("1"):
                self._deposit(diff, "deposit (cash arrived with no trade)")
                result.actions.append(f"deposit ${diff}")
            elif diff <= trueup:
                self.ledger.adjust("LIVE", FIRM, diff, kind="ROUNDING", by="reconciler", memo="rounding true-up")
                result.actions.append(f"rounding +${diff}")
            else:
                self._suspense(diff, "unexplained cash increase")
        else:
            out = -diff
            if out <= trueup:
                self.ledger.adjust("LIVE", FIRM, diff, kind="ROUNDING", by="reconciler", memo="rounding true-up")
                result.actions.append(f"rounding -${out}")
                return
            free = self.ledger.free_cash("LIVE", FIRM)
            take = min(out, max(free, ZERO))
            if take > 0:
                self.ledger.external_out(take, by="reconciler", memo="withdrawal (cash left with no trade)")
                self.events.emit("EXTERNAL_FLOW", f"Withdrawal of ${take} detected and taken from unallocated cash.", actor="reconciler", payload={"amount": str(take), "direction": "out"})
                result.actions.append(f"withdrawal ${take}")
            if out - take > 0:
                self._suspense(-(out - take), "withdrawal larger than unallocated cash; choose which manager funds it")

    def _fills_since_last_snapshot(self) -> bool:
        last = self.db.one("SELECT ts FROM broker_snaps ORDER BY id DESC LIMIT 1")
        since = last["ts"] if last else ""
        row = self.db.one("SELECT COUNT(*) AS n FROM orders WHERE book = 'LIVE' AND status IN ('FILLED','CANCELED') AND terminal_at >= ?", (since,))
        return int(row["n"]) > 0

    def _remember_pending(self, value: Decimal) -> None:
        self.settings.put("pending_deposits", {"value": s(value)}, by="reconciler")

    def _dividend_shaped(self, amount: Decimal, broker_pos: dict[str, Decimal], limits: dict[str, Any]) -> bool:
        if not broker_pos:
            return False
        cap = D(limits.get("dividend_max_pct_of_position") or 0.02)
        value = self._market_value(broker_pos)
        return value > 0 and amount <= value * cap

    def _market_value(self, positions: dict[str, Decimal]) -> Decimal:
        try:
            quotes = self.desk.market.quotes(list(positions))
        except BrokerUnavailable:
            return ZERO
        return sum((q * (quotes[s].mid or ZERO) for s, q in positions.items() if s in quotes), ZERO)

    def _dividend(self, amount: Decimal) -> None:
        """Attribute to holders by market value (the paying symbol is not reported by the broker)."""
        holdings = self.ledger.holdings("LIVE")
        managers = {b: syms for b, syms in holdings.items() if b not in (FIRM, SUSPENSE)}
        if not managers:
            self._suspense(amount, "dividend-sized cash with no manager holdings")
            return
        values: dict[str, Decimal] = {}
        quotes = self.desk.market.quotes(sorted({sym for syms in managers.values() for sym in syms}))
        for m, syms in managers.items():
            values[m] = sum((q * (quotes[s].mid or ZERO) for s, q in syms.items() if s in quotes), ZERO)
        self.ledger.dividend("LIVE", "DIVIDEND", values, amount, by="reconciler", memo="dividend (inferred from unexplained cash)")
        self.events.emit("DIVIDEND", f"Dividend of ${amount} detected and credited to holders.", actor="reconciler", payload={"amount": str(amount), "by_manager_value": {k: str(v) for k, v in values.items()}})

    def _deposit(self, amount: Decimal, memo: str) -> None:
        self.ledger.external_in(amount, by="reconciler", memo=memo)
        split = self.settings.get("deposit_split") or {}
        allocated = {}
        for manager, fraction in split.items():
            part = cents(amount * D(fraction))
            if part > 0:
                try:
                    self.ledger.allocate(manager, part, by="deposit_split", memo=f"{fraction:.0%} of a ${amount} deposit")
                    allocated[manager] = str(part)
                except LedgerError:
                    continue
        self.events.emit(
            "EXTERNAL_FLOW",
            f"Deposit of ${amount} detected" + (f"; allocated {', '.join(f'${v} to {k}' for k, v in allocated.items())}" if allocated else ""),
            actor="reconciler",
            payload={"amount": str(amount), "direction": "in", "allocated": allocated, "memo": memo},
        )

    def _suspense(self, cash: Decimal, why: str) -> None:
        self.ledger.adjust("LIVE", SUSPENSE, cash, kind="SUSPENSE_IN", by="reconciler", memo=why)
        self.events.emit("SUSPENSE", f"${cents(abs(cash))} moved to suspense: {why}.", actor="reconciler", severity="warning", payload={"cash": str(cash), "why": why})

    def _drawdown(self, nav: Decimal) -> None:
        limits = self.settings.get("firm_limits")
        state = self.settings.get("hwm") or {}
        hwm = D(state.get("value") or nav)
        # Only money entering or leaving the firm moves the high-water mark; allocations
        # between the firm and its managers are internal and net to zero.
        flows = self._external_flows(since_id=int(state.get("ledger_id") or 0))
        hwm = max(hwm + flows, nav)
        last = self.db.one("SELECT MAX(id) AS id FROM ledger WHERE book = 'LIVE'")
        self.settings.put("hwm", {"value": s(hwm), "ledger_id": int(last["id"] or 0)}, by="reconciler")
        if hwm > 0:
            dd = float(1 - nav / hwm)
            if dd >= float(limits.get("drawdown_brake_pct") or 0.2):
                self.controls.engage_brake(nav=s(nav), hwm=s(hwm), drawdown=dd)

    def _external_flows(self, *, since_id: int) -> Decimal:
        rows = self.db.query(
            "SELECT cash FROM ledger WHERE book = 'LIVE' AND kind IN ('EXTERNAL_IN','EXTERNAL_OUT') AND id > ?",
            (since_id,),
        )
        return sum((D(r["cash"]) for r in rows), ZERO)

    def _snapshot(self, facts, positions: dict[str, Decimal], result: ReconcileResult) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO broker_snaps (ts, nav, cash, buying_power, pending_deposits, positions_json, open_orders, reconciled, diff_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    iso(self.clock()),
                    s(facts.nav),
                    s(facts.cash),
                    s(facts.buying_power) if facts.buying_power is not None else None,
                    s(facts.pending_deposits),
                    json.dumps({k: str(v) for k, v in positions.items()}),
                    0,
                    1 if result.status == "OK" else 0,
                    json.dumps({"reasons": result.reasons, "actions": result.actions, **result.diff}),
                ),
            )

    # -- owner resolutions ----------------------------------------------------
    def resolve_suspense_cash(self, *, as_kind: str, by: str, manager: str | None = None, note: str = "") -> str:
        """Move suspense cash: 'deposit' → unallocated (+split), 'dividend' → holders, 'manager' → debit/credit a manager."""
        amount = cents(self.ledger.cash("LIVE", SUSPENSE))
        if amount == 0:
            raise LedgerError("no suspense cash")
        self.ledger.adjust("LIVE", SUSPENSE, -amount, kind="SUSPENSE_OUT", by=by, memo=f"resolved as {as_kind}: {note}")
        if as_kind == "deposit" and amount > 0:
            self.ledger.adjust("LIVE", FIRM, amount, kind="RESOLVED", by=by, memo=note or "owner: deposit")
        elif as_kind == "dividend" and amount > 0:
            self._dividend(amount)
        elif as_kind == "manager" and manager:
            self.ledger.adjust("LIVE", manager, amount, kind="RESOLVED", by=by, memo=note or "owner: assigned to manager")
        elif as_kind == "unallocated":
            self.ledger.adjust("LIVE", FIRM, amount, kind="RESOLVED", by=by, memo=note or "owner: unallocated")
        else:
            raise LedgerError(f"cannot resolve suspense as {as_kind}")
        self.events.emit("SUSPENSE_RESOLVED", f"${amount} in suspense resolved as {as_kind}" + (f" ({manager})" if manager else ""), actor=by, payload={"amount": str(amount), "as": as_kind, "manager": manager, "note": note})
        return "ok"

    def acknowledge_foreign_order(self, broker_order_id: str, *, by: str, note: str) -> None:
        state = self.settings.get("foreign_orders") or {"open": {}, "acknowledged": []}
        if broker_order_id in state["open"]:
            info = state["open"].pop(broker_order_id)
            state["acknowledged"].append(broker_order_id)
            self.settings.put("foreign_orders", state, by=by)
            self.events.emit("FOREIGN_ORDER_ACKNOWLEDGED", f"Acknowledged outside order {info['side']} {info['symbol']}: {note}", actor=by, payload=info)
