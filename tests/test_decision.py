"""Thesis + Portfolio Decision tests. No broker execution."""

from datetime import datetime, timezone

import pytest

from agentic_portfolio.decision.engine import run_portfolio_decision, run_thesis_and_decision
from agentic_portfolio.decision.reasoner import ScriptedDecisionReasoner
from agentic_portfolio.decision.safety import (
    DECISION_FORBIDDEN_TOOLS,
    DecisionSafetyError,
    inspect_decision_module_for_forbidden_tools,
)
from agentic_portfolio.decision.store import DecisionStore
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
    Decision,
    GateVerdict,
    SecurityClass,
    Sleeve,
    SleeveAssignmentStatus,
    ThesisStatus,
)
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry
from tests.conftest import ctx

TS = "2026-08-30T16:30:00+00:00"
NOW = datetime(2026, 8, 30, 16, 30, tzinfo=timezone.utc)


def _fact(name, value):
    return EvidenceItem(
        evidence_id=f"fact:{name}",
        kind=EvidenceKind.OBSERVED_FACT,
        name=name,
        value=value,
        source="test",
        observed_at=TS,
    )


def _report(
    symbol="QUAL",
    *,
    sleeve=Sleeve.CORE_GROWTH,
    conclusion=ResearchConclusion.ADVANCE_TO_THESIS,
    sc=SecurityClass.INDIVIDUAL_EQUITY,
    sector="INFORMATION_TECHNOLOGY",
    price=100.0,
    rid=None,
):
    return ResearchReport(
        research_id=rid or f"res-{symbol}",
        candidate_id=f"cand-{symbol}",
        symbol=symbol,
        started_at=TS,
        completed_at=TS,
        provisional_sleeve=sleeve,
        security_class=sc,
        sector=sector,
        market_price=price,
        research_status=ResearchStatus.RESEARCH_COMPLETE,
        subject_kind=ResearchSubjectKind.NEW_CANDIDATE,
        executive_summary=f"{symbol} research brief.",
        research_conclusion=conclusion,
        confidence=ResearchConfidence.MEDIUM,
        freshness=ResearchFreshness.FRESH,
        facts=[
            _fact("market_price", price),
            _fact("average_volume", 20_000_000.0),
            _fact("volume", 18_000_000.0),
        ],
        derived_metrics=[_fact("spread_pct", 0.02)],
    )


def _core_exit():
    return {
        "thesis_based": True,
        "mandatory_fixed_stop_loss": False,
        "price_invalidation": None,
        "event_invalidation": None,
        "technical_invalidation": None,
        "risk_invalidation": None,
        "broker_stop_orders_created": False,
    }


def _thesis(symbol="QUAL", sleeve="CORE_GROWTH", extra=None):
    item = {
        "symbol": symbol,
        "research_id": f"res-{symbol}",
        "sleeve": sleeve,
        "thesis_summary": f"{symbol} should exist as a researched core compounder.",
        "bull_case": "Quality and growth persist.",
        "base_case": "Growth decelerates but remains profitable.",
        "bear_case": "Demand rolls over.",
        "catalysts": ["next earnings"],
        "risks": ["competition"],
        "horizon": "12-24 months",
        "invalidation_conditions": ["sustained earnings deterioration"],
        "review_triggers": ["earnings", "material filing"],
        "why_position_should_exist": "Improves expected long-term growth vs idle cash.",
        "confidence": "MEDIUM",
        "exit_policy": _core_exit(),
    }
    if extra:
        item.update(extra)
    return item


def _payload(symbol="QUAL", decision="BUY", alloc=5.0, extra=None, theses=None, decisions=None):
    payload = {
        "theses": theses if theses is not None else [_thesis(symbol)],
        "comparison": {
            "ranking": [symbol, "CASH", "SPY"],
            "vs_cash": "Name is preferable to idle cash if the thesis holds.",
            "vs_spy": "Name is a concentrated alternative to SPY, not a substitute for all cash.",
            "notes": "Cash remains the residual.",
        },
        "decisions": decisions
        or [
            {
                "symbol": symbol,
                "decision": decision,
                "desired_allocation_pct": alloc,
                "rationale": "Thesis vs cash and SPY.",
                "why_preferable_to_cash": "Expected compounding exceeds cash opportunity cost.",
                "why_preferable_to_spy": "Business quality vs broad beta.",
                "why_preferable_to_alternatives": "Best researched name in this set.",
            },
            {"symbol": "CASH", "decision": "HOLD", "desired_allocation_pct": 100.0 - (alloc or 0), "rationale": "Residual cash."},
        ],
    }
    if extra:
        payload.update(extra)
    return payload


def _run(reports=None, nav=10_000, payload=None, tmp_path=None, persist=False, **kwargs):
    reports = reports or [_report()]
    reasoner = ScriptedDecisionReasoner(payload or _payload())
    kw = dict(
        persist=persist,
        now=NOW,
        journal=None,
        **kwargs,
    )
    if tmp_path is not None:
        kw["theses"] = ThesisRegistry(tmp_path / "theses.json")
        kw["sleeves"] = SleeveRegistry(tmp_path / "sleeves.json")
        kw["store"] = DecisionStore(tmp_path)
        kw["persist"] = True
        kw["journal"] = tmp_path / "journal.jsonl"
    return run_portfolio_decision(reports, ctx(nav), reasoner, **kw)


def test_no_action_is_valid_and_reaches_risk_gate():
    payload = _payload(
        decision="NO_ACTION",
        alloc=0,
        theses=[],
        decisions=[
            {"symbol": "QUAL", "decision": "NO_ACTION", "desired_allocation_pct": 0, "rationale": "Prefer cash."},
            {"symbol": "CASH", "decision": "HOLD", "desired_allocation_pct": 100, "rationale": "Cash is valid."},
        ],
    )
    out = _run(payload=payload)
    assert out.validation_errors == []
    assert out.decisions[0].decision == Decision.NO_ACTION
    assert out.gated_actions
    assert out.gated_actions[0].proposed_action.decision == Decision.NO_ACTION
    assert out.gated_actions[0].risk.verdict == GateVerdict.PASS
    assert out.execution_attempted is False


def test_cash_and_spy_are_required_alternatives():
    packet = _run().packet
    assert "CASH" in packet.alternatives
    assert "SPY" in packet.alternatives
    bad = _payload()
    bad["comparison"]["ranking"] = ["QUAL"]
    del bad["comparison"]["vs_cash"]
    out = _run(payload=bad)
    assert out.theses == []
    assert out.validation_errors


def test_buy_becomes_proposed_action_and_hits_risk_gate(tmp_path):
    out = _run(tmp_path=tmp_path)
    assert len(out.theses) == 1
    assert out.theses[0].status == ThesisStatus.DRAFT
    assert out.theses[0].base_case
    assert out.theses[0].exit_policy is not None
    assert out.theses[0].exit_policy.broker_stop_orders_created is False
    action = out.gated_actions[0].proposed_action
    assert action.decision == Decision.BUY
    assert action.expected_resulting_position_pct == pytest.approx(0.05)
    assert action.proposed_notional == pytest.approx(500.0)
    assert out.gated_actions[0].risk.verdict == GateVerdict.PASS
    assert out.gated_actions[0].risk.execution_permitted is False
    sleeve = SleeveRegistry(tmp_path / "sleeves.json").get("QUAL")
    assert sleeve is not None
    assert sleeve.status == SleeveAssignmentStatus.PROPOSED


def test_thesis_remains_draft_even_if_ai_says_active(tmp_path):
    payload = _payload()
    payload["theses"][0]["status"] = "ACTIVE"
    out = _run(tmp_path=tmp_path, payload=payload)
    assert out.theses[0].status == ThesisStatus.DRAFT
    assert any("attempted_active_thesis" in c for c in out.unsupported_claims)
    loaded = ThesisRegistry(tmp_path / "theses.json").get(out.theses[0].thesis_id)
    assert loaded.status == ThesisStatus.DRAFT


def test_keep_watching_cannot_buy():
    report = _report(conclusion=ResearchConclusion.KEEP_WATCHING)
    out = _run(reports=[report], payload=_payload())
    assert out.gated_actions == []
    assert "buy_add_requires_advance_to_thesis" in out.validation_errors[0]


def test_tactical_buy_requires_price_or_technical_invalidation():
    report = _report("ESTC", sleeve=Sleeve.TACTICAL)
    payload = _payload("ESTC")
    payload["theses"] = [_thesis("ESTC", "TACTICAL")]
    payload["decisions"][0]["symbol"] = "ESTC"
    out = _run(reports=[report], payload=payload)
    assert "tactical_requires_price_or_technical_invalidation" in out.validation_errors[0]


def test_speculative_buy_requires_risk_invalidation():
    report = _report("SPEC", sleeve=Sleeve.SPECULATIVE)
    extra = {"exit_policy": {**_core_exit(), "risk_invalidation": None}}
    payload = _payload("SPEC")
    payload["theses"] = [_thesis("SPEC", "SPECULATIVE", extra=extra)]
    payload["decisions"][0]["symbol"] = "SPEC"
    out = _run(reports=[report], payload=payload)
    assert "speculative_requires_risk_invalidation" in out.validation_errors[0]


def test_core_cannot_require_fixed_stop_loss():
    payload = _payload()
    payload["theses"][0]["exit_policy"]["mandatory_fixed_stop_loss"] = True
    out = _run(payload=payload)
    assert "core_no_mandatory_fixed_stop_loss" in out.validation_errors[0]


def test_broker_stop_orders_rejected():
    payload = _payload()
    payload["theses"][0]["exit_policy"]["broker_stop_orders_created"] = True
    out = _run(payload=payload)
    assert "broker stop orders" in out.validation_errors[0].lower()
    assert out.broker_stop_orders_created == 0


def test_ai_cannot_rewrite_nav_or_classification():
    payload = _payload(extra={"current_nav": 1.0, "security_class": "BROAD_MARKET_INDEX_ETF", "risk_limits": {"x": 1}})
    out = _run(nav=25_000, payload=payload)
    assert "attempted_override:current_nav" in out.unsupported_claims
    assert "attempted_override:security_class" in out.unsupported_claims
    assert "attempted_override:risk_limits" in out.unsupported_claims
    assert out.packet.portfolio_facts.current_nav == 25_000


def test_scale_invariant_allocation_percent():
    pcts = []
    notionals = []
    for nav in (1_000, 10_000, 100_000, 1_000_000):
        out = _run(nav=nav)
        action = out.gated_actions[0].proposed_action
        pcts.append(action.expected_resulting_position_pct)
        notionals.append(action.proposed_notional)
    assert len(set(pcts)) == 1
    assert pcts[0] == pytest.approx(0.05)
    assert notionals[-1] / notionals[0] == pytest.approx(1000.0)


def test_portfolio_level_compares_several_names():
    reports = [
        _report("NVDA"),
        _report("NKE", sleeve=Sleeve.OPPORTUNISTIC, conclusion=ResearchConclusion.KEEP_WATCHING),
        _report("SPY", sc=SecurityClass.BROAD_MARKET_INDEX_ETF, sector="UNKNOWN"),
    ]
    payload = {
        "theses": [_thesis("NVDA")],
        "comparison": {
            "ranking": ["NVDA", "CASH", "SPY", "NKE"],
            "vs_cash": "NVDA preferred to idle cash; others not.",
            "vs_spy": "NVDA preferred to generic beta in this set.",
        },
        "decisions": [
            {
                "symbol": "NVDA",
                "decision": "BUY",
                "desired_allocation_pct": 5.0,
                "rationale": "Best researched name.",
                "why_preferable_to_cash": "Compounding vs residual cash.",
                "why_preferable_to_spy": "Concentrated quality vs incomplete need for more beta.",
            },
            {"symbol": "NKE", "decision": "WATCH", "desired_allocation_pct": 0, "rationale": "KEEP_WATCHING."},
            {"symbol": "SPY", "decision": "NO_ACTION", "desired_allocation_pct": 0, "rationale": "Prefer cash residual plus NVDA."},
            {"symbol": "CASH", "decision": "HOLD", "desired_allocation_pct": 95.0, "rationale": "Valid residual."},
        ],
    }
    out = run_portfolio_decision(reports, ctx(10_000), ScriptedDecisionReasoner(payload), persist=False, now=NOW, journal=None)
    by = {d.symbol: d.decision for d in out.decisions}
    assert by["NVDA"] == Decision.BUY
    assert by["NKE"] == Decision.WATCH
    assert by["SPY"] == Decision.NO_ACTION
    assert by["CASH"] == Decision.HOLD
    assert out.comparison.ranking[0] == "NVDA"
    assert all(t.status == ThesisStatus.DRAFT for t in out.theses)


def test_no_execution_tools_reachable_from_decision():
    assert inspect_decision_module_for_forbidden_tools() == []
    for tool in ("review_equity_order", "place_equity_order", "cancel_equity_order"):
        assert tool in DECISION_FORBIDDEN_TOOLS
    with pytest.raises(DecisionSafetyError):
        from agentic_portfolio.decision.safety import assert_no_forbidden_tools

        assert_no_forbidden_tools(["place_equity_order"])


def test_single_report_wrapper():
    out = run_thesis_and_decision(
        _report(),
        ctx(10_000),
        ScriptedDecisionReasoner(_payload()),
        persist=False,
        now=NOW,
        journal=None,
    )
    assert out.decisions[0].symbol == "QUAL"


def test_paper_existing_research_reports(tmp_path):
    store = ResearchStore()
    reports = [store.latest_for_symbol(s) for s in ("NVDA", "NKE", "ESTC", "SPY")]
    if any(r is None for r in reports):
        pytest.skip("existing ResearchReports required")
    payload = {
        "theses": [_thesis("NVDA", extra={"research_id": reports[0].research_id})],
        "comparison": {
            "ranking": ["NVDA", "CASH", "SPY", "NKE", "ESTC"],
            "vs_cash": "NVDA is the only name in this set worth funding vs idle cash.",
            "vs_spy": "Incomplete SPY packet and LOW confidence lose to cash residual; NVDA is the researched core name.",
        },
        "decisions": [
            {
                "symbol": "NVDA",
                "decision": "BUY",
                "desired_allocation_pct": 5.0,
                "rationale": "ADVANCE_TO_THESIS vs cash and SPY.",
                "why_preferable_to_cash": "Core compounder vs 100% cash.",
                "why_preferable_to_spy": "Researched franchise vs incomplete ETF packet.",
            },
            {"symbol": "NKE", "decision": "WATCH", "desired_allocation_pct": 0, "rationale": "KEEP_WATCHING."},
            {"symbol": "ESTC", "decision": "WATCH", "desired_allocation_pct": 0, "rationale": "KEEP_WATCHING tactical."},
            {"symbol": "SPY", "decision": "NO_ACTION", "desired_allocation_pct": 0, "rationale": "Prefer cash + NVDA."},
            {"symbol": "CASH", "decision": "HOLD", "desired_allocation_pct": 95.0, "rationale": "Valid residual."},
        ],
    }
    out = run_portfolio_decision(
        reports,
        ctx(500),
        ScriptedDecisionReasoner(payload),
        theses=ThesisRegistry(tmp_path / "theses.json"),
        sleeves=SleeveRegistry(tmp_path / "sleeves.json"),
        store=DecisionStore(tmp_path),
        persist=True,
        now=NOW,
        journal=tmp_path / "journal.jsonl",
    )
    by = {d.symbol: d.decision for d in out.decisions}
    assert by["NVDA"] == Decision.BUY
    assert by["NKE"] == Decision.WATCH
    assert by["ESTC"] == Decision.WATCH
    assert by["SPY"] == Decision.NO_ACTION
    assert by["CASH"] == Decision.HOLD
    nvda = next(g for g in out.gated_actions if g.proposed_action.symbol == "NVDA")
    assert nvda.proposed_action.decision == Decision.BUY
    assert nvda.risk.execution_permitted is False
    assert out.theses[0].status == ThesisStatus.DRAFT
    assert out.execution_attempted is False
    journal = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert "review_equity_order" not in journal
    assert "place_equity_order" not in journal
    assert "cancel_equity_order" not in journal
