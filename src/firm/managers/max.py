"""Max — Short-Term Trading Manager. Swing trades with defined entries, exits, and stops. No AI.

Strategy: trend pullback (buy short, sharp dips in established uptrends; sell into the bounce).
Universe: highly liquid ETFs and mega-cap stocks (the mandate's `universe`; the desk refuses
anything else). Two runs per trading day:

  10:30 ET  risk check only: sell any position at or below its stop.
  15:15 ET  full run, using the live price as today's close:
            exits   - target: close above the 5-day average (the bounce happened)
                    - stop: price at or below entry × (1 − stop_pct)          [per-trade stop]
                    - time: held max_hold_days sessions without reaching the target
                    - trend: close below the 200-day average
            entries - close above the 200-day average AND RSI(2) below the entry threshold
                      (an oversold dip in an uptrend), most oversold first, up to
                      max_positions, each position_pct of the sleeve, never below the
                      minimum trade.
The desk adds the owner-approved sleeve stops (new buys pause at −10% from the
sleeve's peak or −3% in a day), the spread limit, and the daily order limit.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from firm.clock import CALENDAR, et, parse_hhmm
from firm.managers.base import ManagerContext, OrderIntent, RunResult
from firm.money import ZERO, D, cents

ID = "max"

UNIVERSE = [
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "SMH",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "JPM",
]

DEFAULT_MANDATE: dict[str, Any] = {
    "strategy": "trend_pullback_v1",
    "universe": UNIVERSE,
    "max_positions": 4,
    "position_pct": 0.25,
    "min_trade_usd": 5,
    "max_spread_bps": 20,
    "max_orders_per_day": 10,
    "run_times_et": ["10:30", "15:15"],
    "entry_time_et": "15:15",
    "trend_sma_days": 200,
    "exit_sma_days": 5,
    "rsi_days": 2,
    "rsi_entry_below": 10,
    "stop_pct": 0.06,
    "max_hold_days": 10,
}


def validate_mandate(m: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    universe = m.get("universe") or []
    if not isinstance(universe, list) or not universe or not all(isinstance(s, str) and s.isalpha() and s.isupper() for s in universe):
        problems.append("universe must be a non-empty list of upper-case ticker symbols")
    if not m.get("stop_pct"):
        problems.append("stop_pct is required: every trade needs a stop")
    checks = (
        ("max_positions", 1, 10), ("position_pct", 0.05, 0.5), ("min_trade_usd", 1, 1000), ("max_spread_bps", 1, 100),
        ("max_orders_per_day", 1, 40), ("trend_sma_days", 50, 300), ("exit_sma_days", 2, 20), ("rsi_days", 2, 14),
        ("rsi_entry_below", 1, 40), ("stop_pct", 0.01, 0.2), ("max_hold_days", 2, 40),
        ("sleeve_drawdown_pause", 0.03, 0.5), ("daily_loss_pause", 0.01, 0.2),
    )
    for key, lo, hi in checks:
        if key in m and m[key] is not None and not (lo <= float(m[key]) <= hi):
            problems.append(f"{key} must be between {lo} and {hi}")
    if float(m.get("position_pct", 0.25)) * int(m.get("max_positions", 4)) > 1.0001:
        problems.append("max_positions × position_pct cannot exceed 100% of the sleeve")
    try:
        for t in [*(m.get("run_times_et") or []), m.get("entry_time_et", "15:15")]:
            parse_hhmm(str(t))
    except ValueError:
        problems.append("run times must be HH:MM")
    return problems


def needs_strategy(m: Mapping[str, Any]) -> bool:
    return not m.get("universe")


# -- indicators (pure) ------------------------------------------------------------
def sma(xs: Sequence[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def rsi(xs: Sequence[float], n: int) -> float | None:
    """Wilder's RSI over n periods, seeded over the available history."""
    if len(xs) < n + 1:
        return None
    gains = losses = 0.0
    changes = [xs[i] - xs[i - 1] for i in range(1, len(xs))]
    for c in changes[:n]:
        gains += max(c, 0)
        losses += max(-c, 0)
    avg_g, avg_l = gains / n, losses / n
    for c in changes[n:]:
        avg_g = (avg_g * (n - 1) + max(c, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-c, 0)) / n
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def sessions_since(opened: date, today: date) -> int:
    n, d = 0, opened
    while d < today and n < 60:
        d += timedelta(days=1)
        if CALENDAR.session_on(d) is not None:
            n += 1
    return n


class Max:
    id = ID

    def symbols(self, mandate: Mapping[str, Any]) -> list[str]:
        return sorted(set(mandate.get("universe") or []))

    def due(self, now: datetime, mandate: Mapping[str, Any], last_scheduled_run: datetime | None) -> bool:
        session = CALENDAR.session_for(now)
        if session is None:
            return False
        local = et(now)
        if local.time() >= session.close_time:
            return False
        last = et(last_scheduled_run) if last_scheduled_run else None
        for t in mandate.get("run_times_et") or ["15:15"]:
            slot = local.replace(hour=parse_hhmm(str(t)).hour, minute=parse_hhmm(str(t)).minute, second=0, microsecond=0)
            if local >= slot and (last is None or last < slot):
                return True
        return False

    def inputs(self, firm: Any, mandate: Mapping[str, Any]) -> dict[str, Any]:
        book = firm.registry.get(ID).book or "SIM"
        positions: dict[str, dict[str, Any]] = {}
        for lot in firm.ledger.open_lots(book, ID):
            p = positions.setdefault(lot.symbol, {"qty": ZERO, "cost": ZERO, "opened_at": lot.opened_at})
            p["qty"] += lot.open_quantity
            p["cost"] += lot.open_cost
            p["opened_at"] = min(p["opened_at"], lot.opened_at)
        return {
            "closes": firm.market.recent_closes(self.symbols(mandate), sessions=int(mandate.get("trend_sma_days", 200)) + 30),
            "last_bar_dates": {s: d for s, d in ((s, firm.market.last_bar_date(s)) for s in self.symbols(mandate)) if d},
            "positions": {s: {"avg_cost": float(p["cost"] / p["qty"]) if p["qty"] > 0 else 0.0, "opened_at": p["opened_at"]} for s, p in positions.items()},
        }

    def run(self, ctx: ManagerContext) -> RunResult:
        m = ctx.mandate
        view = ctx.view
        local = et(ctx.now)
        today = local.date()
        stored: Mapping[str, Sequence[float]] = ctx.extras.get("closes") or {}
        bar_dates: Mapping[str, str] = ctx.extras.get("last_bar_dates") or {}
        positions: Mapping[str, Mapping[str, Any]] = ctx.extras.get("positions") or {}
        stop_pct = float(m.get("stop_pct", 0.06))
        min_trade = D(m.get("min_trade_usd", 5))
        entry_window = local.time() >= parse_hhmm(str(m.get("entry_time_et", "15:15")))

        held = {s: q for s, q in view.holdings.items() if q > 0}
        missing = sorted(s for s in held if not view.price(s))
        if missing:
            return RunResult(f"Waiting for prices on {', '.join(missing)}; no trades.", activity="waiting for market data", detail={"missing_prices": missing})

        def series(sym: str) -> list[float]:
            xs = list(stored.get(sym) or [])
            px = view.price(sym)
            if px and bar_dates.get(sym) != today.isoformat():
                xs.append(float(px))  # the live price stands in for today's close
            return xs

        detail: dict[str, Any] = {"mode": "full" if entry_window else "stops only", "stop_pct": stop_pct}
        exits: dict[str, str] = {}
        for sym, qty in sorted(held.items()):
            px = float(view.price(sym))  # type: ignore[arg-type]
            pos = positions.get(sym) or {}
            entry = float(pos.get("avg_cost") or 0)
            if entry and px <= entry * (1 - stop_pct):
                exits[sym] = f"stop hit: {px:.2f} ≤ {entry * (1 - stop_pct):.2f} ({stop_pct:.0%} below entry {entry:.2f})"
                continue
            if not entry_window:
                continue
            xs = series(sym)
            fast, slow = sma(xs, int(m.get("exit_sma_days", 5))), sma(xs, int(m.get("trend_sma_days", 200)))
            days = sessions_since(datetime.fromisoformat(str(pos["opened_at"])).astimezone(local.tzinfo).date(), today) if pos.get("opened_at") else 0
            if fast is not None and px > fast:
                exits[sym] = f"target: {px:.2f} above the {int(m.get('exit_sma_days', 5))}-day average {fast:.2f}" + (f" ({(px / entry - 1):+.1%})" if entry else "")
            elif days >= int(m.get("max_hold_days", 10)):
                exits[sym] = f"time stop: held {days} sessions"
            elif slow is not None and px < slow:
                exits[sym] = f"trend broke: below the {int(m.get('trend_sma_days', 200))}-day average {slow:.2f}"
        if exits:
            detail["exits"] = exits
            intents = [OrderIntent(ctx.manager_id, ctx.book, s, "sell", quantity=held[s], reason=why) for s, why in exits.items()]
            return RunResult(f"Exiting {'; '.join(f'{s} ({why})' for s, why in exits.items())}.", intents=intents, detail=detail, rerun_after_minutes=5 if entry_window else None, activity="exiting")

        if not entry_window:
            stops = {s: round(float((positions.get(s) or {}).get("avg_cost") or 0) * (1 - stop_pct), 2) for s in held}
            detail["stops"] = stops
            what = f"{len(held)} position(s) above their stops" if held else "no positions"
            return RunResult(f"Risk check: {what}. Entries are decided at {m.get('entry_time_et', '15:15')} ET.", detail=detail, activity="watching")

        nav = view.cash + sum((q * view.price(s) for s, q in held.items()), ZERO)  # type: ignore[operator]
        slots = int(m.get("max_positions", 4)) - len(held)
        candidates = []
        for sym in m.get("universe") or []:
            if sym in held or not view.price(sym):
                continue
            xs = series(sym)
            slow = sma(xs, int(m.get("trend_sma_days", 200)))
            r = rsi(xs[-30:], int(m.get("rsi_days", 2)))
            if slow is None or r is None:
                continue
            if xs[-1] > slow and r < float(m.get("rsi_entry_below", 10)):
                candidates.append((r, sym, xs[-1], slow))
        candidates.sort()
        detail["candidates"] = [{"symbol": s, "rsi": round(r, 1), "price": round(p, 2), "sma200": round(sl, 2)} for r, s, p, sl in candidates[:8]]
        if slots <= 0 or not candidates:
            why = "all position slots are in use" if slots <= 0 else "no oversold dips in uptrends today"
            return RunResult(f"Holding {', '.join(sorted(held)) or 'cash'}: {why}.", detail=detail, activity="holding")
        size = cents(nav * D(m.get("position_pct", 0.25)))
        cash = view.cash
        intents = []
        for r, sym, px, _ in candidates[:slots]:
            amt = cents(min(size, cash))
            if amt < min_trade:
                break
            stop = px * (1 - stop_pct)
            intents.append(OrderIntent(ctx.manager_id, ctx.book, sym, "buy", notional=amt, reason=f"pullback entry: RSI(2) {r:.1f} in an uptrend; stop {stop:.2f} ({stop_pct:.0%}), target the 5-day average"))
            cash -= amt
        if not intents:
            return RunResult(f"Signals on {', '.join(s for _, s, _, _ in candidates[:slots])}, but free cash is below the ${min_trade} minimum trade.", detail=detail, activity="holding")
        names = ", ".join(f"${i.notional} {i.symbol}" for i in intents)
        return RunResult(f"Entering pullbacks: {names}; each with a {stop_pct:.0%} stop.", intents=intents, detail=detail, activity="entering")
