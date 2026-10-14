from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from forex import indicators as ind
from forex.agents import ALL_AGENTS, FOUNDING_CLASS, default_params
from forex.backtest import AgentConfig, RandomDirection, bootstrap_drawdown, load_data, run
from forex.instruments import Converter, default_spec
from forex.market import MarketStore
from forex.sim import BarPrices, Book, Executor, Quote, RiskEnvelope
from forex.strategies import STRATEGIES
from forex.strategy import Close, Context, Modify, Open

from tests.forex_suite import synth

TUE_10_NY = int(datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc).timestamp())  # Tuesday 10:00 New York
SPECS = {n: default_spec(n) for n in ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "USD_CHF", "NZD_USD", "EUR_GBP", "EUR_JPY", "AUD_NZD", "EUR_CHF", "AUD_CAD"]}
CONV = Converter({"EUR_USD": 1.10, "GBP_USD": 1.27, "USD_JPY": 150.0, "AUD_USD": 0.66, "USD_CAD": 1.36, "USD_CHF": 0.88, "NZD_USD": 0.61})


# -- indicators -------------------------------------------------------------------------
def test_indicators_basic_values():
    x = [1.0, 2, 3, 4, 5, 6]
    assert ind.sma(x, 3)[2:] == [2, 3, 4, 5]
    e = ind.ema(x, 3)
    assert e[2] == 2 and e[3] == pytest.approx(3.0)
    assert ind.highest(x, 3)[2:] == [3, 4, 5, 6] and ind.lowest([5, 4, 3, 6, 7], 2)[1:] == [4, 3, 3, 6]
    r = ind.rsi([1.0 + i for i in range(30)], 14)
    assert r[-1] == 100.0 and r[13] is None
    assert ind.roc([1.0, 1.1, 1.21], 1)[2] == pytest.approx(0.1)


def test_indicators_are_causal():
    """Appending future data never changes past indicator values."""
    b = synth.bars("EUR_USD", "H1", 400)
    full = ind.adx(b.h, b.l, b.c, 14), ind.atr(b.h, b.l, b.c, 14), ind.rsi(b.c, 14), ind.bollinger(b.c, 20, 2)[1]
    part = ind.adx(b.h[:300], b.l[:300], b.c[:300], 14), ind.atr(b.h[:300], b.l[:300], b.c[:300], 14), ind.rsi(b.c[:300], 14), ind.bollinger(b.c[:300], 20, 2)[1]
    for f, p in zip(full, part):
        assert f[:300] == p


# -- paper broker and risk ---------------------------------------------------------------
def env(**kw):
    return RiskEnvelope(**{"style": "swing", "risk_pct": 0.01, "max_positions": 3, **kw})


def q(bid, spread=0.00012, t=TUE_10_NY):
    return Quote(bid, bid + spread, t)


def test_sizing_by_risk_then_capped_by_leverage():
    book = Book("a", 10_000)
    ex = Executor(env(), SPECS, 1, new_id=lambda: "p1")
    quotes = {"EUR_USD": q(1.1000)}
    # 1% risk ($100) over a 50-pip stop on EUR/USD -> 20,000 units (2.2x leverage)
    ev = ex.apply(book, [Open("EUR_USD", 1, stop=1.0950)], quotes, TUE_10_NY, CONV)
    p = book.positions["p1"]
    assert ev[0]["type"] == "POSITION_OPENED" and 19_000 < p.units < 20_100
    assert p.risk_usd == pytest.approx(100, rel=0.05)
    # a 5-pip stop would want ~200k units (22x); the 5:1 cap and margin cap bind instead
    book2 = Book("b", 10_000)
    Executor(env(), SPECS, 1, new_id=lambda: "p2").apply(book2, [Open("EUR_USD", 1, stop=1.0995)], quotes, TUE_10_NY, CONV)
    lev = book2.positions["p2"].units * 1.10 / 10_000
    assert lev <= 5.0 + 1e-9


def test_entry_gates():
    ex = Executor(env(max_positions=1), SPECS, 1)
    book = Book("a", 10_000)
    sat = int(datetime(2026, 10, 17, 14, tzinfo=timezone.utc).timestamp())
    assert ex.check_open(book, Open("EUR_USD", 1, 1.095), {"EUR_USD": q(1.1, t=sat)}, sat, CONV)[0] == "MARKET_WINDOW"
    assert ex.check_open(book, Open("EUR_USD", 1, 1.095), {"EUR_USD": q(1.1, spread=0.0005)}, TUE_10_NY, CONV)[0].startswith("SPREAD_WIDE")
    assert ex.check_open(book, Open("EUR_USD", 1, 1.1001), {"EUR_USD": q(1.1)}, TUE_10_NY, CONV)[0] == "STOP_TOO_CLOSE"
    assert ex.check_open(book, Open("EUR_USD", -1, 1.11, target=1.105), {"EUR_USD": q(1.1)}, TUE_10_NY, CONV)[0] == "TARGET_WRONG_SIDE"
    book.paused = "owner"
    assert ex.check_open(book, Open("EUR_USD", 1, 1.095), {"EUR_USD": q(1.1)}, TUE_10_NY, CONV)[0].startswith("PAUSED")
    day = Executor(env(style="day"), SPECS, 1)
    late = int(datetime(2026, 10, 13, 20, 30, tzinfo=timezone.utc).timestamp())  # 16:30 NY
    assert day.check_open(Book("d", 10_000), Open("EUR_USD", 1, 1.095), {"EUR_USD": q(1.1, t=late)}, late, CONV)[0] == "DAY_ENTRY_CUTOFF"


def test_currency_concentration_counts_direction():
    ex = Executor(env(max_same_currency=1), SPECS, 1)
    book = Book("a", 10_000)
    quotes = {"EUR_USD": q(1.1), "GBP_USD": q(1.27), "USD_JPY": q(150.0, spread=0.015)}
    ex.apply(book, [Open("EUR_USD", 1, 1.095)], quotes, TUE_10_NY, CONV)  # long EUR, short USD
    assert ex.check_open(book, Open("GBP_USD", 1, 1.265), quotes, TUE_10_NY, CONV)[0] == "CURRENCY_CONCENTRATION: USD"
    assert ex.check_open(book, Open("USD_JPY", 1, 149.5), quotes, TUE_10_NY, CONV)[0] is None  # long USD offsets


def test_stops_gap_targets_and_pnl_in_usd():
    ex = Executor(env(), SPECS, 1, new_id=lambda: "j")
    book = Book("a", 10_000)
    ex.apply(book, [Open("USD_JPY", -1, stop=151.0, target=148.0)], {"USD_JPY": q(150.0, spread=0.015)}, TUE_10_NY, CONV)
    p = book.positions["j"]
    # next bar gaps above the stop: filled at the worse open plus slippage
    bar = BarPrices(TUE_10_NY, TUE_10_NY + 900, 151.2, 151.3, 151.1, 151.2, 151.215, 151.315, 151.115, 151.215)
    ev = ex.check_exits(book, "USD_JPY", bar, CONV)
    t = book.closed[0]
    assert ev[0]["payload"]["reason"] == "stop (gap)" and t.exit == pytest.approx(151.215 + 0.002)
    assert t.pnl_usd == pytest.approx((150.0 - 0.001 - t.exit) * p.units / 150.0, rel=1e-9) and t.r < -1


def test_modify_through_price_closes_and_day_trader_flat_before_rollover():
    ex = Executor(env(style="day"), SPECS, 1, new_id=lambda: "x")
    book = Book("a", 10_000)
    quotes = {"EUR_USD": q(1.1)}
    ex.apply(book, [Open("EUR_USD", 1, stop=1.095)], quotes, TUE_10_NY, CONV)
    ex.apply(book, [Modify("x", stop=1.2)], quotes, TUE_10_NY + 60, CONV)
    assert not book.positions and book.closed[0].exit_reason == "stop moved through price"
    ex2 = Executor(env(style="day"), SPECS, 1, new_id=lambda: "y")
    b2 = Book("b", 10_000)
    ex2.apply(b2, [Open("EUR_USD", 1, stop=1.095)], quotes, TUE_10_NY, CONV)
    at_1650 = int(datetime(2026, 10, 13, 20, 50, tzinfo=timezone.utc).timestamp())
    ex2.time_rules(b2, at_1650, quotes, CONV)
    assert not b2.positions and b2.closed[0].exit_reason == "day trader flat before rollover"


def test_weekend_trim_and_financing():
    ex = Executor(env(weekend_hold=True, risk_pct=0.02), SPECS, 1)
    book = Book("a", 10_000)
    quotes = {"EUR_USD": q(1.1), "GBP_USD": q(1.27), "AUD_USD": q(0.66)}
    for inst, stop in (("EUR_USD", 1.08), ("GBP_USD", 1.25), ("AUD_USD", 0.645)):
        ex.apply(book, [Open(inst, 1, stop)], quotes, TUE_10_NY, CONV)
    fri = int(datetime(2026, 10, 16, 19, 45, tzinfo=timezone.utc).timestamp())  # Friday 15:45 NY
    ex.time_rules(book, fri, quotes, CONV)
    lev, loss, _ = ex.weekend_risk(book, quotes, CONV)
    assert lev <= 2.0 and loss <= 0.015 and book.closed
    tue = int(datetime(2026, 10, 13, 21, 5, tzinfo=timezone.utc).timestamp())  # Tue 17:05 NY
    assert ex.financing(book, tue, quotes, CONV) == []  # first call only starts the count
    before = book.cash
    wed = tue + 86400  # Wed 17:05 NY (rollover)
    ex.financing(book, wed, quotes, CONV)
    assert ex.financing(book, wed + 60, quotes, CONV) == []  # once per trading day
    if book.positions:
        assert book.cash != before


def test_drawdown_pause():
    ex = Executor(env(), SPECS, 1)
    book = Book("a", 10_000, peak_equity=10_000)
    book.cash = 8_900
    ev = ex.roll(book, TUE_10_NY, {}, CONV)
    assert book.paused and ev[0]["type"] == "AGENT_PAUSED"


# -- strategies, backtests, baseline -------------------------------------------------------
@pytest.fixture(scope="module")
def store(tmp_path_factory):
    s = MarketStore(tmp_path_factory.mktemp("md") / "marketdata.db")
    synth.fill(s, list(SPECS) + ["AUD_NZD", "EUR_CHF", "AUD_CAD"], {"M15": 9000, "H1": 2400, "H4": 1500, "D": 700})
    return s


@pytest.mark.parametrize("agent", ALL_AGENTS, ids=[a["id"] for a in ALL_AGENTS])
def test_every_founding_agent_backtests(store, agent):
    params = default_params(agent["strategy"])
    cfg = AgentConfig(agent["id"], agent["strategy"], params, agent["envelope"])
    trig = STRATEGIES[agent["strategy"]].trigger(params)
    first = store.first_time(params["instruments"][0], trig)
    last = store.last_time(params["instruments"][0], trig)
    span = {"M15": 86400 * 60, "H4": 86400 * 200, "D": 86400 * 300}[trig]
    start, end = last - span, last
    assert start > first
    data = load_data(store, cfg, start, end)
    res = run(cfg, data, SPECS, start, end)
    m = res.metrics()
    assert res.equity and all(math.isfinite(e) for _, e in res.equity)
    assert m["trades"] == len(res.trades)
    for t in res.trades:
        assert t.closed_at >= t.opened_at and t.units >= 1
    base = run(cfg, data, SPECS, start, end, strategy=RandomDirection(STRATEGIES[agent["strategy"]], 3))
    assert base.metrics()["trades"] >= 0


def test_strategy_never_sees_the_future(store):
    """A decision at time T is identical whether or not later bars exist in the data."""
    agent = FOUNDING_CLASS[0]
    params = default_params(agent["strategy"])
    cfg = AgentConfig("atlas", agent["strategy"], params, agent["envelope"])
    last = store.last_time("EUR_USD", "H4")
    data = load_data(store, cfg, last - 86400 * 120, last)
    strat = STRATEGIES[agent["strategy"]]
    for T in (last - 86400 * 60, last - 86400 * 30):
        views = {k: b.view(b.end_at(T)) for k, b in data.items()}
        trimmed = {k: store.bars(k[0], k[1], start=0, end=T - b.dur + 1) for k, b in data.items()}
        a = strat.on_bar(Context(T, params, views, [], 10_000, SPECS))
        b = strat.on_bar(Context(T, params, trimmed, [], 10_000, SPECS))
        assert [(x.instrument, x.side, round(x.stop, 8)) for x in a if isinstance(x, Open)] == [(x.instrument, x.side, round(x.stop, 8)) for x in b if isinstance(x, Open)]


def test_bootstrap_drawdown():
    assert bootstrap_drawdown([1.0, -1.0] * 3, 0.01) is None
    dd = bootstrap_drawdown([1.5, -1.0, -1.0, 2.0, -1.0] * 10, 0.01)
    assert 0 < dd < 0.2


def test_swing_backtest_converts_prices_across_the_whole_window(tmp_path):
    """Regression: swing validations must not depend on hourly history (stored for only ~3 years)."""
    s = MarketStore(tmp_path / "m.db")
    synth.fill(s, list(SPECS), {"H4": 2500, "D": 500})
    for inst in SPECS:  # hourly history covers only the last stretch
        s.upsert(inst, "H1", synth.series(inst, "H1", 500, start=synth.START + 2400 * 4 * 3600)[:200])
    agent = FOUNDING_CLASS[3]  # Orion trades crosses that need USD conversion
    params = default_params(agent["strategy"])
    cfg = AgentConfig("orion", agent["strategy"], params, agent["envelope"])
    last = s.last_time("EUR_USD", "H4")
    start = last - 86400 * 300
    res = run(cfg, load_data(s, cfg, start, last), SPECS, start, last)
    assert res.refused.get("NO_CONVERSION_PRICE", 0) == 0


def test_rollover_decisions_execute_after_the_window(tmp_path):
    """Regression: daily bars close at 17:00 NY, inside the no-entry rollover window; their
    decisions must execute 15 minutes later instead of being refused every time."""
    from forex.fxclock import execution_time, rollover_window

    five_pm = int(datetime(2026, 1, 13, 22, 0, tzinfo=timezone.utc).timestamp())  # 17:00 EST
    assert rollover_window(five_pm) and not rollover_window(execution_time(five_pm))
    assert execution_time(TUE_10_NY) == TUE_10_NY
    winter = MarketStore(tmp_path / "w.db")  # Jan-Mar 2024: every synthetic daily bar closes at 17:00 EST
    synth.fill(winter, ["EUR_USD", "GBP_USD"], {"D": 120, "H4": 720}, seed=3)
    agent = next(a for a in ALL_AGENTS if a["id"] == "sable")
    params = {**default_params(agent["strategy"]), "instruments": ["EUR_USD", "GBP_USD"], "entry_n": 5, "exit_n": 5, "trend_filter": False, "trend_ema": 10, "max_cost_r": 1.0}
    cfg = AgentConfig("sable", agent["strategy"], params, agent["envelope"])
    start, end = synth.START + 15 * 86400, synth.START + 68 * 86400  # Jan 16 - Mar 9, 2024: before daylight saving time
    res = run(cfg, load_data(winter, cfg, start, end), SPECS, start, end)
    assert res.trades, res.refused  # before the fix: zero trades, every entry refused as MARKET_WINDOW
