from __future__ import annotations

import json

import pytest

from forex import validation
from forex.agents import FOUNDING_CLASS, default_params
from forex.backtest import AgentConfig
from forex.instruments import UNIVERSE, default_spec
from forex.market import MarketStore
from forex.revision import candidates, review

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


def test_revision_candidates_stay_in_bounds_and_learn_from_instruments():
    c = cfg("mira")
    paper = {"EUR_GBP": {"trades": 12, "avg_r": -0.6}, "AUD_USD": {"trades": 10, "avg_r": 0.2}}
    cands = candidates(c, paper, {"EUR_GBP": {"trades": 30, "avg_r": -0.1}})
    labels = [label for label, _ in cands]
    assert any(label.startswith("drop EUR_GBP") for label in labels)
    from forex.strategies import STRATEGIES

    for _, p in cands:
        for t in STRATEGIES[c.strategy].tunables:
            assert t.lo <= p[t.name] <= t.hi


def test_review_never_uses_the_holdout(store, short_windows, monkeypatch):
    seen = []
    import forex.revision as rev

    real_load = rev.load_data

    def spy(store_, cfg_, start, end, *a, **k):
        seen.append(end)
        return real_load(store_, cfg_, start, end, *a, **k)

    monkeypatch.setattr(rev, "load_data", spy)
    monkeypatch.setattr(rev, "validate", lambda *a, **k: {"status": "FAILED", "passed": False, "score": -1, "checks": [{"passed": False}]})
    c = cfg("kestrel")
    out = review(store, c, SPECS, current_validation=None, trials=1)
    end = validation.data_end(store, c)
    assert seen and all(e <= end - 20 * 86400 for e in seen)
    assert out["outcome"] in ("KEPT", "REJECTED", "ADOPTED") and out["trials"] >= 2
