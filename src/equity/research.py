"""Floor 1 for stock challengers: nightly research sprints (the forex Academy's method on daily stock bars).

A sprint (one per research agent per night):
  1. Candidates around the current version: each setting moved one or two steps (or switched
     to another choice), dropping the name with the weakest evidence, and two-setting
     combinations. Settings tried on earlier nights are skipped.
  2. Score each candidate on the research window only (before the locked final year), split
     into four periods: it must hold up across periods, not just in total.
  3. The best candidate (or the current version if nothing beats it) goes through the gates:
     full validation and the day-1 test (30 fresh $10,000 accounts on random start dates).
  4. Passes every gate -> new version, promoted to Floor 2 (paper trading)
     Clearly better on research data -> new version, stays on Floor 1
     Otherwise -> unchanged
"""

from __future__ import annotations

import random
from typing import Any, Callable, Mapping

from equity.data import Panel
from equity.sim import Config, run, with_params
from equity.strategies import get as get_strategy
from equity.validation import to_epochs, validate, windows
from forex.research import describe, params_hash, research_score

BUDGET = 16
IMPROVE_BY = 0.25
RETIRE_AFTER = 14
MIN_SYMBOLS = 8
DAY1 = {"runs": 30, "horizon": 126, "min_profitable": 0.55, "min_p10": -0.08}  # 126 sessions = about 6 months


def generate(cfg: Config, *, tested: set[str], budget: int, rng: random.Random, evidence: Mapping[str, float] | None = None) -> list[tuple[str, dict[str, Any]]]:
    strat = get_strategy(cfg.strategy)
    base = dict(cfg.params)
    singles = [{**base, t.name: v} for t in strat.tunables if t.name in base for v in t.neighbors(base[t.name], steps=(-2, -1, 1, 2))]
    syms = list(base.get("symbols") or [])
    drops = []
    if len(syms) > MIN_SYMBOLS:
        ranked = sorted(syms, key=lambda s: (evidence or {}).get(s, 0.0))
        drops = [{**base, "symbols": [x for x in syms if x != s]} for s in ranked]
    pairs = []
    tun = [t for t in strat.tunables if t.name in base]
    for _ in range(budget * 2):
        if len(tun) < 2:
            break
        a, b = rng.sample(tun, 2)
        na, nb = a.neighbors(base[a.name]), b.neighbors(base[b.name])
        if na and nb:
            pairs.append({**base, a.name: rng.choice(na), b.name: rng.choice(nb)})
    rng.shuffle(singles)
    out, seen = [], set(tested) | {params_hash(base)}
    for p in drops[:1] + singles + drops[1:2] + pairs:
        h = params_hash(p)
        if h in seen:
            continue
        seen.add(h)
        out.append((_describe(base, p), p))
        if len(out) >= budget:
            break
    return out


def _describe(base: Mapping[str, Any], p: Mapping[str, Any]) -> str:
    b = {**base, "instruments": base.get("symbols")}
    q = {**p, "instruments": p.get("symbols")}
    b.pop("symbols", None)
    q.pop("symbols", None)
    return describe(b, q)


def day1_test(panel: Panel, cfg: Config, *, seed: int = 11, runs: int | None = None, prep: Any = None, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Start the agent on random historical dates, each with a fresh $10,000, and see how often it ends ahead."""
    w = windows(panel, cfg.params)
    if w is None:
        return {"passed": False, "error": "not enough history"}
    lo, hi = w["all"][0], len(panel.dates) - DAY1["horizon"]
    n = runs or DAY1["runs"]
    rng = random.Random(seed)
    starts = sorted(rng.randint(lo, hi) for _ in range(n)) if hi > lo else []
    prep = prep if prep is not None else get_strategy(cfg.strategy).prepare(panel, cfg.params)
    rets, dds = [], []
    for k, s in enumerate(starts):
        if progress and k % 10 == 0:
            progress(f"day-1 test {k + 1}/{n}")
        res = run(cfg, panel, s, s + DAY1["horizon"], prep=prep)
        last = res.equity[-1][1] if res.equity else res.capital
        rets.append(last / res.capital - 1)
        dds.append(res.metrics()["max_drawdown"])
    if not rets:
        return {"passed": False, "error": "not enough history"}
    srt = sorted(rets)
    share = sum(1 for x in rets if x > 0) / len(rets)
    median, p10 = srt[len(srt) // 2], srt[max(0, int(0.1 * (len(srt) - 1)))]
    passed = share >= DAY1["min_profitable"] and median > 0 and p10 >= DAY1["min_p10"]
    return {
        "passed": passed, "runs": len(rets), "horizon_sessions": DAY1["horizon"], "profitable_share": round(share, 3),
        "median_return": round(median, 4), "p10_return": round(p10, 4), "best_return": round(srt[-1], 4), "worst_return": round(srt[0], 4),
        "worst_drawdown": round(max(dds), 4),
        "rule": f">= {DAY1['min_profitable']:.0%} of starts profitable, median > 0, 10th percentile >= {DAY1['min_p10']:.0%}",
        "starts": [[panel.dates[s], round(r, 4)] for s, r in zip(starts, rets)],
    }


def sprint(panel: Panel, cfg: Config, *, tested: set[str], trials: int, day: str, evidence: Mapping[str, float] | None = None,
           progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    say = progress or (lambda s: None)
    w = windows(panel, cfg.params)
    if w is None:
        return {"outcome": "NO_DATA", "evaluated": [], "trials": 0, "verdict": "Waiting for enough daily history (4+ years) for most of its names."}
    rng = random.Random(f"{cfg.agent_id}:{day}")
    pool = generate(cfg, tested=tested, budget=BUDGET, rng=rng, evidence=evidence)
    strat = get_strategy(cfg.strategy)
    research = w["research"]
    ep = {"all": to_epochs(panel, w["all"]), "holdout": to_epochs(panel, w["holdout"])}

    def evaluate(params: Mapping[str, Any]) -> dict[str, Any]:
        c = with_params(cfg, params)
        ww = windows(panel, params) or w
        res = run(c, panel, max(ww["research"][0], research[0]), research[1])
        return research_score(res, ep)

    say("scoring the current version")
    current = evaluate(cfg.params)
    evaluated = []
    for k, (label, params) in enumerate(pool):
        say(f"candidate {k + 1}/{len(pool)}: {label}")
        evaluated.append({"label": label, "hash": params_hash(params), **evaluate(params), "_params": params})
    ranked = sorted(evaluated, key=lambda e: (e["score"], e["positive_folds"]), reverse=True)
    best = ranked[0] if ranked else None
    result: dict[str, Any] = {
        "day": day, "current": current, "tested": len(pool), "trials": len(pool) + 1,
        "evaluated": [{k: v for k, v in e.items() if k != "_params"} for e in ranked],
        "windows": {"research": [panel.dates[research[0]], panel.dates[research[1] - 1]], "holdout": [panel.dates[w["holdout"][0]], panel.dates[-1]]},
    }
    improves = bool(best) and best["score"] > current["score"] + IMPROVE_BY and best["positive_folds"] >= current["positive_folds"]
    gate_params = best["_params"] if improves else dict(cfg.params)
    gcfg = with_params(cfg, gate_params)
    say("validation (gates)")
    v = validate(panel, gcfg, trials=trials + len(pool) + 1, progress=say)
    say("day-1 test (gates)")
    d1 = day1_test(panel, gcfg, progress=say)
    passed = bool(v.get("passed")) and bool(d1.get("passed"))
    result.update({
        "best": {k: x for k, x in best.items() if k != "_params"} if best else None, "gated": "candidate" if improves else "current",
        "validation": v, "day1": d1, "passed": passed, "change": _describe(cfg.params, gate_params) if improves else None, "params": gate_params,
    })
    result["outcome"] = ("PROMOTE" if passed else "IMPROVED") if improves else ("PROMOTE_CURRENT" if passed else "NO_IMPROVEMENT")
    result["verdict"] = verdict(result)
    return result


def verdict(r: Mapping[str, Any]) -> str:
    v, d1 = r.get("validation") or {}, r.get("day1") or {}
    if v.get("status") == "NO_DATA":
        return "Waiting for enough daily history (4+ years) for most of its names."
    fails = [c["name"] for c in v.get("checks", []) if not c["passed"]]
    unseen = (v.get("metrics") or {}).get("unseen") or {}
    tail = (f"unseen data {(unseen.get('avg_r') or 0):+.2f}R over {unseen.get('trades') or 0} trades, "
            f"return {(unseen.get('return_pct') or 0):+.1%}; day-1 test {(d1.get('profitable_share') or 0):.0%} of starts profitable")
    if r["outcome"] in ("PROMOTE", "PROMOTE_CURRENT"):
        return f"Passed every gate ({tail}). Promoted to Floor 2 for paper trading."
    head = (f"Adopted '{r.get('change')}' (research score {r['current']['score']:+.2f} -> {r['best']['score']:+.2f})"
            if r["outcome"] == "IMPROVED" else "No candidate beat the current version")
    if not d1.get("passed") and not fails:
        fails = ["the day-1 test"]
    elif not d1.get("passed"):
        fails.append("the day-1 test")
    return f"{head}; still failing {', '.join(fails)} ({tail})."


def evidence_from(validation: Mapping[str, Any] | None) -> dict[str, float]:
    return {s: d.get("avg_r", 0.0) for s, d in ((validation or {}).get("by_symbol") or {}).items()}
