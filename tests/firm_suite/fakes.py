"""A behaviorally faithful fake of the Robinhood client used by the firm.

Account cash and positions move the way Robinhood's do when orders fill, so
reconciliation tests exercise real accounting. Failure modes are switchable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from firm.broker.errors import AmbiguousSubmission, BrokerAccountMismatch, BrokerUnavailable, BrokerWriteRefused
from firm.broker.types import Bar, BrokerOrder, Execution, PortfolioFacts, Quote, Tradability
from firm.clock import iso
from firm.money import D, ZERO, cents, shares_floor

ACCOUNT = "549688554"


@dataclass
class FakeRobinhood:
    clock: Any
    cash: Decimal = D("1010")
    positions_: dict[str, Decimal] = field(default_factory=dict)
    prices: dict[str, Decimal] = field(default_factory=lambda: {"VTI": D("300"), "VXUS": D("70"), "BND": D("72"), "SPY": D("670")})
    spread_bps: Decimal = D("4")
    account_number: str = ACCOUNT
    writer: bool = True
    approval_required: bool = False
    place_mode: str = "fill"  # fill | partial | open | reject | ambiguous | ambiguous_but_placed | tool_error
    review_checks: list = field(default_factory=list)
    unreachable: bool = False
    wrong_account: bool = False
    fractional: bool = True
    quote_age_seconds: float = 1.0
    pending_deposits: Decimal = ZERO
    orders_: list[BrokerOrder] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    # -- helpers for tests ------------------------------------------------------
    def deposit(self, amount: str) -> None:
        self.cash += D(amount)

    def foreign_order(self, symbol: str, side: str, qty: str, placed_agent: str = "user") -> None:
        price = self.prices[symbol]
        q = D(qty)
        self._apply(side, symbol, q, price, D("0"))
        self.orders_.append(self._order(symbol, side, q, price, ref=None, state="filled", agent=placed_agent))

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if self.unreachable:
            raise BrokerUnavailable(f"{name}: unreachable")

    def _order(self, symbol, side, qty, price, *, ref, state, agent="agentic", dollars=None, filled=None) -> BrokerOrder:
        filled_q = qty if filled is None else filled
        execs = (Execution(id=str(uuid4()), price=price, quantity=filled_q, fees=ZERO, timestamp=iso(self.clock())),) if filled_q > 0 else ()
        return BrokerOrder(
            id=str(uuid4()), ref_id=ref, symbol=symbol, side=side, state=state, quantity=qty, cumulative_quantity=filled_q,
            average_price=price if filled_q > 0 else None, fees=ZERO, dollar_amount=dollars, placed_agent=agent,
            created_at=iso(self.clock()), reject_reason="insufficient buying power" if state == "rejected" else None, executions=execs,
        )

    def _apply(self, side: str, symbol: str, qty: Decimal, price: Decimal, fees: Decimal) -> None:
        gross = cents(qty * price)
        if side == "buy":
            self.cash -= gross + fees
            self.positions_[symbol] = self.positions_.get(symbol, ZERO) + qty
        else:
            self.cash += gross - fees
            self.positions_[symbol] = self.positions_.get(symbol, ZERO) - qty
            if self.positions_[symbol] == 0:
                del self.positions_[symbol]

    # -- reader ------------------------------------------------------------
    def accounts(self) -> list[dict[str, Any]]:
        self._check("get_accounts")
        number = "000" if self.wrong_account else self.account_number
        return [{"account_number": number, "agentic_allowed": True, "state": "active", "type": "limited_margin"}]

    def confirm_account(self) -> dict[str, Any]:
        match = next((a for a in self.accounts() if a["account_number"] == self.account_number), None)
        if match is None:
            raise BrokerAccountMismatch("configured Agentic account not found")
        return match

    def portfolio(self) -> PortfolioFacts:
        self._check("get_portfolio")
        equity = sum((q * self.prices[s] for s, q in self.positions_.items()), ZERO)
        return PortfolioFacts(nav=cents(self.cash + equity), cash=self.cash, buying_power=self.cash * 2, unleveraged_buying_power=self.cash, pending_deposits=self.pending_deposits, equity_value=cents(equity))

    def positions(self) -> dict[str, Decimal]:
        self._check("get_equity_positions")
        return dict(self.positions_)

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        self._check("get_equity_quotes")
        out = {}
        for sym in symbols:
            if sym not in self.prices:
                continue
            mid = self.prices[sym]
            half = mid * self.spread_bps / D("20000")
            out[sym] = Quote(sym, bid=mid - half, ask=mid + half, last=mid, observed_at=self.clock() - timedelta(seconds=self.quote_age_seconds), close=mid)
        return out

    def orders(self, *, order_id=None, symbol=None, since=None) -> list[BrokerOrder]:
        self._check("get_equity_orders")
        rows = self.orders_
        if order_id:
            rows = [o for o in rows if o.id == order_id]
        if symbol:
            rows = [o for o in rows if o.symbol == symbol]
        if since:
            rows = [o for o in rows if o.created_at >= since]
        return list(reversed(rows))

    def tradability(self, symbols: list[str]) -> dict[str, Tradability]:
        self._check("get_equity_tradability")
        return {s: Tradability(s, tradable=True, fractional=self.fractional, halted=False) for s in symbols}

    def trade_approval_required(self) -> bool:
        self._check("get_trade_approval_setting")
        return self.approval_required

    def daily_bars(self, symbols: list[str], *, start: str) -> dict[str, list[Bar]]:
        return {}

    # -- writer ------------------------------------------------------------
    def review(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.writer:
            raise BrokerWriteRefused("read-only")
        self._check("review_equity_order")
        return {"symbol": payload["symbol"], "order_checks": list(self.review_checks)}

    def place(self, payload: dict[str, Any]) -> BrokerOrder | None:
        if not self.writer:
            raise BrokerWriteRefused("read-only")
        self.calls.append("place_equity_order")
        assert payload.get("ref_id"), "every order must carry an idempotency ref_id"
        existing = next((o for o in self.orders_ if o.ref_id == payload["ref_id"]), None)
        if existing:
            return existing  # upstream de-duplicates by ref_id
        symbol, side = payload["symbol"], payload["side"]
        price = self.prices[symbol] * (D("1") + (self.spread_bps / D("20000")) * (1 if side == "buy" else -1))
        if self.place_mode == "tool_error":
            raise BrokerUnavailable("tool error: invalid order", code="TOOL_ERROR")
        if self.place_mode == "ambiguous":
            raise AmbiguousSubmission("timed out")
        dollars = D(payload["dollar_amount"]) if payload.get("dollar_amount") else None
        qty = shares_floor(dollars / price) if dollars else D(payload["quantity"])
        if self.place_mode == "reject":
            order = self._order(symbol, side, qty, price, ref=payload["ref_id"], state="rejected", dollars=dollars, filled=ZERO)
        elif self.place_mode == "open":
            order = self._order(symbol, side, qty, price, ref=payload["ref_id"], state="queued", dollars=dollars, filled=ZERO)
        elif self.place_mode == "partial":
            part = shares_floor(qty / 2)
            order = self._order(symbol, side, qty, price, ref=payload["ref_id"], state="partially_filled", dollars=dollars, filled=part)
            self._apply(side, symbol, part, price, ZERO)
        else:
            order = self._order(symbol, side, qty, price, ref=payload["ref_id"], state="filled", dollars=dollars)
            self._apply(side, symbol, qty, price, ZERO)
        self.orders_.append(order)
        if self.place_mode == "ambiguous_but_placed":
            raise AmbiguousSubmission("timed out after the broker accepted")
        return order

    def complete(self, order_id: str) -> None:
        """Finish an open/partial order (the market filled the rest)."""
        idx = next(i for i, o in enumerate(self.orders_) if o.id == order_id)
        o = self.orders_[idx]
        remaining = (o.quantity or ZERO) - o.cumulative_quantity
        price = self.prices[o.symbol]
        ex = Execution(id=str(uuid4()), price=price, quantity=remaining, fees=D("0.01"), timestamp=iso(self.clock()))
        self._apply(o.side, o.symbol, remaining, price, D("0.01"))
        avg = ((o.average_price or ZERO) * o.cumulative_quantity + price * remaining) / (o.quantity or 1)
        self.orders_[idx] = BrokerOrder(**{**o.__dict__, "state": "filled", "cumulative_quantity": o.quantity, "average_price": avg, "fees": o.fees + D("0.01"), "executions": o.executions + (ex,)})
