"""Tim — Core Investment Manager. Diversified long-term ETF portfolio. No AI.

Each run:
  1. Value the sleeve at mid prices. Without a price for every target fund, do nothing.
  2. Full rebalance when: cash is negative, a fund outside the targets is held, any fund
     drifts beyond the absolute band (5 pp) or the relative band (25% of its target,
     if that is at least the minimum trade), or it is a new quarter and drift exceeds
     the quarterly band (1 pp). Overweight funds are sold first; the proceeds are
     invested on a follow-up run 10 minutes later.
  3. Otherwise invest free cash above the buffer into the underweight funds, in
     proportion to how far each is below target. Nothing smaller than the minimum
     trade is ever sent.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Mapping

from firm.clock import CALENDAR, et, parse_hhmm
from firm.managers.base import ManagerContext, OrderIntent, RunResult
from firm.money import ZERO, D, cents, shares_floor

ID = "tim"


def _quarter(dt: datetime) -> str:
    local = et(dt)
    return f"{local.year}Q{(local.month - 1) // 3 + 1}"


def normalized_targets(mandate: Mapping[str, Any]) -> dict[str, Decimal]:
    raw = {str(k).upper(): D(v) for k, v in (mandate.get("targets") or {}).items() if D(v) > 0}
    total = sum(raw.values(), ZERO)
    if total <= 0:
        raise ValueError("Tim's mandate has no target weights")
    return {k: v / total for k, v in raw.items()}


def validate_mandate(mandate: Mapping[str, Any]) -> list[str]:
    problems = []
    targets = mandate.get("targets") or {}
    if not targets:
        problems.append("targets are required")
    elif abs(sum(float(v) for v in targets.values()) - 1.0) > 0.0001:
        problems.append("target weights must sum to 100%")
    if any(float(v) < 0 for v in targets.values()):
        problems.append("target weights cannot be negative")
    for key, lo, hi in (("cash_buffer_pct", 0, 0.2), ("abs_band", 0.005, 0.5), ("rel_band", 0.05, 1), ("quarterly_band", 0, 0.2), ("min_trade_usd", 1, 1000), ("max_spread_bps", 1, 200)):
        if key in mandate and not (lo <= float(mandate[key]) <= hi):
            problems.append(f"{key} must be between {lo} and {hi}")
    try:
        parse_hhmm(str(mandate.get("run_time_et", "10:05")))
    except ValueError:
        problems.append("run_time_et must be HH:MM")
    return problems


class Tim:
    id = ID

    def symbols(self, mandate: Mapping[str, Any]) -> list[str]:
        return sorted(normalized_targets(mandate))

    def due(self, now: datetime, mandate: Mapping[str, Any], last_scheduled_run: datetime | None) -> bool:
        session = CALENDAR.session_for(now)
        if session is None:
            return False
        local = et(now)
        if local.time() < parse_hhmm(str(mandate.get("run_time_et", "10:05"))) or local.time() >= session.close_time:
            return False
        return last_scheduled_run is None or et(last_scheduled_run).date() != local.date()

    def run(self, ctx: ManagerContext) -> RunResult:
        m = ctx.mandate
        targets = normalized_targets(m)
        view = ctx.view
        min_trade = D(m.get("min_trade_usd", 5))
        buffer_pct = D(m.get("cash_buffer_pct", 0.01))
        prices = {sym: view.price(sym) for sym in set(targets) | set(view.holdings)}
        missing = sorted(sym for sym, px in prices.items() if not px)
        if missing:
            return RunResult(f"Waiting for prices on {', '.join(missing)}; no trades.", activity="waiting for market data", detail={"missing_prices": missing})

        pause = m.get("volatility_pause_pct")
        if pause:
            wild = sorted(
                f"{sym} {float(q.last / q.close - 1):+.1%}"
                for sym in targets
                if (q := view.quotes.get(sym)) and q.last and q.close and abs(q.last / q.close - 1) >= D(pause)
            )
            if wild:
                return RunResult(
                    f"Extreme market day ({', '.join(wild)} vs. yesterday's close); waiting for calmer prices before trading.",
                    activity="waiting out an extreme market day",
                    detail={"volatility": wild},
                )

        values = {sym: view.holdings.get(sym, ZERO) * prices[sym] for sym in set(targets) | set(view.holdings)}
        total = view.cash + sum(values.values(), ZERO)
        detail: dict[str, Any] = {
            "nav": str(cents(total)),
            "cash": str(cents(view.cash)),
            "targets": {k: float(v) for k, v in targets.items()},
            "weights": {k: float(values.get(k, ZERO) / total) if total > 0 else 0.0 for k in sorted(values)},
            "last_quarterly": (ctx.last_run or {}).get("last_quarterly"),
        }
        if total < min_trade:
            return RunResult(f"Sleeve holds ${cents(total)}, below the ${min_trade} minimum trade; nothing to do.", activity="idle", detail=detail)

        drift = {sym: (values.get(sym, ZERO) / total) - targets.get(sym, ZERO) for sym in values}
        detail["drift"] = {k: float(v) for k, v in drift.items()}
        abs_band, rel_band = D(m.get("abs_band", 0.05)), D(m.get("rel_band", 0.25))
        invested = sum(values.values(), ZERO)
        why: list[str] = []
        if view.cash < 0:
            why.append("cash is negative")
        strays = sorted(sym for sym in view.holdings if sym not in targets and view.holdings[sym] > 0)
        if strays:
            why.append(f"holds funds outside the mandate ({', '.join(strays)})")
        if invested > 0:
            for sym, d in drift.items():
                t = targets.get(sym, ZERO)
                if abs(d) > abs_band:
                    why.append(f"{sym} is {float(d):+.1%} from target")
                elif t > 0 and abs(d) / t > rel_band and abs(d) * total >= min_trade and values.get(sym, ZERO) > 0:
                    why.append(f"{sym} is {float(abs(d) / t):.0%} away from its {float(t):.0%} target")
            quarter = _quarter(ctx.now)
            if detail["last_quarterly"] != quarter:
                detail["last_quarterly"] = quarter
                if max(abs(d) for d in drift.values()) > D(m.get("quarterly_band", 0.01)):
                    why.append(f"quarterly rebalance ({quarter})")

        investable = total * (1 - buffer_pct)
        desired = {sym: targets.get(sym, ZERO) * investable for sym in values}

        if why:
            sells = []
            for sym in sorted(values):
                excess = values[sym] - desired[sym]
                if excess >= min_trade or (sym in strays and values[sym] > 0):
                    held = view.holdings.get(sym, ZERO)
                    qty = held if sym in strays or excess >= values[sym] else min(held, shares_floor(excess / prices[sym]))
                    if qty > 0:
                        sells.append(OrderIntent(ctx.manager_id, ctx.book, sym, "sell", quantity=qty, reason=f"rebalance: {'; '.join(why)}"))
            sells, paced = self._pace(sells, ctx, total, prices)
            if sells:
                names = ", ".join(f"{i.symbol} ({i.quantity} sh)" for i in sells)
                return RunResult(
                    f"Rebalancing because {'; '.join(why)}. Selling {names}; will buy underweight funds with the proceeds.",
                    intents=sells,
                    detail={**detail, "rebalance_reasons": why},
                    rerun_after_minutes=10,
                    activity="rebalancing",
                )

        available = view.cash - buffer_pct * total
        deficits = {sym: desired[sym] - values.get(sym, ZERO) for sym in targets if desired[sym] - values.get(sym, ZERO) > 0}
        buys = self._allocate(available, deficits, min_trade)
        if buys:
            paced_intents, paced = self._pace(
                [OrderIntent(ctx.manager_id, ctx.book, sym, "buy", notional=amt, reason="") for sym, amt in buys.items()], ctx, total, prices
            )
            buys = {i.symbol: i.notional for i in paced_intents if i.notional and i.notional >= min_trade}
        if buys:
            intents = [OrderIntent(ctx.manager_id, ctx.book, sym, "buy", notional=amt, reason="invest toward targets" + (f" ({'; '.join(why)})" if why else "")) for sym, amt in sorted(buys.items())]
            names = ", ".join(f"${amt} {sym}" for sym, amt in sorted(buys.items()))
            return RunResult(
                f"Investing ${sum(buys.values(), ZERO)} of free cash toward targets: {names}.",
                intents=intents,
                detail={**detail, "rebalance_reasons": why},
                activity="investing",
            )
        status = "within bands" if not why else "nothing large enough to trade"
        return RunResult(
            f"Holding: portfolio {status}; ${cents(max(available, ZERO))} free cash above the buffer.",
            detail={**detail, "rebalance_reasons": why},
            activity="holding",
        )

    @staticmethod
    def _pace(intents: list[OrderIntent], ctx: ManagerContext, total: Decimal, prices: dict[str, Decimal]) -> tuple[list[OrderIntent], bool]:
        """Crash pacing: deep in a drawdown, cap each trade so rebalancing is spread over several days."""
        pacing = ctx.mandate.get("crash_pacing") or {}
        if not pacing or ctx.drawdown < float(pacing.get("threshold", 1.0)):
            return intents, False
        cap = total * D(pacing.get("max_trade_pct_of_sleeve", 0.10))
        out = []
        for i in intents:
            if i.side == "buy":
                out.append(OrderIntent(i.manager_id, i.book, i.symbol, "buy", notional=min(i.notional, cap).quantize(D("0.01")), reason=i.reason + " (crash pacing)"))
            else:
                qty = min(i.quantity, shares_floor(cap / prices[i.symbol]))
                if qty > 0:
                    out.append(OrderIntent(i.manager_id, i.book, i.symbol, "sell", quantity=qty, reason=i.reason + " (crash pacing)"))
        return out, True

    @staticmethod
    def _allocate(available: Decimal, deficits: dict[str, Decimal], min_trade: Decimal) -> dict[str, Decimal]:
        """Split cash across deficits proportionally, dropping and redistributing sub-minimum buys."""
        if available < min_trade or not deficits:
            return {}
        live = dict(deficits)
        while live:
            need = sum(live.values(), ZERO)
            budget = min(available, need)
            plan = {sym: (budget * d / need).quantize(Decimal("0.01"), rounding=ROUND_DOWN) for sym, d in live.items()}
            small = [sym for sym, amt in plan.items() if amt < min_trade]
            if not small:
                return {sym: amt for sym, amt in plan.items() if amt > 0}
            # Drop the smallest deficit and retry so the cash concentrates where it matters most.
            live.pop(min(small, key=lambda x: live[x]))
        return {}
