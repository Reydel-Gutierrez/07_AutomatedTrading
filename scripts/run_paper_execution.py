"""Paper Execution Controller.

Converts current monitoring ProposedActions into paper OrderPlans.
Does not call review/place/cancel. Does not invent stop orders. Does not move money.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agentic_portfolio.context import build_context
from agentic_portfolio.execution.engine import run_execution
from agentic_portfolio.execution.store import OrderPlanStore
from agentic_portfolio.execution.types import QuoteSnapshot, TradabilitySnapshot
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.risk_gate import evaluate
from agentic_portfolio.schemas import (
    ClassificationStatus,
    Decision,
    LiquidityInputs,
    Position,
    ProposedAction,
    SecurityClass,
    Sleeve,
)

NOW = datetime(2026, 8, 30, 18, 30, tzinfo=timezone.utc)
TS = NOW.isoformat()
ACCOUNT = load_account_rules()["account"]["account_number"]
NAV = 10_000.0
MONITOR_RUN_ID = "3401b2d2-8dbe-4bb8-9b08-148255c21154"

HUGE_ADV = LiquidityInputs(median_daily_dollar_volume_20d=1e12, bid_ask_spread_pct=0.001)


def _held(symbol, pct, sleeve, price):
    return Position(
        symbol=symbol,
        market_value=pct * NAV,
        quantity=(pct * NAV) / price,
        current_price=price,
        sleeve=sleeve,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        classification_status=ClassificationStatus.VALIDATED,
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
        source="paper_monitor",
    )


def _tradable(symbol):
    return TradabilitySnapshot(symbol=symbol, tradable=True, state="active", observed_at=TS, source="paper")


def _action(symbol, decision, notional, resulting, price, sleeve, thesis_id=None):
    return ProposedAction(
        symbol=symbol,
        decision=decision,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        classification_status=ClassificationStatus.VALIDATED,
        sleeve=sleeve,
        current_price=price,
        proposed_notional=notional,
        expected_resulting_position_pct=resulting,
        thesis_id=thesis_id,
        explicitly_risk_reducing=decision in {Decision.SELL, Decision.REDUCE},
        liquidity=HUGE_ADV,
    )


def _write_reports(result, path_json: Path, path_md: Path) -> None:
    rows = []
    skipped = {s.symbol: s.reason for s in result.skipped}
    by_plan = {p.symbol: p for p in result.plans}
    for symbol in ("NVDA", "NKE", "ESTC", "IONQ"):
        plan = by_plan.get(symbol)
        rows.append(
            {
                "symbol": symbol,
                "action": plan.action.value if plan else ("HOLD" if symbol == "NVDA" else None),
                "skipped": skipped.get(symbol),
                "order_plan_id": plan.order_plan_id if plan else None,
                "execution_status": plan.execution_status.value if plan else None,
                "side": plan.order_side.value if plan and plan.order_side else None,
                "quantity": plan.quantity if plan else None,
                "notional": plan.notional if plan else None,
                "estimated_price": plan.estimated_price if plan else None,
                "estimated_position_pct_after": plan.estimated_position_pct_after if plan else None,
                "blocked_reasons": list(plan.blocked_reasons) if plan else [],
                "stop_orders_created": plan.stop_orders_created if plan else 0,
                "broker_submitted": plan.broker_submitted if plan else False,
            }
        )
    payload = {
        "run": "paper_execution",
        "observed_at": TS,
        "run_id": result.run_id,
        "source_decision_id": MONITOR_RUN_ID,
        "nav_observed": NAV,
        "nav_is_not_a_policy_constraint": True,
        "note": "Paper OrderPlans from current monitoring outputs. Live book remains 100% cash. No broker calls.",
        "rows": rows,
        "execution_attempted": False,
        "broker_orders_submitted": 0,
        "broker_stop_orders_created": 0,
        "live_trade_actions_allowed": False,
        "auto_execution": False,
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
        "# Paper Execution Controller",
        "",
        f"Observed at {TS}. OrderPlans from the current monitoring outputs on a $10,000 paper NAV.",
        "Live book remains 100% cash. Status is PAPER_ONLY / BLOCKED_FROM_LIVE. No broker calls.",
        "",
        "| Symbol | Action | Status | Side | Notional | Qty | Position after |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        md.append(
            f"| {row['symbol']} | {row['action']} | {row['execution_status'] or row['skipped'] or '—'} | "
            f"{row['side'] or '—'} | {row['notional'] if row['notional'] is not None else '—'} | "
            f"{row['quantity'] if row['quantity'] is not None else '—'} | "
            f"{row['estimated_position_pct_after'] if row['estimated_position_pct_after'] is not None else '—'} |"
        )
    md += [
        "",
        "NVDA HOLD created no order. No review/place/cancel. No stop orders. No transfers.",
        "",
    ]
    path_md.write_text("\n".join(md) + "\n", encoding="utf-8")


def main() -> None:
    root = project_root()
    positions = [
        _held("NVDA", 0.05, Sleeve.CORE_GROWTH, 180),
        _held("NKE", 0.04, Sleeve.OPPORTUNISTIC, 60),
        _held("ESTC", 0.02, Sleeve.TACTICAL, 70),
        _held("IONQ", 0.01, Sleeve.SPECULATIVE, 8),
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
    specs = [
        ("NVDA", Decision.HOLD, None, 0.05, 180, Sleeve.CORE_GROWTH),
        ("NKE", Decision.REDUCE, 200.0, 0.02, 60, Sleeve.OPPORTUNISTIC),
        ("ESTC", Decision.SELL, 200.0, 0.0, 70, Sleeve.TACTICAL),
        ("IONQ", Decision.SELL, 100.0, 0.0, 8, Sleeve.SPECULATIVE),
    ]
    items = []
    quotes = {}
    trad = {}
    for symbol, decision, notional, resulting, price, sleeve in specs:
        action = _action(symbol, decision, notional, resulting, price, sleeve)
        items.append((action, evaluate(context, action)))
        quotes[symbol] = _quote(symbol, price)
        trad[symbol] = _tradable(symbol)
    result = run_execution(
        items,
        context,
        quotes,
        trad,
        persist=True,
        now=NOW,
        store=OrderPlanStore(),
        journal=root / "logs" / "order_plan.jsonl",
        source_decision_id=MONITOR_RUN_ID,
    )
    reports_dir = root / "reports"
    _write_reports(result, reports_dir / "2026-08-30_order_plan.json", reports_dir / "2026-08-30_order_plan.md")
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "paper": [
                    (p.symbol, p.action.value, p.execution_status.value, p.notional, p.quantity)
                    for p in result.paper_plans
                ],
                "blocked": [(p.symbol, p.blocked_reasons) for p in result.blocked_plans],
                "skipped": [(s.symbol, s.action.value, s.reason) for s in result.skipped],
                "execution_attempted": result.execution_attempted,
                "broker_orders_submitted": result.broker_orders_submitted,
                "broker_stop_orders_created": result.broker_stop_orders_created,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
