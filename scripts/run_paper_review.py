"""Controlled Robinhood review-only run.

Takes one APPROVED paper/live-shaped packet, revalidates, calls review_equity_order
via a StaticReviewClient fed with the MCP response, persists ReviewResult, and stops.
Never places. Never cancels. Never moves money.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from agentic_portfolio.approval.engine import record_human_decision
from agentic_portfolio.approval.store import ApprovalStore
from agentic_portfolio.approval.types import ApprovalStatus, proposed_action_from_dict
from agentic_portfolio.context import build_context
from agentic_portfolio.execution.store import OrderPlanStore
from agentic_portfolio.execution.types import QuoteSnapshot
from agentic_portfolio.paper_fill.types import order_plan_from_dict
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.review.engine import run_review
from agentic_portfolio.review.report import render_result
from agentic_portfolio.review.store import ReviewStore
from agentic_portfolio.review.types import ReviewRequest, StaticReviewClient
from agentic_portfolio.review.validate import build_review_payload
from agentic_portfolio.schemas import ClassificationStatus, Position, SecurityClass, Sleeve
from agentic_portfolio.thesis_registry import ThesisRegistry

NOW = datetime(2026, 8, 30, 18, 30, tzinfo=timezone.utc)
TS = NOW.isoformat()
ACCOUNT = load_account_rules()["account"]["account_number"]
NAV = 10_000.0
NKE_APPROVAL_ID = "697adcc9-d79d-4b43-9fb2-a3b87f3b8db1"
ORDER_PLAN_RUN_ID = "efbd6372-3b7a-43b6-bd1e-a43843e0ba24"
MONITOR_RUN_ID = "3401b2d2-8dbe-4bb8-9b08-148255c21154"
PRICE = 60.0
PCT = 0.04


def _held():
    return Position(
        symbol="NKE",
        market_value=PCT * NAV,
        quantity=(PCT * NAV) / PRICE,
        current_price=PRICE,
        sleeve=Sleeve.OPPORTUNISTIC,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        classification_status=ClassificationStatus.VALIDATED,
        sector="CONSUMER_STAPLES",
    )


def _other():
    return [
        Position(
            symbol="NVDA",
            market_value=0.05 * NAV,
            quantity=(0.05 * NAV) / 180.0,
            current_price=180.0,
            sleeve=Sleeve.CORE_GROWTH,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            classification_status=ClassificationStatus.VALIDATED,
            sector="INFORMATION_TECHNOLOGY",
        ),
        _held(),
        Position(
            symbol="ESTC",
            market_value=0.02 * NAV,
            quantity=(0.02 * NAV) / 70.0,
            current_price=70.0,
            sleeve=Sleeve.TACTICAL,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            classification_status=ClassificationStatus.VALIDATED,
            sector="INFORMATION_TECHNOLOGY",
        ),
        Position(
            symbol="IONQ",
            market_value=0.01 * NAV,
            quantity=(0.01 * NAV) / 8.0,
            current_price=8.0,
            sleeve=Sleeve.SPECULATIVE,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            classification_status=ClassificationStatus.VALIDATED,
        ),
    ]


def _quote():
    half = PRICE * 0.001 / 2.0
    return QuoteSnapshot(
        symbol="NKE",
        last_price=PRICE,
        bid=PRICE - half,
        ask=PRICE + half,
        spread_pct=0.001,
        observed_at=TS,
        stale=False,
        source="paper_order_plan",
    )


def _write_reports(result, payload, path_json: Path, path_md: Path) -> None:
    body = {
        "run": "robinhood_review_only",
        "observed_at": TS,
        "review_id": result.review_id,
        "approval_id": result.approval_id,
        "order_plan_id": result.order_plan_id,
        "symbol": result.symbol,
        "side": result.side,
        "quantity": result.quantity,
        "notional": result.notional,
        "requested_order_type": result.requested_order_type,
        "status": result.status.value,
        "estimated_cost": result.estimated_cost,
        "estimated_proceeds": result.estimated_proceeds,
        "warnings": result.warnings,
        "errors": result.errors,
        "fail_reasons": result.fail_reasons,
        "risk_gate_verdict": result.risk_gate_verdict,
        "review_payload": payload,
        "robinhood_response": result.robinhood_response,
        "order_placed": False,
        "broker_submitted": False,
        "execution_attempted": False,
        "review_accepted_does_not_execute": True,
        "mcp_called": ["review_equity_order"],
        "mcp_not_called": [
            "place_equity_order",
            "cancel_equity_order",
            "create_scan",
            "watchlist_writes",
            "any_deposit_withdrawal_transfer",
        ],
        "note": "One APPROVED NKE REDUCE paper/live-shaped packet. Review is preflight only. Did not place.",
    }
    path_json.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
    path_md.write_text(
        "\n".join(
            [
                "# Robinhood Review-Only (controlled test)",
                "",
                f"Observed at {TS}. One APPROVED NKE REDUCE packet. Preflight only. Did not place.",
                "",
                render_result(result),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def build_request():
    root = project_root()
    store = ApprovalStore()
    packet = store.get_packet(NKE_APPROVAL_ID)
    if packet is None:
        raise SystemExit(f"missing approval packet {NKE_APPROVAL_ID}")
    if packet.status != ApprovalStatus.APPROVED:
        packet = record_human_decision(
            packet,
            ApprovalStatus.APPROVED,
            note="controlled review-only test; does not place",
            now=NOW,
            store=store,
            persist=True,
            journal=root / "logs" / "approval.jsonl",
        )
    plan_run = OrderPlanStore().get(ORDER_PLAN_RUN_ID) or {}
    plan = None
    for raw in plan_run.get("plans") or []:
        candidate = order_plan_from_dict(raw)
        if candidate.symbol == "NKE":
            plan = candidate
            break
    if plan is None:
        raise SystemExit("missing NKE OrderPlan")
    monitor = json.loads((root / "state" / "position_monitoring" / f"{MONITOR_RUN_ID}.json").read_text(encoding="utf-8"))
    row = next((p for p in monitor.get("positions") or [] if p.get("symbol") == "NKE"), {})
    gated = (row.get("gated_actions") or [{}])[0]
    action = proposed_action_from_dict(gated.get("proposed_action") or {})
    action.decision = plan.action
    action.proposed_notional = plan.notional
    action.expected_resulting_position_pct = plan.estimated_position_pct_after
    action.current_price = plan.estimated_price
    action.explicitly_risk_reducing = True
    context = build_context(
        account_number=ACCOUNT,
        current_nav=NAV,
        cash=NAV - sum(p.market_value for p in _other()),
        buying_power=NAV - sum(p.market_value for p in _other()),
        positions=_other(),
        start_of_day_nav=NAV,
        prior_hwm=NAV,
        timestamp=TS,
    )
    theses = ThesisRegistry(root / "state" / "paper_monitor" / "theses.json")
    thesis = theses.get(str(packet.evidence_refs.thesis_id)) if packet.evidence_refs.thesis_id else theses.current_for_symbol("NKE")
    from agentic_portfolio.research.store import ResearchStore

    report = ResearchStore().get(packet.evidence_refs.research_id) if packet.evidence_refs.research_id else None
    return ReviewRequest(
        packet=packet,
        plan=plan,
        action=action,
        context=context,
        quote=_quote(),
        thesis=thesis,
        research=report,
    )


def main() -> None:
    root = project_root()
    req = build_request()
    payload = build_review_payload(req.plan, ACCOUNT)
    payload_path = root / "reports" / "2026-08-30_review_payload.json"
    payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if len(sys.argv) < 2:
        print(json.dumps({"mode": "payload_only", "payload": payload, "approval_id": req.packet.approval_id}, indent=2))
        return
    raw = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    client = StaticReviewClient(response=raw)
    result = run_review(
        req,
        client,
        persist=True,
        now=NOW,
        store=ReviewStore(),
        journal=root / "logs" / "robinhood_review.jsonl",
    )
    reports = root / "reports"
    _write_reports(result, payload, reports / "2026-08-30_review.json", reports / "2026-08-30_review.md")
    print(
        json.dumps(
            {
                "review_id": result.review_id,
                "status": result.status.value,
                "symbol": result.symbol,
                "side": result.side,
                "quantity": result.quantity,
                "notional": result.notional,
                "order_placed": result.order_placed,
                "fail_reasons": result.fail_reasons,
                "warnings": result.warnings,
                "errors": result.errors,
                "calls": len(client.calls),
                "human": render_result(result),
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
