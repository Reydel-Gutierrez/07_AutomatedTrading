"""The Examiner for stock challengers: deterministic validation of one version on daily history.

Windows (trading days, ending at the latest bar):
  research       everything before the locked final year, after the strategy's warm-up
    in-sample      its first two thirds
    out-of-sample  its last third
  hold-out       the most recent 12 months (never used to choose settings)
One run covers all windows (settings are fixed), so nothing is re-fit.

Checks (all must pass):
  history      at least 4 years of bars after warm-up
  evidence     at least 40 trades on unseen data (out-of-sample + hold-out)
  expectancy   unseen average R above zero and profit factor at least 1.1
  sharpe       unseen Sharpe at least 0.5 after costs
  hold-out     the locked year's average R at least -0.05
  skill        beats 20 random-name baselines (same entry days, stops and exits; p <= 0.10)
  luck         trial-adjusted Sharpe at least 0.3: the unseen Sharpe minus what the best of all
               variants tried so far would show by chance
  cost stress  still profitable on unseen data with 10 bps more cost on every fill
  drawdown     at least 80% of rolling one-year windows stay within the desk's 10% sleeve stop
  cash account never 3 good-faith violations within 12 months (Robinhood restricts the account)
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Mapping

from equity.data import Panel
from equity.sim import Config, RandomNames, run
from equity.strategies import get as get_strategy, warmup
from forex.validation import trial_adjusted_sharpe

YEAR_SESSIONS = 252
MIN_YEARS = 4.0
MIN_TRADES = 40
BASELINES = 20
SKILL_P = 0.10
MIN_ADJ_SHARPE = 0.3
STRESS_BPS = 10.0
DD_LIMIT, DD_SHARE = 0.10, 0.80


def windows(panel: Panel, params: Mapping[str, Any]) -> dict[str, tuple[int, int]] | None:
    """Day-index windows [a, b). None when the history is too short."""
    # Starts once two thirds of the list has history; a name that listed later joins when it has its own.
    firsts = sorted(f for f in (panel.series[s].first() for s in params["symbols"] if s in panel.series) if f is not None)
    need = max(3, math.ceil(len(params["symbols"]) * 2 / 3))
    if len(firsts) < need:
        return None
    start = firsts[need - 1] + warmup(params)
    end = len(panel.dates)
    hold = end - YEAR_SESSIONS
    if (end - start) / YEAR_SESSIONS < MIN_YEARS + 1e-9 or hold <= start:
        return None
    oos = start + (hold - start) * 2 // 3
    return {"all": (start, end), "in_sample": (start, oos), "out_of_sample": (oos, hold), "holdout": (hold, end), "unseen": (oos, end), "research": (start, hold)}


def to_epochs(panel: Panel, w: tuple[int, int]) -> tuple[int, int]:
    t = panel.times
    return t[w[0]], (t[w[1]] if w[1] < len(t) else t[-1] + 1)


def rolling_dd_share(equity: list[tuple[int, float]], limit: float = DD_LIMIT, window: int = YEAR_SESSIONS, step: int = 21) -> float | None:
    eq = [e for _, e in equity]
    if len(eq) < window:
        return None
    ok = total = 0
    for a in range(0, len(eq) - window + 1, step):
        peak, mdd = eq[a], 0.0
        for e in eq[a:a + window]:
            peak = max(peak, e)
            mdd = max(mdd, 1 - e / peak if peak > 0 else 0)
        total += 1
        ok += mdd <= limit
    return ok / total if total else None


def validate(panel: Panel, cfg: Config, *, trials: int = 1, baselines: int = BASELINES, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    t0 = time.time()
    say = progress or (lambda s: None)
    w = windows(panel, cfg.params)
    if w is None:
        return {"status": "NO_DATA", "passed": False, "checks": [], "error": f"needs {MIN_YEARS:.0f}+ years of daily bars for most of its names"}
    strat = get_strategy(cfg.strategy)
    say("backtesting")
    prep = strat.prepare(panel, cfg.params)
    start, end = w["all"]
    res = run(cfg, panel, start, end, prep=prep)
    ep = {k: to_epochs(panel, v) for k, v in w.items()}
    m = {k: res.metrics(*v) for k, v in ep.items()}
    unseen = m["unseen"]
    base = []
    for seed in range(baselines):
        say(f"random baseline {seed + 1}/{baselines}")
        b = run(cfg, panel, start, end, strategy=RandomNames(strat, panel, seed + 1), prep=prep).metrics(*ep["unseen"])
        base.append(b["return_pct"] or 0.0)
    mine = unseen["return_pct"] or 0.0
    p_value = (1 + sum(1 for b in base if b >= mine)) / (baselines + 1)
    say("cost stress")
    stress = run(cfg, panel, start, end, extra_cost_bps=STRESS_BPS, prep=prep).metrics(*ep["unseen"])
    dd_share = rolling_dd_share(res.equity)
    worst_gfv = max((sum(1 for u in res.gfv if g <= u < g + 365 * 86400) for g in res.gfv), default=0)
    adj = trial_adjusted_sharpe(unseen["sharpe"], trials, unseen["years"])

    def check(name: str, value: Any, rule: str, ok: bool) -> dict[str, Any]:
        return {"name": name, "value": value, "rule": rule, "passed": bool(ok)}

    pf = unseen["profit_factor"]
    years = (end - start) / YEAR_SESSIONS
    checks = [
        check("history", round(years, 1), f">= {MIN_YEARS:.0f} years after warm-up", years >= MIN_YEARS),
        check("evidence", unseen["trades"], f">= {MIN_TRADES} unseen trades", unseen["trades"] >= MIN_TRADES),
        check("expectancy", _r(unseen["avg_r"]), "avg R > 0 and profit factor >= 1.1 on unseen data", (unseen["avg_r"] or 0) > 0 and pf is not None and pf >= 1.1),
        check("sharpe", _r(unseen["sharpe"]), "unseen Sharpe >= 0.5 after costs", (unseen["sharpe"] or 0) >= 0.5),
        check("hold-out", _r(m["holdout"]["avg_r"]), "locked final year avg R >= -0.05", m["holdout"]["avg_r"] is not None and m["holdout"]["avg_r"] >= -0.05),
        check("skill", round(p_value, 3), f"beats {baselines} random-name baselines (p <= {SKILL_P})", p_value <= SKILL_P),
        check("luck", _r(adj), f"trial-adjusted Sharpe >= {MIN_ADJ_SHARPE} ({trials} variants tried)", adj is not None and adj >= MIN_ADJ_SHARPE),
        check("cost stress", stress["net_usd"], f"net profit on unseen data with +{STRESS_BPS:.0f} bps per fill", stress["net_usd"] > 0),
        check("drawdown", _r(dd_share, 3), f">= {DD_SHARE:.0%} of rolling 1-year windows within a {DD_LIMIT:.0%} drawdown", dd_share is not None and dd_share >= DD_SHARE),
        check("cash account", worst_gfv, "fewer than 3 good-faith violations in any 12 months", worst_gfv < 3),
    ]
    passed = all(c["passed"] for c in checks)
    score = (unseen["avg_r"] or 0.0) * math.sqrt(max(unseen["trades"], 0))
    eq = res.equity[:: max(1, len(res.equity) // 400)]
    return {
        "status": "PASSED" if passed else "FAILED", "passed": passed, "score": round(score, 4), "checks": checks,
        "windows": {k: [panel.dates[v[0]], panel.dates[min(v[1], len(panel.dates)) - 1]] for k, v in w.items()},
        "metrics": {k: _clean(v) for k, v in m.items()},
        "baseline": {"return_pct": [round(b, 4) for b in base], "p_value": round(p_value, 4)},
        "stress": _clean(stress), "dd_within_limit_share": _r(dd_share, 3), "trial_adjusted_sharpe": _r(adj), "trials": trials,
        "refused": res.refused, "good_faith_violations": len(res.gfv), "by_symbol": by_symbol([t for t in res.trades if t.closed_at >= ep["unseen"][0]]),
        "equity": [[t, round(e, 2)] for t, e in eq], "seconds": round(time.time() - t0, 1),
    }


def by_symbol(trades) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for t in trades:
        d = out.setdefault(t.instrument, {"trades": 0, "net_usd": 0.0, "sum_r": 0.0})
        d["trades"] += 1
        d["net_usd"] += t.net_usd
        d["sum_r"] += t.r
    for d in out.values():
        d["avg_r"] = round(d.pop("sum_r") / d["trades"], 4)
        d["net_usd"] = round(d["net_usd"], 2)
    return out


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, n)


def _clean(m: dict[str, Any]) -> dict[str, Any]:
    return {k: (_r(v) if isinstance(v, float) else v) for k, v in m.items()}
