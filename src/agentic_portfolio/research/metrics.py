"""Deterministic derived metrics. No qualitative investment judgment."""

from __future__ import annotations

from typing import Any


def period_growth(series: list[float] | None, *, periods: int = 1) -> float | None:
    """Newest-first series. periods=1 is most-recent vs prior period."""
    if not series or len(series) <= periods:
        return None
    older = series[periods]
    newer = series[0]
    if older in (None, 0):
        return None
    try:
        return (float(newer) - float(older)) / abs(float(older))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def trailing_growth(series: list[float] | None) -> float | None:
    """First vs last observation in a newest-first series."""
    if not series or len(series) < 2:
        return None
    older = series[-1]
    newer = series[0]
    if older in (None, 0):
        return None
    try:
        return (float(newer) - float(older)) / abs(float(older))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def margin(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    try:
        return float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    return margin(numerator, denominator)


def fcf_yield(free_cash_flow: float | None, market_cap: float | None) -> float | None:
    return margin(free_cash_flow, market_cap)


def drawdown(price: float | None, high: float | None) -> float | None:
    if price is None or high in (None, 0):
        return None
    try:
        return max(0.0, (float(high) - float(price)) / float(high))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def share_change(current: float | None, prior: float | None) -> float | None:
    return period_growth([current, prior], periods=1) if current is not None and prior is not None else None


def sma_alignment(price: float | None, sma_fast: float | None, sma_slow: float | None) -> str | None:
    """Descriptive geometry only — not a buy/sell signal."""
    if price is None or sma_fast is None:
        return None
    if sma_slow is None:
        return "price_above_fast_sma" if price > sma_fast else "price_below_fast_sma"
    if price > sma_fast > sma_slow:
        return "price_above_fast_above_slow"
    if price < sma_fast < sma_slow:
        return "price_below_fast_below_slow"
    return "mixed_sma_alignment"


def as_float(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
