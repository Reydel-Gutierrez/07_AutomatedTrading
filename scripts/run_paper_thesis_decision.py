"""Paper-only Thesis + Portfolio Decision using existing ResearchReports.

Does not call review/place/cancel or capital-transfer tools.
Theses remain DRAFT. No broker stop orders. No money movement.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agentic_portfolio.context import build_context
from agentic_portfolio.decision.engine import run_portfolio_decision
from agentic_portfolio.decision.reasoner import ScriptedDecisionReasoner
from agentic_portfolio.decision.store import DecisionStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.schemas import to_dict
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry

NOW = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)
ACCOUNT = load_account_rules()["account"]["account_number"]
SYMBOLS = ("NVDA", "NKE", "ESTC", "SPY")


def _context():
    live = project_root() / "reports" / "2026-08-29_live_context.json"
    facts = json.loads(live.read_text(encoding="utf-8"))["facts"]
    return build_context(
        account_number=ACCOUNT,
        current_nav=float(facts["current_nav"]),
        cash=float(facts["cash"]),
        buying_power=float(facts["buying_power"]),
        positions=[],
        start_of_day_nav=float(facts["start_of_day_nav"]),
        prior_hwm=float(facts["high_water_mark"]),
        timestamp=NOW.isoformat(),
    )


def _payload(reports):
    by = {r.symbol.upper(): r for r in reports}
    nvda = by["NVDA"]
    return {
        "theses": [
            {
                "symbol": "NVDA",
                "research_id": nvda.research_id,
                "sleeve": "CORE_GROWTH",
                "thesis_summary": "NVIDIA remains a Core compounder: observed AI-compute franchise quality is high enough to justify a draft position versus idle cash, sized well below the Core equity ceiling.",
                "bull_case": "AI infrastructure demand stays strong and NVDA keeps most of the stack economics.",
                "base_case": "Growth remains high but decelerates; the multiple stays in a mid-20s P/E area.",
                "bear_case": "Hyperscaler capex pause or share loss to custom silicon compresses earnings and the multiple.",
                "catalysts": ["next earnings 2026-11-17", "hyperscaler capex commentary"],
                "risks": ["custom-silicon competition", "valuation compression", "higher long-term debt"],
                "horizon": "12-24 months",
                "invalidation_conditions": [
                    "sustained deterioration in compute demand or margins versus the observed series",
                    "evidence that NVDA has structurally lost stack economics to custom silicon",
                ],
                "review_triggers": ["earnings", "material 10-Q/10-K", "major competitive disclosure"],
                "why_position_should_exist": "It is the only researched name in this set with ADVANCE_TO_THESIS quality versus a 100% cash book.",
                "confidence": "MEDIUM",
                "exit_policy": {
                    "thesis_based": True,
                    "mandatory_fixed_stop_loss": False,
                    "price_invalidation": None,
                    "event_invalidation": "thesis invalidation on structural demand or share-loss evidence",
                    "technical_invalidation": None,
                    "risk_invalidation": None,
                    "broker_stop_orders_created": False,
                    "notes": "CORE: no mandatory fixed stop. No broker stop orders.",
                },
            }
        ],
        "comparison": {
            "ranking": ["NVDA", "CASH", "SPY", "NKE", "ESTC"],
            "vs_cash": "A small Core NVDA draft is preferable to leaving the entire book in cash given ADVANCE_TO_THESIS evidence. Residual cash remains the majority and is a valid position.",
            "vs_spy": "SPY advanced with LOW confidence and an incomplete ETF packet. It is a valid alternative, but not stronger than cash residual plus a researched NVDA core stub in this set.",
            "notes": "NKE and ESTC are KEEP_WATCHING. Unused sleeve capacity is not a mandate to fill Opp/Tactical.",
        },
        "decisions": [
            {
                "symbol": "NVDA",
                "decision": "BUY",
                "desired_allocation_pct": 5.0,
                "rationale": "Draft Core thesis vs cash and SPY. Size is conviction, not unused ceiling.",
                "why_preferable_to_cash": "Observed franchise quality and growth justify a small Core stub versus 100% cash.",
                "why_preferable_to_spy": "More specific researched exposure than an incomplete SPY packet.",
                "why_preferable_to_alternatives": "Only ADVANCE_TO_THESIS individual name in this comparison set.",
            },
            {
                "symbol": "NKE",
                "decision": "WATCH",
                "desired_allocation_pct": 0,
                "rationale": "Research conclusion KEEP_WATCHING. Dislocation vs deterioration is not resolved enough to fund.",
            },
            {
                "symbol": "ESTC",
                "decision": "WATCH",
                "desired_allocation_pct": 0,
                "rationale": "KEEP_WATCHING tactical setup. No predefined invalidation funded here.",
            },
            {
                "symbol": "SPY",
                "decision": "NO_ACTION",
                "desired_allocation_pct": 0,
                "rationale": "Valid alternative, not selected. Prefer cash residual plus NVDA stub.",
            },
            {
                "symbol": "CASH",
                "decision": "HOLD",
                "desired_allocation_pct": 95.0,
                "rationale": "Cash is a position. Residual after a 5% NVDA draft.",
            },
        ],
    }


def _write_reports(result, path_json: Path, path_md: Path) -> None:
    rows = []
    for d in result.decisions:
        gate = next((g for g in result.gated_actions if g.proposed_action.symbol == d.symbol), None)
        rows.append(
            {
                "symbol": d.symbol,
                "decision": d.decision.value,
                "desired_allocation_pct": d.desired_allocation_pct,
                "thesis_id": d.thesis_id,
                "thesis_status": next((t.status.value for t in result.theses if t.thesis_id == d.thesis_id), None),
                "risk_verdict": gate.risk.verdict.value if gate else None,
                "execution_permitted": gate.risk.execution_permitted if gate else False,
            }
        )
    payload = {
        "run": "paper_thesis_portfolio_decision",
        "observed_at": NOW.isoformat(),
        "batch_id": result.batch_id,
        "nav_observed": result.context.current_nav if result.context else None,
        "nav_is_not_a_policy_constraint": True,
        "symbols": list(SYMBOLS),
        "comparison": to_dict(result.comparison),
        "theses_created": len(result.theses),
        "theses_activated": 0,
        "theses_status": [t.status.value for t in result.theses],
        "proposed_actions_created": len(result.gated_actions),
        "buy_actions_created": sum(1 for g in result.gated_actions if g.proposed_action.decision.value == "BUY"),
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
        "rows": rows,
        "note": "DRAFT theses and ProposedActions are not permission to trade. Risk Gate still vetoes execution.",
    }
    path_json.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    md = [
        "# Paper Thesis + Portfolio Decision",
        "",
        f"Observed at {NOW.isoformat()}. NAV is an observed snapshot, not a constraint.",
        "",
        "| Symbol | Decision | Desired % NAV | Thesis | Risk |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        md.append(
            f"| {row['symbol']} | {row['decision']} | {row['desired_allocation_pct']} | "
            f"{row['thesis_status'] or '—'} | {row['risk_verdict'] or '—'} |"
        )
    md += [
        "",
        "Theses remain DRAFT. No broker stop orders. No review/place/cancel. No transfers.",
        "",
        "Cash and SPY were valid alternatives. NO_ACTION was used for SPY. Residual cash 95%.",
    ]
    path_md.write_text("\n".join(md) + "\n", encoding="utf-8")


def main() -> None:
    root = project_root()
    reports = []
    store = ResearchStore()
    for symbol in SYMBOLS:
        report = store.latest_for_symbol(symbol)
        if report is None:
            raise SystemExit(f"missing ResearchReport for {symbol}")
        reports.append(report)
    result = run_portfolio_decision(
        reports,
        _context(),
        ScriptedDecisionReasoner(_payload(reports)),
        theses=ThesisRegistry(),
        sleeves=SleeveRegistry(),
        store=DecisionStore(),
        persist=True,
        now=NOW,
        journal=root / "logs" / "thesis_decision.jsonl",
    )
    if result.validation_errors:
        raise SystemExit(f"validation failed: {result.validation_errors}")
    reports_dir = root / "reports"
    _write_reports(result, reports_dir / "2026-08-30_thesis_decision.json", reports_dir / "2026-08-30_thesis_decision.md")
    print(json.dumps({
        "batch_id": result.batch_id,
        "theses": [(t.symbol, t.status.value) for t in result.theses],
        "decisions": [(d.symbol, d.decision.value, d.desired_allocation_pct) for d in result.decisions],
        "execution_attempted": result.execution_attempted,
        "risk": [
            (g.proposed_action.symbol, g.proposed_action.decision.value, g.risk.verdict.value, g.risk.execution_permitted)
            for g in result.gated_actions
        ],
    }, indent=2))


if __name__ == "__main__":
    main()
