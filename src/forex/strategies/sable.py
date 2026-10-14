"""Sable: daily channel-breakout trend follower (low turnover).

The classic long-horizon trend system, on daily bars only so it pays the spread rarely:
enter when the daily close breaks the prior 55-day high (or low), optionally only in the
direction of a rising (falling) 100-day EMA; initial stop at 2x the 20-day ATR; exit when
the close crosses the opposite 20-day channel. No profit target: winners run until the
trend breaks. Expect a low win rate and occasional large winners.
"""

from __future__ import annotations

from typing import Any, Mapping

from forex.strategies.common import atr, ema, highest_high, last, lowest_low, ready
from forex.strategy import Action, Close, Context, Open, Tunable

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "USD_CHF", "NZD_USD", "EUR_JPY", "EUR_GBP"]


class DailyBreakout:
    key = "sable_breakout_v1"
    style = "swing"
    title = "Daily channel breakout trend"
    description = __doc__ or ""
    defaults: Mapping[str, Any] = {"instruments": PAIRS, "entry_n": 55, "exit_n": 20, "atr_n": 20, "stop_atr": 2.0, "trend_filter": True, "trend_ema": 100, "max_cost_r": 0.2}
    tunables = (
        Tunable("entry_n", 5, 20, 100, True), Tunable("exit_n", 5, 10, 40, True), Tunable("stop_atr", 0.25, 1.0, 4.0),
        Tunable("trend_filter", choices=(True, False)), Tunable("trend_ema", 25, 50, 200, True), Tunable("max_cost_r", choices=(1.0, 0.3, 0.2, 0.12)),
    )

    def feeds(self, p: Mapping[str, Any]) -> dict[tuple[str, str], int]:
        return {(i, "D"): 600 for i in p["instruments"]}

    def trigger(self, p: Mapping[str, Any]) -> str:
        return "D"

    def on_bar(self, ctx: Context) -> list[Action]:
        p = ctx.params
        out: list[Action] = []
        for inst in p["instruments"]:
            d = ctx.bars(inst, "D")
            if len(d) < max(p["entry_n"], p["trend_ema"]) + 2:
                continue
            close = d.c[d.end - 1]
            pos = ctx.position(inst)
            lo_x, hi_x = last(d, lowest_low(d, p["exit_n"]), 2), last(d, highest_high(d, p["exit_n"]), 2)
            if pos:
                if (pos.side > 0 and lo_x is not None and close < lo_x) or (pos.side < 0 and hi_x is not None and close > hi_x):
                    out.append(Close(pos.id, f"{p['exit_n']}-day channel exit"))
                continue
            hi_e, lo_e, a = last(d, highest_high(d, p["entry_n"]), 2), last(d, lowest_low(d, p["entry_n"]), 2), last(d, atr(d, p["atr_n"]))
            e1, e5 = last(d, ema(d, p["trend_ema"])), last(d, ema(d, p["trend_ema"]), 6)
            if not ready(hi_e, lo_e, a, e1, e5):
                continue
            side = 1 if close > hi_e else -1 if close < lo_e else 0
            if side == 0 or (p["trend_filter"] and (e1 - e5) * side <= 0):
                continue
            out.append(Open(inst, side, stop=close - side * p["stop_atr"] * a, reason=f"daily close beyond the {p['entry_n']}-day channel", max_cost_r=p.get("max_cost_r")))
        return out
