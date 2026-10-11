from __future__ import annotations

from datetime import date

from firm.backtest import run_tim_backtest, synthetic_bars
from firm.money import D


def test_two_year_replay_keeps_invariants_and_controls_drift(tmp_path):
    bars = synthetic_bars(["VTI", "VXUS", "BND"], start=date(2024, 1, 2), days=500, seed=11)
    r = run_tim_backtest(bars, monthly_contribution=D("50"), workdir=tmp_path)
    assert r.invariant_failures == []
    assert r.days == 500
    assert r.contributions == D("909") + D("50") * 23
    assert r.max_abs_drift < 0.08  # 5pp band plus one day's move before the next run
    assert r.trades > 3
    assert r.final_nav > 0
    # Turnover stays modest for a long-term portfolio.
    assert r.sells < r.buys * D("0.5")
