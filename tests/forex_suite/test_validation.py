from __future__ import annotations

import json

import pytest

from forex import validation
from forex.agents import FOUNDING_CLASS, default_params
from forex.backtest import AgentConfig
from forex.instruments import UNIVERSE, default_spec
from forex.market import MarketStore
from forex.research import day1_test, generate, params_hash, sprint

from tests.forex_suite import synth

SPECS = {n: default_spec(n) for n in UNIVERSE}


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    s = MarketStore(tmp_path_factory.mktemp("val") / "marketdata.db")
    synth.fill(s, ["EUR_USD", "GBP_USD", "USD_JPY", "EUR_GBP", "AUD_USD", "USD_CAD", "USD_CHF", "NZD_USD"], {"M15": 9500, "H1": 2400})
    return s


@pytest.fixture
def short_windows(monkeypatch):
    monkeypatch.setattr(validation, "YEAR", 20 * 86400)  # "hold-out year" = 20 days
    monkeypatch.setattr(validation, "HISTORY_YEARS", {"day": 4.0, "swing": 4.0})
    monkeypatch.setattr(validation, "MIN_TRADES", {"day": 5, "swing": 5})


def cfg(agent_id: str) -> AgentConfig:
    a = next(x for x in FOUNDING_CLASS if x["id"] == agent_id)
    return AgentConfig(a["id"], a["strategy"], default_params(a["strategy"]), a["envelope"])


def test_validation_report_shape(store, short_windows):
    c = cfg("kestrel")
    rep = validation.validate(store, c, SPECS, trials=3, baselines=3)
    json.dumps(rep)  # stored as JSON
    assert rep["status"] in ("PASSED", "FAILED") and len(rep["checks"]) == 6
    assert {c["name"] for c in rep["checks"]} == {"evidence", "expectancy", "hold-out", "skill", "cost stress", "tail risk"}
    w = rep["windows"]
    assert w["in_sample"][1] == w["out_of_sample"][0] and w["out_of_sample"][1] == w["holdout"][0] == w["all"][1] - 20 * 86400
    assert len(rep["baseline"]["avg_r"]) == 3 and 0 < rep["baseline"]["p_value"] <= 1
    assert rep["passed"] == all(x["passed"] for x in rep["checks"])


def test_validation_without_data():
    from tempfile import mkdtemp
    from pathlib import Path

    empty = MarketStore(Path(mkdtemp()) / "m.db")
    assert validation.validate(empty, cfg("atlas"), SPECS)["status"] == "NO_DATA"


def test_generate_explores_new_candidates_within_bounds():
    import random

    from forex.strategies import STRATEGIES

    c = cfg("mira")
    first = generate(c, tested=set(), budget=12, rng=random.Random(1), evidence={"AUD_USD": -20.0})
    assert len(first) == 12 and first[0][0] == "drop AUD_USD"  # the worst evidence is tried first
    hashes = {params_hash(p) for _, p in first}
    assert params_hash(c.params) not in hashes
    second = generate(c, tested=hashes, budget=12, rng=random.Random(2))
    assert not hashes & {params_hash(p) for _, p in second}  # later nights keep exploring
    for _, p in first + second:
        for t in STRATEGIES[c.strategy].tunables:
            if t.choices:
                assert p[t.name] in t.choices
            else:
                assert t.lo <= p[t.name] <= t.hi


def test_day1_test_runs_fresh_accounts_on_random_dates(store, short_windows, monkeypatch):
    import forex.research as research

    monkeypatch.setitem(research.DAY1, "horizon_days", {"day": 10, "swing": 20})
    out = day1_test(store, cfg("kestrel"), SPECS, runs=6)
    assert out["runs"] == 6 and 0 <= out["profitable_share"] <= 1 and out["worst_return"] <= out["median_return"] <= out["best_return"]
    assert out == day1_test(store, cfg("kestrel"), SPECS, runs=6)  # same dates every time (fair comparisons)


def test_sprint_never_chooses_on_the_holdout(store, short_windows, monkeypatch):
    import forex.research as research

    monkeypatch.setitem(research.DAY1, "horizon_days", {"day": 10, "swing": 20})
    monkeypatch.setitem(research.BUDGET, "day", 4)
    windows_seen = []
    real_run = research.run

    def spy(cfg_, data, specs, start, end, **k):
        windows_seen.append((start, end))
        return real_run(cfg_, data, specs, start, end, **k)

    monkeypatch.setattr(research, "run", spy)
    monkeypatch.setattr(research, "validate", lambda *a, **k: {"status": "FAILED", "passed": False, "score": 0, "checks": [{"name": "x", "passed": False}], "metrics": {}})
    c = cfg("kestrel")
    out = sprint(store, c, SPECS, tested=set(), trials=1, day="2026-10-13")
    hold_start = out["windows"]["holdout"][0]
    scoring = windows_seen[:5]  # current + 4 candidates
    assert all(end <= hold_start for _, end in scoring)
    assert out["outcome"] in ("IMPROVED", "NO_IMPROVEMENT") and out["tested"] == 4 and out["trials"] == 5
    assert out["verdict"]
