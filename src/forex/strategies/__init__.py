"""The Academy's strategy library. Each class is reviewed, tested code; agents choose one
and configure it with bounded parameters (agent versions)."""

from __future__ import annotations

from forex.strategies.atlas import TrendBreakout
from forex.strategies.kestrel import LondonBreakout
from forex.strategies.mira import RangeReversion
from forex.strategies.orion import StrengthRotation
from forex.strategy import Strategy

STRATEGIES: dict[str, Strategy] = {s.key: s for s in (TrendBreakout(), LondonBreakout(), RangeReversion(), StrengthRotation())}


def get(key: str) -> Strategy:
    return STRATEGIES[key]
