"""Synthetic bid/ask candles for tests (deterministic random walks with trends and ranges)."""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone

from forex.market import SECONDS, Bars, MarketStore
from forex.oanda import OHLC, Candle

START = int(datetime(2024, 1, 1, 22, tzinfo=timezone.utc).timestamp())  # Monday 17:00 NY

LEVEL = {"EUR_USD": 1.10, "GBP_USD": 1.27, "USD_JPY": 150.0, "AUD_USD": 0.66, "USD_CAD": 1.36, "USD_CHF": 0.88, "NZD_USD": 0.61, "EUR_GBP": 0.86, "EUR_JPY": 163.0}


def series(instrument: str, gran: str, n: int, *, seed: int = 1, start: int = START) -> list[Candle]:
    rng = random.Random(f"{instrument}{seed}")
    dur = SECONDS[gran]
    px = LEVEL.get(instrument, 1.0)
    pip = 0.01 if instrument.endswith("JPY") else 0.0001
    vol = px * 0.0012 * math.sqrt(dur / 3600)
    drift = 0.0
    out = []
    t = start
    for i in range(n):
        if i % 300 == 0:
            drift = rng.choice([-1, 0, 0, 1]) * vol * 0.15
        o = px
        c = o + drift + rng.gauss(0, vol)
        h = max(o, c) + abs(rng.gauss(0, vol * 0.5))
        lo = min(o, c) - abs(rng.gauss(0, vol * 0.5))
        sp = 1.2 * pip
        bid = OHLC(o, h, lo, c)
        ask = OHLC(o + sp, h + sp, lo + sp, c + sp)
        out.append(Candle(datetime.fromtimestamp(t, tz=timezone.utc), 100, True, bid, ask, None))
        px = c
        t += dur
    return out


def fill(store: MarketStore, instruments: list[str], grans: dict[str, int], seed: int = 1) -> None:
    for inst in instruments:
        for gran, n in grans.items():
            store.upsert(inst, gran, series(inst, gran, n, seed=seed))


def bars(instrument: str, gran: str, n: int, seed: int = 1) -> Bars:
    cs = series(instrument, gran, n, seed=seed)
    return Bars(
        instrument, gran, [int(c.time.timestamp()) for c in cs],
        [float(c.bid.o) for c in cs], [float(c.bid.h) for c in cs], [float(c.bid.l) for c in cs], [float(c.bid.c) for c in cs],
        [float(c.ask.o) for c in cs], [float(c.ask.h) for c in cs], [float(c.ask.l) for c in cs], [float(c.ask.c) for c in cs],
    )
