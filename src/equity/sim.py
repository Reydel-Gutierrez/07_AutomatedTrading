"""Replay daily bars through a strategy as a long-only cash account. Used for research,
validation, the day-1 test, and Floor 2 paper trading (the same code on newer bars).

Timing (no look-ahead): decisions are made at day i's close from bars up to i; orders fill at
day i+1's open. A stop is checked against each later day's low and fills at the stop, or at
the open when the day gaps below it. A buy whose next open is already at or below its stop is
refused.

Cash account: sale proceeds settle on the next session (T+1). Buys may use unsettled proceeds,
as a Robinhood cash account allows, but selling a position bought with them before they settle
is a good-faith violation (three in 12 months restricts the account); the run counts them and
validation gates on it. Fractional shares; no leverage, no shorting. Costs: `cost_bps` on every fill (half spread plus slippage), extra in stress tests.

Desk guards, as on Max's desk: no re-entry in a name stopped out in the last
`cooldown_sessions`, and at most `max_per_group` positions per group of names that move together.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from equity.data import Panel, epoch
from equity.strategies import GROUPS, Entry, Pos, Strategy, get as get_strategy

CAPITAL = 10_000.0
MIN_TRADE = 1.0


@dataclass(frozen=True)
class Envelope:
    cost_bps: float = 7.0
    cooldown_sessions: int = 3
    max_per_group: int = 2

    def as_dict(self) -> dict[str, Any]:
        return {"cost_bps": self.cost_bps, "cooldown_sessions": self.cooldown_sessions, "max_per_group": self.max_per_group}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Envelope":
        return cls(**{k: d[k] for k in ("cost_bps", "cooldown_sessions", "max_per_group") if k in d})


@dataclass(frozen=True)
class Config:
    agent_id: str
    strategy: str
    params: Mapping[str, Any]
    envelope: Envelope = Envelope()
    version: int = 1


@dataclass
class Trade:
    id: str
    instrument: str
    shares: float
    entry: float
    exit: float
    opened_at: int
    closed_at: int
    net_usd: float
    r: float
    ret: float  # net P&L as a fraction of equity at entry
    exit_reason: str
    version: int = 1

    @property
    def pnl_usd(self) -> float:
        return self.net_usd


@dataclass
class Result:
    trades: list[Trade]
    equity: list[tuple[int, float]]
    start: int
    end: int
    capital: float
    refused: dict[str, int] = field(default_factory=dict)
    positions: list[Pos] = field(default_factory=list)
    cash: float = 0.0
    gfv: list[int] = field(default_factory=list)  # times of good-faith violations

    def metrics(self, start: int | None = None, end: int | None = None) -> dict[str, Any]:
        from forex.backtest import metrics

        return metrics(self.trades, self.equity, start or self.start, end or self.end, self.capital)


class RandomNames:
    """Baseline: the strategy's own entry days, count, stop distance (as a fraction of price) and
    exit rules, but on randomly chosen names from its list. Selection must beat this to count."""

    def __init__(self, inner: Strategy, panel: Panel, seed: int) -> None:
        self.inner, self.panel, self.rng = inner, panel, random.Random(seed)
        for k in ("key", "title", "description", "defaults", "tunables"):
            setattr(self, k, getattr(inner, k))

    def sizing(self, p):
        return self.inner.sizing(p)

    def prepare(self, panel, p):
        return self.inner.prepare(panel, p)

    def update_stop(self, prep, i, pos, p):
        return self.inner.update_stop(prep, i, pos, p)

    def exit(self, prep, i, pos, p):
        return self.inner.exit(prep, i, pos, p)

    def entries(self, prep, i, p, held):
        real = self.inner.entries(prep, i, p, held)
        if not real:
            return []
        pool = [s for s in prep if s not in held and prep[s]["c"][i] is not None]
        self.rng.shuffle(pool)
        out = []
        for e, sym in zip(real, pool):
            ref = prep[e.symbol]["c"][i]
            c = prep[sym]["c"][i]
            frac = (ref - e.stop) / ref if ref else 0.05
            out.append(Entry(sym, c * (1 - frac), e.score, "random baseline"))
        return out


def run(cfg: Config, panel: Panel, start: int, end: int, *, capital: float = CAPITAL, strategy: Strategy | None = None,
        extra_cost_bps: float = 0.0, prep: Any = None) -> Result:
    """Simulate day indices start..end-1 (decisions from the close of `start`)."""
    strat = strategy or get_strategy(cfg.strategy)
    p = cfg.params
    env = cfg.envelope
    cost = (env.cost_bps + extra_cost_bps) / 10_000
    max_pos, pct = strat.sizing(p)
    prep = prep if prep is not None else strat.prepare(panel, p)
    times = panel.times
    end = min(end, len(panel.dates))
    cash = capital  # settled
    unsettled: list[tuple[int, float]] = []  # (day index it settles, amount)
    positions: dict[str, Pos] = {}
    trades: list[Trade] = []
    equity: list[tuple[int, float]] = []
    refused: dict[str, int] = {}
    stopped_at: dict[str, int] = {}
    sells: dict[str, str] = {}
    buys: list[Entry] = []
    counter = 0
    last_close: dict[str, float] = {}
    free_after: dict[str, int] = {}  # symbol -> day its purchase money settles (sooner sale = violation)
    gfv: list[int] = []

    def series(sym):
        return panel.series[sym]

    def mark(i: int) -> float:
        total = cash + sum(a for _, a in unsettled)
        for sym, pos in positions.items():
            c = series(sym).c[i]
            if c is not None:
                last_close[sym] = c
            total += pos.shares * last_close.get(sym, pos.entry)
        return total

    def close(sym: str, px: float, i: int, why: str) -> None:
        nonlocal counter
        pos = positions.pop(sym)
        if i < free_after.pop(sym, -1):
            gfv.append(times[i])
        fill = px * (1 - cost)
        proceeds = pos.shares * fill
        unsettled.append((i + 1, proceeds))
        net = proceeds - pos.cost_usd
        risk = pos.shares * pos.initial_risk
        counter += 1
        trades.append(Trade(f"{cfg.agent_id}-{counter}", sym, pos.shares, pos.entry, fill, times[pos.opened], times[i], round(net, 4),
                            net / risk if risk > 0 else 0.0, net / pos.equity_at_entry if pos.equity_at_entry else 0.0, why, cfg.version))

    for i in range(max(start, 0), end):
        # settle yesterday's sales
        if unsettled:
            cash += sum(a for d, a in unsettled if d <= i)
            unsettled = [(d, a) for d, a in unsettled if d > i]
        # 1. orders decided at yesterday's close fill at today's open
        if i > start:
            for sym, why in list(sells.items()):
                o = series(sym).o[i]
                if sym in positions and o is not None:
                    close(sym, o, i, why)
            sells = {}
            eq_now = mark(i - 1) if i - 1 >= 0 else cash
            for e in buys:
                if len(positions) >= max_pos or e.symbol in positions:
                    continue
                o = series(e.symbol).o[i]
                if o is None:
                    refused["no_open"] = refused.get("no_open", 0) + 1
                    continue
                if o <= e.stop:
                    refused["gapped_below_stop"] = refused.get("gapped_below_stop", 0) + 1
                    continue
                pending = sum(a for _, a in unsettled)
                amt = min(eq_now * pct, cash + pending)
                if amt < MIN_TRADE or amt < eq_now * pct * 0.5:
                    refused["cash"] = refused.get("cash", 0) + 1
                    continue
                fill = o * (1 + cost)
                shares = amt / fill
                from_settled = min(amt, cash)
                cash -= from_settled
                need, used_until = amt - from_settled, -1
                while need > 1e-9 and unsettled:
                    d, a = unsettled[0]
                    take = min(a, need)
                    need -= take
                    used_until = max(used_until, d)
                    unsettled = [(d, a - take)] + unsettled[1:] if a - take > 1e-9 else unsettled[1:]
                if used_until > i:
                    free_after[e.symbol] = used_until
                positions[e.symbol] = Pos(e.symbol, shares, fill, e.stop, i, fill - e.stop, o, amt, eq_now)
            buys = []
        # 2. stops during the day
        for sym in list(positions):
            s = series(sym)
            lo, o = s.l[i], s.o[i]
            pos = positions[sym]
            if lo is not None and lo <= pos.stop:
                px = min(o, pos.stop) if o is not None else pos.stop
                close(sym, px, i, "stop")
                stopped_at[sym] = i
        # 3. at the close: trailing stops, rule exits, entries for tomorrow
        for sym, pos in positions.items():
            c = series(sym).c[i]
            if c is None:
                continue
            pos.high = max(pos.high, c)
            new = strat.update_stop(prep, i, pos, p)
            if new is not None and new > pos.stop:
                pos.stop = new
            why = strat.exit(prep, i, pos, p)
            if why:
                sells[sym] = why
        equity.append((times[i], mark(i)))
        if i + 1 >= end:
            break
        staying = [s for s in positions if s not in sells]
        slots = max_pos - len(staying)
        if slots <= 0:
            continue
        in_group: dict[str, int] = {}
        for s in staying:
            g = GROUPS.get(s, s)
            in_group[g] = in_group.get(g, 0) + 1
        for e in strat.entries(prep, i, p, set(positions)):
            if len(buys) >= slots:
                break
            if i - stopped_at.get(e.symbol, -10**9) < env.cooldown_sessions:
                refused["cooldown"] = refused.get("cooldown", 0) + 1
                continue
            g = GROUPS.get(e.symbol, e.symbol)
            if in_group.get(g, 0) >= env.max_per_group:
                refused["group_full"] = refused.get("group_full", 0) + 1
                continue
            in_group[g] = in_group.get(g, 0) + 1
            buys.append(e)
    final = list(positions.values())
    return Result(trades, equity, times[max(start, 0)] if panel.dates else 0, times[end - 1] + 1 if end > 0 else 0, capital, refused, final, cash + sum(a for _, a in unsettled), gfv)


def with_params(cfg: Config, params: Mapping[str, Any]) -> Config:
    return replace(cfg, params=dict(params))


def pending_orders(cfg: Config, panel: Panel, start: int, end: int) -> dict[str, Any]:
    """What a paper account would do at the next open (for the agent's page): rerun and read the
    last close's decisions. Cheap enough at Floor 2 sizes."""
    res = run(cfg, panel, start, end)
    return {"positions": [{"symbol": p.symbol, "shares": round(p.shares, 6), "entry": round(p.entry, 4), "stop": round(p.stop, 4),
                           "opened": panel.dates[p.opened]} for p in res.positions]}


def day_epoch(panel: Panel, i: int) -> int:
    return epoch(panel.dates[i])
