"""Paper Human Approval Packet.

Packages existing PAPER_ONLY OrderPlans into human-readable approval packets.
APPROVED would still not place a live order. No review/place/cancel. No transfers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agentic_portfolio.approval.engine import run_approval
from agentic_portfolio.approval.report import render_packet, render_run
from agentic_portfolio.approval.store import ApprovalStore
from agentic_portfolio.approval.types import ApprovalRequest, proposed_action_from_dict, risk_from_dict
from agentic_portfolio.context import build_context
from agentic_portfolio.decision.types import NameDecision, PortfolioComparison
from agentic_portfolio.execution.store import OrderPlanStore
from agentic_portfolio.execution.types import QuoteSnapshot
from agentic_portfolio.monitoring.store import MonitoringStore
from agentic_portfolio.paper_fill.types import order_plan_from_dict
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.schemas import ClassificationStatus, Decision, Position, SecurityClass, Sleeve
from agentic_portfolio.thesis_registry import ThesisRegistry

NOW = datetime(2026, 8, 30, 18, 30, tzinfo=timezone.utc)
TS = NOW.isoformat()
ACCOUNT = load_account_rules()["account"]["account_number"]
NAV = 10_000.0
ORDER_PLAN_RUN_ID = "efbd6372-3b7a-43b6-bd1e-a43843e0ba24"
MONITOR_RUN_ID = "3401b2d2-8dbe-4bb8-9b08-148255c21154"
PRICES = {"NVDA": 180.0, "NKE": 60.0, "ESTC": 70.0, "IONQ": 8.0}
PCTS = {"NVDA": 0.05, "NKE": 0.04, "ESTC": 0.02, "IONQ": 0.01}
SLEEVES = {
    "NVDA": Sleeve.CORE_GROWTH,
    "NKE": Sleeve.OPPORTUNISTIC,
    "ESTC": Sleeve.TACTICAL,
    "IONQ": Sleeve.SPECULATIVE,
}


def _held(symbol, pct, sleeve, price, sector=None):
    return Position(
        symbol=symbol,
        market_value=pct * NAV,
        quantity=(pct * NAV) / price,
        current_price=price,
        sleeve=sleeve,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        classification_status=ClassificationStatus.VALIDATED,
        sector=sector,
    )


def _quote(symbol, price):
    half = price * 0.001 / 2.0
    return QuoteSnapshot(
        symbol=symbol,
        last_price=price,
        bid=price - half,
        ask=price + half,
        spread_pct=0.001,
        observed_at=TS,
        stale=False,
        source="paper_order_plan",
    )


def _monitor_by_symbol(raw: dict) -> dict[str, dict]:
    return {str(row["symbol"]).upper(): row for row in raw.get("positions") or []}


def _gated(row: dict) -> tuple:
    gated = (row.get("gated_actions") or [{}])[0]
    action_raw = gated.get("proposed_action") or {}
    risk_raw = gated.get("risk") or {}
    return action_raw, risk_raw


def _write_reports(result, path_json: Path, path_md: Path, example) -> None:
    payload = {
        "run": "paper_approval",
        "observed_at": TS,
        "run_id": result.run_id,
        "source_order_plan_run_id": ORDER_PLAN_RUN_ID,
        "source_monitoring_run_id": MONITOR_RUN_ID,
        "nav_observed": NAV,
        "nav_is_not_a_policy_constraint": True,
        "note": "Human approval packets from existing paper OrderPlans. APPROVED does not place a live order. Live book remains 100% cash.",
        "packets": [
            {
                "approval_id": p.approval_id,
                "symbol": p.symbol,
                "action": p.action.value,
                "status": p.status.value,
                "current_allocation_pct": p.current_allocation_pct,
                "desired_allocation_pct": p.desired_allocation_pct,
                "order_notional": p.order_notional,
                "order_quantity": p.order_quantity,
                "current_price": p.current_price,
                "sleeve": p.sleeve.value if p.sleeve else None,
                "risk_gate_verdict": p.risk_gate_verdict,
                "broker_submitted": p.broker_submitted,
                "approved_does_not_place_order": p.approved_does_not_place_order,
            }
            for p in result.packets
        ],
        "skipped": [{"symbol": s.symbol, "action": s.action.value, "reason": s.reason} for s in result.skipped],
        "nvda_hold": "HOLD created no OrderPlan and no approval packet.",
        "execution_attempted": False,
        "broker_orders_submitted": 0,
        "live_execution_attempted": False,
        "mcp_not_called": [
            "review_equity_order",
            "place_equity_order",
            "cancel_equity_order",
            "create_scan",
            "watchlist_writes",
            "any_deposit_withdrawal_transfer",
        ],
    }
    path_json.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    md = [
        "# Paper Human Approval Packets",
        "",
        f"Observed at {TS}. Packets from existing paper OrderPlans on a $10,000 paper NAV.",
        "Live book remains 100% cash. APPROVED still does not place a live order. No broker calls.",
        "",
        render_run(result),
        "",
        "NVDA HOLD created no OrderPlan and no approval packet.",
        "",
    ]
    if example is not None:
        md += ["## Example packet (human-readable)", "", render_packet(example), ""]
    path_md.write_text("\n".join(md) + "\n", encoding="utf-8")


def main() -> None:
    root = project_root()
    positions = [
        _held("NVDA", PCTS["NVDA"], SLEEVES["NVDA"], PRICES["NVDA"], "INFORMATION_TECHNOLOGY"),
        _held("NKE", PCTS["NKE"], SLEEVES["NKE"], PRICES["NKE"], "CONSUMER_STAPLES"),
        _held("ESTC", PCTS["ESTC"], SLEEVES["ESTC"], PRICES["ESTC"], "INFORMATION_TECHNOLOGY"),
        _held("IONQ", PCTS["IONQ"], SLEEVES["IONQ"], PRICES["IONQ"]),
    ]
    context = build_context(
        account_number=ACCOUNT,
        current_nav=NAV,
        cash=NAV - sum(p.market_value for p in positions),
        buying_power=NAV - sum(p.market_value for p in positions),
        positions=positions,
        start_of_day_nav=NAV,
        prior_hwm=NAV,
        timestamp=TS,
    )
    plan_run = OrderPlanStore().get(ORDER_PLAN_RUN_ID)
    if not plan_run:
        raise SystemExit(f"missing OrderPlan run {ORDER_PLAN_RUN_ID}")
    monitor_raw = MonitoringStore().get(MONITOR_RUN_ID) or {}
    by_mon = _monitor_by_symbol(monitor_raw)
    theses = ThesisRegistry(root / "state" / "paper_monitor" / "theses.json")
    research = ResearchStore()
    items: list[ApprovalRequest] = []
    for raw_plan in plan_run.get("plans") or []:
        plan = order_plan_from_dict(raw_plan)
        row = by_mon.get(plan.symbol, {})
        action_raw, risk_raw = _gated(row)
        if action_raw:
            action = proposed_action_from_dict(action_raw)
            action.decision = plan.action
            action.proposed_notional = plan.notional
            action.expected_resulting_position_pct = plan.estimated_position_pct_after
            action.current_price = plan.estimated_price
        else:
            raise SystemExit(f"missing ProposedAction for {plan.symbol}")
        risk = risk_from_dict(risk_raw) if risk_raw.get("verdict") else None
        if risk is None:
            from agentic_portfolio.risk_gate import evaluate

            risk = evaluate(context, action)
        reassessment = row.get("reassessment") or {}
        thesis = None
        if reassessment.get("thesis_id"):
            thesis = theses.get(str(reassessment["thesis_id"]))
        if thesis is None:
            thesis = theses.current_for_symbol(plan.symbol)
        report = None
        research_id = (thesis.research_id if thesis else None) or None
        if research_id:
            report = research.get(research_id)
        decision = NameDecision(
            symbol=plan.symbol,
            decision=plan.action,
            desired_allocation_pct=reassessment.get("desired_allocation_pct"),
            rationale=reassessment.get("rationale"),
            thesis_id=thesis.thesis_id if thesis else None,
            research_id=research_id,
        )
        items.append(
            ApprovalRequest(
                plan=plan,
                action=action,
                risk=risk,
                context=context,
                thesis=thesis,
                decision=decision,
                comparison=PortfolioComparison(
                    ranking=[plan.symbol, "CASH", "SPY"],
                    vs_cash="Action moves toward cash." if plan.action in {Decision.SELL, Decision.REDUCE} else None,
                    vs_spy="Not a swap into SPY.",
                ),
                report=report,
                monitoring=row,
                quote=_quote(plan.symbol, PRICES[plan.symbol]),
                monitoring_run_id=MONITOR_RUN_ID,
            )
        )
    result = run_approval(
        items,
        context,
        persist=True,
        now=NOW,
        store=ApprovalStore(),
        journal=root / "logs" / "approval.jsonl",
    )
    example = next((p for p in result.packets if p.symbol == "NKE"), result.packets[0] if result.packets else None)
    reports_dir = root / "reports"
    _write_reports(result, reports_dir / "2026-08-30_approval.json", reports_dir / "2026-08-30_approval.md", example)
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "packets": [(p.symbol, p.action.value, p.status.value, p.order_notional) for p in result.packets],
                "skipped": [(s.symbol, s.reason) for s in result.skipped],
                "execution_attempted": result.execution_attempted,
                "broker_orders_submitted": result.broker_orders_submitted,
                "example": render_packet(example) if example else None,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
