"""Atlas: multi-timeframe trend follower (swing).

Regime (daily): price on the right side of the 200-day EMA, the 50-day EMA sloping the same
way, and ADX above a threshold, so it only trades established trends.
Entries (4-hour), in the trend's direction only:
  * breakout: a close beyond the prior 20-bar Donchian channel, above/below the 4h 50 EMA;
  * pullback: RSI(14) dips below a level and turns back while price holds the 4h 50 EMA.
Risk: an ATR stop (2.5x 4h ATR). No fixed target: once a trade is 1R in profit, a chandelier
trail (3x ATR from the best close) follows it. The trade closes if the daily regime fails.
"""

from __future__ import annotations

from typing import Any, Mapping

from forex.strategies.common import adx, atr, ema, highest_high, last, lowest_low, ready, rsi
from forex.strategy import Action, Close, Context, Modify, Open, Tunable

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "NZD_USD", "USD_CHF"]


class TrendBreakout:
    key = "atlas_trend_v1"
    style = "swing"
    title = "Multi-timeframe trend breakout"
    description = __doc__ or ""
    defaults: Mapping[str, Any] = {
        "instruments": PAIRS, "d_ema": 200, "d_fast": 50, "d_adx_min": 20, "donchian": 20, "h4_ema": 50,
        "pullback_rsi": 40, "atr_n": 14, "stop_atr": 2.5, "trail_atr": 3.0, "trail_after_r": 1.0,
    }
    tunables = (Tunable("d_adx_min", 2, 14, 30, True), Tunable("donchian", 5, 10, 40, True), Tunable("stop_atr", 0.25, 1.5, 3.5), Tunable("trail_atr", 0.5, 2.0, 4.5))

    def feeds(self, p: Mapping[str, Any]) -> dict[tuple[str, str], int]:
        return {**{(i, "D"): 600 for i in p["instruments"]}, **{(i, "H4"): 600 for i in p["instruments"]}}

    def trigger(self, p: Mapping[str, Any]) -> str:
        return "H4"

    def regime(self, ctx: Context, inst: str) -> int:
        p, d = ctx.params, ctx.bars(inst, "D")
        slow, fast, strength = last(d, ema(d, p["d_ema"])), ema(d, p["d_fast"]), last(d, adx(d, 14))
        f1, f5 = last(d, fast), last(d, fast, 6)
        if not ready(slow, f1, f5, strength) or len(d) < 2:
            return 0
        close = d.c[d.end - 1]
        if strength < p["d_adx_min"]:
            return 0
        if close > slow and f1 > f5:
            return 1
        if close < slow and f1 < f5:
            return -1
        return 0

    def on_bar(self, ctx: Context) -> list[Action]:
        p = ctx.params
        out: list[Action] = []
        for inst in p["instruments"]:
            h = ctx.bars(inst, "H4")
            if len(h) < max(p["donchian"], p["h4_ema"]) + 2:
                continue
            trend = self.regime(ctx, inst)
            a = last(h, atr(h, p["atr_n"]))
            close = h.c[h.end - 1]
            pos = ctx.position(inst)
            if pos:
                if trend != pos.side:
                    out.append(Close(pos.id, "daily trend regime ended"))
                    continue
                gain = (close - pos.entry) * pos.side
                since = h.end_at(pos.opened_at)
                if a and since < h.end and gain >= p["trail_after_r"] * pos.initial_risk:
                    best = max(h.c[since : h.end]) if pos.side > 0 else min(h.c[since : h.end])
                    trail = best - pos.side * p["trail_atr"] * a
                    if (trail - pos.stop) * pos.side > 0:
                        out.append(Modify(pos.id, stop=trail, reason="chandelier trail"))
                continue
            if trend == 0 or not a:
                continue
            e = last(h, ema(h, p["h4_ema"]))
            hh, ll = last(h, highest_high(h, p["donchian"]), 2), last(h, lowest_low(h, p["donchian"]), 2)
            r1, r2 = last(h, rsi(h, 14)), last(h, rsi(h, 14), 2)
            if not ready(e, hh, ll, r1, r2):
                continue
            why = None
            if trend > 0 and close > e:
                if close > hh:
                    why = f"4h breakout above {p['donchian']}-bar high in daily uptrend"
                elif r2 < p["pullback_rsi"] <= r1:
                    why = "4h pullback (RSI turned up) in daily uptrend"
            elif trend < 0 and close < e:
                if close < ll:
                    why = f"4h breakdown below {p['donchian']}-bar low in daily downtrend"
                elif r2 > 100 - p["pullback_rsi"] >= r1:
                    why = "4h pullback (RSI turned down) in daily downtrend"
            if why:
                out.append(Open(inst, trend, stop=close - trend * p["stop_atr"] * a, reason=why))
        return out
