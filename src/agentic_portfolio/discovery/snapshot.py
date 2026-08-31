"""Normalized security facts for Candidate Discovery.

Discovery scores these facts. It does not place orders or write theses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentic_portfolio.schemas import ClassificationResult, LiquidityEvidence, ProvenanceFact


def compute_spread_metrics(bid: float | None, ask: float | None) -> dict[str, float] | None:
    """Dollar, fraction, and basis-point spread from an observed NBBO.

    `spread_percent` is (ask - bid) / midpoint as a fraction: 0.0193 means
    1.93 percent, not $0.0193. `spread_bps` is that fraction times 10_000.
    `absolute_spread_usd` is ask minus bid in dollars.
    """
    if bid is None or ask is None:
        return None
    try:
        bid_px = float(bid)
        ask_px = float(ask)
    except (TypeError, ValueError):
        return None
    if bid_px <= 0 or ask_px <= 0:
        return None
    midpoint = (ask_px + bid_px) / 2.0
    if midpoint <= 0:
        return None
    absolute = ask_px - bid_px
    percent = absolute / midpoint
    return {
        "bid_price": bid_px,
        "ask_price": ask_px,
        "midpoint": midpoint,
        "absolute_spread_usd": absolute,
        "spread_percent": percent,
        "spread_bps": percent * 10000.0,
    }


@dataclass
class SecuritySnapshot:
    """Broker-agnostic observation bundle for one symbol.

    Public data sources can populate the same fields later without rewriting
    Discovery. Missing fields stay None — they are not invented.
    """

    symbol: str
    observed_at: str
    sources: list[str] = field(default_factory=list)
    name: str | None = None
    instrument_kind: str | None = None
    tradable: bool | None = None
    trade_state: str | None = None
    current_price: float | None = None
    previous_close: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None
    market_cap: float | None = None
    shares_outstanding: float | None = None
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    dividend_yield: float | None = None
    sector: str | None = None
    industry: str | None = None
    description: str | None = None
    average_volume: float | None = None
    high_52_week: float | None = None
    low_52_week: float | None = None
    revenue_periods: list[float] = field(default_factory=list)
    net_income_periods: list[float] = field(default_factory=list)
    net_margin_periods: list[float] = field(default_factory=list)
    gross_profit_periods: list[float] = field(default_factory=list)
    rsi: float | None = None
    sma_50: float | None = None
    sma_200: float | None = None
    atr: float | None = None
    return_5d: float | None = None
    return_21d: float | None = None
    return_63d: float | None = None
    return_252d: float | None = None
    drawdown_from_52w_high: float | None = None
    volume_vs_avg: float | None = None
    earnings_surprise_last: float | None = None
    earnings_upcoming_days: int | None = None
    news_headlines: list[str] = field(default_factory=list)
    sec_going_concern: bool | None = None
    sec_dilution_flag: bool | None = None
    is_leveraged: bool | None = None
    is_inverse: bool | None = None
    data_stale: bool = False
    classification: ClassificationResult | None = None
    liquidity: LiquidityEvidence | None = None
    evidence_refs: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    data_origin: str | None = None
    broker_instrument_id: str | None = None
    exchange: str | None = None
    quote_as_of: str | None = None
    quote_source: str | None = None
    bid_as_of: str | None = None
    ask_as_of: str | None = None
    fact_provenance: dict[str, ProvenanceFact] = field(default_factory=dict)

    @property
    def dollar_volume(self) -> float | None:
        if self.liquidity and self.liquidity.recent_dollar_volume:
            return self.liquidity.recent_dollar_volume
        if self.average_volume and self.current_price:
            return self.average_volume * self.current_price
        if self.volume and self.current_price:
            return self.volume * self.current_price
        return None

    @property
    def spread_pct(self) -> float | None:
        """Relative spread as a fraction: 0.0193 means 1.93%, not $0.0193.

        Eligibility and scoring consume this field. AI context must not
        receive it as a unitless `spread` number.
        """
        if self.liquidity and self.liquidity.bid_ask_spread_pct is not None:
            return self.liquidity.bid_ask_spread_pct
        metrics = compute_spread_metrics(self.bid, self.ask)
        if metrics is None:
            return None
        return metrics["spread_percent"]

    @property
    def spread_metrics(self) -> dict[str, float] | None:
        return compute_spread_metrics(self.bid, self.ask)
