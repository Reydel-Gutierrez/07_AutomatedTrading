"""The Examiner: deterministic validation of an agent version on OANDA history.

Windows (ending at the latest stored bar):
  in-sample      the first two thirds of the period before the hold-out
  out-of-sample  the last third before the hold-out
  hold-out       the most recent 12 months (never used to choose parameters)
One backtest covers all three (parameters are fixed), so no window is re-fit.

Checks (all must pass for VALIDATED):
  evidence        enough out-of-sample + hold-out trades
  expectancy      out-of-sample average R above zero, profit factor at least 1.05
  hold-out        the locked year is not losing (average R >= -0.05)
  skill           beats a random-direction baseline with identical entry times, stops,
                  targets and exits (p <= 0.15 over the baseline runs)
  cost stress     still profitable out of sample with 0.5 pip more slippage each way
  tail risk       95th percentile resampled drawdown at most 25%
A trial-adjusted Sharpe discounts the best-of-many effect from every version tried.
"""

from __future__ import annotations

import math
import time
from dataclasses import replace
from typing import Any, Callable, Mapping

from forex.backtest import AgentConfig, RandomDirection, bootstrap_drawdown, load_data, run
from forex.instruments import Spec
from forex.market import MarketStore
from forex.strategies import get as get_strategy

YEAR = 365 * 86400
HISTORY_YEARS = {"day": 2.5, "swing": 8.0}
MIN_TRADES = {"day": 60, "swing": 20}


def windows(end: int, style: str) -> dict[str, tuple[int, int]]:
    start = end - int(HISTORY_YEARS[style] * YEAR)
    hold = end - YEAR
    oos = start + (hold - start) * 2 // 3
    return {"all": (start, end), "in_sample": (start, oos), "out_of_sample": (oos, hold), "holdout": (hold, end), "unseen": (oos, end)}


def data_end(store: MarketStore, cfg: AgentConfig) -> int | None:
    trig = get_strategy(cfg.strategy).trigger(cfg.params)
    ends = [store.last_time(i, trig) for i in cfg.params["instruments"]]
    return None if not ends or any(e is None for e in ends) else min(ends)  # type: ignore[type-var]


def trial_adjusted_sharpe(sharpe: float | None, trials: int, years: float) -> float | None:
    """Subtract the Sharpe a best-of-N search would show by luck alone (expected max of N nulls)."""
    if sharpe is None or years <= 0:
        return None
    return sharpe - math.sqrt(2 * math.log(max(trials, 1))) / math.sqrt(years) if trials > 1 else sharpe


def validate(store: MarketStore, cfg: AgentConfig, specs: Mapping[str, Spec], *, trials: int = 1, baselines: int = 10,
             progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    t0 = time.time()
    style = cfg.envelope.style
    end = data_end(store, cfg)
    if end is None:
        return {"status": "NO_DATA", "passed": False, "checks": [], "error": "market data not loaded yet"}
    w = windows(end, style)
    start = w["all"][0]
    data = load_data(store, cfg, start, end)
    say = progress or (lambda s: None)
    say("backtesting")
    res = run(cfg, data, specs, start, end)
    m = {k: res.metrics(*v) for k, v in w.items()}
    unseen = [t for t in res.trades if w["unseen"][0] <= t.closed_at < end]
    strat = get_strategy(cfg.strategy)
    base_r = []
    for seed in range(baselines):
        say(f"random baseline {seed + 1}/{baselines}")
        b = run(cfg, data, specs, start, end, strategy=RandomDirection(strat, seed + 1)).metrics(*w["unseen"])
        base_r.append(b["avg_r"] if b["avg_r"] is not None else 0.0)
    my_r = m["unseen"]["avg_r"]
    p_value = (1 + sum(1 for b in base_r if my_r is None or b >= my_r)) / (baselines + 1)
    say("cost stress")
    stress = run(cfg, data, specs, start, end, extra_slip_pips=0.5).metrics(*w["unseen"])
    mc_dd = bootstrap_drawdown([t.r for t in unseen], cfg.envelope.risk_pct)
    oos, hold = m["unseen"], m["holdout"]
    adj = trial_adjusted_sharpe(oos["sharpe"], trials, oos["years"])

    def check(name: str, value: Any, rule: str, ok: bool) -> dict[str, Any]:
        return {"name": name, "value": value, "rule": rule, "passed": bool(ok)}

    pf = oos["profit_factor"]
    checks = [
        check("evidence", oos["trades"], f">= {MIN_TRADES[style]} unseen trades", oos["trades"] >= MIN_TRADES[style]),
        check("expectancy", _r(oos["avg_r"]), "avg R > 0 and profit factor >= 1.05 out of sample", (oos["avg_r"] or 0) > 0 and pf is not None and pf >= 1.05),
        check("hold-out", _r(hold["avg_r"]), "locked final year avg R >= -0.05", hold["avg_r"] is not None and hold["avg_r"] >= -0.05),
        check("skill", round(p_value, 3), f"beats {baselines} random-direction baselines (p <= 0.15)", p_value <= 0.15),
        check("cost stress", stress["net_usd"], "net profit with +0.5 pip slippage each way", stress["net_usd"] > 0),
        check("tail risk", _r(mc_dd), "95th pct resampled drawdown <= 25%", mc_dd is not None and mc_dd <= 0.25),
    ]
    passed = all(c["passed"] for c in checks)
    score = (oos["avg_r"] or 0.0) * math.sqrt(max(oos["trades"], 0))
    equity = res.equity[:: max(1, len(res.equity) // 400)]
    return {
        "status": "PASSED" if passed else "FAILED",
        "passed": passed,
        "score": round(score, 4),
        "checks": checks,
        "windows": {k: [v[0], v[1]] for k, v in w.items()},
        "metrics": {k: _clean(v) for k, v in m.items()},
        "baseline": {"avg_r": [round(b, 4) for b in base_r], "p_value": round(p_value, 4)},
        "stress": _clean(stress),
        "monte_carlo_dd95": _r(mc_dd),
        "trial_adjusted_sharpe": _r(adj),
        "trials": trials,
        "refused": res.refused,
        "by_instrument": by_instrument(unseen),
        "equity": [[t, round(e, 2)] for t, e in equity],
        "seconds": round(time.time() - t0, 1),
    }


def score_window(store: MarketStore, cfg: AgentConfig, specs: Mapping[str, Spec], data: Mapping, start: int, end: int) -> dict[str, Any]:
    res = run(cfg, data, specs, start, end)
    return res.metrics()


def by_instrument(trades) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for t in trades:
        d = out.setdefault(t.instrument, {"trades": 0, "net_usd": 0.0, "sum_r": 0.0})
        d["trades"] += 1
        d["net_usd"] += t.net_usd
        d["sum_r"] += t.r
    for d in out.values():
        d["avg_r"] = round(d["sum_r"] / d["trades"], 4)
        d["net_usd"] = round(d["net_usd"], 2)
        del d["sum_r"]
    return out


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, n)


def _clean(m: dict[str, Any]) -> dict[str, Any]:
    return {k: (_r(v) if isinstance(v, float) else v) for k, v in m.items()}


def with_params(cfg: AgentConfig, params: Mapping[str, Any]) -> AgentConfig:
    return replace(cfg, params=dict(params))
