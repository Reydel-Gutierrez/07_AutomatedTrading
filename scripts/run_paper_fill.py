"""Paper fill + blotter reconciliation.

Simulates fills for existing PAPER_ONLY OrderPlans, then re-runs
position monitoring against the updated paper book.

Does not call review/place/cancel. Does not invent stop orders. Does not move money.
Does not modify live thesis/sleeve registries.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from agentic_portfolio.context import build_context
from agentic_portfolio.decision.reasoner import ScriptedDecisionReasoner
from agentic_portfolio.execution.store import OrderPlanStore
from agentic_portfolio.execution.types import QuoteSnapshot, SkippedAction
from agentic_portfolio.monitoring.engine import run_position_monitor
from agentic_portfolio.monitoring.reasoner import ScriptedMonitoringReasoner
from agentic_portfolio.monitoring.store import MonitoringStore
from agentic_portfolio.monitoring.types import PositionObservation
from agentic_portfolio.paper_fill.engine import run_paper_fill
from agentic_portfolio.paper_fill.store import PaperFillStore
from agentic_portfolio.paper_fill.types import order_plan_from_dict
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.schemas import (
    ClassificationStatus,
    Decision,
    Position,
    SecurityClass,
    Sleeve,
)
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry

NOW = datetime(2026, 8, 30, 18, 45, tzinfo=timezone.utc)
TS = NOW.isoformat()
ACCOUNT = load_account_rules()["account"]["account_number"]
NAV = 10_000.0
ORDER_PLAN_RUN_ID = "efbd6372-3b7a-43b6-bd1e-a43843e0ba24"

PRICES = {"NVDA": 180.0, "NKE": 60.0, "ESTC": 70.0, "IONQ": 8.0}
PCTS = {"NVDA": 0.05, "NKE": 0.04, "ESTC": 0.02, "IONQ": 0.01}
SLEEVES = {
    "NVDA": Sleeve.CORE_GROWTH,
    "NKE": Sleeve.OPPORTUNISTIC,
    "ESTC": Sleeve.TACTICAL,
    "IONQ": Sleeve.SPECULATIVE,
}


def _held(symbol, pct, sleeve, price, thesis_id=None):
    qty = (pct * NAV) / price
    return Position(
        symbol=symbol,
        market_value=pct * NAV,
        quantity=qty,
        average_cost=price,
        current_price=price,
        sleeve=sleeve,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        classification_status=ClassificationStatus.VALIDATED,
        unrealized_pnl=0.0,
        thesis_id=thesis_id,
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
        source="paper_fill",
    )


def _seed_paper_registries(root: Path) -> tuple[ThesisRegistry, SleeveRegistry]:
    src = root / "state" / "paper_monitor"
    dest = root / "state" / "paper_book"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("theses.json", "sleeves.json"):
        src_path = src / name
        if src_path.exists():
            shutil.copy(src_path, dest / name)
    return ThesisRegistry(dest / "theses.json"), SleeveRegistry(dest / "sleeves.json")


def _monitor(request):
    symbol = request.facts["symbol"]
    return {
        "NVDA": {
            "symbol": "NVDA",
            "thesis_status": "UNCHANGED",
            "monitoring_state": "RESEARCH_REFRESH_REQUIRED",
            "recommended_action": "HOLD",
            "desired_allocation_pct": 5.0,
            "rationale": "Remaining core holding. 15% decline is not CORE invalidation. Refresh research; hold.",
            "broker_stop_orders_created": False,
        },
        "NKE": {
            "symbol": "NKE",
            "thesis_status": "WEAKENED",
            "monitoring_state": "THESIS_WEAKENED",
            "recommended_action": "HOLD",
            "desired_allocation_pct": 2.0,
            "rationale": "Paper REDUCE already brought NKE to 2% NAV. Thesis still weakened; hold residual. Do not add.",
            "opportunistic_verdict": "LIKELY_DETERIORATION",
            "broker_stop_orders_created": False,
        },
    }[symbol]


def _decision(request):
    symbol = request.reports[0]["symbol"]
    action, alloc = {"NVDA": ("HOLD", 5.0), "NKE": ("HOLD", 2.0)}[symbol]
    return {
        "comparison": {
            "ranking": [symbol, "CASH", "SPY"],
            "vs_cash": "Post-fill monitoring versus residual cash.",
            "vs_spy": "Post-fill monitoring versus SPY as a valid alternative.",
        },
        "decisions": [
            {
                "symbol": symbol,
                "decision": action,
                "desired_allocation_pct": alloc,
                "rationale": "Portfolio decision after paper fill updated the book.",
            },
            {"symbol": "CASH", "decision": "HOLD", "desired_allocation_pct": 100.0 - alloc, "rationale": "Residual cash is a position."},
        ],
    }


def _write_fill_reports(result, path_json: Path, path_md: Path) -> None:
    book = result.context_after
    rows = []
    for fill in result.fills:
        blot = next((b for b in result.blotter if b.fill_id == fill.fill_id), None)
        rows.append(
            {
                "symbol": fill.symbol,
                "order_plan_id": fill.order_plan_id,
                "fill_id": fill.fill_id,
                "status": fill.status.value,
                "side": fill.side.value if fill.side else None,
                "quantity": fill.quantity,
                "fill_price": fill.fill_price,
                "filled_notional": fill.filled_notional,
                "realized_pnl": blot.realized_pnl if blot else None,
                "position_closed": blot.position_closed if blot else None,
                "thesis_id": fill.thesis_id,
            }
        )
    positions = [
        {
            "symbol": p.symbol,
            "quantity": p.quantity,
            "average_cost": p.average_cost,
            "market_value": p.market_value,
            "pct_nav": (p.market_value / book.current_nav) if book and book.current_nav else None,
            "sleeve": p.sleeve.value if p.sleeve else None,
            "unrealized_pnl": p.unrealized_pnl,
            "thesis_id": p.thesis_id,
        }
        for p in (book.positions if book else [])
    ]
    payload = {
        "run": "paper_fill",
        "observed_at": TS,
        "run_id": result.run_id,
        "source_order_plan_run_id": ORDER_PLAN_RUN_ID,
        "nav_observed": book.current_nav if book else None,
        "cash": book.cash if book else None,
        "realized_pnl": book.realized_pnl if book else None,
        "nav_is_not_a_policy_constraint": True,
        "note": "Paper fills from existing OrderPlans. Isolated paper book. Live Agentic account remains 100% cash. No broker calls.",
        "fills": rows,
        "skipped": [{"symbol": s.symbol, "action": s.action.value, "reason": s.reason} for s in result.skipped],
        "positions_after": positions,
        "reconciliation_ok": result.reconciliation.ok,
        "reconciliation_checks": result.reconciliation.checks,
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
        "# Paper Fill + Blotter Reconciliation",
        "",
        f"Observed at {TS}. Simulated fills on a $10,000 paper NAV. Live book remains 100% cash.",
        "Status is paper-only. No broker calls. No money movement.",
        "",
        "| Symbol | Action | Status | Qty | Price | Notional | Closed |",
        "|---|---|---|---|---|---|---|",
    ]
    by_fill = {f.symbol: f for f in result.fills}
    blot_by = {b.symbol: b for b in result.blotter}
    skipped = {s.symbol: s for s in result.skipped}
    for symbol in ("NVDA", "NKE", "ESTC", "IONQ"):
        fill = by_fill.get(symbol)
        blot = blot_by.get(symbol)
        skip = skipped.get(symbol)
        if fill:
            md.append(
                f"| {symbol} | {blot.action.value if blot else '—'} | {fill.status.value} | "
                f"{fill.quantity} | {fill.fill_price} | {fill.filled_notional} | "
                f"{blot.position_closed if blot else '—'} |"
            )
        else:
            md.append(f"| {symbol} | {skip.action.value if skip else '—'} | {skip.reason if skip else 'no fill'} | — | — | — | — |")
    md += [
        "",
        f"Cash after: {book.cash if book else '—'}. NAV after: {book.current_nav if book else '—'}.",
        f"Holdings: {', '.join(p.symbol for p in book.positions) if book and book.positions else '(none)'}.",
        f"Reconciliation: {'PASS' if result.reconciliation.ok else 'FAIL'}.",
        "NVDA HOLD created no fill. No review/place/cancel. No stop orders. No transfers.",
        "",
    ]
    path_md.write_text("\n".join(md) + "\n", encoding="utf-8")


def _write_monitor_reports(result, path_json: Path, path_md: Path) -> None:
    rows = []
    for p in result.positions:
        rows.append(
            {
                "symbol": p.symbol,
                "sleeve": p.facts.sleeve.value if p.facts.sleeve else None,
                "state": p.state.value,
                "action": p.recommended_action.value,
                "pct_nav": p.facts.position_pct,
            }
        )
    payload = {
        "run": "paper_fill_monitor",
        "observed_at": TS,
        "run_id": result.run_id,
        "note": "Monitoring re-run against the updated paper book after simulated fills.",
        "rows": rows,
        "execution_attempted": False,
        "broker_stop_orders_created": 0,
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
        "# Paper Fill — Monitoring Re-run",
        "",
        f"Observed at {TS}. Remaining paper holdings after NKE REDUCE / ESTC SELL / IONQ SELL.",
        "",
        "| Symbol | Sleeve | State | Action |",
        "|---|---|---|---|",
    ]
    for row in rows:
        md.append(f"| {row['symbol']} | {row['sleeve']} | {row['state']} | {row['action']} |")
    md += [
        "",
        "ESTC and IONQ are absent (closed). NVDA HOLD produced no new fill. No broker calls.",
        "",
    ]
    path_md.write_text("\n".join(md) + "\n", encoding="utf-8")


def main() -> None:
    root = project_root()
    raw = OrderPlanStore().get(ORDER_PLAN_RUN_ID)
    if raw is None:
        raise SystemExit(f"missing OrderPlan run {ORDER_PLAN_RUN_ID}")
    plans = [order_plan_from_dict(p) for p in raw.get("plans") or []]
    skipped = [
        SkippedAction(symbol=s["symbol"], action=Decision(s["action"]), reason=s["reason"])
        for s in raw.get("skipped") or []
    ]
    theses, sleeves = _seed_paper_registries(root)
    positions = []
    for symbol in ("NVDA", "NKE", "ESTC", "IONQ"):
        rec = theses.current_for_symbol(symbol)
        positions.append(
            _held(symbol, PCTS[symbol], SLEEVES[symbol], PRICES[symbol], thesis_id=rec.thesis_id if rec else None)
        )
    context = build_context(
        account_number=ACCOUNT,
        current_nav=NAV,
        cash=NAV - sum(p.market_value for p in positions),
        buying_power=NAV - sum(p.market_value for p in positions),
        positions=positions,
        start_of_day_nav=NAV,
        prior_hwm=NAV,
        realized_pnl=0.0,
        timestamp=TS,
    )
    quotes = {symbol: _quote(symbol, price) for symbol, price in PRICES.items()}
    result = run_paper_fill(
        plans,
        context,
        quotes,
        skipped=skipped,
        persist=True,
        now=NOW,
        store=PaperFillStore(),
        theses=theses,
        sleeves=sleeves,
        journal=root / "logs" / "paper_fill.jsonl",
    )
    reports_dir = root / "reports"
    _write_fill_reports(result, reports_dir / "2026-08-30_paper_fill.json", reports_dir / "2026-08-30_paper_fill.md")

    store = ResearchStore()
    reports = {}
    for symbol in ("NVDA", "NKE"):
        report = store.latest_for_symbol(symbol)
        if report is None:
            raise SystemExit(f"missing ResearchReport for {symbol}")
        reports[symbol] = report
    observations = [
        PositionObservation(symbol="NVDA", current_price=180, reference_price=210, price_move_pct=-0.15, sources_observed=["get_equity_quotes"]),
        PositionObservation(symbol="NKE", current_price=60, reference_price=70, earnings_event=True, major_news=True, sources_observed=["get_equity_quotes", "get_earnings_results", "get_equity_news"]),
    ]
    monitored = run_position_monitor(
        result.context_after,
        observations,
        reasoner=ScriptedMonitoringReasoner(_monitor),
        decision_reasoner=ScriptedDecisionReasoner(_decision),
        reports=reports,
        theses=theses,
        sleeves=sleeves,
        store=MonitoringStore(),
        persist=True,
        now=NOW,
        journal=root / "logs" / "position_monitor.jsonl",
    )
    _write_monitor_reports(
        monitored,
        reports_dir / "2026-08-30_paper_fill_monitor.json",
        reports_dir / "2026-08-30_paper_fill_monitor.md",
    )
    print(
        json.dumps(
            {
                "fill_run_id": result.run_id,
                "filled": [
                    (f.symbol, f.status.value, f.quantity, f.fill_price, f.filled_notional)
                    for f in result.filled
                ],
                "rejected": [(f.symbol, f.reject_reasons) for f in result.rejected],
                "skipped": [(s.symbol, s.action.value, s.reason) for s in result.skipped],
                "cash": result.context_after.cash if result.context_after else None,
                "nav": result.context_after.current_nav if result.context_after else None,
                "holdings": [
                    (p.symbol, p.quantity, p.market_value, p.sleeve.value if p.sleeve else None)
                    for p in (result.context_after.positions if result.context_after else [])
                ],
                "reconciliation_ok": result.reconciliation.ok,
                "monitor_run_id": monitored.run_id,
                "monitor": [(p.symbol, p.state.value, p.recommended_action.value) for p in monitored.positions],
                "execution_attempted": result.execution_attempted,
                "broker_orders_submitted": result.broker_orders_submitted,
                "broker_stop_orders_created": result.broker_stop_orders_created,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
