"""The Stock Swing Class: indicators, the cash-account simulator, gates, the engine, and HQ's read-only view."""

from __future__ import annotations

import random
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from equity.academy import AcademyDB
from equity.agents import CLASS, wanted_symbols
from equity.data import build_panel, load_panel
from equity.engine import Engine
from equity.indicators import highest, rsi, sma
from equity.research import sprint
from equity.sim import Config, Envelope, run
from equity.strategies import STRATEGIES, Entry
from equity.validation import validate, windows


def sessions(n: int, start: date = date(2019, 1, 2)) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def noise_rows(symbols, n=1800, seed=3):
    rng, days, rows = random.Random(seed), sessions(n), {}
    for s in sorted(symbols):
        px, drift, vol, out = 100.0, rng.uniform(0, 0.0006), rng.uniform(0.01, 0.03), []
        for d in days:
            o = px * (1 + rng.gauss(0, vol / 4))
            c = o * (1 + rng.gauss(drift, vol))
            out.append((d, o, max(o, c) * (1 + abs(rng.gauss(0, vol / 3))), min(o, c) * (1 - abs(rng.gauss(0, vol / 3))), c))
            px = c
        rows[s] = out
    return rows


ALL = set(wanted_symbols())


def test_indicators_use_only_the_past():
    assert sma([1, 2, None, 3], 2)[3] == 2.5 and sma([1, 2], 3) == [None, None]
    assert highest([1, 5, 2, 3], 2) == [None, None, 5, 5]  # excludes today: a breakout compares with the prior days
    r = rsi([float(x) for x in range(1, 20)], 2)
    assert r[-1] == 100.0 and r[0] is None


class OneShot:
    """Buys SYM once at a fixed day's close with a fixed stop; never exits by rule."""

    key, title, description, defaults, tunables = "oneshot", "", "", {}, ()

    def __init__(self, day, stop):
        self.day, self.stop = day, stop

    def sizing(self, p):
        return 1, 0.5

    def prepare(self, panel, p):
        return {"SYM": {"c": panel.series["SYM"].c}}

    def update_stop(self, prep, i, pos, p):
        return None

    def exit(self, prep, i, pos, p):
        return None

    def entries(self, prep, i, p, held):
        return [Entry("SYM", self.stop, 0, "test")] if i == self.day and "SYM" not in held else []


def panel_of(bars):
    days = sessions(len(bars))
    return build_panel({"SPY": [(d, 1, 1, 1, 1) for d in days], "SYM": [(d, *b) for d, b in zip(days, bars)]})


def test_fills_at_the_next_open_and_stops_gap_at_the_open():
    bars = [(10, 10, 10, 10)] * 3 + [(11, 11.5, 10.5, 11), (9, 9, 8, 8.5)] + [(8.5, 8.5, 8.5, 8.5)] * 2
    res = run(Config("t", "oneshot", {}, Envelope(cost_bps=0)), panel_of(bars), 0, 7, strategy=OneShot(2, 9.5))
    (t,) = res.trades
    assert t.entry == 11 and t.exit == 9 and t.exit_reason == "stop"  # bought at day 3's open; day 4 opened below the stop
    assert round(t.net_usd, 2) == round(5000 / 11 * 9 - 5000, 2)


def test_refuses_a_buy_whose_next_open_is_below_its_stop():
    bars = [(10, 10, 10, 10)] * 3 + [(9, 9, 9, 9)] * 3
    res = run(Config("t", "oneshot", {}, Envelope(cost_bps=0)), panel_of(bars), 0, 6, strategy=OneShot(2, 9.5))
    assert not res.trades and not res.positions and res.refused == {"gapped_below_stop": 1}


def test_guards_cooldown_and_group_cap():
    panel = build_panel(noise_rows(ALL, n=900))
    p = dict(STRATEGIES["pullback_atr_v1"].defaults)
    loose = run(Config("j", "pullback_atr_v1", p, Envelope(cooldown_sessions=0, max_per_group=10)), panel, 300, 900)
    tight = run(Config("j", "pullback_atr_v1", p, Envelope(cooldown_sessions=5, max_per_group=1)), panel, 300, 900)
    assert tight.refused.get("cooldown", 0) > 0 and tight.refused.get("group_full", 0) > 0
    assert "cooldown" not in loose.refused and "group_full" not in loose.refused


def test_validation_needs_four_years_and_noise_does_not_pass():
    short = build_panel(noise_rows(ALL, n=700))
    p = dict(STRATEGIES["rotation_v1"].defaults)
    assert windows(short, p) is None
    assert validate(short, Config("m", "rotation_v1", p))["status"] == "NO_DATA"
    panel = build_panel(noise_rows(ALL, n=1800, seed=5))
    for a in CLASS:
        r = sprint(panel, Config(a["id"], a["strategy"], dict(STRATEGIES[a["strategy"]].defaults), a["envelope"]), tested=set(), trials=1, day="d")
        assert r["outcome"] in ("IMPROVED", "NO_IMPROVEMENT"), (a["id"], r["verdict"])
        names = {c["name"] for c in r["validation"]["checks"]}
        assert {"skill", "luck", "cost stress", "drawdown", "cash account", "hold-out"} <= names


@pytest.fixture
def setup(tmp_path, monkeypatch):
    firm_db = tmp_path / "firm" / "firm.db"
    firm_db.parent.mkdir()
    conn = sqlite3.connect(firm_db)
    conn.execute("CREATE TABLE market_bars (symbol TEXT NOT NULL, date TEXT NOT NULL, open TEXT, high TEXT, low TEXT, close TEXT NOT NULL, volume TEXT, PRIMARY KEY (symbol, date))")
    for sym, bars in noise_rows(ALL, n=1800, seed=9).items():
        conn.executemany("INSERT INTO market_bars VALUES (?,?,?,?,?,?,?)", [(sym, d, str(o), str(h), str(lo), str(c), "1") for d, o, h, lo, c in bars])
    conn.commit()
    conn.close()
    eq_dir = tmp_path / "equity_academy"
    (eq_dir / "inbox").mkdir(parents=True)
    monkeypatch.setenv("EQUITY_STATE_ROOT", str(eq_dir))
    monkeypatch.setenv("FIRM_STATE_ROOT", str(tmp_path / "firm"))
    now = [datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc).timestamp()]  # 02:00 New York
    eng = Engine(AcademyDB(eq_dir / "academy.db"), firm_db, eq_dir / "inbox", clock=lambda: now[0])
    return eng, now


def test_engine_enrolls_the_class_and_asks_for_its_bars(setup):
    eng, _ = setup
    eng.start()
    assert [a["id"] for a in eng.db.agents()] == ["juniper", "rook", "meridian", "talon"]
    assert all(a["stage"] == "RESEARCH" for a in eng.db.agents())
    from firm.api import equity_academy

    assert equity_academy.wanted_symbols() == wanted_symbols()


def test_engine_sprints_then_paper_replays_from_the_promotion_day(setup):
    eng, _ = setup
    eng.start()
    eng.load_data()
    assert eng.panel is not None and eng.panel.dates[-1] == load_panel(eng.firm_db, ["SPY"]).dates[-1]
    r = eng.run_sprint("juniper", "2026-10-17")
    assert eng.db.one("SELECT outcome FROM sprints WHERE agent_id = 'juniper'")["outcome"] == r["outcome"]
    # promoted at 01:30 New York on the session 40 back: its last known close is the session before
    promo_day = eng.panel.dates[-40]
    eng.db.set_stage("juniper", "PAPER", by="test", reason="test")
    eng.db.write("UPDATE agents SET stage_since = ? WHERE id = 'juniper'", (f"{promo_day}T05:30:00+00:00",))
    eng.paper("juniper")
    first = eng.db.one("SELECT min(date) AS d FROM paper_equity WHERE agent_id = 'juniper'")["d"]
    assert first == eng.panel.dates[-41]
    assert all(row["opened"] >= promo_day for row in eng.db.q("SELECT opened FROM paper_trades WHERE agent_id = 'juniper'"))


def test_one_academy_serves_both_classes(setup, tmp_path, monkeypatch):
    """Stock agents come through the Academy's own API in its shapes, tagged market=stocks."""
    eng, now = setup
    monkeypatch.setenv("FOREX_STATE_ROOT", str(tmp_path / "no-forex"))  # only the stock class exists here
    eng.start()
    eng.load_data()
    eng.run_sprint("juniper", "2026-10-17")
    eng.db.set_stage("rook", "PAPER", by="test", reason="test")
    eng.db.write("UPDATE agents SET stage_since = ? WHERE id = 'rook'", (f"{eng.panel.dates[-30]}T05:30:00+00:00",))
    eng.paper("rook")
    now[0] = time.time()  # HQ judges the engine online by the wall clock, not the test's fixed one
    eng.heartbeat()
    from firm.api import academy

    ov = academy.overview()
    assert ov["available"] and [a["name"] for a in ov["agents"]] == ["Juniper", "Rook", "Meridian", "Talon"]
    assert {a["market"] for a in ov["agents"]} == {"stocks"} and ov["stock_engine"]["online"]
    juniper = academy.agent("juniper")
    assert juniper["readiness"]["next"] == "Floor 2 · Paper trading" and juniper["validation"]["report"]["market"] == "stocks"
    assert isinstance(juniper["validation"]["report"]["windows"]["holdout"][0], int)  # epochs, like forex
    assert juniper["sprints"][0]["summary"]["verdict"]
    rook = academy.agent("rook")
    assert rook["stage"] == "PAPER" and rook["equity_series"] and rook["weekly"] and rook["readiness"]["next"].startswith("Live swing seat")
    assert all(t["side"] == 1 for t in rook["trades"])
    space = academy.space_block()
    assert [a["id"] for a in space["agents"]] == ["juniper", "rook", "meridian", "talon"] and space["agents"][1]["stage"] == "PAPER"
    assert academy.floor_analytics("PAPER")["agents"][0]["market"] == "stocks"
    assert academy.report("2026-10-17")["agents"][0]["name"] == "Juniper"
    assert academy.promotion_check("rook")["allowed"] is False
    academy.send_command("talon", "pause", by="reydel", note="testing")
    with pytest.raises(ValueError):
        academy.send_command("talon", "to_demo", by="reydel")
    eng.commands()
    assert eng.db.agent("talon")["status"] == "PAUSED"
    assert academy.agent("nobody") is None

def test_a_sprint_that_waited_for_history_reruns_when_more_arrives(setup):
    eng, _ = setup
    eng.start()
    eng.load_data()
    full = eng.panel
    from equity.data import Panel

    eng.panel = Panel(full.dates[-300:], {s: type(v)(v.o[-300:], v.h[-300:], v.l[-300:], v.c[-300:]) for s, v in full.series.items()})
    assert eng.run_sprint("rook", "2026-10-17")["outcome"] == "NO_DATA"
    assert not eng.untested("rook")  # same short history: no rerun
    eng.panel = full
    assert eng.untested("rook")  # the backfill arrived: run a real sprint
    eng.run_sprint("rook", "2026-10-17")
    assert not eng.untested("rook")
