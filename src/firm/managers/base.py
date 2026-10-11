"""The manager contract.

A manager is a pure decision function over its own book. It cannot see other
managers' books, cannot reach the broker, and cannot write the ledger: it
returns OrderIntents and a narrative, and the scheduler hands the intents to
the Execution Desk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Protocol

from firm.broker.types import Quote


@dataclass(frozen=True)
class OrderIntent:
    manager_id: str
    book: str
    symbol: str
    side: str  # 'buy' | 'sell'
    notional: Decimal | None = None  # BUY: dollars
    quantity: Decimal | None = None  # SELL: shares
    reason: str = ""


@dataclass(frozen=True)
class BookView:
    """Read-only snapshot of one manager's sleeve in one book."""

    book: str
    cash: Decimal  # free cash (excludes cash reserved by open buys)
    holdings: Mapping[str, Decimal]
    quotes: Mapping[str, Quote]

    def price(self, symbol: str) -> Decimal | None:
        q = self.quotes.get(symbol)
        return q.mid if q else None

    def value(self, symbol: str) -> Decimal:
        px = self.price(symbol)
        return (self.holdings.get(symbol, Decimal(0)) * px) if px else Decimal(0)

    @property
    def invested(self) -> Decimal:
        return sum((self.value(s) for s in self.holdings), Decimal(0))

    @property
    def nav(self) -> Decimal:
        return self.cash + self.invested


@dataclass(frozen=True)
class ManagerContext:
    manager_id: str
    book: str
    now: datetime
    trigger: str  # 'scheduled' | 'allocation_changed' | 'dividend' | 'rerun' | 'manual'
    mandate: Mapping[str, Any]
    view: BookView
    last_run: Mapping[str, Any] | None = None  # this manager's previous run summary
    drawdown: float = 0.0  # this sleeve's flow-adjusted decline from its peak, in this book


@dataclass
class RunResult:
    summary: str  # one human sentence for the activity feed
    intents: list[OrderIntent] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)  # numbers behind the decision
    rerun_after_minutes: int | None = None
    activity: str = "idle"  # what the agent is doing now (drives the HQ status / animation)


class Manager(Protocol):
    id: str
    universe_key: str

    def symbols(self, mandate: Mapping[str, Any]) -> list[str]: ...
    def due(self, now: datetime, mandate: Mapping[str, Any], last_scheduled_run: datetime | None) -> bool: ...
    def run(self, ctx: ManagerContext) -> RunResult: ...
