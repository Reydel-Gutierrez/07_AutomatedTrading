"""The Academy's paper broker and risk engine, shared by live paper trading and backtests.

Fills follow OANDA semantics: buy at the ask, sell at the bid; stops can gap (fill at the
worse open price, plus slippage); targets fill at the target or better on a gap; if a bar
touches both, the stop is assumed first. P&L is in the quote currency, converted to USD at
the moment of close. Financing is charged at each 17:00 New York rollover from OANDA's
annual rates. One net position per instrument per agent (FIFO and no-hedging hold).
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from forex.fxclock import in_window, is_friday_after, market_open, rollover_window, trading_day, weekday_ny
from forex.instruments import Converter, Spec
from forex.strategy import Action, Close, Modify, Open, PositionView


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    t: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class BarPrices:
    """One completed candle's bid/ask OHLC, used to check stops and targets."""

    t: int
    close_t: int
    bo: float
    bh: float
    bl: float
    bc: float
    ao: float
    ah: float
    al: float
    ac: float


@dataclass
class Position:
    id: str
    instrument: str
    side: int
    units: int
    entry: float
    stop: float
    target: float | None
    opened_at: int
    initial_risk: float
    risk_usd: float
    tag: str
    version: int
    max_hold_until: int | None = None
    financing_usd: float = 0.0

    def view(self) -> PositionView:
        return PositionView(self.id, self.instrument, self.side, self.units, self.entry, self.stop, self.target, self.opened_at, self.initial_risk, self.tag)


@dataclass
class Trade:
    id: str
    version: int
    instrument: str
    side: int
    units: int
    entry: float
    exit: float
    opened_at: int
    closed_at: int
    pnl_usd: float
    financing_usd: float
    r: float
    exit_reason: str
    tag: str

    @property
    def net_usd(self) -> float:
        return self.pnl_usd + self.financing_usd


@dataclass
class RiskEnvelope:
    style: str  # "day" | "swing"
    risk_pct: float  # per trade, fraction of equity
    max_positions: int
    max_leverage: float = 5.0
    max_margin_frac: float = 0.25
    daily_loss: float = 0.02
    drawdown_pause: float = 0.10
    weekend_hold: bool = False
    weekend_max_leverage: float = 2.0
    weekend_gap_loss: float = 0.015
    max_same_currency: int = 2
    day_flat_at: str = "16:45"
    day_last_entry: str = "16:15"
    friday_entry_cutoff: str = "12:00"
    entry_slippage_pips: float = 0.1
    stop_slippage_pips: float = 0.2

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "RiskEnvelope":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Book:
    """One agent's virtual account."""

    agent_id: str
    cash: float  # realized balance, including financing
    start_capital: float = 10_000.0
    peak_equity: float = 0.0
    day_key: str = ""
    day_start_equity: float = 0.0
    paused: str | None = None  # reason new entries are paused (drawdown, owner)
    positions: dict[str, Position] = field(default_factory=dict)
    closed: list[Trade] = field(default_factory=list)  # trades closed since the last flush
    last_financing_day: str = ""

    def unrealized(self, p: Position, quotes: Mapping[str, Quote], conv: Converter) -> float:
        q = quotes.get(p.instrument)
        if q is None:
            return 0.0
        px = q.bid if p.side > 0 else q.ask
        return (px - p.entry) * p.units * p.side * conv.usd(p.instrument.split("_")[1])

    def equity(self, quotes: Mapping[str, Quote], conv: Converter) -> float:
        return self.cash + sum(self.unrealized(p, quotes, conv) for p in self.positions.values())

    def views(self) -> list[PositionView]:
        return [p.view() for p in self.positions.values()]


def currency_signs(instrument: str, side: int) -> dict[str, int]:
    """Long EUR_USD is long EUR (+1) and short USD (-1)."""
    base, quote = instrument.split("_")
    return {base: side, quote: -side}


def notional_usd(instrument: str, units: int, conv: Converter) -> float:
    return abs(units) * conv.usd(instrument.split("_")[0])


class Executor:
    """Applies a strategy's actions to a book under the agent's risk envelope."""

    def __init__(self, env: RiskEnvelope, specs: Mapping[str, Spec], version: int, *, new_id: Callable[[], str] | None = None) -> None:
        self.env = env
        self.specs = specs
        self.version = version
        self.new_id = new_id or (lambda: uuid.uuid4().hex[:12])

    # -- events -------------------------------------------------------------------
    @staticmethod
    def _ev(kind: str, summary: str, **payload: Any) -> dict[str, Any]:
        return {"type": kind, "summary": summary, "payload": payload}

    # -- bookkeeping ----------------------------------------------------------------
    def roll(self, book: Book, now: int, quotes: Mapping[str, Quote], conv: Converter) -> list[dict]:
        """New trading day bookkeeping, peak tracking, and the drawdown pause."""
        events: list[dict] = []
        eq = book.equity(quotes, conv)
        key = trading_day(now).isoformat()
        if key != book.day_key:
            book.day_key, book.day_start_equity = key, eq
        if eq > book.peak_equity:
            book.peak_equity = eq
        if book.paused is None and book.peak_equity > 0 and eq <= book.peak_equity * (1 - self.env.drawdown_pause):
            book.paused = f"drawdown {1 - eq / book.peak_equity:.1%} from peak"
            events.append(self._ev("AGENT_PAUSED", f"New entries paused: {book.paused}.", equity=eq, peak=book.peak_equity))
        return events

    def daily_loss_hit(self, book: Book, quotes: Mapping[str, Quote], conv: Converter) -> bool:
        return book.day_start_equity > 0 and book.equity(quotes, conv) <= book.day_start_equity * (1 - self.env.daily_loss)

    # -- actions --------------------------------------------------------------------
    def apply(self, book: Book, actions: list[Action], quotes: Mapping[str, Quote], now: int, conv: Converter) -> list[dict]:
        events: list[dict] = []
        for a in actions:
            if isinstance(a, Close):
                p = book.positions.get(a.position_id)
                if p and (q := quotes.get(p.instrument)):
                    events.append(self._close(book, p, q.bid if p.side > 0 else q.ask, now, a.reason or "strategy exit", conv))
            elif isinstance(a, Modify):
                events.extend(self._modify(book, a, quotes, now, conv))
            elif isinstance(a, Open):
                events.append(self._open(book, a, quotes, now, conv))
        return [e for e in events if e]

    def _modify(self, book: Book, a: Modify, quotes: Mapping[str, Quote], now: int, conv: Converter) -> list[dict]:
        p = book.positions.get(a.position_id)
        q = quotes.get(p.instrument) if p else None
        if p is None or q is None:
            return []
        exit_px = q.bid if p.side > 0 else q.ask
        if a.stop is not None:
            # a stop already crossed by the market is executed now rather than placed
            if (p.side > 0 and a.stop >= exit_px) or (p.side < 0 and a.stop <= exit_px):
                return [self._close(book, p, exit_px, now, a.reason or "stop moved through price", conv)]
            p.stop = a.stop
        if a.target is not None:
            p.target = a.target
        return []

    def check_open(self, book: Book, a: Open, quotes: Mapping[str, Quote], now: int, conv: Converter) -> tuple[str | None, int]:
        """(reason refused or None, units)."""
        env, spec, q = self.env, self.specs.get(a.instrument), quotes.get(a.instrument)
        if spec is None or q is None:
            return "NO_PRICE", 0
        if book.paused:
            return f"PAUSED: {book.paused}", 0
        if not market_open(now) or rollover_window(now):
            return "MARKET_WINDOW", 0
        if is_friday_after(now, env.friday_entry_cutoff):
            return "FRIDAY_CUTOFF", 0
        if env.style == "day" and in_window(now, env.day_last_entry, "17:10"):
            return "DAY_ENTRY_CUTOFF", 0
        if self.daily_loss_hit(book, quotes, conv):
            return "DAILY_LOSS_LIMIT", 0
        if any(p.instrument == a.instrument for p in book.positions.values()):
            return "ALREADY_IN_INSTRUMENT", 0
        if len(book.positions) >= env.max_positions:
            return "MAX_POSITIONS", 0
        if q.spread / spec.pip > spec.max_spread_pips:
            return f"SPREAD_WIDE: {q.spread / spec.pip:.1f} pips", 0
        entry = q.ask if a.side > 0 else q.bid
        dist = (entry - a.stop) * a.side
        if dist <= max(2 * q.spread, 3 * spec.pip):
            return "STOP_TOO_CLOSE", 0
        if a.target is not None and (a.target - entry) * a.side <= q.spread:
            return "TARGET_WRONG_SIDE", 0  # the strategy's price disagrees with the market
        if a.max_cost_r is not None and q.spread > a.max_cost_r * dist:
            return "COST_TOO_HIGH", 0  # the spread would eat too much of the trade's risk
        for ccy, sign in currency_signs(a.instrument, a.side).items():
            if sum(1 for p in book.positions.values() if currency_signs(p.instrument, p.side).get(ccy) == sign) >= env.max_same_currency:
                return f"CURRENCY_CONCENTRATION: {ccy}", 0
        equity = book.equity(quotes, conv)
        quote_usd = conv.usd(spec.quote)
        risk_usd = equity * env.risk_pct * max(min(a.risk_scale, 1.0), 0.0)
        units = int(risk_usd / (dist * quote_usd))
        # leverage and margin caps (they can only shrink the size)
        held = sum(notional_usd(p.instrument, p.units, conv) for p in book.positions.values())
        base_usd = conv.usd(spec.base)
        room = max(equity * env.max_leverage - held, 0.0)
        units = min(units, int(room / base_usd))
        margin_used = sum(notional_usd(p.instrument, p.units, conv) * self.specs[p.instrument].margin_rate for p in book.positions.values())
        margin_room = max(equity * env.max_margin_frac - margin_used, 0.0)
        units = min(units, int(margin_room / (base_usd * spec.margin_rate)))
        if units < 1:
            return "SIZE_BELOW_ONE_UNIT", 0
        return None, units

    def _open(self, book: Book, a: Open, quotes: Mapping[str, Quote], now: int, conv: Converter) -> dict:
        reason, units = self.check_open(book, a, quotes, now, conv)
        if reason:
            return self._ev("ENTRY_REFUSED", f"{'BUY' if a.side > 0 else 'SELL'} {a.instrument} refused: {reason}", instrument=a.instrument, reason=reason, why=a.reason)
        spec, q = self.specs[a.instrument], quotes[a.instrument]
        slip = self.env.entry_slippage_pips * spec.pip
        entry = q.ask + slip if a.side > 0 else q.bid - slip
        initial = (entry - a.stop) * a.side
        p = Position(
            id=self.new_id(), instrument=a.instrument, side=a.side, units=units, entry=entry, stop=a.stop, target=a.target,
            opened_at=now, initial_risk=initial, risk_usd=units * initial * conv.usd(spec.quote), tag=a.reason[:80], version=self.version,
            max_hold_until=now + a.max_hold_minutes * 60 if a.max_hold_minutes else None,
        )
        book.positions[p.id] = p
        return self._ev(
            "POSITION_OPENED",
            f"{'BUY' if a.side > 0 else 'SELL'} {units:,} {a.instrument} @ {entry:.5g}, stop {a.stop:.5g}" + (f", target {a.target:.5g}" if a.target else "") + f" ({a.reason})",
            instrument=a.instrument, position_id=p.id, units=units, side=a.side, entry=entry, stop=a.stop, target=a.target, risk_usd=round(p.risk_usd, 2),
        )

    def _close(self, book: Book, p: Position, price: float, now: int, reason: str, conv: Converter) -> dict:
        pnl = (price - p.entry) * p.units * p.side * conv.usd(p.instrument.split("_")[1])
        book.cash += pnl
        r = (pnl + p.financing_usd) / p.risk_usd if p.risk_usd > 0 else 0.0
        t = Trade(p.id, p.version, p.instrument, p.side, p.units, p.entry, price, p.opened_at, now, pnl, p.financing_usd, r, reason, p.tag)
        book.closed.append(t)
        del book.positions[p.id]
        return self._ev(
            "POSITION_CLOSED",
            f"Closed {'long' if p.side > 0 else 'short'} {p.instrument} @ {price:.5g}: {'+' if t.net_usd >= 0 else '-'}${abs(t.net_usd):,.2f} ({r:+.2f}R, {reason})",
            instrument=p.instrument, position_id=p.id, pnl_usd=round(pnl, 2), financing_usd=round(p.financing_usd, 2), r=round(r, 3), reason=reason,
        )

    # -- exits from price action ----------------------------------------------------------
    def check_exits(self, book: Book, instrument: str, bar: BarPrices, conv: Converter) -> list[dict]:
        events = []
        pip = self.specs[instrument].pip if instrument in self.specs else 0.0001
        slip = self.env.stop_slippage_pips * pip
        for p in [p for p in book.positions.values() if p.instrument == instrument]:
            if bar.close_t <= p.opened_at:
                continue  # bar finished before the position existed
            if p.side > 0:
                o, hi, lo = bar.bo, bar.bh, bar.bl
                if o <= p.stop:
                    events.append(self._close(book, p, o - slip, bar.close_t, "stop (gap)", conv))
                elif p.target is not None and o >= p.target:
                    events.append(self._close(book, p, o, bar.close_t, "target (gap)", conv))
                elif lo <= p.stop:
                    events.append(self._close(book, p, p.stop - slip, bar.close_t, "stop", conv))
                elif p.target is not None and hi >= p.target:
                    events.append(self._close(book, p, p.target, bar.close_t, "target", conv))
            else:
                o, hi, lo = bar.ao, bar.ah, bar.al
                if o >= p.stop:
                    events.append(self._close(book, p, o + slip, bar.close_t, "stop (gap)", conv))
                elif p.target is not None and o <= p.target:
                    events.append(self._close(book, p, o, bar.close_t, "target (gap)", conv))
                elif hi >= p.stop:
                    events.append(self._close(book, p, p.stop + slip, bar.close_t, "stop", conv))
                elif p.target is not None and lo <= p.target:
                    events.append(self._close(book, p, p.target, bar.close_t, "target", conv))
        return events

    # -- time rules -------------------------------------------------------------------------
    def time_rules(self, book: Book, now: int, quotes: Mapping[str, Quote], conv: Converter) -> list[dict]:
        events: list[dict] = []
        env = self.env

        def close_all(reason: str, keep: Callable[[Position], bool] = lambda p: False) -> None:
            for p in list(book.positions.values()):
                q = quotes.get(p.instrument)
                if q and not keep(p):
                    events.append(self._close(book, p, q.bid if p.side > 0 else q.ask, now, reason, conv))

        for p in list(book.positions.values()):
            if p.max_hold_until and now >= p.max_hold_until and (q := quotes.get(p.instrument)):
                events.append(self._close(book, p, q.bid if p.side > 0 else q.ask, now, "time exit", conv))
        if env.style == "day" and in_window(now, env.day_flat_at, "17:10"):
            close_all("day trader flat before rollover")
        if env.style == "swing" and is_friday_after(now, "15:30"):
            if not env.weekend_hold:
                if is_friday_after(now, "16:30"):
                    close_all("no weekend holding")
            else:
                events.extend(self._weekend_trim(book, now, quotes, conv))
        return events

    def weekend_risk(self, book: Book, quotes: Mapping[str, Quote], conv: Converter) -> tuple[float, float, dict[str, float]]:
        """(leverage, gap-adjusted loss as fraction of equity, per-position loss) if held over the weekend."""
        eq = book.equity(quotes, conv) or 1.0
        lev = sum(notional_usd(p.instrument, p.units, conv) for p in book.positions.values()) / eq
        losses = {}
        for p in book.positions.values():
            q = quotes.get(p.instrument)
            if not q:
                continue
            px = q.mid
            gap = self.specs[p.instrument].weekend_gap * px
            losses[p.id] = p.units * (abs(px - p.stop) + gap) * conv.usd(p.instrument.split("_")[1])
        return lev, sum(losses.values()) / eq, losses

    def _weekend_trim(self, book: Book, now: int, quotes: Mapping[str, Quote], conv: Converter) -> list[dict]:
        events: list[dict] = []
        while book.positions:
            lev, loss, per = self.weekend_risk(book, quotes, conv)
            if lev <= self.env.weekend_max_leverage and loss <= self.env.weekend_gap_loss:
                break
            worst = max(per, key=per.get) if per else next(iter(book.positions))
            p = book.positions[worst]
            q = quotes[p.instrument]
            events.append(self._close(book, p, q.bid if p.side > 0 else q.ask, now, f"weekend trim (leverage {lev:.1f}x, gap risk {loss:.1%})", conv))
        return events

    # -- financing at the 17:00 New York rollover ---------------------------------------------
    def financing(self, book: Book, now: int, quotes: Mapping[str, Quote], conv: Converter) -> list[dict]:
        """Charge or credit one rollover (call once per trading day, at or after 17:00 NY)."""
        key = trading_day(now).isoformat()
        if key == book.last_financing_day:
            return []
        first = not book.last_financing_day
        book.last_financing_day = key
        if first:
            return []  # start counting at the next rollover (no charge for a day not yet held)
        total = 0.0
        for p in book.positions.values():
            spec = self.specs[p.instrument]
            rate = spec.financing_long if p.side > 0 else spec.financing_short
            day = weekday_ny(now - 3600)  # the day that just ended
            days = spec.financing_days.get(day, 3 if day == "Wed" else 1) if spec.financing_days else (3 if day == "Wed" else 1)
            amount = notional_usd(p.instrument, p.units, conv) * rate / 365.0 * days
            p.financing_usd += amount
            book.cash += amount
            total += amount
        if not book.positions:
            return []
        return [self._ev("FINANCING", f"Rollover financing {'+' if total >= 0 else '-'}${abs(total):,.2f} on {len(book.positions)} position(s)", amount=round(total, 4))]
