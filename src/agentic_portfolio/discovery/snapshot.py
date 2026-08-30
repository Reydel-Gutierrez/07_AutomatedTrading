"""Normalized security facts for Candidate Discovery.

Discovery scores these facts. It does not place orders or write theses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentic_portfolio.schemas import ClassificationResult, LiquidityEvidence


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
        if self.liquidity and self.liquidity.bid_ask_spread_pct is not None:
            return self.liquidity.bid_ask_spread_pct
        if self.bid and self.ask and self.bid > 0 and self.ask > 0:
            mid = (self.bid + self.ask) / 2.0
            if mid > 0:
                return (self.ask - self.bid) / mid
        return None
