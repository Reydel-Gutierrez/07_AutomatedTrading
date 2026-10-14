"""Vega: volatility-compression breakout (swing, flat for weekends).

Quiet days often come before large moves. When the last completed daily bar is the
narrowest of the past 7 (an "NR7" day), Vega waits for a 4-hour close beyond that day's
high or low, in the direction of the 50-day trend, and takes only the first such break.
Stop on the other side of the quiet day, target 2x the risk, stop to breakeven at +1R,
and out after 5 days if nothing happened.
"""

from __future__ import annotations

from typing import Any, Mapping

from forex.strategies.common import ema, last, ready
from forex.strategy import Action, Close, Context, Modify, Open, Tunable

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "USD_CHF", "NZD_USD", "EUR_JPY"]


class CompressionBreakout:
    key = "vega_compression_v1"
    style = "swing"
    title = "Volatility-compression (NR7) breakout"
    description = __doc__ or ""
    defaults: Mapping[str, Any] = {"instruments": PAIRS, "nr_n": 7, "trend_filter": True, "trend_ema": 50, "target_r": 2.0, "breakeven_r": 1.0, "max_hold_days": 5, "buffer_frac": 0.05, "max_cost_r": 0.2}
    tunables = (
        Tunable("nr_n", choices=(4, 7, 10)), Tunable("target_r", 0.5, 1.0, 4.0), Tunable("max_hold_days", 1, 2, 10, True),
        Tunable("trend_filter", choices=(True, False)), Tunable("buffer_frac", 0.05, 0.0, 0.3), Tunable("max_cost_r", choices=(1.0, 0.3, 0.2, 0.12)),
    )

    def feeds(self, p: Mapping[str, Any]) -> dict[tuple[str, str], int]:
        return {**{(i, "D"): 300 for i in p["instruments"]}, **{(i, "H4"): 200 for i in p["instruments"]}}

    def trigger(self, p: Mapping[str, Any]) -> str:
        return "H4"

    def on_bar(self, ctx: Context) -> list[Action]:
        p = ctx.params
        out: list[Action] = []
        for inst in p["instruments"]:
            d, h = ctx.bars(inst, "D"), ctx.bars(inst, "H4")
            if len(d) < max(p["nr_n"], p["trend_ema"]) + 7 or len(h) < 10:
                continue
            close = h.c[h.end - 1]
            pos = ctx.position(inst)
            if pos:
                if (close - pos.entry) * pos.side >= p["breakeven_r"] * pos.initial_risk and (pos.entry - pos.stop) * pos.side > 0:
                    out.append(Modify(pos.id, stop=pos.entry, reason="breakeven at +1R"))
                continue
            k = d.end - 1  # the last completed daily bar
            ranges = [d.h[i] - d.l[i] for i in range(k - p["nr_n"] + 1, k + 1)]
            if ranges[-1] > min(ranges):
                continue  # not the narrowest
            hi, lo = d.h[k], d.l[k]
            buf = p["buffer_frac"] * (hi - lo)
            e1 = last(d, ema(d, p["trend_ema"]))
            if not ready(e1):
                continue
            trend = 1 if d.c[k] > e1 else -1
            # 4-hour bars since the quiet day closed; act only on the first close beyond it
            since = h.end_at(d.close_time(k))
            seg = list(h.c[since : h.end])
            if not seg:
                continue
            outside = [i for i, c in enumerate(seg) if c > hi + buf or c < lo - buf]
            if not outside or outside[0] != len(seg) - 1:
                continue
            side = 1 if close > hi + buf else -1
            if p["trend_filter"] and side != trend:
                continue
            stop = lo - buf if side > 0 else hi + buf
            dist = abs(close - stop)
            out.append(Open(inst, side, stop=stop, target=close + side * p["target_r"] * dist, max_hold_minutes=int(p["max_hold_days"]) * 1440,
                            reason=f"break of an NR{p['nr_n']} day with the 50-day trend", max_cost_r=p.get("max_cost_r")))
        return out
