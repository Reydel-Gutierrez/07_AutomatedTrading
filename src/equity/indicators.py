"""Indicators over daily series with gaps (None). Each returns a list aligned to the input,
None until enough history exists. Values at index i use only bars up to and including i."""

from __future__ import annotations

from typing import Sequence

Num = float | None


def sma(xs: Sequence[Num], n: int) -> list[Num]:
    out: list[Num] = [None] * len(xs)
    window: list[float] = []
    total = 0.0
    for i, x in enumerate(xs):
        if x is None:
            continue
        window.append(x)
        total += x
        if len(window) > n:
            total -= window.pop(0)
        if len(window) == n:
            out[i] = total / n
    return out


def rsi(xs: Sequence[Num], n: int) -> list[Num]:
    """Wilder's RSI, seeded with the first n changes."""
    out: list[Num] = [None] * len(xs)
    prev = None
    seen = 0
    avg_g = avg_l = 0.0
    for i, x in enumerate(xs):
        if x is None:
            continue
        if prev is not None:
            ch = x - prev
            g, l = max(ch, 0.0), max(-ch, 0.0)
            seen += 1
            if seen <= n:
                avg_g += g / n
                avg_l += l / n
            else:
                avg_g = (avg_g * (n - 1) + g) / n
                avg_l = (avg_l * (n - 1) + l) / n
            if seen >= n:
                out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
        prev = x
    return out


def atr(h: Sequence[Num], l: Sequence[Num], c: Sequence[Num], n: int) -> list[Num]:
    out: list[Num] = [None] * len(c)
    prev = None
    trs: list[float] = []
    val = None
    for i in range(len(c)):
        if c[i] is None or h[i] is None or l[i] is None:
            continue
        tr = h[i] - l[i] if prev is None else max(h[i] - l[i], abs(h[i] - prev), abs(l[i] - prev))
        prev = c[i]
        if val is None:
            trs.append(tr)
            if len(trs) == n:
                val = sum(trs) / n
        else:
            val = (val * (n - 1) + tr) / n
        out[i] = val
    return out


def highest(xs: Sequence[Num], n: int) -> list[Num]:
    """Highest value of the n bars BEFORE i (excludes i): a breakout compares today with this."""
    out: list[Num] = [None] * len(xs)
    window: list[float] = []
    for i, x in enumerate(xs):
        if len(window) == n:
            out[i] = max(window)
        if x is not None:
            window.append(x)
            if len(window) > n:
                window.pop(0)
    return out


def change(xs: Sequence[Num], n: int) -> list[Num]:
    """Return over the last n traded bars."""
    out: list[Num] = [None] * len(xs)
    hist: list[float] = []
    for i, x in enumerate(xs):
        if x is None:
            continue
        hist.append(x)
        if len(hist) > n:
            base = hist[-n - 1]
            out[i] = x / base - 1 if base else None
    return out
