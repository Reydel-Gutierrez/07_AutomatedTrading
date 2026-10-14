"""Floor 1: research sprints. How an agent's strategy gets polished, every night, by itself.

A sprint (one per research agent per night, on the Pi):
  1. Generate candidates around the current version: each setting moved one or two steps
     (or switched to another choice, e.g. a slower timeframe or a cost filter), dropping
     the pair with the worst evidence, and a sample of two-setting combinations. Settings
     already tried on an earlier night are skipped, so sprints keep exploring.
  2. Score every candidate on the research window only (all history before the locked
     final year), split into four consecutive folds: a candidate must hold up across
     periods, not just in total.
  3. Take the best candidate through the gates: the Examiner's full validation and the
     random day-1 test (30 fresh $10,000 starts on random historical dates).
  4. Decide:  passes every gate               -> new version, promoted to Floor 2 (paper)
              clearly better on research data -> new version, stays on Floor 1
              otherwise                       -> unchanged
Every candidate counts as a trial; the trial-adjusted Sharpe in validation gets stricter
the more an agent has tried. The locked final year is used only as a gate, never to choose.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from typing import Any, Callable, Mapping

from forex.backtest import AgentConfig, load_data, run
from forex.instruments import Spec
from forex.market import MarketStore
from forex.strategies import get as get_strategy
from forex.validation import YEAR, data_end, validate, windows, with_params

FOLDS = 4
BUDGET = {"day": 16, "swing": 24}  # candidates per nightly sprint (sized for the Pi)
DAY1 = {"runs": 30, "horizon_days": {"day": 90, "swing": 180}, "min_profitable": 0.55, "min_p10": -0.08}
IMPROVE_BY = 0.25  # research-score margin a candidate needs over the current version
RETIRE_AFTER = 14  # sprints without passing before the report recommends retirement


def params_hash(params: Mapping[str, Any]) -> str:
    return hashlib.sha1(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]


def describe(base: Mapping[str, Any], params: Mapping[str, Any]) -> str:
    parts = []
    for k in sorted(set(base) | set(params)):
        if base.get(k) != params.get(k):
            if k == "instruments":
                gone = [i for i in base.get(k, []) if i not in params.get(k, [])]
                added = [i for i in params.get(k, []) if i not in base.get(k, [])]
                parts.append(", ".join([f"drop {g}" for g in gone] + [f"add {a}" for a in added]))
            else:
                parts.append(f"{k} {base.get(k)} -> {params.get(k)}")
    return "; ".join(parts) or "no change"


def generate(cfg: AgentConfig, *, tested: set[str], budget: int, rng: random.Random, evidence: Mapping[str, float] | None = None) -> list[tuple[str, dict[str, Any]]]:
    strat = get_strategy(cfg.strategy)
    base = dict(cfg.params)
    singles: list[dict[str, Any]] = []
    for t in strat.tunables:
        if t.name not in base:
            continue
        for v in t.neighbors(base[t.name], steps=(-2, -1, 1, 2)):
            singles.append({**base, t.name: v})
    drops: list[dict[str, Any]] = []
    insts = list(base.get("instruments") or [])
    if len(insts) >= 3:
        ranked = sorted(insts, key=lambda i: (evidence or {}).get(i, 0.0))  # worst evidence first
        drops = [{**base, "instruments": [x for x in insts if x != i]} for i in ranked]
    pairs: list[dict[str, Any]] = []
    tun = [t for t in strat.tunables if t.name in base]
    for _ in range(budget * 2):
        if len(tun) < 2:
            break
        a, b = rng.sample(tun, 2)
        na, nb = a.neighbors(base[a.name]), b.neighbors(base[b.name])
        if na and nb:
            pairs.append({**base, a.name: rng.choice(na), b.name: rng.choice(nb)})
    rng.shuffle(singles)
    ordered = drops[:1] + singles + drops[1:] + pairs
    out, seen = [], set(tested) | {params_hash(base)}
    for p in ordered:
        h = params_hash(p)
        if h in seen:
            continue
        seen.add(h)
        out.append((describe(base, p), p))
        if len(out) >= budget:
            break
    return out


def research_score(res, w: Mapping[str, tuple[int, int]]) -> dict[str, Any]:
    """t-like score over the research window plus how many of the four folds were positive."""
    start, end = w["all"][0], w["holdout"][0]
    trades = [t for t in res.trades if start <= t.closed_at < end]
    n = len(trades)
    avg = sum(t.r for t in trades) / n if n else 0.0
    edges = [start + (end - start) * k // FOLDS for k in range(FOLDS + 1)]
    folds = []
    for a, b in zip(edges, edges[1:]):
        ft = [t.r for t in trades if a <= t.closed_at < b]
        folds.append(round(sum(ft) / len(ft), 4) if ft else None)
    positive = sum(1 for f in folds if f is not None and f > 0)
    return {"score": round(avg * math.sqrt(n), 4) if n else 0.0, "avg_r": round(avg, 4), "trades": n, "folds": folds, "positive_folds": positive}


def day1_test(store: MarketStore, cfg: AgentConfig, specs: Mapping[str, Spec], *, seed: int = 11, data=None, runs: int | None = None,
              progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Drop the agent into history on random dates, each with a fresh $10,000, and see how often it ends ahead."""
    end = data_end(store, cfg)
    if end is None:
        return {"passed": False, "error": "no data"}
    style = cfg.envelope.style
    w = windows(end, style)
    horizon = DAY1["horizon_days"][style] * 86400
    lo, hi = w["all"][0], end - horizon
    n = runs or DAY1["runs"]
    rng = random.Random(seed)
    starts = sorted(rng.randint(lo, hi) for _ in range(n)) if hi > lo else []
    data = data or load_data(store, cfg, lo, end)
    rets, dds = [], []
    for i, s in enumerate(starts):
        if progress and i % 10 == 0:
            progress(f"day-1 test {i + 1}/{n}")
        r = run(cfg, data, specs, s, s + horizon)
        m = r.metrics()
        rets.append(m["return_pct"] or 0.0)
        dds.append(m["max_drawdown"])
    if not rets:
        return {"passed": False, "error": "not enough history"}
    srt = sorted(rets)
    share = sum(1 for x in rets if x > 0) / len(rets)
    median = srt[len(srt) // 2]
    p10 = srt[max(0, int(0.1 * (len(srt) - 1)))]
    passed = share >= DAY1["min_profitable"] and median > 0 and p10 >= DAY1["min_p10"]
    return {
        "passed": passed, "runs": len(rets), "horizon_days": DAY1["horizon_days"][style], "profitable_share": round(share, 3),
        "median_return": round(median, 4), "p10_return": round(p10, 4), "best_return": round(srt[-1], 4), "worst_return": round(srt[0], 4),
        "worst_drawdown": round(max(dds), 4), "rule": f">= {DAY1['min_profitable']:.0%} of starts profitable, median > 0, 10th percentile >= {DAY1['min_p10']:.0%}",
        "starts": [[s, round(r, 4)] for s, r in zip(starts, rets)],
    }


def sprint(store: MarketStore, cfg: AgentConfig, specs: Mapping[str, Spec], *, tested: set[str], trials: int, day: str,
           evidence: Mapping[str, float] | None = None, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    say = progress or (lambda s: None)
    end = data_end(store, cfg)
    if end is None:
        return {"outcome": "NO_DATA", "evaluated": [], "trials": 0}
    style = cfg.envelope.style
    w = windows(end, style)
    rng = random.Random(f"{cfg.agent_id}:{day}")
    pool = generate(cfg, tested=tested, budget=BUDGET[style], rng=rng, evidence=evidence)
    union = sorted({i for _, p in pool for i in p["instruments"]} | set(cfg.params["instruments"]))
    # candidates may switch timeframe: load every feed any candidate needs, for the full history
    data: dict = {}
    for p in [cfg.params] + [q for _, q in pool]:
        for k, v in load_data(store, with_params(cfg, {**p, "instruments": union}), w["all"][0], end).items():
            data.setdefault(k, v)
    research = (w["all"][0], w["holdout"][0])

    def evaluate(params: Mapping[str, Any]) -> dict[str, Any]:
        return research_score(run(with_params(cfg, params), data, specs, *research), w)

    say("scoring the current version")
    current = evaluate(cfg.params)
    evaluated = []
    for i, (label, params) in enumerate(pool):
        say(f"candidate {i + 1}/{len(pool)}: {label}")
        evaluated.append({"label": label, "hash": params_hash(params), **evaluate(params), "_params": params})
    ranked = sorted(evaluated, key=lambda e: (e["score"], e["positive_folds"]), reverse=True)
    best = ranked[0] if ranked else None
    trials_now = trials + len(pool) + 1
    result: dict[str, Any] = {
        "day": day, "current": current, "tested": len(pool), "trials": len(pool) + 1,
        "evaluated": [{k: v for k, v in e.items() if k != "_params"} for e in ranked],
        "windows": {"research": list(research), "holdout": list(w["holdout"])},
    }
    if best is None:
        return {**result, "outcome": "NO_CANDIDATES", "verdict": "Nothing new left to try around this version."}
    improves = best["score"] > current["score"] + IMPROVE_BY and best["positive_folds"] >= current["positive_folds"]
    # gates: the candidate if it improves, otherwise the current version (so every agent gets a verdict each night)
    gate_params = best["_params"] if improves else cfg.params
    gcfg = with_params(cfg, gate_params)
    say("validation (gates)")
    v = validate(store, gcfg, specs, trials=trials_now)
    say("day-1 test (gates)")
    d1 = day1_test(store, gcfg, specs, data=data, progress=say)
    passed = bool(v.get("passed")) and bool(d1.get("passed"))
    result.update({
        "best": {k: v_ for k, v_ in best.items() if k != "_params"}, "gated": "candidate" if improves else "current",
        "validation": v, "day1": d1, "passed": passed,
        "change": describe(cfg.params, gate_params) if improves else None, "params": gate_params,
    })
    if improves:
        result["outcome"] = "PROMOTE" if passed else "IMPROVED"
    else:
        result["outcome"] = "PROMOTE_CURRENT" if passed else "NO_IMPROVEMENT"
    result["verdict"] = verdict(result)
    return result


def verdict(r: Mapping[str, Any]) -> str:
    v, d1 = r.get("validation") or {}, r.get("day1") or {}
    fails = [c["name"] for c in v.get("checks", []) if not c["passed"]]
    unseen = (v.get("metrics") or {}).get("unseen") or {}
    tail = f"unseen data {(unseen.get('avg_r') or 0):+.2f}R over {unseen.get('trades') or 0} trades; day-1 test {(d1.get('profitable_share') or 0):.0%} of starts profitable"
    if r["outcome"] in ("PROMOTE", "PROMOTE_CURRENT"):
        return f"Passed every gate ({tail}). Promoted to Floor 2 for paper trading."
    head = f"Adopted '{r.get('change')}' (research score {r['current']['score']:+.2f} -> {r['best']['score']:+.2f})" if r["outcome"] == "IMPROVED" else "No candidate beat the current version"
    return f"{head}; still failing {', '.join(fails) or 'the day-1 test'} ({tail})."
