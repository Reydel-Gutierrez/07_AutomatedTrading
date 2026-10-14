"""The strategy contract. A strategy is a deterministic decision function over completed
OANDA candles and its own open positions. It returns actions; it cannot place orders, see
other agents, or read the future. The Executor (sim.py) applies risk rules and fills."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from forex.instruments import Spec
from forex.market import Bars


@dataclass(frozen=True)
class Open:
    instrument: str
    side: int  # +1 long, -1 short
    stop: float
    target: float | None = None
    reason: str = ""
    max_hold_minutes: int | None = None
    risk_scale: float = 1.0  # fraction of the envelope's per-trade risk (0 < x <= 1)
    max_cost_r: float | None = None  # refuse if the spread exceeds this fraction of the stop distance


@dataclass(frozen=True)
class Close:
    position_id: str
    reason: str = ""


@dataclass(frozen=True)
class Modify:
    position_id: str
    stop: float | None = None
    target: float | None = None
    reason: str = ""


Action = Open | Close | Modify


@dataclass(frozen=True)
class PositionView:
    id: str
    instrument: str
    side: int
    units: int
    entry: float
    stop: float
    target: float | None
    opened_at: int
    initial_risk: float  # price distance entry -> original stop
    tag: str


@dataclass
class Context:
    now: int  # decision time (close of the triggering bar), epoch seconds
    params: Mapping[str, Any]
    data: Mapping[tuple[str, str], Bars]  # (instrument, gran) -> bars visible at `now`
    positions: list[PositionView]
    equity: float
    specs: Mapping[str, Spec]
    note: dict[str, Any] = field(default_factory=dict)  # numbers behind the decision (for the journal)

    def bars(self, instrument: str, gran: str) -> Bars:
        return self.data[(instrument, gran)]

    def position(self, instrument: str) -> PositionView | None:
        return next((p for p in self.positions if p.instrument == instrument), None)


@dataclass(frozen=True)
class Tunable:
    """A parameter research may adjust: numeric steps within bounds, or one of fixed choices."""

    name: str
    step: float = 0.0
    lo: float = 0.0
    hi: float = 0.0
    integer: bool = False
    choices: tuple | None = None

    def neighbors(self, cur, *, steps: tuple[int, ...] = (-1, 1)) -> list:
        if self.choices is not None:
            return [c for c in self.choices if c != cur]
        out = []
        for k in steps:
            v = cur + k * self.step
            v = int(round(v)) if self.integer else round(v, 6)
            if self.lo <= v <= self.hi and v != cur:
                out.append(v)
        return out


class Strategy(Protocol):
    key: str
    style: str  # "day" | "swing"
    title: str
    description: str
    defaults: Mapping[str, Any]
    tunables: tuple[Tunable, ...]

    def feeds(self, params: Mapping[str, Any]) -> dict[tuple[str, str], int]:
        """(instrument, granularity) -> bars of history the strategy needs."""
        ...

    def trigger(self, params: Mapping[str, Any]) -> str:
        """Granularity whose bar close runs the strategy."""
        ...

    def on_bar(self, ctx: Context) -> list[Action]: ...
