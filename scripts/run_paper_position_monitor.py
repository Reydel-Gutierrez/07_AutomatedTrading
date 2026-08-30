"""Paper/read-only position monitoring + thesis reassessment.

Uses mocked holdings plus existing ResearchReports. Does not call
review/place/cancel or capital-transfer tools. Exit conditions are not
broker stop orders. No money movement.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agentic_portfolio.context import build_context
from agentic_portfolio.decision.reasoner import ScriptedDecisionReasoner
from agentic_portfolio.monitoring.engine import run_position_monitor
from agentic_portfolio.monitoring.reasoner import ScriptedMonitoringReasoner
from agentic_portfolio.monitoring.store import MonitoringStore
from agentic_portfolio.monitoring.types import PositionObservation
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.types import (
    EvidenceItem,
    EvidenceKind,
    ResearchConclusion,
    ResearchConfidence,
    ResearchFreshness,
    ResearchReport,
    ResearchStatus,
    ResearchSubjectKind,
)
from agentic_portfolio.schemas import (
    ClassificationStatus,
    Decision,
    ExitPolicy,
    Position,
    SecurityClass,
    Sleeve,
    SleeveAssignmentStatus,
    ThesisStatus,
)
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry

NOW = datetime(2026, 8, 30, 18, 30, tzinfo=timezone.utc)
TS = NOW.isoformat()
ACCOUNT = load_account_rules()["account"]["account_number"]
NAV = 10_000.0


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


def _exit(**kwargs):
    fields = dict(
        thesis_based=True,
        mandatory_fixed_stop_loss=False,
        broker_stop_orders_created=False,
    )
    fields.update(kwargs)
    return ExitPolicy(**fields)


def _ionq_report():
    return ResearchReport(
        research_id="paper-ionq-monitor",
        candidate_id="paper-ionq",
        symbol="IONQ",
        started_at=TS,
        completed_at=TS,
        provisional_sleeve=Sleeve.SPECULATIVE,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        market_price=10.0,
        research_status=ResearchStatus.RESEARCH_COMPLETE,
        subject_kind=ResearchSubjectKind.EXISTING_POSITION_REVIEW,
        executive_summary="Stub speculative report for paper monitoring only.",
        research_conclusion=ResearchConclusion.ADVANCE_TO_THESIS,
        confidence=ResearchConfidence.LOW,
        freshness=ResearchFreshness.FRESH,
        facts=[
            EvidenceItem(
                evidence_id="fact:market_price",
                kind=EvidenceKind.OBSERVED_FACT,
                name="market_price",
                value=10.0,
                source="paper",
                observed_at=TS,
            )
        ],
    )


def _seed(theses: ThesisRegistry, sleeves: SleeveRegistry, reports: dict) -> None:
    specs = [
        (
            "NVDA",
            Sleeve.CORE_GROWTH,
            _exit(event_invalidation="structural demand or share-loss evidence"),
            reports["NVDA"].research_id,
            "Core compounder. Invalidation is fundamental, not a price print.",
        ),
        (
            "NKE",
            Sleeve.OPPORTUNISTIC,
            _exit(event_invalidation="recovery thesis fails vs structural deterioration"),
            reports["NKE"].research_id,
            "Post-selloff recovery. Must separate dislocation from deterioration.",
        ),
        (
            "ESTC",
            Sleeve.TACTICAL,
            _exit(technical_invalidation="close below 50-day SMA", price_invalidation="break of setup low"),
            reports["ESTC"].research_id,
            "Short-horizon setup with predefined technical invalidation.",
        ),
        (
            "IONQ",
            Sleeve.SPECULATIVE,
            _exit(risk_invalidation="catalyst fails or financing/dilution stress"),
            reports["IONQ"].research_id,
            "Asymmetric catalyst. Predefined risk invalidation required.",
        ),
    ]
    for symbol, sleeve, policy, rid, summary in specs:
        rec = theses.create(
            symbol=symbol,
            sleeve=sleeve,
            status=ThesisStatus.ACTIVE,
            decision=Decision.HOLD,
            expected_horizon="varies by sleeve",
            thesis_summary=summary,
            bull_case="Thesis intact.",
            base_case="Base.",
            bear_case="Bear.",
            catalysts=["next earnings or catalyst"],
            risks=["thesis failure"],
            invalidation_conditions=[policy.event_invalidation or policy.technical_invalidation or policy.risk_invalidation or "thesis failure"],
            review_triggers=["earnings", "material filing", "major news"],
            exit_policy=policy,
            why_position_should_exist=summary,
            research_id=rid,
            desired_allocation_pct={"NVDA": 5.0, "NKE": 4.0, "ESTC": 2.0, "IONQ": 1.0}[symbol],
            confidence="MEDIUM",
        )
        sleeves.assign(symbol=symbol, sleeve=sleeve, thesis_id=rec.thesis_id, status=SleeveAssignmentStatus.ACTIVE)


def _monitor(request):
    symbol = request.facts["symbol"]
    return {
        "NVDA": {
            "symbol": "NVDA",
            "thesis_status": "UNCHANGED",
            "monitoring_state": "RESEARCH_REFRESH_REQUIRED",
            "recommended_action": "HOLD",
            "desired_allocation_pct": 5.0,
            "rationale": "15% price decline is not CORE invalidation. No fundamental evidence that compute demand or stack economics broke. Refresh research; hold.",
            "broker_stop_orders_created": False,
        },
        "NKE": {
            "symbol": "NKE",
            "thesis_status": "WEAKENED",
            "monitoring_state": "THESIS_WEAKENED",
            "recommended_action": "REDUCE",
            "desired_allocation_pct": 2.0,
            "rationale": "Earnings plus news keep the recovery vs deterioration question open, with more weight on structural brand/demand risk than a clean dislocation. Reduce; do not add.",
            "opportunistic_verdict": "LIKELY_DETERIORATION",
            "broker_stop_orders_created": False,
        },
        "ESTC": {
            "symbol": "ESTC",
            "thesis_status": "INVALIDATED",
            "monitoring_state": "EXIT_CONDITION_TRIGGERED",
            "recommended_action": "SELL",
            "desired_allocation_pct": 0,
            "rationale": "Predefined tactical invalidation (close below 50-day SMA) was observed. Exit the setup. This is not a broker stop order.",
            "tactical_invalidation_detected": True,
            "exit_condition_triggered": True,
            "broker_stop_orders_created": False,
        },
        "IONQ": {
            "symbol": "IONQ",
            "thesis_status": "INVALIDATED",
            "monitoring_state": "EXIT_CONDITION_TRIGGERED",
            "recommended_action": "SELL",
            "desired_allocation_pct": 0,
            "rationale": "Predefined speculative catalyst/risk invalidation fired. Exit. Not a broker stop.",
            "speculative_invalidation_detected": True,
            "exit_condition_triggered": True,
            "broker_stop_orders_created": False,
        },
    }[symbol]


def _decision(request):
    symbol = request.reports[0]["symbol"]
    action, alloc = {
        "NVDA": ("HOLD", 5.0),
        "NKE": ("REDUCE", 2.0),
        "ESTC": ("SELL", 0.0),
        "IONQ": ("SELL", 0.0),
    }[symbol]
    return {
        "comparison": {
            "ranking": [symbol, "CASH", "SPY"],
            "vs_cash": "Monitoring reassessment versus residual cash.",
            "vs_spy": "Monitoring reassessment versus SPY as a valid alternative.",
        },
        "decisions": [
            {
                "symbol": symbol,
                "decision": action,
                "desired_allocation_pct": alloc,
                "rationale": "Portfolio decision after position-monitor thesis reassessment.",
            },
            {"symbol": "CASH", "decision": "HOLD", "desired_allocation_pct": 100.0 - alloc, "rationale": "Residual cash is a position."},
        ],
    }


def _write_reports(result, path_json: Path, path_md: Path) -> None:
    rows = []
    for p in result.positions:
        gate = p.gated_actions[0] if p.gated_actions else None
        rows.append(
            {
                "symbol": p.symbol,
                "sleeve": p.facts.sleeve.value if p.facts.sleeve else None,
                "state": p.state.value,
                "preliminary_state": p.preliminary_state.value,
                "action": p.recommended_action.value,
                "triggers": [t.kind.value for t in p.triggers],
                "thesis_status": p.thesis.status.value if p.thesis else None,
                "risk_verdict": gate.risk.verdict.value if gate else None,
                "execution_permitted": gate.risk.execution_permitted if gate else False,
                "research_refresh_requested": p.research_refresh_requested,
                "core_price_not_used_as_invalidation": bool(p.reassessment and p.reassessment.core_price_not_used_as_invalidation),
                "opportunistic_verdict": p.reassessment.opportunistic_verdict if p.reassessment else None,
                "exit_condition_triggered": bool(p.reassessment and p.reassessment.exit_condition_triggered),
            }
        )
    payload = {
        "run": "paper_position_monitor",
        "observed_at": NOW.isoformat(),
        "run_id": result.run_id,
        "nav_observed": NAV,
        "nav_is_not_a_policy_constraint": True,
        "note": "Live Agentic book was 100% cash. This paper run uses mocked holdings plus existing ResearchReports.",
        "rows": rows,
        "execution_attempted": False,
        "broker_stop_orders_created": 0,
        "theses_activated": 0,
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
        "# Paper Position Monitoring + Thesis Reassessment",
        "",
        f"Observed at {NOW.isoformat()}. Mocked holdings on a $10,000 paper NAV. Live book remains 100% cash.",
        "",
        "| Symbol | Sleeve | State | Action | Risk |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        md.append(f"| {row['symbol']} | {row['sleeve']} | {row['state']} | {row['action']} | {row['risk_verdict'] or '—'} |")
    md += [
        "",
        "CORE price decline did not invalidate NVDA. ESTC/IONQ exit conditions are not broker stop orders.",
        "No review/place/cancel. No transfers. Execution remains gated off.",
        "",
    ]
    path_md.write_text("\n".join(md) + "\n", encoding="utf-8")


def main() -> None:
    root = project_root()
    store = ResearchStore()
    reports = {}
    for symbol in ("NVDA", "NKE", "ESTC"):
        report = store.latest_for_symbol(symbol)
        if report is None:
            raise SystemExit(f"missing ResearchReport for {symbol}")
        reports[symbol] = report
    reports["IONQ"] = _ionq_report()
    paper = root / "state" / "paper_monitor"
    paper.mkdir(parents=True, exist_ok=True)
    theses = ThesisRegistry(paper / "theses.json")
    sleeves = SleeveRegistry(paper / "sleeves.json")
    _seed(theses, sleeves, reports)
    positions = [
        _held("NVDA", 0.05, Sleeve.CORE_GROWTH, 180),
        _held("NKE", 0.04, Sleeve.OPPORTUNISTIC, 60),
        _held("ESTC", 0.02, Sleeve.TACTICAL, 70),
        _held("IONQ", 0.01, Sleeve.SPECULATIVE, 8),
    ]
    observations = [
        PositionObservation(symbol="NVDA", current_price=180, reference_price=210, price_move_pct=-0.15, sources_observed=["get_equity_quotes"]),
        PositionObservation(symbol="NKE", current_price=60, reference_price=70, earnings_event=True, major_news=True, sources_observed=["get_equity_quotes", "get_earnings_results", "get_equity_news"]),
        PositionObservation(symbol="ESTC", current_price=70, reference_price=80, technicals={"sma_50": 80}, technical_invalidation_observed=True, sources_observed=["get_equity_quotes", "get_equity_technical_indicators"]),
        PositionObservation(symbol="IONQ", current_price=8, reference_price=10, catalyst_failed=True, risk_event=True, sources_observed=["get_equity_quotes", "get_equity_news"]),
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
    result = run_position_monitor(
        context,
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
    reports_dir = root / "reports"
    _write_reports(result, reports_dir / "2026-08-30_position_monitor.json", reports_dir / "2026-08-30_position_monitor.md")
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "rows": [(p.symbol, p.state.value, p.recommended_action.value) for p in result.positions],
                "execution_attempted": result.execution_attempted,
                "broker_stop_orders_created": result.broker_stop_orders_created,
                "gated": [
                    (g.proposed_action.symbol, g.proposed_action.decision.value, g.risk.verdict.value, g.risk.execution_permitted)
                    for p in result.positions
                    for g in p.gated_actions
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
