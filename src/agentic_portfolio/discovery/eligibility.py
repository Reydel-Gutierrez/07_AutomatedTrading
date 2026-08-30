"""Universe eligibility and hard rejection. Not the full risk gate."""

from __future__ import annotations

from agentic_portfolio.discovery.signals import make_signal
from agentic_portfolio.discovery.snapshot import SecuritySnapshot
from agentic_portfolio.policy import load_discovery_config
from agentic_portfolio.schemas import SignalDirection, SignalType


def hard_reject(
    snap: SecuritySnapshot,
    config: dict | None = None,
) -> tuple[str | None, list[str], list]:
    """Return (reason, evidence, risk_signals) if the name should not enter discovery."""
    cfg = (config or load_discovery_config())["rejection"]
    evidence: list[str] = []
    signals = []

    if not snap.symbol:
        return "missing_identity", ["symbol_missing"], [
            make_signal(SignalType.RISK_FLAG, "missing_identity", direction=SignalDirection.NEGATIVE, strength=1.0, observed_at=snap.observed_at)
        ]
    if snap.data_stale and cfg.get("stale_data_rejects"):
        return "stale_data", ["snapshot.data_stale=true"], [
            make_signal(SignalType.RISK_FLAG, "stale_data", direction=SignalDirection.NEGATIVE, strength=1.0, observed_at=snap.observed_at, source=snap.sources[0] if snap.sources else None)
        ]
    if snap.tradable is False and cfg.get("non_tradable_rejects"):
        evidence.append(f"tradable={snap.tradable}")
        if snap.trade_state:
            evidence.append(f"trade_state={snap.trade_state}")
        return "non_tradable", evidence, [
            make_signal(SignalType.RISK_FLAG, "non_tradable", value=snap.trade_state, direction=SignalDirection.NEGATIVE, strength=1.0, observed_at=snap.observed_at)
        ]
    if (snap.is_leveraged or snap.is_inverse) and cfg.get("leveraged_inverse_rejects"):
        return "unsupported_instrument_leveraged_or_inverse", [f"leveraged={snap.is_leveraged}", f"inverse={snap.is_inverse}"], [
            make_signal(SignalType.RISK_FLAG, "unsupported_instrument", direction=SignalDirection.NEGATIVE, strength=1.0, observed_at=snap.observed_at)
        ]
    kind = (snap.instrument_kind or "").lower()
    if kind in {"option", "crypto", "future", "event"}:
        return "unsupported_instrument", [f"instrument_kind={kind}"], [
            make_signal(SignalType.RISK_FLAG, "unsupported_instrument", value=kind, direction=SignalDirection.NEGATIVE, strength=1.0, observed_at=snap.observed_at)
        ]
    if snap.sec_going_concern and cfg.get("going_concern_rejects"):
        return "going_concern", ["sec_going_concern=true"], [
            make_signal(SignalType.RISK_FLAG, "going_concern", direction=SignalDirection.NEGATIVE, strength=1.0, observed_at=snap.observed_at, source="sec")
        ]
    if snap.sec_dilution_flag:
        signals.append(
            make_signal(SignalType.RISK_FLAG, "dilution", direction=SignalDirection.NEGATIVE, strength=0.8, observed_at=snap.observed_at, source="sec")
        )
        # Dilution is a strong speculative/core concern; reject if also unprofitable with no cash-flow evidence.
        if snap.net_income_periods and snap.net_income_periods[0] < 0:
            return "severe_dilution", ["sec_dilution_flag=true", "latest_net_income<0"], signals

    spread = snap.spread_pct
    max_spread = float(cfg.get("max_bid_ask_spread_pct") or 0.08)
    if spread is not None and spread >= max_spread:
        return "extreme_spread", [f"spread_pct={spread:.4f}"], [
            make_signal(SignalType.LIQUIDITY, "spread", value=spread, direction=SignalDirection.NEGATIVE, strength=min(1.0, spread / max_spread), observed_at=snap.observed_at)
        ]

    dv = snap.dollar_volume
    unusable = float(cfg.get("unusable_dollar_volume") or 50_000)
    if dv is not None and dv < unusable:
        return "unusable_liquidity", [f"dollar_volume={dv}"], [
            make_signal(SignalType.LIQUIDITY, "unusable_liquidity", value=dv, direction=SignalDirection.NEGATIVE, strength=1.0, observed_at=snap.observed_at)
        ]

    if cfg.get("missing_identity_rejects") and snap.tradable is None and not snap.name and not snap.instrument_kind:
        return "insufficient_data", ["no_tradability_name_or_instrument_kind"], [
            make_signal(SignalType.RISK_FLAG, "insufficient_data", direction=SignalDirection.NEGATIVE, strength=1.0, observed_at=snap.observed_at)
        ]

    return None, evidence, signals


def liquidity_status(snap: SecuritySnapshot, config: dict | None = None) -> str:
    cfg = (config or load_discovery_config())["rejection"]
    dv = snap.dollar_volume
    spread = snap.spread_pct
    if dv is None and spread is None:
        return "UNKNOWN"
    if dv is not None and dv < float(cfg["unusable_dollar_volume"]):
        return "UNUSABLE"
    spec_min = float(cfg["speculative_min_dollar_volume"])
    if (dv is not None and dv < spec_min) or (spread is not None and spread >= float(cfg["speculative_max_spread_pct"])):
        return "THIN"
    if dv is not None:
        return "ADEQUATE"
    return "PARTIAL"
