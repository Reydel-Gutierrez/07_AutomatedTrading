"""The Stock Swing Class's strategies. Each is deterministic over completed daily bars.

A strategy precomputes its indicators once per run (prepare), then at each day's close:
  * update_stop   may raise a position's stop (trailing); never lowers it
  * exit          a rule exit decided at the close (filled at the next open)
  * entries       ranked candidates (best first) with their initial stops
The simulator (sim.py) owns sizing, cash, costs, stops hit during the day, the cooldown
after a stop, and the group cap. Strategies never see the future or other agents.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from equity.data import Panel
from equity.indicators import atr, change, highest, rsi, sma
from forex.strategy import Tunable

# Names that move together; the simulator allows at most `max_per_group` positions per group.
GROUPS: dict[str, str] = {
    **dict.fromkeys(["SPY", "DIA", "IWM"], "broad market"),
    **dict.fromkeys(["QQQ", "XLK", "AAPL", "MSFT", "GOOGL", "META", "NOW", "XLC"], "tech"),
    **dict.fromkeys(["SMH", "NVDA", "AVGO", "AMD", "MU"], "chips"),
    **dict.fromkeys(["PLTR", "NET", "CRWD", "DDOG", "SNOW", "SHOP"], "growth software"),
    **dict.fromkeys(["XLY", "AMZN", "TSLA", "NFLX", "COST", "HD", "TJX", "ROKU", "RBLX", "DKNG"], "consumer"),
    **dict.fromkeys(["XLF", "JPM", "GS", "COIN", "AFRM"], "financials"),
    **dict.fromkeys(["XLV", "LLY", "UNH", "ISRG"], "health"),
    **dict.fromkeys(["XLI", "CAT", "GE"], "industrials"),
    **dict.fromkeys(["XLE", "XOM", "ENPH"], "energy"),
    **dict.fromkeys(["XLP"], "staples"), **dict.fromkeys(["XLU"], "utilities"), **dict.fromkeys(["XLB"], "materials"),
    **dict.fromkeys(["XLRE"], "real estate"), **dict.fromkeys(["GLD"], "gold"), **dict.fromkeys(["TLT"], "bonds"),
}


@dataclass
class Pos:
    symbol: str
    shares: float
    entry: float
    stop: float
    opened: int  # day index of the fill
    initial_risk: float  # entry - initial stop (price)
    high: float  # highest close since entry
    cost_usd: float
    equity_at_entry: float


@dataclass(frozen=True)
class Entry:
    symbol: str
    stop: float
    score: float
    reason: str


class Strategy(Protocol):
    key: str
    title: str
    description: str
    defaults: Mapping[str, Any]
    tunables: tuple[Tunable, ...]

    def sizing(self, p: Mapping[str, Any]) -> tuple[int, float]: ...  # (max positions, fraction of equity each)
    def prepare(self, panel: Panel, p: Mapping[str, Any]) -> Any: ...
    def update_stop(self, prep: Any, i: int, pos: Pos, p: Mapping[str, Any]) -> float | None: ...
    def exit(self, prep: Any, i: int, pos: Pos, p: Mapping[str, Any]) -> str | None: ...
    def entries(self, prep: Any, i: int, p: Mapping[str, Any], held: set[str]) -> list[Entry]: ...


def _get(xs, i):
    return xs[i] if xs is not None and 0 <= i < len(xs) else None


class Pullback:
    """Buy a sharp dip (RSI(2) oversold) in an uptrend; sell into the bounce. Max's rule, with
    stops sized to each name's typical daily range instead of a fixed percentage."""

    key = "pullback_atr_v1"
    title = "Pullback with range-sized stops"
    description = "Close above the trend average and RSI(2) below the entry level: buy at the next open. Exit on a close above the short average, a trend break, the time limit, or the stop (entry minus k × ATR)."
    defaults: Mapping[str, Any] = {
        "symbols": ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "SMH",
                    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "JPM"],
        "trend_sma": 200, "rsi_days": 2, "rsi_entry": 15, "exit_sma": 5, "atr_days": 14, "stop_atr": 2.5, "max_hold": 10,
        "max_positions": 5, "position_pct": 0.2,
    }
    tunables = (
        Tunable("rsi_entry", step=5, lo=5, hi=30), Tunable("exit_sma", step=1, lo=3, hi=10, integer=True),
        Tunable("stop_atr", step=0.5, lo=1.5, hi=4.0), Tunable("max_hold", step=2, lo=4, hi=20, integer=True),
        Tunable("trend_sma", choices=(100, 150, 200)), Tunable("max_positions", step=1, lo=3, hi=6, integer=True),
    )

    def sizing(self, p):
        n = int(p["max_positions"])
        return n, min(float(p.get("position_pct", 1 / n)), 0.98 / n)

    def prepare(self, panel, p):
        out = {}
        for sym in p["symbols"]:
            s = panel.series.get(sym)
            if s is None:
                continue
            out[sym] = {"c": s.c, "trend": sma(s.c, int(p["trend_sma"])), "fast": sma(s.c, int(p["exit_sma"])),
                        "rsi": rsi(s.c, int(p["rsi_days"])), "atr": atr(s.h, s.l, s.c, int(p["atr_days"]))}
        return out

    def update_stop(self, prep, i, pos, p):
        return None

    def exit(self, prep, i, pos, p):
        d = prep.get(pos.symbol)
        c = _get(d["c"], i) if d else None
        if c is None:
            return None
        fast, trend = _get(d["fast"], i), _get(d["trend"], i)
        if fast is not None and c > fast:
            return "target: close above the short average"
        if i - pos.opened >= int(p["max_hold"]):
            return f"time: held {i - pos.opened} sessions"
        if trend is not None and c < trend:
            return "trend broke"
        return None

    def entries(self, prep, i, p, held):
        out = []
        for sym, d in prep.items():
            if sym in held:
                continue
            c, trend, r, a = _get(d["c"], i), _get(d["trend"], i), _get(d["rsi"], i), _get(d["atr"], i)
            if None in (c, trend, r, a) or not a:
                continue
            if c > trend and r < float(p["rsi_entry"]):
                out.append(Entry(sym, c - float(p["stop_atr"]) * a, r, f"RSI({p['rsi_days']}) {r:.1f} in an uptrend"))
        return sorted(out, key=lambda e: e.score)


class HighBetaPullback(Pullback):
    """The pullback rule on volatile growth names, with wider range-sized stops and smaller positions."""

    key = "pullback_highbeta_v1"
    title = "High-beta pullback"
    description = "The pullback rule on volatile growth stocks: smaller positions, stops sized to each name's daily range, at most two per group."
    defaults: Mapping[str, Any] = {
        "symbols": ["TSLA", "AMD", "MU", "PLTR", "COIN", "SHOP", "NET", "CRWD", "DDOG", "SNOW", "RBLX", "DKNG", "AFRM", "ROKU", "ENPH"],
        "trend_sma": 100, "rsi_days": 2, "rsi_entry": 10, "exit_sma": 5, "atr_days": 14, "stop_atr": 2.0, "max_hold": 8,
        "max_positions": 4, "position_pct": 0.2,
    }
    tunables = (
        Tunable("rsi_entry", step=5, lo=5, hi=25), Tunable("exit_sma", step=1, lo=3, hi=10, integer=True),
        Tunable("stop_atr", step=0.5, lo=1.0, hi=3.5), Tunable("max_hold", step=2, lo=4, hi=16, integer=True),
        Tunable("trend_sma", choices=(50, 100, 150)), Tunable("position_pct", step=0.05, lo=0.1, hi=0.25),
    )


class Breakout:
    """Buy a close above the prior N-day high in an uptrend; ride it on a trailing stop."""

    key = "breakout_trail_v1"
    title = "Breakout with a trailing stop"
    description = "Close above the highest close of the prior N days while above the trend average: buy at the next open. Exit on the trailing stop (highest close since entry minus k × ATR) or the time limit."
    defaults: Mapping[str, Any] = {
        "symbols": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "NFLX", "COST", "LLY", "JPM", "GS", "CAT", "GE",
                    "UNH", "XOM", "HD", "ISRG", "NOW", "TJX"],
        "lookback": 20, "trend_sma": 100, "atr_days": 14, "stop_atr": 2.0, "trail_atr": 3.0, "max_hold": 40,
        "max_positions": 5, "position_pct": 0.2,
    }
    tunables = (
        Tunable("lookback", step=5, lo=10, hi=55, integer=True), Tunable("trend_sma", choices=(50, 100, 200)),
        Tunable("stop_atr", step=0.5, lo=1.0, hi=3.5), Tunable("trail_atr", step=0.5, lo=2.0, hi=5.0),
        Tunable("max_hold", step=10, lo=20, hi=60, integer=True), Tunable("max_positions", step=1, lo=3, hi=6, integer=True),
    )

    def sizing(self, p):
        n = int(p["max_positions"])
        return n, min(float(p.get("position_pct", 1 / n)), 0.98 / n)

    def prepare(self, panel, p):
        out = {}
        for sym in p["symbols"]:
            s = panel.series.get(sym)
            if s is None:
                continue
            out[sym] = {"c": s.c, "trend": sma(s.c, int(p["trend_sma"])), "hi": highest(s.c, int(p["lookback"])),
                        "atr": atr(s.h, s.l, s.c, int(p["atr_days"])), "mom": change(s.c, 63)}
        return out

    def update_stop(self, prep, i, pos, p):
        a = _get(prep[pos.symbol]["atr"], i) if pos.symbol in prep else None
        return pos.high - float(p["trail_atr"]) * a if a else None

    def exit(self, prep, i, pos, p):
        return f"time: held {i - pos.opened} sessions" if i - pos.opened >= int(p["max_hold"]) else None

    def entries(self, prep, i, p, held):
        out = []
        for sym, d in prep.items():
            if sym in held:
                continue
            c, trend, hi, a, mom = (_get(d[k], i) for k in ("c", "trend", "hi", "atr", "mom"))
            if None in (c, trend, hi, a) or not a:
                continue
            if c > hi and c > trend:
                out.append(Entry(sym, c - float(p["stop_atr"]) * a, -(mom or 0.0), f"new {p['lookback']}-day high in an uptrend"))
        return sorted(out, key=lambda e: e.score)


class Rotation:
    """Hold the strongest few sector and asset ETFs by recent return, reranked on a fixed cycle."""

    key = "rotation_v1"
    title = "Sector rotation"
    description = "Every rebalance day, rank the ETFs by their return over the lookback and hold the top N that are above their trend average. A holding is sold when it drops out of the top N + 1 or below its trend, or hits its stop."
    defaults: Mapping[str, Any] = {
        "symbols": ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLC", "XLRE", "SMH", "GLD", "TLT"],
        "lookback": 63, "top_n": 3, "rebalance_days": 5, "trend_sma": 100, "atr_days": 14, "stop_atr": 3.0,
    }
    tunables = (
        Tunable("lookback", step=21, lo=21, hi=126, integer=True), Tunable("top_n", step=1, lo=2, hi=4, integer=True),
        Tunable("rebalance_days", choices=(5, 10, 21)), Tunable("trend_sma", choices=(50, 100, 200)), Tunable("stop_atr", step=0.5, lo=2.0, hi=5.0),
    )

    def sizing(self, p):
        n = int(p["top_n"])
        return n, 0.98 / n

    def prepare(self, panel, p):
        out = {}
        for sym in p["symbols"]:
            s = panel.series.get(sym)
            if s is None:
                continue
            out[sym] = {"c": s.c, "trend": sma(s.c, int(p["trend_sma"])), "mom": change(s.c, int(p["lookback"])), "atr": atr(s.h, s.l, s.c, int(p["atr_days"]))}
        return out

    def _ranked(self, prep, i):
        rows = []
        for sym, d in prep.items():
            c, trend, mom = _get(d["c"], i), _get(d["trend"], i), _get(d["mom"], i)
            if None in (c, trend, mom):
                continue
            rows.append((-mom, sym, c > trend))
        return [(sym, ok) for _, sym, ok in sorted(rows)]

    def update_stop(self, prep, i, pos, p):
        return None

    def exit(self, prep, i, pos, p):
        d = prep.get(pos.symbol)
        c, trend = (_get(d["c"], i), _get(d["trend"], i)) if d else (None, None)
        if c is not None and trend is not None and c < trend:
            return "below its trend average"
        if i % int(p["rebalance_days"]):
            return None
        names = [s for s, _ in self._ranked(prep, i)]
        if pos.symbol in names and names.index(pos.symbol) > int(p["top_n"]):
            return f"rebalance: ranked #{names.index(pos.symbol) + 1}"
        return None

    def entries(self, prep, i, p, held):
        if i % int(p["rebalance_days"]):
            return []
        out = []
        for rank, (sym, ok) in enumerate(self._ranked(prep, i)[: int(p["top_n"])]):
            if sym in held or not ok:
                continue
            c, a = _get(prep[sym]["c"], i), _get(prep[sym]["atr"], i)
            if c is None or not a:
                continue
            out.append(Entry(sym, c - float(p["stop_atr"]) * a, rank, f"rank #{rank + 1} by {p['lookback']}-day return"))
        return out


STRATEGIES: dict[str, Strategy] = {s.key: s for s in (Pullback(), HighBetaPullback(), Breakout(), Rotation())}


def get(key: str) -> Strategy:
    return STRATEGIES[key]


def warmup(p: Mapping[str, Any]) -> int:
    """Sessions of history a strategy needs before its first decision."""
    return max(int(p.get(k, 0) or 0) for k in ("trend_sma", "lookback", "atr_days")) + 70
