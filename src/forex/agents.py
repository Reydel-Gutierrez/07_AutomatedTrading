"""The Academy's founding class: four independent forex agents.

Personality is presentation only (dashboard, Firm Space, journal tone); it never reaches a
trading decision. Each agent's strategy, parameters, and risk envelope are versioned.
"""

from __future__ import annotations

from typing import Any

from forex.sim import RiskEnvelope
from forex.strategies import STRATEGIES

FOUNDING_CLASS: list[dict[str, Any]] = [
    {
        "id": "atlas",
        "name": "Atlas",
        "style": "swing",
        "strategy": "atlas_trend_v1",
        "persona": {
            "tagline": "Patient trend follower",
            "about": "Waits for an established daily trend, then joins it on a 4-hour breakout or pullback and lets winners run on a trailing stop. Unbothered by small losses; lives for the occasional big trend.",
            "color": "#38bdf8", "look": {"skin": "#c99a76", "hair": "#2b2523", "hairStyle": "neat", "top": "#1e3a5f", "inner": "#dbeafe", "accent": "#38bdf8"},
        },
        "envelope": RiskEnvelope(style="swing", risk_pct=0.0075, max_positions=3, weekend_hold=True),
    },
    {
        "id": "kestrel",
        "name": "Kestrel",
        "style": "day",
        "strategy": "kestrel_london_v1",
        "persona": {
            "tagline": "London-open hunter",
            "about": "Up before London. Maps the quiet Asian range overnight and strikes on the first clean break, with the hourly trend at its back. Done by late morning, flat every night.",
            "color": "#f97316", "look": {"skin": "#f0c8a4", "hair": "#a0522d", "hairStyle": "buzz", "top": "#3b2a1e", "inner": "#fed7aa", "accent": "#f97316", "headset": True},
        },
        "envelope": RiskEnvelope(style="day", risk_pct=0.005, max_positions=2),
    },
    {
        "id": "mira",
        "name": "Mira",
        "style": "day",
        "strategy": "mira_reversion_v1",
        "persona": {
            "tagline": "Calm range trader",
            "about": "Works the quiet hours when markets go sideways, fading stretched moves back to the middle. Steps aside the moment a trend shows up.",
            "color": "#34d399", "look": {"skin": "#8d5a3b", "hair": "#141012", "hairStyle": "curly", "top": "#134e4a", "inner": "#ccfbf1", "accent": "#34d399", "glasses": "#a7f3d0"},
        },
        "envelope": RiskEnvelope(style="day", risk_pct=0.005, max_positions=2),
    },
    {
        "id": "orion",
        "name": "Orion",
        "style": "swing",
        "strategy": "orion_strength_v1",
        "persona": {
            "tagline": "Macro rotation strategist",
            "about": "Thinks in currencies, not pairs: ranks all eight by momentum and pairs the strongest against the weakest, buying pullbacks and minding the cost of carry.",
            "color": "#c084fc", "look": {"skin": "#e2b48f", "hair": "#d6d3d1", "hairStyle": "executive", "top": "#3b0764", "inner": "#f5f3ff", "accent": "#c084fc", "tie": True},
        },
        "envelope": RiskEnvelope(style="swing", risk_pct=0.0075, max_positions=2, weekend_hold=True),
    },
]


def default_params(strategy_key: str) -> dict[str, Any]:
    return dict(STRATEGIES[strategy_key].defaults)
