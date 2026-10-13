"""Replay OANDA history through an agent's real strategy, risk envelope, and paper broker.

Timing: the strategy decides at a bar's close using only completed bars; entries fill at
the next bar's opening bid/ask (plus slippage). Stops and targets are checked on each later
bar of the decision timeframe (stop assumed first when a bar touches both). Financing is
charged at every 17:00 New York rollover. USD conversion uses the hourly closes of the
USD pairs. Nothing here can see a bar before it has closed.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from forex.fxclock import trading_day
from forex.instruments import Converter, Spec
from forex.market import SECONDS, Bars, MarketStore
from forex.sim import BarPrices, Book, Executor, Quote, RiskEnvelope, Trade
from forex.strategies import get as get_strategy
from forex.strategy import Context, Open, Strategy

USD_PAIR = {"EUR": "EUR_USD", "GBP": "GBP_USD", "AUD": "AUD_USD", "NZD": "NZD_USD", "JPY": "USD_JPY", "CAD": "USD_CAD", "CHF": "USD_CHF"}


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    strategy: str
    params: Mapping[str, Any]
    envelope: RiskEnvelope
    version: int = 1


@dataclass
class Result:
    trades: list[Trade]
    equity: list[tuple[int, float]]  # (time, equity) at each trading day's last decision
    start: int
    end: int
    capital: float
    refused: dict[str, int] = field(default_factory=dict)

    def metrics(self, start: int | None = None, end: int | None = None) -> dict[str, Any]:
        return metrics(self.trades, self.equity, start or self.start, end or self.end, self.capital)


def metrics(trades: list[Trade], equity: list[tuple[int, float]], start: int, end: int, capital: float) -> dict[str, Any]:
    ts = [t for t in trades if start <= t.closed_at < end]
    eq = [(t, e) for t, e in equity if start <= t < end]
    rs = [t.r for t in ts]
    wins = [t.net_usd for t in ts if t.net_usd > 0]
    losses = [-t.net_usd for t in ts if t.net_usd < 0]
    net = sum(t.net_usd for t in ts)
    peak, mdd = -1e18, 0.0
    for _, e in eq:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, 1 - e / peak)
    rets = [eq[i][1] / eq[i - 1][1] - 1 for i in range(1, len(eq)) if eq[i - 1][1] > 0]
    sharpe = None
    if len(rets) > 20:
        m = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) or 0.0
        sharpe = m / sd * math.sqrt(260) if sd > 0 else None
    years = max((end - start) / (365.25 * 86400), 1e-9)
    start_eq = eq[0][1] if eq else capital
    return {
        "trades": len(ts),
        "win_rate": len(wins) / len(ts) if ts else None,
        "avg_r": sum(rs) / len(rs) if rs else None,
        "r_stdev": (math.sqrt(sum((r - sum(rs) / len(rs)) ** 2 for r in rs) / (len(rs) - 1)) if len(rs) > 1 else None),
        "profit_factor": (sum(wins) / sum(losses)) if losses else (None if not wins else float("inf")),
        "net_usd": round(net, 2),
        "return_pct": net / start_eq if start_eq else None,
        "max_drawdown": mdd,
        "sharpe": sharpe,
        "years": round(years, 2),
        "trades_per_month": len(ts) / (years * 12) if years > 0 else None,
    }


class RandomDirection:
    """Baseline: the same strategy's entry times, stop and target distances, and exits, but
    with a coin-flip direction. Edge must beat this to count as skill, not risk management."""

    def __init__(self, inner: Strategy, seed: int) -> None:
        self.inner = inner
        self.rng = random.Random(seed)
        for k in ("key", "style", "title", "description", "defaults", "tunables"):
            setattr(self, k, getattr(inner, k))

    def feeds(self, p):
        return self.inner.feeds(p)

    def trigger(self, p):
        return self.inner.trigger(p)

    def on_bar(self, ctx: Context):
        out = []
        for a in self.inner.on_bar(ctx):
            if isinstance(a, Open):
                b = ctx.bars(a.instrument, self.inner.trigger(ctx.params))
                ref = b.c[b.end - 1]
                side = 1 if self.rng.random() < 0.5 else -1
                stop = ref - side * abs(ref - a.stop)
                target = ref + side * abs(a.target - ref) if a.target is not None else None
                a = replace(a, side=side, stop=stop, target=target, reason="random baseline")
            out.append(a)
        return out


def load_data(store: MarketStore, cfg: AgentConfig, start: int, end: int, strategy: Strategy | None = None) -> dict[tuple[str, str], Bars]:
    strat = strategy or get_strategy(cfg.strategy)
    data: dict[tuple[str, str], Bars] = {}
    for (inst, gran), lookback in strat.feeds(cfg.params).items():
        warm = lookback * SECONDS[gran]
        data[(inst, gran)] = store.bars(inst, gran, start=start - warm * 2, end=end)
    trig = strat.trigger(cfg.params)
    for inst in cfg.params["instruments"]:
        data.setdefault((inst, trig), store.bars(inst, trig, start=start - 86400 * 30, end=end))
    ccys = {c for inst in cfg.params["instruments"] for c in inst.split("_")} - {"USD"}
    for c in ccys:
        data.setdefault((USD_PAIR[c], "H1"), store.bars(USD_PAIR[c], "H1", start=start - 86400 * 10, end=end))
    return data


def run(cfg: AgentConfig, data: Mapping[tuple[str, str], Bars], specs: Mapping[str, Spec], start: int, end: int, *, capital: float = 10_000.0,
        strategy: Strategy | None = None, extra_slip_pips: float = 0.0) -> Result:
    strat = strategy or get_strategy(cfg.strategy)
    # The drawdown pause is a live control (it waits for a review); a backtest measures the
    # drawdown instead of freezing at it, so the pause is off here.
    env = replace(
        cfg.envelope, drawdown_pause=1.0,
        entry_slippage_pips=cfg.envelope.entry_slippage_pips + extra_slip_pips, stop_slippage_pips=cfg.envelope.stop_slippage_pips + extra_slip_pips,
    )
    counter = iter(range(10**9))
    ex = Executor(env, specs, cfg.version, new_id=lambda: f"bt{next(counter)}")
    book = Book(cfg.agent_id, capital, start_capital=capital, peak_equity=capital)
    trig = strat.trigger(cfg.params)
    insts = list(cfg.params["instruments"])
    tb = {i: data[(i, trig)] for i in insts}
    times = sorted({tb[i].close_time(k) for i in insts for k in range(len(tb[i].t)) if start <= tb[i].close_time(k) < end})
    conv_bars = {c: data[(p, "H1")] for c, p in USD_PAIR.items() if (p, "H1") in data}
    feeds = list(strat.feeds(cfg.params))
    equity: list[tuple[int, float]] = []
    refused: dict[str, int] = {}
    last_day = None

    def converter(ts: int) -> Converter:
        mids = {}
        for c, b in conv_bars.items():
            i = b.end_at(ts)
            if i > 0:
                mids[USD_PAIR[c]] = (b.bc[i - 1] + b.ac[i - 1]) / 2
        return Converter(mids)

    for T in times:
        quotes: dict[str, Quote] = {}
        for i in insts:
            b = tb[i]
            k = b.end_at(T)  # bars closed by T; bar k (if it starts at T) is the next one
            if k < len(b.t) and b.t[k] == T:
                quotes[i] = Quote(b.bo[k], b.ao[k], T)
            elif k > 0:
                quotes[i] = Quote(b.bc[k - 1], b.ac[k - 1], T)
            # stops/targets on the bar that just closed at T
            if k > 0 and b.close_time(k - 1) == T:
                bar = BarPrices(b.t[k - 1], T, b.bo[k - 1], b.bh[k - 1], b.bl[k - 1], b.bc[k - 1], b.ao[k - 1], b.ah[k - 1], b.al[k - 1], b.ac[k - 1])
                try:
                    ex.check_exits(book, i, bar, converter(T))
                except KeyError:
                    continue
        try:
            conv = converter(T)
            ex.financing(book, T, quotes, conv)
            ex.time_rules(book, T, quotes, conv)
            ex.roll(book, T, quotes, conv)
        except KeyError:
            continue
        ctx = Context(now=T, params=cfg.params, data={k: data[k].view(data[k].end_at(T)) for k in feeds if k in data}, positions=book.views(), equity=book.equity(quotes, conv), specs=specs)
        for e in ex.apply(book, strat.on_bar(ctx), quotes, T, conv):
            if e["type"] == "ENTRY_REFUSED":
                code = e["payload"]["reason"].split(":")[0]
                refused[code] = refused.get(code, 0) + 1
        day = trading_day(T)
        eq = book.equity(quotes, conv)
        if equity and last_day == day:
            equity[-1] = (T, eq)
        else:
            equity.append((T, eq))
        last_day = day
    return Result(book.closed, equity, start, end, capital, refused)


def bootstrap_drawdown(rs: list[float], risk_pct: float, *, runs: int = 1000, seed: int = 7) -> float | None:
    """95th percentile max drawdown (fraction of equity) from resampling the trade sequence."""
    if len(rs) < 10:
        return None
    rng = random.Random(seed)
    dds = []
    for _ in range(runs):
        eq, peak, mdd = 1.0, 1.0, 0.0
        for _ in range(len(rs)):
            eq *= 1 + rng.choice(rs) * risk_pct
            peak = max(peak, eq)
            mdd = max(mdd, 1 - eq / peak)
        dds.append(mdd)
    dds.sort()
    return dds[int(0.95 * (len(dds) - 1))]


def first_index_after(times: list[int], ts: int) -> int:
    return bisect_right(times, ts)
