"""Send-time Risk Gate re-evaluation.

An authorization is not permission to execute stale state. Right before the
broker call the executor rebuilds the ProposedAction captured at proposal time,
refreshes price/size/spread from the live quote and fresh portfolio context, and
runs the full deterministic Risk Gate again.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from agentic_portfolio.schemas import Decision, LiquidityInputs, PortfolioContext, RiskGateResult

HUMAN_REVIEW_FLAGS = (
    "enhanced_concentration_review_complete",
    "high_concentration_review_complete",
    "sector_concentration_review_complete",
    "speculative_liquidity_review_complete",
    "opportunistic_enhanced_risk_review_complete",
)


def proposed_action_from_snapshot(snapshot: dict[str, Any] | None):
    if not snapshot:
        return None
    from agentic_portfolio.approval.types import proposed_action_from_dict

    try:
        return proposed_action_from_dict(dict(snapshot))
    except (KeyError, ValueError, TypeError):
        return None


def send_time_risk(
    approval: Any,
    context: PortfolioContext,
    *,
    price: float | None,
    notional: float | None,
    spread_pct: float | None = None,
    root: Path | None = None,
    human_decided: bool = False,
) -> RiskGateResult | None:
    """Re-run the Risk Gate on fresh facts. None when no snapshot exists."""
    from agentic_portfolio.risk_gate import evaluate
    from agentic_portfolio.sleeve_registry import SleeveRegistry
    from agentic_portfolio.thesis_registry import ThesisRegistry

    action = proposed_action_from_snapshot(getattr(approval, "proposed_action_snapshot", None))
    if action is None:
        return None
    action.current_price = price if price is not None else action.current_price
    action.proposed_notional = notional
    # Recompute exposures from the fresh context instead of proposal-time estimates.
    action.expected_resulting_position_pct = None
    action.expected_resulting_sleeve_pct = None
    action.expected_resulting_sector_pct = None
    if spread_pct is not None:
        action.liquidity = replace(action.liquidity or LiquidityInputs(), bid_ask_spread_pct=spread_pct)
    if human_decided:
        # A human APPROVE of an escalated proposal is the enhanced review the gate asked for.
        for flag in HUMAN_REVIEW_FLAGS:
            setattr(action, flag, True)
        if action.decision in {Decision.SELL, Decision.REDUCE}:
            action.human_authorized_halted_execution = True
    sleeves = theses = None
    if root is not None:
        base = Path(root)
        sleeves = SleeveRegistry(base / "state" / "sleeve_registry.json")
        theses = ThesisRegistry(base / "state" / "thesis_registry.json")
    return evaluate(context, action, sleeves=sleeves, theses=theses)
