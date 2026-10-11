"""Replay daily history through the real manager → desk (SIM) → ledger → marks path.

This validates mechanics and accounting (turnover, drift control, invariants),
not future returns. Fills use the day's close ± half the assumed spread;
dividends are not modeled (price-only bars), so returns are understated.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from firm.broker.types import Bar, Quote
from firm.calendar import EASTERN
from firm.core import Firm
from firm.db import Database
from firm.managers.registry import TIM_DEFAULT_MANDATE
from firm.money import ZERO, D, cents


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class ReplayReader:
    """Serves quotes from the current day's close. Read-only; no writes exist."""

    account_number = "BACKTEST"

    def __init__(self, bars: dict[str, dict[str, Bar]], clock: _Clock, spread_bps: Decimal) -> None:
        self.bars = bars
        self.clock = clock
        self.half = spread_bps / Decimal(20000)
        self.today = ""

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out = {}
        for s in symbols:
            bar = self.bars.get(s, {}).get(self.today)
            if bar:
                out[s] = Quote(s, bid=bar.close * (1 - self.half), ask=bar.close * (1 + self.half), last=bar.close, observed_at=self.clock(), close=bar.close, close_date=self.today)
        return out


@dataclass
class BacktestResult:
    start: str
    end: str
    days: int
    contributions: Decimal
    final_nav: Decimal
    twr: Decimal | None
    cagr: float | None
    max_drawdown: float
    trades: int
    buys: Decimal
    sells: Decimal
    rebalance_days: int
    max_abs_drift: float
    invariant_failures: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items()}


def run_tim_backtest(
    bars: dict[str, list[Bar]],
    *,
    initial: Decimal = D("909"),
    monthly_contribution: Decimal = ZERO,
    mandate: dict[str, Any] | None = None,
    spread_bps: Decimal = D("4"),
    workdir: Path | None = None,
) -> BacktestResult:
    mandate = mandate or TIM_DEFAULT_MANDATE
    symbols = sorted(mandate["targets"])
    by_day = {s: {b.date: b for b in bars[s]} for s in symbols}
    days = sorted(set.intersection(*(set(v) for v in by_day.values())))
    if len(days) < 2:
        raise ValueError("not enough overlapping history")
    tmp = Path(workdir or tempfile.mkdtemp(prefix="firm-backtest-"))
    clock = _Clock(_at(days[0], time(10, 6)))
    reader = ReplayReader(by_day, clock, spread_bps)
    db = Database(tmp / "backtest.db")
    firm = Firm(db, reader=reader, writer=lambda: None, clock=clock, environ={})
    firm.registry.update_mandate("tim", dict(mandate), by="backtest", note="backtest mandate")
    firm.ledger.opening_balance(initial, by="backtest")
    firm.ledger.allocate("tim", initial, by="backtest")
    firm.registry.set_mode("tim", "SIM", by="backtest")

    contributions = initial
    peak, max_dd, max_drift = ZERO, 0.0, 0.0
    rebalance_days = 0
    failures: list[str] = []
    last_month = days[0][:7]
    for day in days:
        reader.today = day
        firm.market._cache.clear()
        if monthly_contribution > 0 and day[:7] != last_month:
            last_month = day[:7]
            clock.now = _at(day, time(9, 0))
            firm.ledger.external_in(monthly_contribution, by="backtest", memo="monthly contribution")
            firm.ledger.allocate("tim", monthly_contribution, by="backtest")
            contributions += monthly_contribution
        clock.now = _at(day, time(10, 6))
        out = firm.run_manager("tim")
        sold = any(o["side"] == "sell" and o["status"] == "FILLED" for o in out.orders)
        if sold:
            rebalance_days += 1
            clock.now = _at(day, time(10, 20))
            firm.market._cache.clear()
            firm.run_manager("tim", trigger="rerun")
        closes = {s: by_day[s][day].close for s in symbols}
        clock.now = _at(day, time(16, 20))
        firm.performance.take_marks(date.fromisoformat(day), closes=closes, benchmark_close=None, books=("SIM",))
        sleeve = firm.ledger.holdings("SIM", "tim").get("tim", {})
        cash = firm.ledger.cash("SIM", "tim")
        nav = cash + sum((q * closes[s] for s, q in sleeve.items()), ZERO)
        if cash < -D("0.01"):
            failures.append(f"{day}: negative cash {cash}")
        if nav > 0:
            for s, w in mandate["targets"].items():
                max_drift = max(max_drift, abs(float(sleeve.get(s, ZERO) * closes[s] / nav) - float(w)))
        if (problems := firm.ledger.verify_lots("SIM")):
            failures.extend(f"{day}: {p}" for p in problems)
        peak = max(peak, nav)
        if peak > 0:
            max_dd = max(max_dd, float(1 - nav / peak))

    marks = firm.performance.marks("SIM", "tim")
    twr = firm.performance.twr(marks)
    years = (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days / 365.25
    orders = firm.db.query("SELECT side, notional, filled_quantity, average_price FROM orders WHERE status = 'FILLED'")
    buys = sum((D(o["filled_quantity"]) * D(o["average_price"]) for o in orders if o["side"] == "buy"), ZERO)
    sells = sum((D(o["filled_quantity"]) * D(o["average_price"]) for o in orders if o["side"] == "sell"), ZERO)
    final = marks[-1].nav if marks else ZERO
    db.close()
    return BacktestResult(
        start=days[0],
        end=days[-1],
        days=len(days),
        contributions=cents(contributions),
        final_nav=cents(final),
        twr=twr,
        cagr=(float(1 + twr) ** (1 / years) - 1) if twr is not None and years > 0 else None,
        max_drawdown=max_dd,
        trades=len(orders),
        buys=cents(buys),
        sells=cents(sells),
        rebalance_days=rebalance_days,
        max_abs_drift=max_drift,
        invariant_failures=failures,
    )


def _at(day: str, t: time) -> datetime:
    return datetime.combine(date.fromisoformat(day), t, tzinfo=EASTERN)


def synthetic_bars(symbols: list[str], *, start: date, days: int, seed: int = 1) -> dict[str, list[Bar]]:
    """Random-walk daily bars on NYSE trading days (for tests)."""
    import random

    from firm.clock import CALENDAR

    rng = random.Random(seed)
    out: dict[str, list[Bar]] = {s: [] for s in symbols}
    price = {s: D(100) for s in symbols}
    d = start
    while len(out[symbols[0]]) < days:
        if CALENDAR.session_on(d):
            for s in symbols:
                price[s] = (price[s] * D(str(1 + rng.gauss(0.0003, 0.012)))).quantize(D("0.01"))
                out[s].append(Bar(d.isoformat(), price[s], price[s], price[s], price[s], 1_000_000))
        d += timedelta(days=1)
    return out
