"""Expand compact CORE committee AI output into the canonical decision payload.

The committee compares many alternatives in one call. Rankings and cash are
compact. Full thesis material is required only for selected BUY/ADD names.
"""

from __future__ import annotations

from typing import Any

from agentic_portfolio.decision.types import CASH_SYMBOL, SPY_SYMBOL
from agentic_portfolio.schemas import Decision

RISK_UP_VALUES = {Decision.BUY.value, Decision.ADD.value}
WATCH_LIKE = {Decision.WATCH.value, Decision.REJECT.value, Decision.NO_ACTION.value, Decision.HOLD.value}


def is_compact_committee_payload(payload: Any) -> bool:
    """True when the model returned the compact committee schema, not theses/decisions."""
    if not isinstance(payload, dict):
        return False
    if "decisions" in payload:
        return False
    return "rankings" in payload or "selected_allocations" in payload or "portfolio_action" in payload


def expand_compact_committee_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministically adapt compact committee output to theses/decisions/comparison."""
    rankings_raw = payload.get("rankings") or []
    if rankings_raw and not isinstance(rankings_raw, list):
        raise ValueError("rankings must be a list")
    selected_raw = payload.get("selected_allocations") or []
    if selected_raw and not isinstance(selected_raw, list):
        raise ValueError("selected_allocations must be a list")
    cash_raw = payload.get("cash") if isinstance(payload.get("cash"), dict) else {}
    comparison_raw = payload.get("comparison") if isinstance(payload.get("comparison"), dict) else {}

    selected_by: dict[str, dict[str, Any]] = {}
    theses: list[dict[str, Any]] = []
    for item in selected_raw:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        row = dict(item)
        sym = str(row["symbol"]).upper()
        row["symbol"] = sym
        action = str(row.get("action") or row.get("decision") or "").upper()
        if action == "EXIT":
            action = "SELL"
        row["action"] = action
        selected_by[sym] = row
        if action in RISK_UP_VALUES:
            theses.append(_thesis_from_selected(row))

    ranked_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rankings_raw:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        row = dict(item)
        sym = str(row["symbol"]).upper()
        row["symbol"] = sym
        action = str(row.get("action") or row.get("decision") or "").upper()
        if action == "EXIT":
            action = "SELL"
        if not action:
            action = Decision.WATCH.value if sym != CASH_SYMBOL else Decision.HOLD.value
        row["action"] = action
        ranked_items.append(row)
        seen.add(sym)

    for sym, row in selected_by.items():
        if sym not in seen:
            ranked_items.append(
                {
                    "symbol": sym,
                    "rank": len(ranked_items) + 1,
                    "action": row.get("action") or Decision.WATCH.value,
                    "confidence": row.get("confidence"),
                    "concise_reason": row.get("rationale") or row.get("why_vs_alternatives") or "",
                }
            )
            seen.add(sym)

    if CASH_SYMBOL not in seen:
        ranked_items.append(
            {
                "symbol": CASH_SYMBOL,
                "rank": len(ranked_items) + 1,
                "action": str(cash_raw.get("action") or Decision.HOLD.value).upper(),
                "concise_reason": str(cash_raw.get("rationale") or ""),
            }
        )
        seen.add(CASH_SYMBOL)
    if SPY_SYMBOL not in seen:
        ranked_items.append(
            {
                "symbol": SPY_SYMBOL,
                "rank": len(ranked_items) + 1,
                "action": Decision.NO_ACTION.value,
                "concise_reason": "Broad-market residual considered; not selected.",
            }
        )
        seen.add(SPY_SYMBOL)

    ranked_items.sort(key=lambda row: (_rank_key(row.get("rank")), str(row.get("symbol") or "")))
    ranking = [str(row["symbol"]).upper() for row in ranked_items]

    selected_names = [sym for sym, row in selected_by.items() if row.get("action") in RISK_UP_VALUES]
    vs_cash = comparison_raw.get("vs_cash") or cash_raw.get("rationale") or (
        "Selected residual improves expected CORE outcome versus retaining cash."
        if selected_names
        else "Cash is retained as the residual. Unused sleeve capacity is not a mandate to buy."
    )
    vs_spy = comparison_raw.get("vs_spy") or (
        "Selected residual versus generic beta, or cash if no residual is justified."
        if selected_names
        else "Prefer cash residual to funding generic beta solely because capital is available."
    )
    comparison = {
        "ranking": ranking,
        "vs_cash": vs_cash,
        "vs_spy": vs_spy,
        "notes": comparison_raw.get("notes") or str(payload.get("portfolio_action") or ""),
    }
    if comparison_raw.get("ranking_dimensions"):
        comparison["ranking_dimensions"] = comparison_raw["ranking_dimensions"]

    cash_weight = cash_raw.get("target_weight")
    if cash_weight is None:
        allocated = sum(float(selected_by[s].get("target_weight") or 0.0) for s in selected_names)
        cash_weight = max(0.0, 100.0 - allocated)
    cash_action = str(cash_raw.get("action") or Decision.HOLD.value).upper()
    if cash_action not in {Decision.HOLD.value, Decision.NO_ACTION.value, Decision.WATCH.value}:
        cash_action = Decision.HOLD.value

    decisions: list[dict[str, Any]] = []
    decided: set[str] = set()
    for row in ranked_items:
        sym = row["symbol"]
        if sym in decided:
            continue
        if sym == CASH_SYMBOL:
            decisions.append(
                {
                    "symbol": CASH_SYMBOL,
                    "decision": cash_action,
                    "desired_allocation_pct": float(cash_weight) if cash_weight is not None else None,
                    "rationale": str(cash_raw.get("rationale") or row.get("concise_reason") or "Cash remains a valid residual."),
                }
            )
            decided.add(CASH_SYMBOL)
            continue
        selected = selected_by.get(sym)
        if selected is not None and selected.get("action") in RISK_UP_VALUES:
            decisions.append(_decision_from_selected(selected))
        else:
            action = str((selected or row).get("action") or Decision.WATCH.value)
            if action in RISK_UP_VALUES:
                action = Decision.WATCH.value
            decisions.append(_watch_like_decision(row, action=action, lost_to=selected_names))
        decided.add(sym)

    return {
        "theses": theses,
        "comparison": comparison,
        "decisions": decisions,
        "portfolio_action": payload.get("portfolio_action"),
    }


def _rank_key(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 10_000


def _thesis_from_selected(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": row["symbol"],
        "research_id": row.get("research_id"),
        "sleeve": row.get("sleeve") or "CORE_GROWTH",
        "thesis_summary": row.get("thesis_summary") or "",
        "bull_case": row.get("bull_case") or "",
        "base_case": row.get("base_case") or "",
        "bear_case": row.get("bear_case") or "",
        "catalysts": list(row.get("catalysts") or []),
        "thesis_drivers": list(row.get("thesis_drivers") or []),
        "risks": list(row.get("risks") or []),
        "horizon": row.get("horizon") or "",
        "invalidation_conditions": list(row.get("invalidation_conditions") or []),
        "review_triggers": list(row.get("review_triggers") or []),
        "why_position_should_exist": row.get("why_position_should_exist") or row.get("why_vs_cash") or "",
        "confidence": str(row.get("confidence") or "MEDIUM").upper(),
        "exit_policy": row.get("exit_policy") or {
            "thesis_based": True,
            "mandatory_fixed_stop_loss": False,
            "broker_stop_orders_created": False,
        },
        "status": "DRAFT",
    }


def _decision_from_selected(row: dict[str, Any]) -> dict[str, Any]:
    alloc = row.get("target_weight")
    if alloc is None:
        alloc = row.get("desired_allocation_pct")
    out = {
        "symbol": row["symbol"],
        "decision": row["action"],
        "desired_allocation_pct": alloc,
        "starter_position": bool(row.get("starter_position")),
        "rationale": row.get("rationale") or row.get("why_vs_alternatives") or row.get("thesis_summary") or "",
        "why_preferable_to_cash": row.get("why_vs_cash") or row.get("why_preferable_to_cash"),
        "why_preferable_to_spy": row.get("why_vs_spy") or row.get("why_preferable_to_spy"),
        "why_preferable_to_alternatives": row.get("why_vs_alternatives") or row.get("why_preferable_to_alternatives"),
    }
    return {key: value for key, value in out.items() if value is not None}


def _watch_like_decision(row: dict[str, Any], *, action: str, lost_to: list[str]) -> dict[str, Any]:
    reason = str(row.get("concise_reason") or row.get("rationale") or f"{row['symbol']} is not the residual allocation today.")
    recon = row.get("reconsideration") if isinstance(row.get("reconsideration"), dict) else {}
    lost = recon.get("lost_to") or row.get("lost_to") or list(lost_to or []) or [CASH_SYMBOL]
    if isinstance(lost, str):
        lost = [lost]
    item = {
        "symbol": row["symbol"],
        "decision": action if action in WATCH_LIKE | {Decision.REDUCE.value, Decision.SELL.value} else Decision.WATCH.value,
        "desired_allocation_pct": row.get("target_weight") if row.get("target_weight") is not None else 0,
        "rationale": reason,
        "reconsideration": {
            "why_lost": recon.get("why_lost") or row.get("why_lost") or reason,
            "lost_to": [str(name).upper() for name in lost if str(name).strip()],
            "valuation_condition": recon.get("valuation_condition") or row.get("valuation_condition"),
            "thesis_condition": recon.get("thesis_condition") or row.get("thesis_condition"),
            "required_evidence_improvement": recon.get("required_evidence_improvement")
            or row.get("required_evidence_improvement"),
            "next_review_reason": recon.get("next_review_reason") or row.get("next_review_reason") or "committee_residual",
            "next_review_at": recon.get("next_review_at") or row.get("next_review_at"),
        },
    }
    if row.get("confidence"):
        item["confidence"] = row["confidence"]
    if row.get("score") is not None:
        item["score"] = row["score"]
    return item
