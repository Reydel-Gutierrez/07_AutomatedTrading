"""Four independent discovery channels. Empty output is valid."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from agentic_portfolio.discovery.signals import make_signal
from agentic_portfolio.discovery.snapshot import SecuritySnapshot
from agentic_portfolio.policy import load_discovery_config
from agentic_portfolio.schemas import (
    ClassificationStatus,
    DiscoveryChannel,
    DiscoverySignal,
    MarketRegime,
    MarketRegimeStatus,
    SecurityClass,
    SignalDirection,
    SignalType,
    Sleeve,
)


@dataclass
class ChannelNomination:
    channel: DiscoveryChannel
    sleeve: Sleeve
    sleeve_reason: str
    sleeve_confidence: str
    signals: list[DiscoverySignal] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    research_questions: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    event_flags: list[str] = field(default_factory=list)
    known_risks: list[str] = field(default_factory=list)
    thesis_type: str | None = None


def run_channels(
    snap: SecuritySnapshot,
    regime: MarketRegime,
    config: dict | None = None,
) -> list[ChannelNomination]:
    cfg = config or load_discovery_config()
    out: list[ChannelNomination] = []
    for fn in (_core_quality, _opportunistic, _tactical, _speculative):
        nom = fn(snap, regime, cfg)
        if nom is not None:
            out.append(nom)
    return out


def _growth(series: list[float], lag: int = 4) -> float | None:
    if len(series) > lag and series[lag] not in (None, 0):
        return (series[0] - series[lag]) / abs(series[lag])
    if len(series) >= 2 and series[1] not in (None, 0):
        return (series[0] - series[1]) / abs(series[1])
    return None


def _delta(series: list[float], lag: int = 4) -> float | None:
    if len(series) > lag:
        return series[0] - series[lag]
    if len(series) >= 2:
        return series[0] - series[1]
    return None


def _core_quality(snap: SecuritySnapshot, regime: MarketRegime, cfg: dict) -> ChannelNomination | None:
    ts = snap.observed_at
    src = snap.sources[0] if snap.sources else "snapshot"
    signals: list[DiscoverySignal] = []
    reasons: list[str] = []
    risks: list[str] = []

    cls = snap.classification
    if cls and cls.security_class == SecurityClass.BROAD_MARKET_INDEX_ETF and cls.status == ClassificationStatus.VALIDATED:
        signals.append(make_signal(SignalType.QUALITY, "diversified_fund", value=cls.security_class.value, direction=SignalDirection.POSITIVE, strength=0.85, observed_at=ts, source=src, evidence_ref="classification"))
        reasons.append("validated_broad_market_index_etf")
    elif cls and cls.security_class == SecurityClass.OTHER_DIVERSIFIED_ETF and cls.status == ClassificationStatus.VALIDATED:
        signals.append(make_signal(SignalType.QUALITY, "diversified_fund", value=cls.security_class.value, direction=SignalDirection.POSITIVE, strength=0.45, observed_at=ts, source=src, evidence_ref="classification"))
        reasons.append("validated_other_diversified_etf")

    profitable = snap.net_income_periods[0] > 0 if snap.net_income_periods else None
    if profitable is True:
        signals.append(make_signal(SignalType.FUNDAMENTAL, "profitability", value=snap.net_income_periods[0], direction=SignalDirection.POSITIVE, strength=0.8, observed_at=ts, source=src, evidence_ref="financials"))
        reasons.append("positive_net_income")
    elif profitable is False:
        signals.append(make_signal(SignalType.FUNDAMENTAL, "profitability", value=snap.net_income_periods[0], direction=SignalDirection.NEGATIVE, strength=0.85, observed_at=ts, source=src, evidence_ref="financials"))
        risks.append("unprofitable")

    g = _growth(snap.revenue_periods)
    if g is not None:
        if g >= 0.08:
            signals.append(make_signal(SignalType.FUNDAMENTAL, "revenue_growth", value=g, direction=SignalDirection.POSITIVE, strength=min(1.0, g / 0.25), observed_at=ts, source=src, evidence_ref="financials"))
            reasons.append("durable_revenue_growth")
        elif g <= -0.05:
            signals.append(make_signal(SignalType.FUNDAMENTAL, "revenue_growth", value=g, direction=SignalDirection.NEGATIVE, strength=min(1.0, abs(g) / 0.25), observed_at=ts, source=src, evidence_ref="financials"))
            risks.append("revenue_contraction")

    eg = _growth(snap.net_income_periods)
    if eg is not None and eg >= 0.08:
        if eg > 1.5:
            signals.append(make_signal(SignalType.FUNDAMENTAL, "earnings_growth", value=eg, direction=SignalDirection.MIXED, strength=0.3, observed_at=ts, source=src, evidence_ref="financials"))
            risks.append("earnings_change_may_be_non_recurring")
        else:
            signals.append(make_signal(SignalType.FUNDAMENTAL, "earnings_growth", value=eg, direction=SignalDirection.POSITIVE, strength=min(1.0, eg / 0.30), observed_at=ts, source=src, evidence_ref="financials"))

    if snap.net_margin_periods and abs(snap.net_margin_periods[0]) > 0.70:
        signals.append(make_signal(SignalType.FUNDAMENTAL, "margins_trend", value=snap.net_margin_periods[0], direction=SignalDirection.MIXED, strength=0.25, observed_at=ts, source=src, evidence_ref="financials"))
        risks.append("margin_level_may_include_one_time_items")
        unusual_margin = True
    else:
        unusual_margin = False

    md = _delta(snap.net_margin_periods)
    if md is not None and not unusual_margin:
        if md >= 0.01:
            signals.append(make_signal(SignalType.FUNDAMENTAL, "margins_trend", value=md, direction=SignalDirection.POSITIVE, strength=min(1.0, md / 0.05), observed_at=ts, source=src, evidence_ref="financials"))
            reasons.append("improving_margins")
        elif md <= -0.02:
            signals.append(make_signal(SignalType.FUNDAMENTAL, "margins_trend", value=md, direction=SignalDirection.NEGATIVE, strength=min(1.0, abs(md) / 0.05), observed_at=ts, source=src, evidence_ref="financials"))
            risks.append("margin_compression")

    if profitable and g is not None and g > 0:
        signals.append(make_signal(SignalType.FUNDAMENTAL, "cash_generation", direction=SignalDirection.POSITIVE, strength=0.55, observed_at=ts, source=src, evidence_ref="financials"))
        signals.append(make_signal(SignalType.FUNDAMENTAL, "balance_sheet", direction=SignalDirection.POSITIVE, strength=0.5, observed_at=ts, source=src, evidence_ref="financials"))
        signals.append(make_signal(SignalType.QUALITY, "competitive_position", direction=SignalDirection.POSITIVE, strength=0.6, observed_at=ts, source=src))

    if snap.pe_ratio and g is not None and g > 0 and 0 < snap.pe_ratio < 40:
        peg = snap.pe_ratio / max(g * 100.0, 1.0)
        if peg < 2.5:
            signals.append(make_signal(SignalType.VALUATION, "pe_vs_growth", value={"pe": snap.pe_ratio, "growth": g}, direction=SignalDirection.POSITIVE, strength=0.55, observed_at=ts, source=src, evidence_ref="fundamentals"))
            reasons.append("valuation_not_extreme_vs_growth")
    elif snap.pe_ratio and snap.pe_ratio > 80:
        signals.append(make_signal(SignalType.VALUATION, "pe_stretched", value=snap.pe_ratio, direction=SignalDirection.NEGATIVE, strength=0.4, observed_at=ts, source=src))

    _liquidity_signal(snap, signals, ts, src)

    # Fame / size alone is not a Core signal. Do not add a quality boost from market_cap.
    quality_positive = any(
        s.direction == SignalDirection.POSITIVE and s.signal_type in {SignalType.QUALITY, SignalType.FUNDAMENTAL}
        for s in signals
    )
    if not quality_positive:
        return None

    collapsing = _collapsing(snap, cfg)
    if collapsing:
        signals.append(make_signal(SignalType.FUNDAMENTAL, "collapsing_fundamentals", direction=SignalDirection.NEGATIVE, strength=0.9, observed_at=ts, source=src))
        risks.append("collapsing_fundamentals")

    _maybe_regime(regime, signals, ts)

    confidence = "HIGH" if len(reasons) >= 3 else ("MEDIUM" if reasons else "LOW")
    return ChannelNomination(
        channel=DiscoveryChannel.CORE_QUALITY_DISCOVERY,
        sleeve=Sleeve.CORE_GROWTH,
        sleeve_reason="quality_and_compounding_evidence_not_fame",
        sleeve_confidence=confidence,
        signals=signals,
        reasons=reasons or ["partial_quality_evidence"],
        observations=["Discovery does not prove the Core thesis; it only nominates for research."],
        known_risks=risks,
        thesis_type="core_quality",
        research_questions=["Is the moat and growth durability real at this valuation?"],
    )


def _opportunistic(snap: SecuritySnapshot, regime: MarketRegime, cfg: dict) -> ChannelNomination | None:
    ts = snap.observed_at
    src = snap.sources[0] if snap.sources else "snapshot"
    selloff = (snap.return_21d is not None and snap.return_21d <= -0.12) or (
        snap.drawdown_from_52w_high is not None and snap.drawdown_from_52w_high >= 0.20
    )
    if not selloff:
        return None

    signals: list[DiscoverySignal] = []
    reasons: list[str] = []
    questions: list[str] = [
        "TEMPORARY_PRICE_DISLOCATION vs FUNDAMENTAL_BUSINESS_DETERIORATION — Research must decide."
    ]
    flags: list[str] = ["PRICE_DISLOCATION_OBSERVED"]

    if snap.return_21d is not None and snap.return_21d <= -0.12:
        signals.append(make_signal(SignalType.PRICE_ACTION, "selloff", value=snap.return_21d, direction=SignalDirection.POSITIVE, strength=min(1.0, abs(snap.return_21d) / 0.30), observed_at=ts, source=src, evidence_ref="historicals"))
        reasons.append("sharp_selloff")
    if snap.drawdown_from_52w_high is not None and snap.drawdown_from_52w_high >= 0.20:
        signals.append(make_signal(SignalType.PRICE_ACTION, "drawdown_from_high", value=snap.drawdown_from_52w_high, direction=SignalDirection.POSITIVE, strength=min(1.0, snap.drawdown_from_52w_high / 0.45), observed_at=ts, source=src, evidence_ref="fundamentals"))
        reasons.append("drawdown_from_52w_high")

    collapsing = _collapsing(snap, cfg)
    g = _growth(snap.revenue_periods)
    profitable = snap.net_income_periods[0] > 0 if snap.net_income_periods else None
    if collapsing:
        signals.append(make_signal(SignalType.FUNDAMENTAL, "collapsing_fundamentals", value=g, direction=SignalDirection.NEGATIVE, strength=0.9, observed_at=ts, source=src, evidence_ref="financials"))
        signals.append(make_signal(SignalType.FUNDAMENTAL, "revenue_growth", value=g, direction=SignalDirection.NEGATIVE, strength=0.85, observed_at=ts, source=src))
        signals.append(make_signal(SignalType.QUALITY, "resilience", direction=SignalDirection.NEGATIVE, strength=0.8, observed_at=ts, source=src))
        reasons.append("fundamental_deterioration_evidence")
        flags.append("FUNDAMENTAL_BUSINESS_DETERIORATION_FLAG")
    else:
        if profitable:
            signals.append(make_signal(SignalType.FUNDAMENTAL, "profitability", value=snap.net_income_periods[0], direction=SignalDirection.POSITIVE, strength=0.7, observed_at=ts, source=src, evidence_ref="financials"))
        if g is not None and g > -0.05:
            signals.append(make_signal(SignalType.FUNDAMENTAL, "revenue_growth", value=g, direction=SignalDirection.POSITIVE, strength=0.55, observed_at=ts, source=src, evidence_ref="financials"))
            reasons.append("fundamentals_not_obviously_collapsing")
        signals.append(make_signal(SignalType.QUALITY, "resilience", direction=SignalDirection.POSITIVE, strength=0.7, observed_at=ts, source=src))
        flags.append("TEMPORARY_PRICE_DISLOCATION_CANDIDATE")

    if snap.pe_ratio and snap.pe_ratio < 25:
        signals.append(make_signal(SignalType.VALUATION, "valuation_reset", value=snap.pe_ratio, direction=SignalDirection.POSITIVE, strength=0.6, observed_at=ts, source=src, evidence_ref="fundamentals"))
        reasons.append("valuation_reset")
    elif (snap.drawdown_from_52w_high or 0) >= 0.25:
        signals.append(make_signal(SignalType.VALUATION, "valuation_reset", value=snap.drawdown_from_52w_high, direction=SignalDirection.POSITIVE, strength=0.45, observed_at=ts, source=src))
        reasons.append("valuation_reset_from_drawdown")

    if snap.earnings_surprise_last is not None and snap.earnings_surprise_last < -0.05 and (snap.return_5d or 0) < -0.05:
        signals.append(make_signal(SignalType.EARNINGS, "post_earnings_overreaction", value=snap.earnings_surprise_last, direction=SignalDirection.POSITIVE, strength=0.55, observed_at=ts, source=src, evidence_ref="earnings"))
        flags.append("POST_EARNINGS_MOVE")

    if snap.return_5d is not None and snap.return_5d > 0 and (snap.return_21d or 0) < 0:
        signals.append(make_signal(SignalType.MOMENTUM, "post_dislocation_confirmation", value=snap.return_5d, direction=SignalDirection.POSITIVE, strength=0.4, observed_at=ts, source=src))
        signals.append(make_signal(SignalType.PRICE_ACTION, "bounce", value=snap.return_5d, direction=SignalDirection.POSITIVE, strength=0.4, observed_at=ts, source=src))

    _liquidity_signal(snap, signals, ts, src)
    _maybe_regime(regime, signals, ts)
    if snap.earnings_upcoming_days is not None and snap.earnings_upcoming_days <= 14:
        signals.append(make_signal(SignalType.CATALYST, "upcoming_earnings", value=snap.earnings_upcoming_days, direction=SignalDirection.MIXED, strength=0.4, observed_at=ts, source=src))
        flags.append("UPCOMING_EARNINGS")

    return ChannelNomination(
        channel=DiscoveryChannel.OPPORTUNISTIC_DISLOCATION_DISCOVERY,
        sleeve=Sleeve.OPPORTUNISTIC,
        sleeve_reason="price_dislocation_with_unresolved_fundamental_question",
        sleeve_confidence="MEDIUM" if not collapsing else "LOW",
        signals=signals,
        reasons=reasons,
        research_questions=questions,
        observations=["Discovery flags the dislocation question; it does not conclude it."],
        event_flags=flags,
        known_risks=["value_trap_if_deterioration"] + (["collapsing_fundamentals"] if collapsing else []),
        thesis_type="opportunistic_dislocation",
    )


def _tactical(snap: SecuritySnapshot, regime: MarketRegime, cfg: dict) -> ChannelNomination | None:
    ts = snap.observed_at
    src = snap.sources[0] if snap.sources else "snapshot"
    signals: list[DiscoverySignal] = []
    reasons: list[str] = []
    flags: list[str] = []

    aligned = (
        snap.current_price is not None
        and snap.sma_50 is not None
        and snap.sma_200 is not None
        and snap.current_price > snap.sma_50 > snap.sma_200
    )
    momentum_ok = (snap.return_21d is not None and snap.return_21d >= 0.05) or (
        snap.rsi is not None and 55 <= snap.rsi <= 72
    )
    volume_ok = snap.volume_vs_avg is not None and snap.volume_vs_avg >= 1.4
    pullback = (
        aligned
        and snap.rsi is not None
        and 40 <= snap.rsi <= 55
        and snap.current_price is not None
        and snap.sma_50 is not None
        and abs(snap.current_price - snap.sma_50) / snap.sma_50 <= 0.04
    )

    if not (aligned or (momentum_ok and volume_ok) or pullback):
        return None

    if aligned:
        signals.append(make_signal(SignalType.PRICE_ACTION, "sma_alignment", value={"px": snap.current_price, "s50": snap.sma_50, "s200": snap.sma_200}, direction=SignalDirection.POSITIVE, strength=0.8, observed_at=ts, source=src, evidence_ref="technicals"))
        signals.append(make_signal(SignalType.PRICE_ACTION, "trend", direction=SignalDirection.POSITIVE, strength=0.75, observed_at=ts, source=src))
        reasons.append("uptrend_sma_alignment")
    if momentum_ok:
        signals.append(make_signal(SignalType.MOMENTUM, "medium_term", value={"r21": snap.return_21d, "rsi": snap.rsi}, direction=SignalDirection.POSITIVE, strength=0.65, observed_at=ts, source=src, evidence_ref="historicals"))
        reasons.append("momentum_present")
    if volume_ok:
        signals.append(make_signal(SignalType.VOLUME, "expansion", value=snap.volume_vs_avg, direction=SignalDirection.POSITIVE, strength=min(1.0, (snap.volume_vs_avg - 1.0) / 1.5), observed_at=ts, source=src))
        reasons.append("volume_expansion")
    if pullback:
        signals.append(make_signal(SignalType.PRICE_ACTION, "pullback", direction=SignalDirection.POSITIVE, strength=0.7, observed_at=ts, source=src))
        reasons.append("pullback_in_trend")
    if aligned and volume_ok:
        signals.append(make_signal(SignalType.PRICE_ACTION, "breakout", direction=SignalDirection.POSITIVE, strength=0.55, observed_at=ts, source=src))

    if snap.earnings_upcoming_days is not None and snap.earnings_upcoming_days <= 5:
        signals.append(make_signal(SignalType.EARNINGS, "near_event", value=snap.earnings_upcoming_days, direction=SignalDirection.MIXED, strength=0.5, observed_at=ts, source=src))
        flags.append("NEAR_EARNINGS")
        # Event risk can be incompatible with a clean tactical hold.
        signals.append(make_signal(SignalType.CATALYST, "event_timing", direction=SignalDirection.MIXED, strength=0.4, observed_at=ts, source=src))

    _liquidity_signal(snap, signals, ts, src)
    _maybe_regime(regime, signals, ts)

    return ChannelNomination(
        channel=DiscoveryChannel.TACTICAL_SETUP_DISCOVERY,
        sleeve=Sleeve.TACTICAL,
        sleeve_reason="short_duration_technical_or_event_setup",
        sleeve_confidence="MEDIUM",
        signals=signals,
        reasons=reasons,
        event_flags=flags,
        observations=["No requirement to fill the tactical sleeve. Setup quality over quantity."],
        thesis_type="tactical_setup",
        research_questions=["Is the setup still valid at regular-hours liquidity, and where is invalidation?"],
    )


_SPEC_THEME = re.compile(
    r"\b(clinical|pipeline|phase\s*[23]|biotech|turnaround|spac|pre-revenue|emerging)\b",
    re.I,
)


def _speculative(snap: SecuritySnapshot, regime: MarketRegime, cfg: dict) -> ChannelNomination | None:
    ts = snap.observed_at
    src = snap.sources[0] if snap.sources else "snapshot"
    signals: list[DiscoverySignal] = []
    reasons: list[str] = []
    risks: list[str] = []
    flags: list[str] = []

    # Low share price is recorded for audit and contributes ZERO to score.
    if snap.current_price is not None:
        signals.append(
            make_signal(
                SignalType.VALUATION,
                "share_price",
                value=snap.current_price,
                direction=SignalDirection.NEUTRAL,
                strength=0.0,
                observed_at=ts,
                source=src,
                evidence_ref="quote",
            )
        )

    small = snap.market_cap is not None and snap.market_cap < 2_000_000_000
    g = _growth(snap.revenue_periods)
    hyper_growth = g is not None and g >= 0.35
    unprofitable = snap.net_income_periods[0] < 0 if snap.net_income_periods else False
    catalyst = (snap.earnings_upcoming_days is not None and snap.earnings_upcoming_days <= 21) or bool(snap.news_headlines)
    blob = " ".join(filter(None, [snap.description, snap.industry, snap.name, *snap.news_headlines])).lower()
    speculative_theme = bool(_SPEC_THEME.search(blob))
    turnaround = unprofitable and (snap.drawdown_from_52w_high or 0) >= 0.40 and g is not None and g > -0.10

    kind = (snap.instrument_kind or "").lower()
    if kind == "etf" and not small:
        return None

    if not (catalyst or hyper_growth or speculative_theme or turnaround):
        return None
    # Size/uncertainty: require some high-uncertainty characteristic, not just a catalyst on a mega-cap.
    if not (small or hyper_growth or speculative_theme or turnaround or unprofitable):
        return None

    if small:
        signals.append(make_signal(SignalType.QUALITY, "asymmetric_upside", value=snap.market_cap, direction=SignalDirection.POSITIVE, strength=0.7, observed_at=ts, source=src, evidence_ref="fundamentals"))
        reasons.append("small_or_micro_cap_optionality")
        risks.append("small_cap_volatility")
    if hyper_growth:
        signals.append(make_signal(SignalType.FUNDAMENTAL, "high_growth_uncertainty", value=g, direction=SignalDirection.POSITIVE, strength=0.75, observed_at=ts, source=src, evidence_ref="financials"))
        signals.append(make_signal(SignalType.QUALITY, "asymmetric_upside", value=g, direction=SignalDirection.POSITIVE, strength=0.7, observed_at=ts, source=src))
        reasons.append("high_growth_high_uncertainty")
    if speculative_theme:
        signals.append(make_signal(SignalType.QUALITY, "optionality", value="theme", direction=SignalDirection.POSITIVE, strength=0.55, observed_at=ts, source=src))
        signals.append(make_signal(SignalType.CATALYST, "binary_or_pipeline", direction=SignalDirection.POSITIVE, strength=0.6, observed_at=ts, source=src, evidence_ref="description_or_news"))
        reasons.append("event_or_theme_optionality")
        risks.append("binary_event_risk")
    if turnaround:
        signals.append(make_signal(SignalType.QUALITY, "asymmetric_upside", direction=SignalDirection.POSITIVE, strength=0.5, observed_at=ts, source=src))
        reasons.append("turnaround_path_possible")
        risks.append("turnaround_failure")
    if catalyst:
        signals.append(make_signal(SignalType.CATALYST, "upcoming", value=snap.earnings_upcoming_days, direction=SignalDirection.POSITIVE, strength=0.6, observed_at=ts, source=src, evidence_ref="earnings_or_news"))
        reasons.append("catalyst_present")
        flags.append("CATALYST")
    if unprofitable:
        signals.append(make_signal(SignalType.FUNDAMENTAL, "profitability", value=snap.net_income_periods[0] if snap.net_income_periods else None, direction=SignalDirection.NEGATIVE, strength=0.6, observed_at=ts, source=src))
        risks.append("unprofitable")
        signals.append(make_signal(SignalType.FUNDAMENTAL, "balance_sheet", direction=SignalDirection.NEGATIVE, strength=0.35, observed_at=ts, source=src))
    else:
        signals.append(make_signal(SignalType.FUNDAMENTAL, "profitability", direction=SignalDirection.POSITIVE, strength=0.4, observed_at=ts, source=src))
        signals.append(make_signal(SignalType.FUNDAMENTAL, "balance_sheet", direction=SignalDirection.POSITIVE, strength=0.4, observed_at=ts, source=src))
    if not snap.sec_going_concern:
        signals.append(make_signal(SignalType.FUNDAMENTAL, "balance_sheet", direction=SignalDirection.POSITIVE, strength=0.35, observed_at=ts, source=src))

    dv = snap.dollar_volume
    spec_min = float(cfg["rejection"]["speculative_min_dollar_volume"])
    spec_spread = float(cfg["rejection"]["speculative_max_spread_pct"])
    if dv is not None and dv < spec_min:
        signals.append(make_signal(SignalType.LIQUIDITY, "thin_speculative", value=dv, direction=SignalDirection.NEGATIVE, strength=min(1.0, spec_min / max(dv, 1.0) / 10.0 + 0.5), observed_at=ts, source=src))
        risks.append("speculative_liquidity_concern")
    if snap.spread_pct is not None and snap.spread_pct >= spec_spread:
        signals.append(make_signal(SignalType.LIQUIDITY, "spread", value=snap.spread_pct, direction=SignalDirection.NEGATIVE, strength=0.9, observed_at=ts, source=src))
        risks.append("wide_spread")
    _liquidity_signal(snap, signals, ts, src)
    _maybe_regime(regime, signals, ts)

    risks.append("sleeve_cap_3pct_nav_per_name_5pct_total")

    return ChannelNomination(
        channel=DiscoveryChannel.SPECULATIVE_ASYMMETRY_DISCOVERY,
        sleeve=Sleeve.SPECULATIVE,
        sleeve_reason="asymmetric_upside_with_explicit_speculative_risks",
        sleeve_confidence="LOW",
        signals=signals,
        reasons=reasons,
        event_flags=flags,
        known_risks=risks,
        thesis_type="speculative_asymmetry",
        observations=["Low share price has zero positive score. Downstream cap remains 3% NAV / 5% sleeve."],
        research_questions=["What is probability-weighted downside versus claimed upside, including dilution and survival?"],
    )


def _collapsing(snap: SecuritySnapshot, cfg: dict) -> bool:
    g = _growth(snap.revenue_periods)
    md = _delta(snap.net_margin_periods)
    drop = float(cfg["rejection"]["collapsing_revenue_drop"])
    mdrop = float(cfg["rejection"]["collapsing_margin_drop"])
    if g is not None and g <= drop:
        if md is not None and md <= -mdrop:
            return True
        if snap.net_income_periods and snap.net_income_periods[0] < 0:
            return True
        if g <= drop - 0.10:
            return True
    return False


def _liquidity_signal(snap: SecuritySnapshot, signals: list[DiscoverySignal], ts: str, src: str) -> None:
    dv = snap.dollar_volume
    if dv is None:
        signals.append(make_signal(SignalType.LIQUIDITY, "unknown", direction=SignalDirection.NEUTRAL, strength=0.0, observed_at=ts, source=src))
        return
    if dv >= 20_000_000:
        signals.append(make_signal(SignalType.LIQUIDITY, "dollar_volume", value=dv, direction=SignalDirection.POSITIVE, strength=0.8, observed_at=ts, source=src, evidence_ref="volume"))
    elif dv >= 5_000_000:
        signals.append(make_signal(SignalType.LIQUIDITY, "dollar_volume", value=dv, direction=SignalDirection.POSITIVE, strength=0.5, observed_at=ts, source=src, evidence_ref="volume"))
    elif dv >= 1_000_000:
        signals.append(make_signal(SignalType.LIQUIDITY, "dollar_volume", value=dv, direction=SignalDirection.NEUTRAL, strength=0.3, observed_at=ts, source=src))
    else:
        signals.append(make_signal(SignalType.LIQUIDITY, "dollar_volume", value=dv, direction=SignalDirection.NEGATIVE, strength=0.7, observed_at=ts, source=src))


def _maybe_regime(regime: MarketRegime, signals: list[DiscoverySignal], ts: str) -> None:
    if regime.status != MarketRegimeStatus.OBSERVED:
        return
    if regime.spy_trend:
        direction = SignalDirection.POSITIVE if regime.spy_trend in {"up", "UP", "bullish"} else (
            SignalDirection.NEGATIVE if regime.spy_trend in {"down", "DOWN", "bearish"} else SignalDirection.NEUTRAL
        )
        signals.append(
            make_signal(
                SignalType.MARKET_REGIME,
                "spy_trend",
                value=regime.spy_trend,
                direction=direction,
                strength=0.3 if regime.confidence in {"HIGH", "MEDIUM"} else 0.15,
                observed_at=regime.observed_at or ts,
                source=regime.source or "regime",
            )
        )
