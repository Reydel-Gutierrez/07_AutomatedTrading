"""Mira: range-bound mean reversion (day trading).

Trades only when the 1-hour market is ranging: ADX(14) below a ceiling and the 1-hour
20 EMA nearly flat (its 10-bar drift under a fraction of the 1-hour ATR). On 15-minute
bars it fades exhaustion: the previous bar closed outside the Bollinger band (20, 2.2)
with RSI(14) stretched, and the current bar closes back inside the band (rejection).
Stop beyond the recent swing plus half an ATR; target the band's middle line, skipped if
the reward is under 0.8x the risk. Quiet sessions only (Asia 19:30-02:30 and the New York
afternoon 11:30-15:30). Exits if the trend comes back (ADX rises) or after 6 hours.
"""

from __future__ import annotations

from typing import Any, Mapping

from forex.fxclock import in_window
from forex.strategies.common import adx, atr, bollinger, ema, last, ready, rsi
from forex.strategy import Action, Close, Context, Open, Tunable

PAIRS = ["EUR_GBP", "AUD_USD", "USD_CAD", "EUR_USD"]


class RangeReversion:
    key = "mira_reversion_v1"
    style = "day"
    title = "Ranging-market Bollinger reversion"
    description = __doc__ or ""
    defaults: Mapping[str, Any] = {
        "instruments": PAIRS, "sessions": [["19:30", "02:30"], ["11:30", "15:30"]], "adx_max": 22, "adx_exit": 30,
        "flat_drift_atr": 0.35, "bb_n": 20, "bb_k": 2.2, "rsi_lo": 32, "stop_atr": 0.5, "min_rr": 0.8, "max_hold_minutes": 360,
        "session_set": "both", "max_cost_r": 1.0,
    }
    tunables = (
        Tunable("bb_k", 0.2, 1.6, 3.2), Tunable("adx_max", 2, 14, 30, True), Tunable("stop_atr", 0.25, 0.25, 2.0), Tunable("min_rr", 0.2, 0.4, 2.0),
        Tunable("rsi_lo", 3, 15, 40, True), Tunable("flat_drift_atr", 0.1, 0.1, 0.8), Tunable("session_set", choices=("both", "asia", "ny")),
        Tunable("max_cost_r", choices=(1.0, 0.3, 0.2, 0.12)),
    )

    def feeds(self, p: Mapping[str, Any]) -> dict[tuple[str, str], int]:
        return {**{(i, "M15"): 300 for i in p["instruments"]}, **{(i, "H1"): 300 for i in p["instruments"]}}

    def trigger(self, p: Mapping[str, Any]) -> str:
        return "M15"

    def ranging(self, ctx: Context, inst: str) -> tuple[bool, float | None]:
        p, h = ctx.params, ctx.bars(inst, "H1")
        strength, e1, e10, a = last(h, adx(h, 14)), last(h, ema(h, 20)), last(h, ema(h, 20), 11), last(h, atr(h, 14))
        if not ready(strength, e1, e10, a):
            return False, strength
        return strength < p["adx_max"] and abs(e1 - e10) < p["flat_drift_atr"] * a, strength

    def on_bar(self, ctx: Context) -> list[Action]:
        p = ctx.params
        out: list[Action] = []
        sessions = {"both": p["sessions"], "asia": p["sessions"][:1], "ny": p["sessions"][1:]}[p.get("session_set", "both")]
        in_session = any(in_window(ctx.now, a, b) for a, b in sessions)
        for inst in p["instruments"]:
            m = ctx.bars(inst, "M15")
            if len(m) < p["bb_n"] + 20:
                continue
            ok, strength = self.ranging(ctx, inst)
            pos = ctx.position(inst)
            if pos:
                if strength is not None and strength > p["adx_exit"]:
                    out.append(Close(pos.id, f"trend returned (1h ADX {strength:.0f})"))
                continue
            if not (in_session and ok):
                continue
            mid, up, lo = bollinger(m, p["bb_n"], p["bb_k"])
            r_prev = last(m, rsi(m, 14), 2)
            a = last(m, atr(m, 14))
            m1, u1, l1, u2, l2 = last(m, mid), last(m, up), last(m, lo), last(m, up, 2), last(m, lo, 2)
            if not ready(r_prev, a, m1, u1, l1, u2, l2):
                continue
            c1, c2 = m.c[m.end - 1], m.c[m.end - 2]
            side = 0
            if c2 < l2 and c1 > l1 and r_prev < p["rsi_lo"]:
                side, stop = 1, min(m.l[m.end - 3 : m.end]) - p["stop_atr"] * a
            elif c2 > u2 and c1 < u1 and r_prev > 100 - p["rsi_lo"]:
                side, stop = -1, max(m.h[m.end - 3 : m.end]) + p["stop_atr"] * a
            if side == 0:
                continue
            risk, reward = abs(c1 - stop), (m1 - c1) * side
            if risk <= 0 or reward < p["min_rr"] * risk:
                continue
            out.append(Open(inst, side, stop=stop, target=m1, max_hold_minutes=p["max_hold_minutes"], reason=f"band rejection in a ranging market (1h ADX {strength:.0f})", max_cost_r=p.get("max_cost_r")))
        return out
