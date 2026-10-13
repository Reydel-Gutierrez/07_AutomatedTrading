"""Orion: currency-strength rotation (swing).

Every 4 hours it scores the eight currencies (EUR, GBP, USD, JPY, AUD, CAD, CHF, NZD) by
volatility-adjusted daily momentum (blend of 20- and 60-day returns divided by ATR%),
across every pair in the universe. It wants to be long the strongest currencies against the
weakest, so it ranks pairs by the strength gap between their two currencies and focuses
on the top two. Entry needs:
  * the daily trend agreeing (close vs. 50-day EMA);
  * a 4-hour pullback that is turning (RSI(14) below 45 last bar and rising now, mirrored for shorts);
  * acceptable carry: expected financing over a 10-day hold costs under 15% of the risk.
Stop at 1.5x daily ATR, target 2.5R, trailing 1x daily ATR from the best close once 1.5R
is reached. Exits when the pair falls out of the top four or the strength gap reverses.
"""

from __future__ import annotations

from typing import Any, Mapping

from forex.strategies.common import atr, ema, last, ready, roc, rsi
from forex.strategy import Action, Close, Context, Modify, Open, Tunable

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "USD_CHF", "NZD_USD", "EUR_GBP", "EUR_JPY"]


class StrengthRotation:
    key = "orion_strength_v1"
    style = "swing"
    title = "Currency-strength rotation with pullback entries"
    description = __doc__ or ""
    defaults: Mapping[str, Any] = {
        "instruments": PAIRS, "fast": 20, "slow": 60, "min_gap": 1.0, "focus": 2, "exit_rank": 4, "d_ema": 50,
        "rsi_pullback": 45, "stop_atr": 1.5, "target_r": 2.5, "trail_after_r": 1.5, "trail_atr": 1.0, "hold_days": 10, "carry_max_r": 0.15,
    }
    tunables = (Tunable("min_gap", 0.25, 0.25, 2.5), Tunable("stop_atr", 0.25, 1.0, 2.5), Tunable("target_r", 0.5, 1.5, 4.0))

    def feeds(self, p: Mapping[str, Any]) -> dict[tuple[str, str], int]:
        return {**{(i, "D"): 600 for i in p["instruments"]}, **{(i, "H4"): 300 for i in p["instruments"]}}

    def trigger(self, p: Mapping[str, Any]) -> str:
        return "H4"

    def strengths(self, ctx: Context) -> dict[str, float]:
        p = ctx.params
        tot: dict[str, float] = {}
        n: dict[str, int] = {}
        for inst in p["instruments"]:
            d = ctx.bars(inst, "D")
            f, s, a = last(d, roc(d, p["fast"])), last(d, roc(d, p["slow"])), last(d, atr(d, 14))
            if not ready(f, s, a) or len(d) == 0:
                continue
            vol = a / d.c[d.end - 1]
            score = (0.5 * f + 0.5 * s) / vol if vol > 0 else 0.0
            base, quote = inst.split("_")
            for ccy, sign in ((base, 1), (quote, -1)):
                tot[ccy] = tot.get(ccy, 0.0) + sign * score
                n[ccy] = n.get(ccy, 0) + 1
        return {c: tot[c] / n[c] for c in tot}

    def ranked(self, ctx: Context) -> list[tuple[str, int, float]]:
        """Pairs by strength gap: (instrument, side, |gap|), strongest first."""
        st = self.strengths(ctx)
        out = []
        for inst in ctx.params["instruments"]:
            base, quote = inst.split("_")
            if base in st and quote in st:
                gap = st[base] - st[quote]
                out.append((inst, 1 if gap > 0 else -1, abs(gap)))
        return sorted(out, key=lambda x: -x[2])

    def on_bar(self, ctx: Context) -> list[Action]:
        p = ctx.params
        ranks = self.ranked(ctx)
        order = {inst: (i, side, gap) for i, (inst, side, gap) in enumerate(ranks)}
        ctx.note["strength_top"] = [(i, s, round(g, 2)) for i, s, g in ranks[:4]]
        out: list[Action] = []
        for inst in p["instruments"]:
            pos = ctx.position(inst)
            d, h = ctx.bars(inst, "D"), ctx.bars(inst, "H4")
            datr = last(d, atr(d, 14))
            if pos:
                rank = order.get(inst)
                if rank is None or rank[0] >= p["exit_rank"] or rank[1] != pos.side:
                    out.append(Close(pos.id, "fell out of the strength ranking"))
                    continue
                close = h.c[h.end - 1]
                since = h.end_at(pos.opened_at)
                if datr and since < h.end and (close - pos.entry) * pos.side >= p["trail_after_r"] * pos.initial_risk:
                    best = max(h.c[since : h.end]) if pos.side > 0 else min(h.c[since : h.end])
                    trail = best - pos.side * p["trail_atr"] * datr
                    if (trail - pos.stop) * pos.side > 0:
                        out.append(Modify(pos.id, stop=trail, reason="trailing 1x daily ATR"))
                continue
            rank = order.get(inst)
            if rank is None or rank[0] >= p["focus"] or rank[2] < p["min_gap"]:
                continue
            side = rank[1]
            de = last(d, ema(d, p["d_ema"]))
            r1, r2 = last(h, rsi(h, 14)), last(h, rsi(h, 14), 2)
            if not ready(de, r1, r2, datr) or len(h) == 0:
                continue
            close = h.c[h.end - 1]
            if (d.c[d.end - 1] - de) * side <= 0:
                continue
            pull = r2 < p["rsi_pullback"] and r1 > r2 if side > 0 else r2 > 100 - p["rsi_pullback"] and r1 < r2
            if not pull:
                continue
            dist = p["stop_atr"] * datr
            spec = ctx.specs[inst]
            rate = spec.financing_long if side > 0 else spec.financing_short
            carry = close * rate / 365.0 * p["hold_days"]  # price terms, per unit
            if carry < -p["carry_max_r"] * dist:
                continue
            out.append(Open(inst, side, stop=close - side * dist, target=close + side * p["target_r"] * dist, reason=f"#{rank[0] + 1} strength gap {rank[2]:.2f}, 4h pullback turning"))
        return out
