"""Sleeve-specific discovery scoring.

Weights are transparent heuristics in config/discovery.json. They are not
backtested. Discovery score is a function of structured signals only — NAV
size is not an input.
"""

from __future__ import annotations

from agentic_portfolio.discovery.signals import SHARE_PRICE_SIGNAL_NAMES, contribution
from agentic_portfolio.policy import load_discovery_config
from agentic_portfolio.schemas import DiscoverySignal, Sleeve


def score_signals(
    signals: list[DiscoverySignal],
    sleeve: Sleeve,
    config: dict | None = None,
) -> tuple[float, dict[str, float]]:
    """Return (0..100 score, per-factor breakdown). Reproducible from signals."""
    cfg = config or load_discovery_config()
    sleeve_cfg = cfg["scoring"][sleeve.value]
    weights: dict[str, float] = dict(sleeve_cfg["weights"])
    fmap = cfg.get("factor_signal_map") or {}
    zero_names = frozenset(n.lower() for n in cfg.get("zero_score_signal_names", [])) | SHARE_PRICE_SIGNAL_NAMES

    breakdown: dict[str, float] = {}
    weighted = 0.0
    present_w = 0.0
    total_w = 0.0
    cov_cfg = cfg.get("scoring_coverage") or {}
    haircut_base = float(cov_cfg.get("haircut_base", 0.55))
    haircut_cov = float(cov_cfg.get("haircut_coverage_weight", 0.45))
    for factor, weight in weights.items():
        total_w += weight
        matchers = fmap.get(factor) or []
        if not _any_match(signals, matchers, zero_names):
            # Missing evidence is not a fabricated 0. Skip the factor and haircut later.
            continue
        factor_score = _factor_score(signals, matchers, zero_names)
        present_w += weight
        if sleeve == Sleeve.SPECULATIVE and factor == "liquidity" and sleeve_cfg.get("liquidity_is_penalty_if_weak") and factor_score < 0:
            # Weak speculative liquidity subtracts; other factors never go negative.
            breakdown[factor] = factor_score
            weighted += weight * factor_score
        else:
            breakdown[factor] = factor_score
            weighted += weight * max(0.0, factor_score)

    extra_penalty = _hard_penalties(signals, zero_names)
    coverage = (present_w / total_w) if total_w else 0.0
    inner = (weighted / present_w) if present_w else 0.0
    coverage_haircut = haircut_base + haircut_cov * coverage
    raw = 100.0 * inner * coverage_haircut
    score = max(0.0, min(100.0, raw - 100.0 * extra_penalty))
    breakdown["_penalty"] = extra_penalty
    breakdown["_coverage"] = coverage
    breakdown["_raw"] = raw
    return round(score, 4), breakdown


def _factor_score(
    signals: list[DiscoverySignal],
    matchers: list[dict],
    zero_names: frozenset[str],
) -> float:
    matched = [s for s in signals if _matches(s, matchers)]
    if not matched:
        return 0.0
    vals = [contribution(s, zero_score_names=zero_names) for s in matched]
    return max(-1.0, min(1.0, sum(vals) / len(vals)))


def _any_match(signals: list[DiscoverySignal], matchers: list[dict], zero_names: frozenset[str]) -> bool:
    if not matchers:
        return False
    for s in signals:
        if s.name.lower() in zero_names:
            continue
        if _matches(s, matchers):
            return True
    return False


def _matches(signal: DiscoverySignal, matchers: list[dict]) -> bool:
    for m in matchers:
        st = m.get("signal_type")
        if st and signal.signal_type.value != st:
            continue
        name = m.get("name")
        if name and signal.name != name:
            continue
        return True
    return False


def _hard_penalties(signals: list[DiscoverySignal], zero_names: frozenset[str]) -> float:
    penalty = 0.0
    for s in signals:
        if s.signal_type.value == "RISK_FLAG" and s.direction.value == "NEGATIVE":
            if s.name in {"going_concern", "unsupported_instrument", "non_tradable", "unusable_liquidity"}:
                penalty += 0.35 * s.strength
            else:
                penalty += 0.15 * s.strength
        if s.name == "collapsing_fundamentals" and s.direction.value == "NEGATIVE":
            penalty += 0.30 * s.strength
    return min(1.0, penalty)
