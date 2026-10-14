"""Kestrel: London-open breakout of the Asian range (day trading).

Builds the Asian-session range (19:00-02:00 New York) on 15-minute bars. In the London
window (02:00-05:30 NY) it takes the first 15-minute close outside that range, if:
  * the range is a sensible size relative to the daily ATR (not dead, not already spent);
  * the 1-hour trend (close vs. 50 EMA) agrees with the breakout direction.
Stop at the middle of the range, target at 1.6x the risk, stop to breakeven at +1R, and a
hard time exit at 11:00 NY (the London/New York overlap fades). Never holds overnight.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Mapping

from forex.fxclock import NY, in_window, ny
from forex.strategies.common import atr, ema, last, ready
from forex.strategy import Action, Close, Context, Modify, Open, Tunable

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY"]


def _ts(day, hhmm: str) -> int:
    return int(datetime.combine(day, time(int(hhmm[:2]), int(hhmm[3:])), tzinfo=NY).timestamp())


class LondonBreakout:
    key = "kestrel_london_v1"
    style = "day"
    title = "London-open Asian range breakout"
    description = __doc__ or ""
    defaults: Mapping[str, Any] = {
        "instruments": PAIRS, "range_start": "19:00", "range_end": "02:00", "exit_at": "11:00",
        "min_range_atr": 0.2, "max_range_atr": 0.9, "buffer_pips": 2.0, "h1_ema": 50, "target_r": 1.6, "breakeven_r": 1.0, "entry_end": "05:30", "max_cost_r": 1.0,
    }
    tunables = (
        Tunable("target_r", 0.2, 1.0, 3.0), Tunable("max_range_atr", 0.1, 0.4, 1.4), Tunable("min_range_atr", 0.05, 0.1, 0.5), Tunable("buffer_pips", 1.0, 0.0, 8.0),
        Tunable("breakeven_r", 0.25, 0.5, 2.0), Tunable("h1_ema", 25, 25, 150, True), Tunable("entry_end", choices=("04:00", "05:30", "07:00")),
        Tunable("max_cost_r", choices=(1.0, 0.3, 0.2, 0.12)),
    )

    def feeds(self, p: Mapping[str, Any]) -> dict[tuple[str, str], int]:
        out: dict[tuple[str, str], int] = {}
        for i in p["instruments"]:
            out[(i, "M15")] = 300
            out[(i, "H1")] = 300
            out[(i, "D")] = 100
        return out

    def trigger(self, p: Mapping[str, Any]) -> str:
        return "M15"

    def on_bar(self, ctx: Context) -> list[Action]:
        p = ctx.params
        out: list[Action] = []
        now = ctx.now
        for inst in p["instruments"]:
            m = ctx.bars(inst, "M15")
            if len(m) < 40:
                continue
            close = m.c[m.end - 1]
            pos = ctx.position(inst)
            if pos:
                if in_window(now, p["exit_at"], "16:45"):
                    out.append(Close(pos.id, f"session exit {p['exit_at']} NY"))
                elif (close - pos.entry) * pos.side >= p["breakeven_r"] * pos.initial_risk and (pos.entry - pos.stop) * pos.side > 0:
                    out.append(Modify(pos.id, stop=pos.entry, reason="breakeven at +1R"))
                continue
            if not in_window(now, p["range_end"], p["entry_end"]) or ny(now).weekday() >= 5:
                continue
            day = ny(now).date()
            r_end = _ts(day, p["range_end"])
            r_start = _ts(day - timedelta(days=1), p["range_start"])
            # bars starting in [r_start, r_end)
            lo_i, hi_i = m.end_at(r_start + m.dur - 1), min(m.end_at(r_end + m.dur - 1), m.end)
            if hi_i - lo_i < 20:
                continue
            hi, lo = max(m.h[lo_i:hi_i]), min(m.l[lo_i:hi_i])
            d = ctx.bars(inst, "D")
            datr = last(d, atr(d, 14))
            h1 = ctx.bars(inst, "H1")
            bias_ema = last(h1, ema(h1, p["h1_ema"]))
            if not ready(datr, bias_ema) or len(h1) == 0:
                continue
            width = hi - lo
            if not (p["min_range_atr"] * datr <= width <= p["max_range_atr"] * datr):
                continue
            buf = p["buffer_pips"] * ctx.specs[inst].pip
            up, dn = hi + buf, lo - buf
            # only the first close outside the range since it formed
            if any(c > up or c < dn for c in m.c[hi_i : m.end - 1]):
                continue
            bias = 1 if h1.c[h1.end - 1] > bias_ema else -1
            side = 1 if close > up else -1 if close < dn else 0
            if side == 0 or side != bias:
                continue
            mid = (hi + lo) / 2
            dist = abs(close - mid)
            out.append(Open(inst, side, stop=mid, target=close + side * p["target_r"] * dist, reason=f"London breakout of Asian range ({width / ctx.specs[inst].pip:.0f} pips) with 1h trend", max_cost_r=p.get("max_cost_r")))
        return out
