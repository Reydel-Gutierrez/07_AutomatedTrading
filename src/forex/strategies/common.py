"""Cached indicator accessors shared by the strategies (computed once per series)."""

from __future__ import annotations

from forex import indicators as ind
from forex.market import Bars


def ema(b: Bars, n: int) -> list:
    return b.ind(("ema", n), lambda x: ind.ema(x.c, n))


def sma(b: Bars, n: int) -> list:
    return b.ind(("sma", n), lambda x: ind.sma(x.c, n))


def atr(b: Bars, n: int = 14) -> list:
    return b.ind(("atr", n), lambda x: ind.atr(x.h, x.l, x.c, n))


def adx(b: Bars, n: int = 14) -> list:
    return b.ind(("adx", n), lambda x: ind.adx(x.h, x.l, x.c, n))


def rsi(b: Bars, n: int = 14) -> list:
    return b.ind(("rsi", n), lambda x: ind.rsi(x.c, n))


def highest_high(b: Bars, n: int) -> list:
    return b.ind(("hh", n), lambda x: ind.highest(x.h, n))


def lowest_low(b: Bars, n: int) -> list:
    return b.ind(("ll", n), lambda x: ind.lowest(x.l, n))


def bollinger(b: Bars, n: int, k: float) -> tuple[list, list, list]:
    return b.ind(("bb", n, k), lambda x: ind.bollinger(x.c, n, k))


def roc(b: Bars, n: int) -> list:
    return b.ind(("roc", n), lambda x: ind.roc(x.c, n))


def last(b: Bars, series: list, back: int = 1):
    return b.at(series, back)


def ready(*values) -> bool:
    return all(v is not None for v in values)
