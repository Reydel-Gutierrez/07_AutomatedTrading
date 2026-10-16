"""Alex — Strategic Growth Manager. Systematic stock selection with AI research.

The screen decides; research can only veto. Each run:
  1. Screen the mandate's universe of large, liquid US stocks from stored daily
     closes: 12-1 momentum (return from ~12 months to ~1 month ago) divided by
     annualized volatility, kept only when the price is above its 200-day average
     and momentum is positive. Deterministic and reproducible.
  2. Research (a separate daily job, firm.research) reads the top candidates and
     records a verdict per name: approve or reject, with thesis, risks, and sources.
     A name is buyable only with a fresh approve. Research never adds names.
  3. Target portfolio: the best-ranked approved names, equal weight, capped per
     position, with a cash buffer.
  4. Every run, sell a holding whose trend broke (close below the 200-day average)
     or whose research turned to reject. On the first run of each month (or with new
     money), rebalance fully: sell names that left the target, then buy the
     underweights. Sales come first; the proceeds are invested on a follow-up run.
     A rebalance sale the desk did not carry out (deferred, blocked) is sent again on
     later runs that month while the name is still held and still out of the target;
     a name with a sell order already working is never sold twice.
Nothing smaller than the minimum trade is ever sent. The desk enforces the
universe, spread, order count, and the sleeve drawdown pause on top of this.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from firm.clock import CALENDAR, et, parse_hhmm
from firm.managers.base import ManagerContext, OrderIntent, RunResult
from firm.money import ZERO, D, cents

ID = "alex"

UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "JPM", "V", "MA", "UNH", "LLY", "JNJ",
    "XOM", "CVX", "PG", "HD", "COST", "WMT", "ABBV", "MRK", "PEP", "KO", "ADBE", "CRM", "ORCL", "AMD", "NFLX",
    "CSCO", "QCOM", "TXN", "AMAT", "MU", "LRCX", "NOW", "INTU", "IBM", "ACN", "TMO", "DHR", "ABT", "ISRG",
    "AMGN", "GILD", "VRTX", "REGN", "BAC", "WFC", "GS", "MS", "AXP", "SPGI", "BLK", "SCHW", "CAT", "DE", "GE",
    "HON", "UNP", "RTX", "LMT", "MCD", "SBUX", "NKE", "LOW", "TJX", "BKNG", "DIS", "CMCSA", "TMUS", "LIN",
    "NEE", "PLD", "PANW", "ANET", "UBER", "KLAC", "ETN", "PGR",
]

DEFAULT_MANDATE: dict[str, Any] = {
    "strategy": "momentum_quality_v1",
    "universe": UNIVERSE,
    "positions": 8,
    "max_position_pct": 0.15,
    "cash_buffer_pct": 0.02,
    "min_trade_usd": 5,
    "max_spread_bps": 30,
    "max_orders_per_day": 16,
    "run_time_et": "10:20",
    "momentum_lookback_days": 252,
    "momentum_skip_days": 21,
    "vol_days": 63,
    "trend_sma_days": 200,
    "research_candidates": 16,
    "research_max_age_days": 30,
    "research_daily_limit": 12,
    # Owner-approved risk policy for active strategies (docs/firm/DRAWDOWN_SAFEGUARDS.md).
    "drawdown_policy": "sleeve_pause",
    "sleeve_drawdown_pause": 0.2,
}

# Idle cash (above the buffer) worth investing between monthly rebalances.
TOP_UP_SHARE = Decimal("0.05")

REBALANCE_EXIT = "left the target portfolio at the monthly rebalance"


def validate_mandate(m: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    universe = m.get("universe") or []
    if not isinstance(universe, list) or not universe or not all(isinstance(s, str) and s.isalpha() and s.isupper() for s in universe):
        problems.append("universe must be a non-empty list of upper-case ticker symbols")
    checks = (
        ("positions", 1, 30), ("max_position_pct", 0.02, 0.5), ("cash_buffer_pct", 0, 0.2), ("min_trade_usd", 1, 1000),
        ("max_spread_bps", 1, 200), ("max_orders_per_day", 1, 60), ("momentum_lookback_days", 60, 400),
        ("momentum_skip_days", 0, 60), ("vol_days", 20, 260), ("trend_sma_days", 20, 300),
        ("research_candidates", 1, 60), ("research_max_age_days", 1, 120), ("research_daily_limit", 0, 60),
        ("sleeve_drawdown_pause", 0.05, 0.6),
    )
    for key, lo, hi in checks:
        if key in m and not (lo <= float(m[key]) <= hi):
            problems.append(f"{key} must be between {lo} and {hi}")
    if float(m.get("max_position_pct", 0.15)) * int(m.get("positions", 8)) < 1 - float(m.get("cash_buffer_pct", 0.02)) - 1e-9:
        problems.append("positions × max_position_pct must cover the invested share of the sleeve")
    try:
        parse_hhmm(str(m.get("run_time_et", "10:20")))
    except ValueError:
        problems.append("run_time_et must be HH:MM")
    return problems


def needs_strategy(m: Mapping[str, Any]) -> bool:
    """True while Alex's mandate is still the Phase 1 placeholder (no universe yet)."""
    return not m.get("universe")


# -- the screen (pure; shared with the research job) ---------------------------
def score(closes: Sequence[float], m: Mapping[str, Any]) -> dict[str, float] | None:
    look, skip = int(m.get("momentum_lookback_days", 252)), int(m.get("momentum_skip_days", 21))
    vol_days, sma_days = int(m.get("vol_days", 63)), int(m.get("trend_sma_days", 200))
    need = max(look + 1, sma_days, vol_days + 1)
    if len(closes) < need or min(closes[-need:]) <= 0:
        return None
    last = closes[-1]
    momentum = closes[-1 - skip] / closes[-1 - look] - 1
    rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - vol_days, len(closes))]
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)
    sma = sum(closes[-sma_days:]) / sma_days
    return {"momentum": momentum, "vol": vol, "sma": sma, "last": last, "trend": last > sma, "score": momentum / vol if vol > 0 else 0.0}


def screen(closes_by_symbol: Mapping[str, Sequence[float]], m: Mapping[str, Any]) -> list[tuple[str, dict[str, float]]]:
    """Eligible names (uptrend, positive momentum), best first."""
    out = []
    for sym in m.get("universe") or []:
        s = score(closes_by_symbol.get(sym) or [], m)
        if s and s["trend"] and s["momentum"] > 0:
            out.append((sym, s))
    return sorted(out, key=lambda kv: (-kv[1]["score"], kv[0]))


def fresh(note: Mapping[str, Any] | None, now: datetime, max_age_days: int) -> bool:
    if not note:
        return False
    return (now - datetime.fromisoformat(str(note["created_at"]))).total_seconds() <= max_age_days * 86400


class Alex:
    id = ID

    def symbols(self, mandate: Mapping[str, Any]) -> list[str]:
        return sorted(set(mandate.get("universe") or []))

    def due(self, now: datetime, mandate: Mapping[str, Any], last_scheduled_run: datetime | None) -> bool:
        session = CALENDAR.session_for(now)
        if session is None:
            return False
        local = et(now)
        if local.time() < parse_hhmm(str(mandate.get("run_time_et", "10:20"))) or local.time() >= session.close_time:
            return False
        return last_scheduled_run is None or et(last_scheduled_run).date() != local.date()

    def inputs(self, firm: Any, mandate: Mapping[str, Any]) -> dict[str, Any]:
        """Data the decision needs beyond live quotes: stored closes and research verdicts."""
        days = max(int(mandate.get("momentum_lookback_days", 252)), int(mandate.get("trend_sma_days", 200))) + 40
        from firm.research import Research

        book = firm.registry.get(ID).book
        return {
            "closes": firm.market.recent_closes(self.symbols(mandate), sessions=days),
            "research": Research(firm.db, clock=firm.clock).latest(ID),
            "rebalance_exits": rebalance_exits(firm.events, book, firm.clock()),
            "open_sells": sorted({r["symbol"] for r in firm.db.query(
                "SELECT symbol FROM orders WHERE manager_id = ? AND book = ? AND side = 'sell' AND status IN ('PENDING','SUBMITTED','UNKNOWN')", (ID, book))}),
        }

    def run(self, ctx: ManagerContext) -> RunResult:
        m = ctx.mandate
        view = ctx.view
        closes: Mapping[str, Sequence[float]] = ctx.extras.get("closes") or {}
        research: Mapping[str, Mapping[str, Any]] = ctx.extras.get("research") or {}
        min_trade = D(m.get("min_trade_usd", 5))
        buffer_pct = D(m.get("cash_buffer_pct", 0.02))
        n = int(m.get("positions", 8))
        cap_pct = D(m.get("max_position_pct", 0.15))
        max_age = int(m.get("research_max_age_days", 30))
        decided = set(ctx.extras.get("rebalance_exits") or ())
        working = set(ctx.extras.get("open_sells") or ())

        held = {s: q for s, q in view.holdings.items() if q > 0}
        missing = sorted(s for s in held if not view.price(s))
        if missing:
            return RunResult(f"Waiting for prices on {', '.join(missing)}; no trades.", activity="waiting for market data", detail={"missing_prices": missing})
        values = {s: q * view.price(s) for s, q in held.items()}  # type: ignore[operator]
        total = view.cash + sum(values.values(), ZERO)

        ranked = screen(closes, m)
        approved = [(s, sc) for s, sc in ranked if fresh(research.get(s), ctx.now, max_age) and research[s]["verdict"] == "approve" and view.price(s)]
        targets = [s for s, _ in approved[:n]]
        month = et(ctx.now).strftime("%Y-%m")
        last = ctx.last_run or {}
        rebalance_day = last.get("last_rebalance_month") != month or ctx.trigger in ("allocation_changed", "mode_changed", "mandate_changed") or not held
        detail: dict[str, Any] = {
            "nav": str(cents(total)),
            "cash": str(cents(view.cash)),
            "screened": len(ranked),
            "ranked": [{"symbol": s, "score": round(sc["score"], 3), "momentum": round(sc["momentum"], 4), "vol": round(sc["vol"], 4)} for s, sc in ranked[: int(m.get("research_candidates", 16))]],
            "approved": targets,
            "last_rebalance_month": last.get("last_rebalance_month"),
            "research_coverage": sum(1 for s, _ in ranked[: int(m.get("research_candidates", 16))] if fresh(research.get(s), ctx.now, max_age)),
        }
        if total < min_trade:
            return RunResult(f"Sleeve holds ${cents(total)}, below the ${min_trade} minimum trade; nothing to do.", activity="idle", detail=detail)
        if not closes:
            return RunResult("Waiting for price history to run the screen; no trades.", activity="waiting for market data", detail=detail)

        # 1. Exits that cannot wait for month end.
        exits: dict[str, str] = {}
        for sym in sorted(held):
            if sym in working:
                continue  # a sell is already at the broker; never sell twice
            sc = score(closes.get(sym) or [], m)
            note = research.get(sym)
            if sym not in (m.get("universe") or []):
                exits[sym] = "no longer in the universe"
            elif sc is not None and not sc["trend"]:
                exits[sym] = f"trend broke (close {sc['last']:.2f} below the {int(m.get('trend_sma_days', 200))}-day average {sc['sma']:.2f})"
            elif note and note.get("verdict") == "reject" and fresh(note, ctx.now, max_age):
                exits[sym] = "research now rejects it"
            elif targets and sym not in targets and (rebalance_day or sym in decided):
                exits[sym] = REBALANCE_EXIT  # decided this month but still held: the sale did not go through
        if exits:
            sells = [OrderIntent(ctx.manager_id, ctx.book, s, "sell", quantity=held[s], reason=why) for s, why in exits.items()]
            names = "; ".join(f"{s} ({why})" for s, why in exits.items())
            detail["exits"] = exits
            if rebalance_day:
                detail["last_rebalance_month"] = month
            return RunResult(f"Selling {names}. Proceeds are reinvested on the next run.", intents=sells, detail=detail, rerun_after_minutes=10, activity="rebalancing")

        if not targets:
            covered = detail["research_coverage"]
            why = "no screened names have a fresh research approval yet" if ranked else "no names pass the screen today"
            return RunResult(f"Holding cash: {why} ({len(ranked)} screened, {covered} researched).", detail=detail, activity="waiting for research" if ranked else "holding")

        # 2. Buys toward equal weight among the targets (and existing approved holdings).
        investable = total * (1 - buffer_pct)
        weight = min(Decimal(1) / Decimal(len(targets)), cap_pct)
        desired = {s: investable * weight for s in targets}
        available = view.cash - buffer_pct * total
        idle_enough = available >= max(min_trade, TOP_UP_SHARE * total)
        if not (rebalance_day or idle_enough):
            return RunResult(
                f"Holding {len(held)} names; next full rebalance next month. ${cents(max(available, ZERO))} free cash above the buffer.",
                detail=detail,
                activity="holding",
            )
        deficits = {s: desired[s] - values.get(s, ZERO) for s in targets if desired[s] - values.get(s, ZERO) >= min_trade}
        buys = _allocate(available, deficits, min_trade)
        if rebalance_day:
            detail["last_rebalance_month"] = month
        if not buys:
            return RunResult(f"Holding {', '.join(sorted(held)) or 'cash'}: the portfolio is at its targets.", detail=detail, activity="holding")
        intents = [
            OrderIntent(ctx.manager_id, ctx.book, s, "buy", notional=amt, reason=f"target {float(weight):.0%}: rank #{targets.index(s) + 1}, research approved")
            for s, amt in sorted(buys.items(), key=lambda kv: targets.index(kv[0]))
        ]
        names = ", ".join(f"${amt} {s}" for s, amt in sorted(buys.items(), key=lambda kv: targets.index(kv[0])))
        return RunResult(f"Investing ${sum(buys.values(), ZERO)} in research-approved momentum leaders: {names}.", intents=intents, detail=detail, activity="investing")


def rebalance_exits(events: Any, book: str | None, now: datetime) -> list[str]:
    """Names this month's rebalance decided to sell in this book (from Alex's run log)."""
    month = et(now).strftime("%Y-%m")
    out: set[str] = set()
    for ev in events.recent(manager_id=ID, types=["MANAGER_RUN"], limit=500):
        if et(datetime.fromisoformat(ev.ts)).strftime("%Y-%m") != month:
            break  # newest first: everything further back is an earlier month
        if ev.payload.get("book") != book:
            continue
        out |= {s for s, why in ((ev.payload.get("detail") or {}).get("exits") or {}).items() if why == REBALANCE_EXIT}
    return sorted(out)


def _allocate(available: Decimal, deficits: Mapping[str, Decimal], min_trade: Decimal) -> dict[str, Decimal]:
    """Split available cash across deficits, largest first, never below the minimum trade."""
    out: dict[str, Decimal] = {}
    left = available
    for sym, need in sorted(deficits.items(), key=lambda kv: -kv[1]):
        amt = cents(min(need, left))
        if amt >= min_trade:
            out[sym] = amt
            left -= amt
    return out

