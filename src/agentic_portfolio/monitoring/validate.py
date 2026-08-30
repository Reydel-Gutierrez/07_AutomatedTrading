"""Validate monitoring AI output. Process/schema only — no stock-picking rules."""

from __future__ import annotations

from typing import Any

from agentic_portfolio.monitoring.types import MONITORING_ACTIONS, MonitoringState, ThesisReassessment
from agentic_portfolio.schemas import Decision, ThesisStatus

VALID_STATES = {s.value for s in MonitoringState}
VALID_THESIS = {s.value for s in (ThesisStatus.UNCHANGED, ThesisStatus.STRENGTHENED, ThesisStatus.WEAKENED, ThesisStatus.INVALIDATED)}
VALID_ACTIONS = {d.value for d in MONITORING_ACTIONS}
VALID_OPP = {"LIKELY_DISLOCATION", "MIXED", "LIKELY_DETERIORATION", "INSUFFICIENT_EVIDENCE"}
PROTECTED_KEYS = {
    "current_nav",
    "cash",
    "buying_power",
    "positions",
    "holdings_count",
    "high_water_mark",
    "risk_limits",
    "security_class",
    "classification_status",
}
ACTION_ALIASES = {"EXIT": "SELL", "WATCH": "NO_ACTION", "REJECT": "NO_ACTION"}


class MonitoringValidationError(ValueError):
    """Malformed monitoring AI output."""


def validate_payload(payload: Any, symbol: str) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(payload, dict):
        raise MonitoringValidationError("AI output is not an object")
    unsupported: list[str] = []
    item = dict(payload)
    for key in PROTECTED_KEYS:
        if key in item and item[key] is not None:
            unsupported.append(f"attempted_override:{key}")
            item.pop(key, None)
    item["symbol"] = str(item.get("symbol") or symbol).upper()
    status = str(item.get("thesis_status") or ThesisStatus.UNCHANGED.value).upper()
    if status not in VALID_THESIS:
        raise MonitoringValidationError(f"invalid thesis_status: {item.get('thesis_status')}")
    item["thesis_status"] = status
    state = str(item.get("monitoring_state") or MonitoringState.REVIEW_REQUIRED.value).upper()
    if state not in VALID_STATES:
        raise MonitoringValidationError(f"invalid monitoring_state: {item.get('monitoring_state')}")
    item["monitoring_state"] = state
    raw_action = str(item.get("recommended_action") or Decision.NO_ACTION.value).upper()
    raw_action = ACTION_ALIASES.get(raw_action, raw_action)
    if raw_action not in VALID_ACTIONS:
        raise MonitoringValidationError(f"invalid recommended_action: {item.get('recommended_action')}")
    item["recommended_action"] = raw_action
    alloc = item.get("desired_allocation_pct")
    if alloc is not None:
        try:
            alloc = float(alloc)
        except (TypeError, ValueError) as exc:
            raise MonitoringValidationError("desired_allocation_pct not numeric") from exc
        if alloc < 0 or alloc > 100:
            raise MonitoringValidationError("desired_allocation_pct out of range")
    item["desired_allocation_pct"] = alloc
    if raw_action == Decision.REDUCE.value and alloc is None:
        raise MonitoringValidationError("REDUCE requires desired_allocation_pct")
    if raw_action == Decision.SELL.value:
        item["desired_allocation_pct"] = 0.0 if alloc is None else alloc
    opp = item.get("opportunistic_verdict")
    if opp is not None and str(opp).upper() not in VALID_OPP:
        raise MonitoringValidationError(f"invalid opportunistic_verdict: {opp}")
    if opp is not None:
        item["opportunistic_verdict"] = str(opp).upper()
    if item.get("broker_stop_orders_created"):
        raise MonitoringValidationError("broker stop orders are not allowed")
    item["broker_stop_orders_created"] = False
    return item, unsupported


def to_reassessment(
    item: dict[str, Any],
    *,
    thesis_id: str | None,
    prior_status: str | None,
    unsupported: list[str],
) -> ThesisReassessment:
    return ThesisReassessment(
        symbol=item["symbol"],
        thesis_id=thesis_id,
        prior_status=prior_status,
        new_status=ThesisStatus(item["thesis_status"]),
        monitoring_state=MonitoringState(item["monitoring_state"]),
        recommended_action=Decision(item["recommended_action"]),
        desired_allocation_pct=item.get("desired_allocation_pct"),
        rationale=item.get("rationale"),
        opportunistic_verdict=item.get("opportunistic_verdict"),
        tactical_invalidation_detected=bool(item.get("tactical_invalidation_detected")),
        speculative_invalidation_detected=bool(item.get("speculative_invalidation_detected")),
        exit_condition_triggered=bool(item.get("exit_condition_triggered")),
        research_refresh_needed=bool(item.get("research_refresh_needed")),
        broker_stop_orders_created=False,
        unsupported_claims=list(unsupported),
    )
