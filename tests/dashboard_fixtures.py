"""Synthetic PAPER workspace for dashboard tests.

Dashboard tests must not read or write the developer's gitignored ``state/``
directory: it differs per machine, is absent on a fresh clone or the Pi, and
logging in writes ``state/accounts.json``. Everything here is invented paper
data seeded into a temporary project root.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from agentic_portfolio.paths import project_root

PENDING_ID = "55123138-6dbe-4554-a92b-de57b21f1873"
STAMP = "2026-08-30T18:30:00+00:00"


def pending_packet_raw() -> dict:
    """A PAPER ESTC SELL approval packet awaiting a human decision."""
    return {
        "approval_id": PENDING_ID,
        "symbol": "ESTC",
        "action": "SELL",
        "desired_allocation_pct": 0.0,
        "current_allocation_pct": 2.0,
        "order_notional": 200.0,
        "order_quantity": 2.857142857142857,
        "current_price": 70.0,
        "sleeve": "TACTICAL",
        "thesis_summary": "Short-horizon setup with predefined technical invalidation.",
        "why_now": "Predefined tactical invalidation was observed. This is not a broker stop order.",
        "why_not_cash": "Action moves toward cash.",
        "why_not_spy": "Not a swap into SPY.",
        "bull_case": "Setup recovers.",
        "base_case": "No follow-through.",
        "bear_case": "Setup fails.",
        "key_risks": ["thesis failure"],
        "invalidation_exit_policy": "close below 50-day SMA; not a broker stop order",
        "expected_horizon": "varies by sleeve",
        "portfolio_effect": "ESTC 2.00% -> 0.00% NAV. Book NAV $10000.00.",
        "sector_concentration_effect": "Sector INFORMATION_TECHNOLOGY: book 7.00% NAV.",
        "risk_gate_verdict": "PASS",
        "enhanced_review_requirements": [],
        "order_plan_summary": {
            "order_plan_id": "00000000-0000-4000-8000-000000000001",
            "execution_status": "PAPER_ONLY",
            "side": "sell_to_close",
            "order_type": "market",
            "time_in_force": "gfd",
            "quantity": 2.857142857142857,
            "notional": 200.0,
            "estimated_price": 70.0,
            "estimated_position_pct_after": 0.0,
            "blocked_reasons": [],
            "live_execution_blocked": True,
            "stop_orders_created": 0,
            "broker_submitted": False,
        },
        "evidence_refs": {
            "order_plan_id": "00000000-0000-4000-8000-000000000001",
            "source_decision_id": "00000000-0000-4000-8000-000000000002",
            "thesis_id": "00000000-0000-4000-8000-000000000003",
            "research_id": "00000000-0000-4000-8000-000000000004",
            "risk_evaluation_id": "00000000-0000-4000-8000-000000000005",
            "monitoring_run_id": "00000000-0000-4000-8000-000000000002",
            "supporting_evidence_refs": ["fact:market_price"],
        },
        "created_at": STAMP,
        "status": "PENDING_HUMAN_APPROVAL",
        "snapshot": {
            "nav": 10000.0,
            "cash": 8800.0,
            "cash_allocation_pct": 0.88,
            "holdings_count": 4,
            "position_pct": 0.02,
            "position_quantity": 2.857142857142857,
            "risk_state": "NORMAL",
            "daily_risk_halt": False,
            "quote_observed_at": STAMP,
            "research_id": "00000000-0000-4000-8000-000000000004",
            "research_freshness": "FRESH",
            "thesis_id": "00000000-0000-4000-8000-000000000003",
            "thesis_updated_at": STAMP,
            "source_decision_id": "00000000-0000-4000-8000-000000000002",
        },
        "status_history": [{"status": "PENDING_HUMAN_APPROVAL", "at": STAMP, "reason": None, "note": None}],
        "expiry_reasons": [],
        "superseded_by": None,
        "human_note": None,
        "decided_at": None,
        "monitoring_state": "EXIT_CONDITION_TRIGGERED",
        "sector": "INFORMATION_TECHNOLOGY",
        "live_execution_blocked": True,
        "broker_submitted": False,
        "approved_does_not_place_order": True,
        "stop_orders_created": 0,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def build_paper_workspace(root: Path) -> Path:
    """Seed ``root`` with config plus a small PAPER book, packet, and candidate."""
    shutil.copytree(project_root() / "config", root / "config", dirs_exist_ok=True)
    state = root / "state"
    _write_json(
        state / "paper_book" / "current.json",
        {
            "created_at": STAMP,
            "paper_environment": True,
            "live_book_untouched": True,
            "context": {
                "timestamp": STAMP,
                "current_nav": 10000.0,
                "cash": 9300.0,
                "buying_power": 9300.0,
                "cash_allocation_pct": 0.93,
                "positions": [
                    {
                        "symbol": "NVDA",
                        "market_value": 500.0,
                        "quantity": 2.7777777777777777,
                        "average_cost": 180.0,
                        "current_price": 180.0,
                        "sleeve": "CORE_GROWTH",
                        "security_class": "INDIVIDUAL_EQUITY",
                        "classification_status": "VALIDATED",
                    }
                ],
                "sleeve_allocation_pct": {"CORE_GROWTH": 0.05, "OPPORTUNISTIC": 0.0, "TACTICAL": 0.0, "SPECULATIVE": 0.0},
                "daily_portfolio_return": 0.0,
                "start_of_day_nav": 10000.0,
                "current_drawdown": 0.0,
                "risk_state": "NORMAL",
                "daily_risk_halt": False,
            },
        },
    )
    _write_json(state / "approval_packets" / f"{PENDING_ID}.json", pending_packet_raw())
    _write_json(
        state / "candidates.json",
        {
            "records": {
                "00000000-0000-4000-8000-0000000000aa": {
                    "candidate_id": "00000000-0000-4000-8000-0000000000aa",
                    "symbol": "NVDA",
                    "discovered_at": STAMP,
                    "discovery_source": "core_quality",
                    "provisional_sleeve": "CORE_GROWTH",
                    "security_class": "INDIVIDUAL_EQUITY",
                    "classification_status": "VALIDATED",
                    "current_price": 180.0,
                    "sector": "INFORMATION_TECHNOLOGY",
                    "liquidity_status": "OK",
                    "discovery_score": 72.5,
                    "priority": "HIGH",
                    "reasons": ["core_quality"],
                }
            }
        },
    )
    return root
