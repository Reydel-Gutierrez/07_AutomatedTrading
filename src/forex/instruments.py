"""The Academy's tradable universe, instrument facts, and currency conversion to USD.

The universe is the liquid pairs the P0 probe showed with tight spreads (2026-10-12):
majors plus EUR/GBP and EUR/JPY. Exotics (NOK, ZAR, CZK, HKD, TRY) are excluded.
Margin rates and financing come from OANDA's instrument list (stored in marketdata.db);
the defaults below are the probe's values and are used only when nothing is stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

UNIVERSE = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "USD_CHF", "NZD_USD", "EUR_GBP", "EUR_JPY"]

# Spread ceilings (pips) for new entries; far above normal liquid-hours spreads, below rollover blowouts.
MAX_SPREAD_PIPS = {"EUR_USD": 2.5, "GBP_USD": 3.5, "USD_JPY": 3.0, "AUD_USD": 3.0, "USD_CAD": 3.5, "USD_CHF": 3.5, "NZD_USD": 4.0, "EUR_GBP": 3.0, "EUR_JPY": 4.5}

# Weekend gap assumption (fraction of price) used by the weekend-hold check; JPY crosses gap more.
WEEKEND_GAP = {"EUR_JPY": 0.015}
DEFAULT_WEEKEND_GAP = 0.01

_PROBE_DEFAULTS = {
    # name: (pip_location, margin_rate, financing_long, financing_short)
    "EUR_USD": (-4, 0.02, -0.0244, 0.0042),
    "GBP_USD": (-4, 0.05, -0.0123, -0.0081),
    "USD_JPY": (-2, 0.05, 0.0182, -0.0385),
    "AUD_USD": (-4, 0.03, -0.004, -0.0166),
    "USD_CAD": (-4, 0.02, -0.01, -0.01),
    "USD_CHF": (-4, 0.03, 0.01, -0.03),
    "NZD_USD": (-4, 0.03, -0.01, -0.01),
    "EUR_GBP": (-4, 0.05, -0.01, -0.01),
    "EUR_JPY": (-2, 0.05, 0.01, -0.03),
}


@dataclass(frozen=True)
class Spec:
    name: str
    pip_location: int
    margin_rate: float
    financing_long: float  # annual rate on notional (negative = you pay)
    financing_short: float
    financing_days: Mapping[str, int] = field(default_factory=dict)  # weekday -> day count multiplier

    @property
    def base(self) -> str:
        return self.name.split("_")[0]

    @property
    def quote(self) -> str:
        return self.name.split("_")[1]

    @property
    def pip(self) -> float:
        return 10.0 ** self.pip_location

    @property
    def max_spread_pips(self) -> float:
        return MAX_SPREAD_PIPS.get(self.name, 3.0)

    @property
    def weekend_gap(self) -> float:
        return WEEKEND_GAP.get(self.name, DEFAULT_WEEKEND_GAP)

    @classmethod
    def from_oanda(cls, raw: Mapping) -> "Spec":
        fin = raw.get("financing") or {}
        days = {d.get("dayOfWeek", "")[:3].title(): int(d.get("daysCharged", 0)) for d in fin.get("financingDaysOfWeek") or []}
        return cls(
            name=raw["name"], pip_location=int(raw.get("pipLocation", -4)), margin_rate=float(raw.get("marginRate", 0.02)),
            financing_long=float(fin.get("longRate", 0) or 0), financing_short=float(fin.get("shortRate", 0) or 0), financing_days=days,
        )


def default_spec(name: str) -> Spec:
    pip, margin, fl, fs = _PROBE_DEFAULTS.get(name, (-4, 0.05, 0.0, 0.0))
    return Spec(name, pip, margin, fl, fs)


class Converter:
    """Value of one unit of a currency in USD, from the latest mid prices of the universe."""

    def __init__(self, mids: Mapping[str, float]) -> None:
        self.mids = mids

    def usd(self, ccy: str) -> float:
        if ccy == "USD":
            return 1.0
        if (m := self.mids.get(f"{ccy}_USD")) is not None:
            return m
        if (m := self.mids.get(f"USD_{ccy}")) is not None and m > 0:
            return 1.0 / m
        if ccy == "GBP" and "EUR_GBP" in self.mids and "EUR_USD" in self.mids:
            return self.mids["EUR_USD"] / self.mids["EUR_GBP"]
        raise KeyError(f"no USD conversion for {ccy}")
