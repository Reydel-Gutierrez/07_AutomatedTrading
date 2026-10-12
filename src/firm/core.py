"""Assembles the firm and runs managers. Shared by the service, the API, tests, and the backtest."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping

from firm.broker.errors import BrokerUnavailable
from firm.broker.types import BrokerReader, BrokerWriter
from firm.clock import Clock, et_date, parse, utc_now
from firm.controls import Controls
from firm.db import Database
from firm.desk import Desk
from firm.events import EventLog, Sink
from firm.ledger import Ledger
from firm.managers.base import BookView, Manager, ManagerContext
from firm.managers.registry import Registry
from firm.managers.alex import DEFAULT_MANDATE as ALEX_MANDATE, Alex, needs_strategy
from firm.managers.tim import Tim
from firm.market import MarketData
from firm.money import ZERO, cents
from firm.performance import BENCHMARK, Performance
from firm.reconcile import Reconciler
from firm.settings import Settings

log = logging.getLogger(__name__)


@dataclass
class RunOutcome:
    manager_id: str
    ran: bool
    summary: str
    orders: list[dict[str, Any]] = field(default_factory=list)
    rerun_after_minutes: int | None = None
    error: str | None = None


class Firm:
    def __init__(
        self,
        db: Database,
        *,
        reader: BrokerReader | None,
        writer: Callable[[], BrokerWriter | None] = lambda: None,
        clock: Clock = utc_now,
        environ: Mapping[str, str] | None = None,
        sinks: Iterable[Sink] = (),
        managers: Mapping[str, Manager] | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.reader = reader
        self.events = EventLog(db, clock=clock, sinks=sinks)
        self.settings = Settings(db, clock=clock)
        self.controls = Controls(self.settings, self.events, clock=clock, environ=environ)
        self.registry = Registry(db, self.events, clock=clock)
        self.registry.ensure_roster()
        self.ledger = Ledger(db, clock=clock)
        self.market = MarketData(reader, db, clock=clock)
        self.desk = Desk(
            db=db, ledger=self.ledger, events=self.events, settings=self.settings, controls=self.controls,
            registry=self.registry, market=self.market, writer=writer, clock=clock,
        )
        self.reconciler = Reconciler(db=db, ledger=self.ledger, events=self.events, settings=self.settings, controls=self.controls, desk=self.desk, clock=clock)
        self.performance = Performance(db, self.ledger, self.market, self.settings)
        self.managers: dict[str, Manager] = dict(managers or {"tim": Tim(), "alex": Alex()})
        if "alex" in self.managers:
            self._ensure_alex_strategy()

    def _ensure_alex_strategy(self) -> None:
        """Phase 3: replace Alex's placeholder mandate with his strategy, keeping the owner's risk policy."""
        _, current = self.registry.mandate("alex")
        if not needs_strategy(current):
            return
        risk = {k: current[k] for k in ("drawdown_policy", "sleeve_drawdown_pause", "daily_loss_pause") if k in current}
        self.registry.update_mandate("alex", {**ALEX_MANDATE, **risk}, by="system", note="Phase 3: Alex's screen, research, and portfolio rules (risk policy unchanged)")

    # -- managers ----------------------------------------------------------
    def view(self, manager_id: str, book: str, symbols: list[str]) -> BookView:
        holdings = self.ledger.holdings(book, manager_id).get(manager_id, {})
        quotes = self.market.quotes(sorted(set(symbols) | set(holdings)))
        return BookView(book=book, cash=self.ledger.free_cash(book, manager_id), holdings=holdings, quotes=quotes)

    def run_manager(self, manager_id: str, *, trigger: str = "scheduled") -> RunOutcome:
        row = self.registry.get(manager_id)
        impl = self.managers.get(manager_id)
        if impl is None or row.book is None:
            return RunOutcome(manager_id, False, f"{row.display_name} is {row.mode}; not running.")
        try:
            _, mandate = self.registry.mandate(manager_id)
            last = self.events.last_of("MANAGER_RUN", manager_id=manager_id)
            ctx = ManagerContext(
                manager_id=manager_id,
                book=row.book,
                now=self.clock(),
                trigger=trigger,
                mandate=mandate,
                view=self.view(manager_id, row.book, impl.symbols(mandate)),
                last_run=(last.payload.get("detail") if last else None),
                drawdown=self.performance.drawdown(self.performance.marks(row.book, manager_id)),
                extras=impl.inputs(self, mandate) if hasattr(impl, "inputs") else {},
            )
            result = impl.run(ctx)
        except BrokerUnavailable as exc:
            self.events.emit("MANAGER_WAITING", f"{row.display_name} is waiting for market data: {exc}", actor=manager_id, manager_id=manager_id)
            return RunOutcome(manager_id, False, "broker unavailable", rerun_after_minutes=15)
        except Exception as exc:  # noqa: BLE001 — one manager's failure never stops the firm
            log.exception("manager %s failed", manager_id)
            self.registry.set_status(manager_id, "DEGRADED", f"{type(exc).__name__}: {exc}")
            self.events.emit("MANAGER_DEGRADED", f"{row.display_name} hit an error and paused this run: {exc}", actor=manager_id, manager_id=manager_id, severity="critical", payload={"error": repr(exc)})
            return RunOutcome(manager_id, False, "error", error=repr(exc))

        self.registry.set_status(manager_id, "OK", result.activity)
        self.events.emit(
            "MANAGER_RUN",
            f"{row.display_name}: {result.summary}",
            actor=manager_id,
            manager_id=manager_id,
            payload={
                "book": row.book,
                "trigger": trigger,
                "activity": result.activity,
                "detail": result.detail,
                "intents": [{"symbol": i.symbol, "side": i.side, "notional": str(i.notional) if i.notional else None, "quantity": str(i.quantity) if i.quantity else None, "reason": i.reason} for i in result.intents],
            },
        )
        orders = []
        deferred = False
        for intent in result.intents:
            outcome = self.desk.submit(intent)
            deferred = deferred or outcome.deferred
            orders.append({"symbol": intent.symbol, "side": intent.side, "status": outcome.status, "reasons": outcome.reasons, "order_id": outcome.order_id})
        rerun = result.rerun_after_minutes or (15 if deferred else None)
        return RunOutcome(manager_id, True, result.summary, orders, rerun)

    # -- run requests (outside the daily schedule) ---------------------------
    def request_run(self, manager_id: str, reason: str, *, after_minutes: int = 0) -> None:
        """Ask for an extra run (allocation changed, rerun after sells, manual). Persisted across restarts."""
        pending = self.settings.get("manager_requests") or {}
        at = self.clock() + timedelta(minutes=after_minutes)
        current = pending.get(manager_id)
        if current and parse(current["at"]) <= at:
            current["reasons"] = sorted(set(current["reasons"]) | {reason})
        else:
            pending[manager_id] = {"at": at.isoformat(), "reasons": [reason]}
        self.settings.put("manager_requests", pending, by="firm")

    def due_request(self, manager_id: str) -> list[str] | None:
        req = (self.settings.get("manager_requests") or {}).get(manager_id)
        if req and parse(req["at"]) <= self.clock():
            return list(req["reasons"])
        return None

    def clear_request(self, manager_id: str) -> None:
        pending = self.settings.get("manager_requests") or {}
        if pending.pop(manager_id, None) is not None:
            self.settings.put("manager_requests", pending, by="firm")

    # -- capital (owner actions) ---------------------------------------------
    def allocate(self, manager_id: str, amount: Decimal, *, by: str, note: str = "") -> None:
        self.registry.get(manager_id)
        self.ledger.allocate(manager_id, amount, by=by, memo=note or None)
        self.events.emit("CAPITAL_ALLOCATED", f"${cents(amount)} allocated to {self.registry.get(manager_id).display_name}" + (f": {note}" if note else ""), actor=by, manager_id=manager_id, payload={"amount": str(cents(amount))})
        self.request_run(manager_id, "allocation_changed")

    def deallocate(self, manager_id: str, amount: Decimal, *, by: str, note: str = "") -> None:
        self.ledger.deallocate(manager_id, amount, by=by, memo=note or None)
        self.events.emit("CAPITAL_DEALLOCATED", f"${cents(amount)} returned from {self.registry.get(manager_id).display_name} to unallocated" + (f": {note}" if note else ""), actor=by, manager_id=manager_id, payload={"amount": str(cents(amount))})
        self.request_run(manager_id, "allocation_changed")

    def firm_nav(self, book: str = "LIVE") -> dict[str, Decimal]:
        balances = self.ledger.balances(book)
        holdings = self.ledger.holdings(book)
        symbols = sorted({s for syms in holdings.values() for s in syms})
        quotes = self.market.quotes(symbols) if symbols else {}
        out: dict[str, Decimal] = {}
        for bucket in set(balances) | set(holdings):
            invested = sum((q * (quotes[s].mid or ZERO) for s, q in holdings.get(bucket, {}).items() if s in quotes), ZERO)
            out[bucket] = balances.get(bucket, ZERO) + invested
        return out

    # -- daily close ----------------------------------------------------------
    def take_marks(self) -> int:
        today = et_date(self.clock())
        symbols = sorted({s for book in ("LIVE", "SIM") for syms in self.ledger.holdings(book).values() for s in syms} | {BENCHMARK})
        symbols = [s for s in symbols if s.isalpha()]
        quotes = self.market.quotes(symbols)
        closes = {}
        for sym, q in quotes.items():
            if q.close is not None and q.close_date == today.isoformat():
                closes[sym] = q.close
            elif q.last is not None:
                closes[sym] = q.last
        written = self.performance.take_marks(today, closes=closes, benchmark_close=closes.get(BENCHMARK))
        self._drawdown_alerts()
        return written

    def _drawdown_alerts(self) -> None:
        """Inform (never block) as the firm or a sleeve crosses a drawdown level."""
        levels = sorted(float(x) for x in (self.settings.get("firm_limits").get("drawdown_alert_levels") or []))
        if not levels:
            return
        state = self.settings.get("drawdown_alerts") or {}
        targets = [("_firm", "LIVE", "The firm")] + [(m.id, m.book, m.display_name) for m in self.registry.all() if m.book]
        changed = False
        for bucket, book, name in targets:
            dd = self.performance.drawdown(self.performance.marks(book, bucket))
            key = f"{book}:{bucket}"
            crossed = max((lvl for lvl in levels if dd >= lvl), default=0.0)
            if crossed > float(state.get(key, 0.0)):
                self.events.emit(
                    "DRAWDOWN_ALERT",
                    f"{name}{' (simulated)' if book == 'SIM' else ''} is {dd:.1%} below its peak. Information only: nothing is paused by this alert.",
                    actor="risk",
                    manager_id=None if bucket == "_firm" else bucket,
                    severity="warning",
                    payload={"book": book, "drawdown": dd, "level": crossed},
                )
                state[key], changed = crossed, True
            elif dd < levels[0] / 2 and state.get(key):
                state[key], changed = 0.0, True
        if changed:
            self.settings.put("drawdown_alerts", state, by="risk")
