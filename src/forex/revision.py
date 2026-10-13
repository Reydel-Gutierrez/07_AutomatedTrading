"""Controlled strategy revisions: how an agent learns from its results.

Once a week (and never more than every 21 days per agent), the reviewer:
  1. diagnoses the agent's paper trades and recent backtest by instrument;
  2. builds a small set of candidates: each tunable parameter one step up or down within
     its bounds, plus dropping the worst-performing instrument when the evidence says so;
  3. scores every candidate on the in-sample window and checks it on the out-of-sample
     window (the locked hold-out year is not used to choose);
  4. runs the Examiner's full validation on the best candidate.
A candidate is adopted as a new version only if it passes validation, or if the current
version is failing and the candidate passes more checks with a better score. Every
candidate counts as a trial, which raises the bar (trial-adjusted Sharpe) for later ones.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from forex.backtest import AgentConfig, load_data, run
from forex.instruments import Spec
from forex.market import MarketStore
from forex.strategies import get as get_strategy
from forex.validation import data_end, validate, windows, with_params

MIN_DAYS_BETWEEN = 21


def candidates(cfg: AgentConfig, paper_by_instrument: Mapping[str, Mapping[str, Any]] | None = None, backtest_by_instrument: Mapping[str, Mapping[str, Any]] | None = None) -> list[tuple[str, dict[str, Any]]]:
    strat = get_strategy(cfg.strategy)
    base = dict(cfg.params)
    out: list[tuple[str, dict[str, Any]]] = []
    for t in strat.tunables:
        cur = base.get(t.name)
        if cur is None:
            continue
        for direction in (-1, 1):
            v = cur + direction * t.step
            v = int(round(v)) if t.integer else round(v, 6)
            if t.lo <= v <= t.hi and v != cur:
                out.append((f"{t.name} {cur} -> {v}", {**base, t.name: v}))
    insts = list(base.get("instruments") or [])
    if len(insts) >= 3:
        evidence: dict[str, float] = {}
        for src in (paper_by_instrument or {}, backtest_by_instrument or {}):
            for inst, d in src.items():
                evidence[inst] = evidence.get(inst, 0.0) + float(d.get("avg_r") or 0.0) * int(d.get("trades") or 0)
        worst = min((i for i in insts if i in evidence), key=lambda i: evidence[i], default=None)
        if worst and evidence[worst] < 0:
            out.append((f"drop {worst} (net {evidence[worst]:+.1f}R in paper + backtest)", {**base, "instruments": [i for i in insts if i != worst]}))
    return out


def review(store: MarketStore, cfg: AgentConfig, specs: Mapping[str, Spec], *, current_validation: Mapping[str, Any] | None, trials: int,
           paper_by_instrument: Mapping[str, Mapping[str, Any]] | None = None, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    say = progress or (lambda s: None)
    end = data_end(store, cfg)
    if end is None:
        return {"outcome": "NO_DATA", "trials": 0}
    w = windows(end, cfg.envelope.style)
    start, hold = w["all"][0], w["holdout"][0]
    pool = candidates(cfg, paper_by_instrument, (current_validation or {}).get("by_instrument"))
    if not pool:
        return {"outcome": "NO_CANDIDATES", "trials": 0}
    # one data load covering every candidate's instruments
    wide = with_params(cfg, {**cfg.params, "instruments": sorted({i for _, p in pool for i in p["instruments"]} | set(cfg.params["instruments"]))})
    data = load_data(store, wide, start, hold)

    def evaluate(params: Mapping[str, Any]) -> dict[str, Any]:
        res = run(with_params(cfg, params), data, specs, start, hold)
        ins, oos = res.metrics(*w["in_sample"]), res.metrics(*w["out_of_sample"])
        return {"in_sample": _score(ins), "out_of_sample": _score(oos), "is_trades": ins["trades"], "oos_trades": oos["trades"]}

    say("scoring the current version")
    current = evaluate(cfg.params)
    scored = []
    for i, (label, params) in enumerate(pool):
        say(f"candidate {i + 1}/{len(pool)}: {label}")
        scored.append((label, params, evaluate(params)))
    used = len(pool) + 1
    eligible = [
        s for s in scored
        if s[2]["in_sample"] > current["in_sample"] + max(0.1 * abs(current["in_sample"]), 0.25) and s[2]["out_of_sample"] >= current["out_of_sample"]
    ]
    summary = {"current": current, "candidates": [{"label": label, **sc} for label, _, sc in scored], "trials": used}
    if not eligible:
        return {**summary, "outcome": "KEPT", "reason": "no candidate improved in-sample without giving up out-of-sample"}
    label, params, sc = max(eligible, key=lambda s: s[2]["in_sample"])
    say(f"validating {label}")
    v = validate(store, with_params(cfg, params), specs, trials=trials + used)
    cur_passed = bool(current_validation and current_validation.get("passed"))
    cur_checks = sum(c["passed"] for c in (current_validation or {}).get("checks", []))
    new_checks = sum(c["passed"] for c in v["checks"])
    adopt = v["passed"] or (not cur_passed and new_checks > cur_checks and v["score"] > float((current_validation or {}).get("score") or 0))
    return {
        **summary, "outcome": "ADOPTED" if adopt else "REJECTED", "chosen": label, "params": params, "validation": v,
        "reason": ("passed validation" if v["passed"] else f"improves a failing version ({new_checks} vs {cur_checks} checks passed)") if adopt else f"failed validation ({new_checks}/{len(v['checks'])} checks)",
    }


def _score(m: Mapping[str, Any]) -> float:
    """t-like score: average R times the square root of the trade count (0 with no trades)."""
    if not m.get("trades") or m.get("avg_r") is None:
        return 0.0
    return round(float(m["avg_r"]) * float(m["trades"]) ** 0.5, 4)
