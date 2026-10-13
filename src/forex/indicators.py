"""Causal technical indicators on plain lists. Value i uses only inputs [0, i]; None while warming up."""

from __future__ import annotations

import math
from collections import deque
from typing import Sequence

Num = float | None


def sma(x: Sequence[float], n: int) -> list[Num]:
    out: list[Num] = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(x: Sequence[float], n: int) -> list[Num]:
    out: list[Num] = [None] * len(x)
    if len(x) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(x[:n]) / n
    out[n - 1] = e
    for i in range(n, len(x)):
        e = x[i] * k + e * (1 - k)
        out[i] = e
    return out


def stdev(x: Sequence[float], n: int) -> list[Num]:
    out: list[Num] = [None] * len(x)
    s = s2 = 0.0
    for i, v in enumerate(x):
        s += v
        s2 += v * v
        if i >= n:
            s -= x[i - n]
            s2 -= x[i - n] ** 2
        if i >= n - 1:
            m = s / n
            out[i] = math.sqrt(max(s2 / n - m * m, 0.0))
    return out


def bollinger(x: Sequence[float], n: int = 20, k: float = 2.0) -> tuple[list[Num], list[Num], list[Num]]:
    mid, sd = sma(x, n), stdev(x, n)
    up = [m + k * s if m is not None and s is not None else None for m, s in zip(mid, sd)]
    lo = [m - k * s if m is not None and s is not None else None for m, s in zip(mid, sd)]
    return mid, up, lo


def _wilder(x: Sequence[float], n: int, start: int = 0) -> list[Num]:
    """Wilder smoothing of x beginning at index `start` (values before are ignored)."""
    out: list[Num] = [None] * len(x)
    if len(x) - start < n:
        return out
    v = sum(x[start : start + n]) / n
    out[start + n - 1] = v
    for i in range(start + n, len(x)):
        v = (v * (n - 1) + x[i]) / n
        out[i] = v
    return out


def true_range(h: Sequence[float], l: Sequence[float], c: Sequence[float]) -> list[float]:  # noqa: E741
    tr = [h[0] - l[0]] if h else []
    for i in range(1, len(h)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return tr


def atr(h: Sequence[float], l: Sequence[float], c: Sequence[float], n: int = 14) -> list[Num]:  # noqa: E741
    return _wilder(true_range(h, l, c), n)


def rsi(x: Sequence[float], n: int = 14) -> list[Num]:
    out: list[Num] = [None] * len(x)
    if len(x) <= n:
        return out
    gains = [0.0] + [max(x[i] - x[i - 1], 0.0) for i in range(1, len(x))]
    losses = [0.0] + [max(x[i - 1] - x[i], 0.0) for i in range(1, len(x))]
    ag, al = _wilder(gains, n, 1), _wilder(losses, n, 1)
    for i in range(len(x)):
        g, lo = ag[i], al[i]
        if g is None or lo is None:
            continue
        out[i] = 100.0 if lo == 0 else 100.0 - 100.0 / (1.0 + g / lo)
    return out


def adx(h: Sequence[float], l: Sequence[float], c: Sequence[float], n: int = 14) -> list[Num]:  # noqa: E741
    size = len(h)
    out: list[Num] = [None] * size
    if size <= 2 * n:
        return out
    pdm = [0.0] + [max(h[i] - h[i - 1], 0.0) if (h[i] - h[i - 1]) > (l[i - 1] - l[i]) else 0.0 for i in range(1, size)]
    ndm = [0.0] + [max(l[i - 1] - l[i], 0.0) if (l[i - 1] - l[i]) > (h[i] - h[i - 1]) else 0.0 for i in range(1, size)]
    tr = true_range(h, l, c)
    atr_, sp, sn = _wilder(tr, n, 1), _wilder(pdm, n, 1), _wilder(ndm, n, 1)
    dx: list[float] = [0.0] * size
    first = None
    for i in range(size):
        a, p, m = atr_[i], sp[i], sn[i]
        if a is None or p is None or m is None or a == 0:
            continue
        pdi, ndi = 100 * p / a, 100 * m / a
        dx[i] = 0.0 if pdi + ndi == 0 else 100 * abs(pdi - ndi) / (pdi + ndi)
        first = i if first is None else first
    if first is None:
        return out
    return _wilder(dx, n, first)


def highest(x: Sequence[float], n: int) -> list[Num]:
    """Max of the n values ending at i (inclusive)."""
    out: list[Num] = [None] * len(x)
    dq: deque[int] = deque()
    for i, v in enumerate(x):
        while dq and x[dq[-1]] <= v:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - n:
            dq.popleft()
        if i >= n - 1:
            out[i] = x[dq[0]]
    return out


def lowest(x: Sequence[float], n: int) -> list[Num]:
    out: list[Num] = [None] * len(x)
    dq: deque[int] = deque()
    for i, v in enumerate(x):
        while dq and x[dq[-1]] >= v:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - n:
            dq.popleft()
        if i >= n - 1:
            out[i] = x[dq[0]]
    return out


def roc(x: Sequence[float], n: int) -> list[Num]:
    """Rate of change over n bars, as a fraction."""
    return [None if i < n or x[i - n] == 0 else x[i] / x[i - n] - 1.0 for i in range(len(x))]
