"""Typed broker facts. Parsed once at the edge; the firm never handles raw payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

OPEN_STATES = frozenset({"new", "queued", "unconfirmed", "confirmed", "partially_filled", "pending_cancelled"})
TERMINAL_STATES = frozenset({"filled", "cancelled", "canceled", "rejected", "failed", "voided", "partially_filled_rest_cancelled"})


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    observed_at: datetime | None  # freshest venue timestamp among bid/ask/last
    close: Decimal | None = None  # official settled close of the latest completed session
    close_date: str | None = None
    tradable_state: str = "active"

    @property
    def mid(self) -> Decimal | None:
        if self.bid and self.ask and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last

    @property
    def spread_bps(self) -> Decimal | None:
        mid = self.mid
        if not (self.bid and self.ask and mid and self.bid > 0 and self.ask > 0):
            return None
        return (self.ask - self.bid) / mid * 10000


@dataclass(frozen=True)
class PortfolioFacts:
    nav: Decimal
    cash: Decimal
    buying_power: Decimal | None
    unleveraged_buying_power: Decimal | None
    pending_deposits: Decimal
    equity_value: Decimal


@dataclass(frozen=True)
class Execution:
    id: str
    price: Decimal
    quantity: Decimal
    fees: Decimal
    timestamp: str


@dataclass(frozen=True)
class BrokerOrder:
    id: str
    ref_id: str | None
    symbol: str
    side: str
    state: str
    quantity: Decimal | None
    cumulative_quantity: Decimal
    average_price: Decimal | None
    fees: Decimal
    dollar_amount: Decimal | None
    placed_agent: str | None
    created_at: str
    reject_reason: str | None
    executions: tuple[Execution, ...] = field(default_factory=tuple)

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@dataclass(frozen=True)
class Bar:
    date: str  # ET trading date YYYY-MM-DD
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class Tradability:
    symbol: str
    tradable: bool
    fractional: bool
    halted: bool


class BrokerReader(Protocol):
    account_number: str

    def accounts(self) -> list[dict[str, Any]]: ...
    def portfolio(self) -> PortfolioFacts: ...
    def positions(self) -> dict[str, Decimal]: ...
    def quotes(self, symbols: list[str]) -> dict[str, Quote]: ...
    def orders(self, *, order_id: str | None = None, symbol: str | None = None, since: str | None = None) -> list[BrokerOrder]: ...
    def tradability(self, symbols: list[str]) -> dict[str, Tradability]: ...
    def trade_approval_required(self) -> bool: ...
    def daily_bars(self, symbols: list[str], *, start: str) -> dict[str, list[Bar]]: ...


class BrokerWriter(BrokerReader, Protocol):
    def review(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def place(self, payload: dict[str, Any]) -> BrokerOrder | None: ...
