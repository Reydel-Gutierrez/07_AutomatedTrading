from __future__ import annotations

from agentic_portfolio.policy import load_policy
from agentic_portfolio.schemas import Decision, LiquidityInputs, Sleeve


def liquidity_ok(
    *,
    sleeve: Sleeve | None,
    decision: Decision,
    proposed_notional: float | None,
    liq: LiquidityInputs,
    speculative_review_complete: bool,
) -> tuple[bool, list[str], list[str]]:
    """Return (ok_for_new_risk, fail_codes, required_reviews)."""
    policy = load_policy()["liquidity"]
    risk_increasing = decision in {Decision.BUY, Decision.ADD}
    if not risk_increasing:
        return True, [], []

    adv = liq.median_daily_dollar_volume_20d
    if adv is None or adv <= 0 or proposed_notional is None:
        if policy.get("fail_closed_if_data_unavailable_for_new_risk"):
            return False, ["LIQUIDITY_INSUFFICIENT_EVIDENCE"], []
        return False, ["LIQUIDITY_INSUFFICIENT_EVIDENCE"], []

    reviews: list[str] = []
    if sleeve == Sleeve.SPECULATIVE:
        frac = float(policy["speculative"]["max_order_notional_fraction_of_adv"])
        reviews.append("SPECULATIVE_LIQUIDITY_REVIEW")
        if not speculative_review_complete:
            return False, ["SPECULATIVE_LIQUIDITY_REVIEW_REQUIRED"], reviews
    else:
        frac = float(policy["normal"]["max_order_notional_fraction_of_adv"])

    if proposed_notional > frac * adv:
        return False, ["ORDER_NOTIONAL_EXCEEDS_ADV_FRACTION"], reviews
    return True, [], reviews
