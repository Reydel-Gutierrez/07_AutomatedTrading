"""Halcyon: daily mean reversion on range-bound crosses (low turnover).

Crosses like EUR/GBP, AUD/NZD and EUR/CHF tend to oscillate around a slow mean because the
two currencies share an economic region. Halcyon fades stretched daily moves on them:
  * stretch: the daily close is more than 2 standard deviations from its 20-day mean;
  * calm regime only: daily ADX below a ceiling (no trend in progress);
  * a turn: optionally, the latest close has already moved back toward the mean.
Target the 20-day mean, stop at 1.5x daily ATR beyond the entry, give up after 12 days,
and leave if a trend appears (ADX rises above 30).
"""

from __future__ import annotations

from typing import Any, Mapping

from forex.strategies.common import adx, atr, last, ready, sma
from forex.indicators import stdev
from forex.strategy import Action, Close, Context, Open, Tunable

PAIRS = ["EUR_GBP", "AUD_NZD", "EUR_CHF", "AUD_CAD", "USD_CAD"]


class CrossReversion:
    key = "halcyon_reversion_v1"
    style = "swing"
    title = "Daily mean reversion on range-bound crosses"
    description = __doc__ or ""
    defaults: Mapping[str, Any] = {"instruments": PAIRS, "n": 20, "z_entry": 2.0, "adx_max": 22, "adx_exit": 30, "stop_atr": 1.5, "max_hold_days": 12, "turn": True, "max_cost_r": 0.2}
    tunables = (
        Tunable("z_entry", 0.25, 1.25, 3.0), Tunable("adx_max", 2, 14, 30, True), Tunable("stop_atr", 0.25, 0.75, 3.0),
        Tunable("max_hold_days", 2, 4, 20, True), Tunable("n", 5, 10, 40, True), Tunable("turn", choices=(True, False)), Tunable("max_cost_r", choices=(1.0, 0.3, 0.2, 0.12)),
    )

    def feeds(self, p: Mapping[str, Any]) -> dict[tuple[str, str], int]:
        return {(i, "D"): 400 for i in p["instruments"]}

    def trigger(self, p: Mapping[str, Any]) -> str:
        return "D"

    def on_bar(self, ctx: Context) -> list[Action]:
        p = ctx.params
        out: list[Action] = []
        for inst in p["instruments"]:
            d = ctx.bars(inst, "D")
            if len(d) < p["n"] + 30:
                continue
            strength = last(d, adx(d, 14))
            pos = ctx.position(inst)
            if pos:
                if strength is not None and strength > p["adx_exit"]:
                    out.append(Close(pos.id, f"trend appeared (daily ADX {strength:.0f})"))
                continue
            mean = last(d, sma(d, p["n"]))
            sd = last(d, d.ind(("sd", p["n"]), lambda x: stdev(x.c, p["n"])))
            a = last(d, atr(d, 14))
            if not ready(strength, mean, sd, a) or sd == 0 or strength >= p["adx_max"]:
                continue
            c1, c2 = d.c[d.end - 1], d.c[d.end - 2]
            z = (c1 - mean) / sd
            side = 1 if z < -p["z_entry"] else -1 if z > p["z_entry"] else 0
            if side == 0 or (p["turn"] and (c1 - c2) * side <= 0):
                continue
            if (mean - c1) * side <= 0:
                continue
            out.append(Open(inst, side, stop=c1 - side * p["stop_atr"] * a, target=mean, max_hold_minutes=int(p["max_hold_days"]) * 1440,
                            reason=f"daily stretch z={z:+.1f} in a calm market (ADX {strength:.0f})", max_cost_r=p.get("max_cost_r")))
        return out
