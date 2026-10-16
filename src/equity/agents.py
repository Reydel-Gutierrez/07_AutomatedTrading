"""The Stock Swing Class: four challengers for a live swing seat on the Equity Trading floor.

Named by Claude at the owner's request (2026-10-16). Personality is presentation only; it never
reaches a decision. Each agent trades only its own list of names, chosen for its style:
  Juniper  the incumbent's style improved: Max's 22 liquid ETFs and mega caps
  Rook     breakouts need leaders that trend: 20 large caps across sectors
  Meridian rotation needs distinct assets: 11 sector ETFs, semiconductors, gold, and bonds
  Talon    the owner's "risk taker": 15 volatile growth names, at most two per group
"""

from __future__ import annotations

from typing import Any

from equity.sim import Envelope
from equity.strategies import STRATEGIES

CLASS: list[dict[str, Any]] = [
    {
        "id": "juniper", "name": "Juniper", "strategy": "pullback_atr_v1",
        "persona": {
            "tagline": "Steady dip buyer",
            "about": "Buys sharp, short dips in names that are still trending up and sells into the bounce. Same idea as Max's desk, with stops that fit how much each name normally moves.",
            "color": "#22c55e", "look": {"skin": "#d8a47f", "hair": "#3b2416", "hairStyle": "neat", "top": "#14532d", "inner": "#dcfce7", "accent": "#22c55e"},
        },
        "envelope": Envelope(cost_bps=6.0),
    },
    {
        "id": "rook", "name": "Rook", "strategy": "breakout_trail_v1",
        "persona": {
            "tagline": "Breakout trader",
            "about": "Waits for a leader to close at a new high and rides it on a trailing stop. Loses small when a breakout fails; lets the real ones run for weeks.",
            "color": "#ef4444", "look": {"skin": "#a8714f", "hair": "#111111", "hairStyle": "buzz", "top": "#450a0a", "inner": "#fee2e2", "accent": "#ef4444", "headset": True},
        },
        "envelope": Envelope(cost_bps=6.0),
    },
    {
        "id": "meridian", "name": "Meridian", "strategy": "rotation_v1",
        "persona": {
            "tagline": "Sector rotator",
            "about": "Thinks in sectors, not stocks: every week holds the strongest few of the market's sectors, gold, and bonds, and steps out of anything below its trend.",
            "color": "#0ea5e9", "look": {"skin": "#f1c9a5", "hair": "#9ca3af", "hairStyle": "executive", "top": "#0c4a6e", "inner": "#e0f2fe", "accent": "#0ea5e9", "tie": True},
        },
        "envelope": Envelope(cost_bps=5.0),
    },
    {
        "id": "talon", "name": "Talon", "strategy": "pullback_highbeta_v1",
        "persona": {
            "tagline": "High-beta risk taker",
            "about": "Hunts big dips in fast-moving growth stocks. Smaller positions and wide stops sized to each name's swings; never more than two names that move together.",
            "color": "#f59e0b", "look": {"skin": "#6b4430", "hair": "#1c1917", "hairStyle": "curly", "top": "#451a03", "inner": "#fef3c7", "accent": "#f59e0b", "glasses": "#fcd34d"},
        },
        "envelope": Envelope(cost_bps=10.0),
    },
]


def default_params(strategy_key: str) -> dict[str, Any]:
    return dict(STRATEGIES[strategy_key].defaults)


def wanted_symbols() -> list[str]:
    """Every name any agent may trade (plus the calendar), for the firm's daily bar job."""
    out = {"SPY"}
    for a in CLASS:
        out |= set(STRATEGIES[a["strategy"]].defaults["symbols"])
    return sorted(out)
