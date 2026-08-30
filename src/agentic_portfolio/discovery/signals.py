"""Structured discovery signals. Scores are computed from these records."""

from __future__ import annotations

from typing import Any

from agentic_portfolio.schemas import DiscoverySignal, SignalDirection, SignalType

SHARE_PRICE_SIGNAL_NAMES = frozenset({"share_price", "nominal_price", "penny_stock", "low_price"})


def make_signal(
    signal_type: SignalType | str,
    name: str,
    *,
    value: Any = None,
    direction: SignalDirection | str = SignalDirection.NEUTRAL,
    strength: float = 0.0,
    observed_at: str | None = None,
    source: str | None = None,
    evidence_ref: str | None = None,
) -> DiscoverySignal:
    st = signal_type if isinstance(signal_type, SignalType) else SignalType(signal_type)
    d = direction if isinstance(direction, SignalDirection) else SignalDirection(direction)
    strength = max(0.0, min(1.0, float(strength)))
    return DiscoverySignal(
        signal_type=st,
        name=name,
        value=value,
        direction=d,
        strength=strength,
        observed_at=observed_at,
        source=source,
        evidence_ref=evidence_ref,
    )


def direction_sign(direction: SignalDirection) -> float:
    if direction == SignalDirection.POSITIVE:
        return 1.0
    if direction == SignalDirection.NEGATIVE:
        return -1.0
    return 0.0


def contribution(signal: DiscoverySignal, *, zero_score_names: frozenset[str] | None = None) -> float:
    """Numeric contribution used by sleeve scoring.

    Nominal share price never contributes, even if a caller tags it POSITIVE.
    """
    names = zero_score_names or SHARE_PRICE_SIGNAL_NAMES
    if signal.name.lower() in names:
        return 0.0
    return direction_sign(signal.direction) * float(signal.strength)


def merge_signals(existing: list[DiscoverySignal], incoming: list[DiscoverySignal]) -> list[DiscoverySignal]:
    """Keep the stronger signal when type+name collide; append distinct ones."""
    by_key: dict[tuple[str, str], DiscoverySignal] = {}
    order: list[tuple[str, str]] = []
    for sig in existing + incoming:
        key = (sig.signal_type.value, sig.name)
        if key not in by_key:
            by_key[key] = sig
            order.append(key)
            continue
        prior = by_key[key]
        if abs(contribution(sig)) > abs(contribution(prior)):
            refs = []
            if prior.evidence_ref:
                refs.append(prior.evidence_ref)
            if sig.evidence_ref and sig.evidence_ref not in refs:
                refs.append(sig.evidence_ref)
            if refs:
                sig.evidence_ref = "|".join(refs)
            by_key[key] = sig
        elif prior.evidence_ref and sig.evidence_ref and sig.evidence_ref not in (prior.evidence_ref or ""):
            prior.evidence_ref = f"{prior.evidence_ref}|{sig.evidence_ref}"
    return [by_key[k] for k in order]
