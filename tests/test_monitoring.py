"""Position monitoring + thesis reassessment tests. No broker execution."""

from datetime import datetime, timezone

import pytest

from agentic_portfolio.decision.reasoner import ScriptedDecisionReasoner
from agentic_portfolio.monitoring.engine import run_position_monitor
from agentic_portfolio.monitoring.reasoner import ScriptedMonitoringReasoner
from agentic_portfolio.monitoring.safety import (
    MONITORING_FORBIDDEN_TOOLS,
    MonitoringSafetyError,
    inspect_monitoring_module_for_forbidden_tools,
)
from agentic_portfolio.monitoring.store import MonitoringStore
from agentic_portfolio.monitoring.types import MonitoringState, PositionObservation, TriggerKind
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
    GateVerdict,
    Position,
    SecurityClass,
    Sleeve,
    ThesisStatus,
)
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry
from tests.conftest import ctx
from tests.test_decision import _payload as _decision_payload
from tests.test_decision import _thesis as _decision_thesis

TS = "2026-08-30T18:00:00+00:00"
NOW = datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc)


def _fact(name, value):
    return EvidenceItem(
        evidence_id=f"fact:{name}",
        kind=EvidenceKind.OBSERVED_FACT,
        name=name,
        value=value,
        source="test",
        observed_at=TS,
    )


def _report(symbol="NVDA", *, sleeve=Sleeve.CORE_GROWTH, price=100.0, rid=None):
    return ResearchReport(
        research_id=rid or f"res-{symbol}",
        candidate_id=f"cand-{symbol}",
        symbol=symbol,
        started_at=TS,
        completed_at=TS,
        provisional_sleeve=sleeve,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        sector="INFORMATION_TECHNOLOGY",
        market_price=price,
        research_status=ResearchStatus.RESEARCH_COMPLETE,
        subject_kind=ResearchSubjectKind.EXISTING_POSITION_REVIEW,
        executive_summary=f"{symbol} existing-position research.",
        research_conclusion=ResearchConclusion.ADVANCE_TO_THESIS,
        confidence=ResearchConfidence.MEDIUM,
        freshness=ResearchFreshness.FRESH,
        facts=[_fact("market_price", price), _fact("average_volume", 20_000_000.0)],
        derived_metrics=[_fact("spread_pct", 0.02)],
    )


def _held(symbol, pct, nav, sleeve, price=100.0):
    return Position(
        symbol=symbol,
        market_value=pct * nav,
        quantity=(pct * nav) / price if price else 0,
        current_price=price,
        sleeve=sleeve,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        classification_status=ClassificationStatus.VALIDATED,
    )


def _exit(**kwargs):
    data = dict(
        thesis_based=True,
        mandatory_fixed_stop_loss=False,
        price_invalidation=None,
        event_invalidation=None,
        technical_invalidation=None,
        risk_invalidation=None,
        broker_stop_orders_created=False,
    )
    data.update(kwargs)
    return ExitPolicy(**data)


def _seed_thesis(theses: ThesisRegistry, symbol, sleeve, *, status=ThesisStatus.ACTIVE, exit_policy=None, extra=None):
    kwargs = dict(
        symbol=symbol,
        sleeve=sleeve,
        status=status,
        decision=Decision.HOLD,
        expected_horizon="12-24 months",
        thesis_summary=f"{symbol} existing thesis.",
        bull_case="Thesis holds.",
        base_case="Base.",
        bear_case="Bear.",
        catalysts=["next earnings"],
        risks=["competition"],
        invalidation_conditions=["sustained fundamental deterioration"],
        review_triggers=["earnings", "material 10-Q"],
        exit_policy=exit_policy or _exit(),
        why_position_should_exist="Already held.",
        research_id=f"res-{symbol}",
        desired_allocation_pct=5.0,
        confidence="MEDIUM",
    )
    if extra:
        kwargs.update(extra)
    return theses.create(**kwargs)


def _ai(symbol, *, status="UNCHANGED", state="REVIEW_REQUIRED", action="HOLD", alloc=5.0, extra=None):
    item = {
        "symbol": symbol,
        "thesis_status": status,
        "monitoring_state": state,
        "recommended_action": action,
        "desired_allocation_pct": alloc,
        "rationale": f"{symbol} reassessment.",
        "broker_stop_orders_created": False,
    }
    if extra:
        item.update(extra)
    return item


def _run(positions, observations, reasoner_payload, *, nav=10_000, tmp_path=None, persist=False, reports=None, **kwargs):
    theses = kwargs.pop("theses", None)
    sleeves = kwargs.pop("sleeves", None)
    store = kwargs.pop("store", None)
    if tmp_path is not None:
        theses = theses or ThesisRegistry(tmp_path / "theses.json")
        sleeves = sleeves or SleeveRegistry(tmp_path / "sleeves.json")
        store = store or MonitoringStore(tmp_path)
        persist = True
        kwargs.setdefault("journal", tmp_path / "journal.jsonl")
    return run_position_monitor(
        ctx(nav, positions),
        observations,
        reasoner=ScriptedMonitoringReasoner(reasoner_payload) if reasoner_payload is not None else None,
        reports=reports or {},
        theses=theses,
        sleeves=sleeves,
        store=store,
        persist=persist,
        now=NOW,
        journal=kwargs.pop("journal", None),
        **kwargs,
    )


def test_healthy_position_is_no_action(tmp_path):
    theses = ThesisRegistry(tmp_path / "theses.json")
    _seed_thesis(theses, "NVDA", Sleeve.CORE_GROWTH)
    pos = _held("NVDA", 0.05, 10_000, Sleeve.CORE_GROWTH, price=100)
    out = _run(
        [pos],
        [PositionObservation(symbol="NVDA", current_price=100, reference_price=100, price_move_pct=0.01)],
        _ai("NVDA"),
        reports={"NVDA": _report("NVDA", price=100)},
        theses=theses,
        tmp_path=tmp_path,
    )
    row = out.positions[0]
    assert row.state == MonitoringState.HEALTHY
    assert row.recommended_action == Decision.NO_ACTION
    assert row.reassessment is None
    assert row.gated_actions == []
    assert out.execution_attempted is False
    assert out.broker_stop_orders_created == 0


def test_core_price_move_alone_does_not_invalidate(tmp_path):
    theses = ThesisRegistry(tmp_path / "theses.json")
    rec = _seed_thesis(theses, "NVDA", Sleeve.CORE_GROWTH)
    pos = _held("NVDA", 0.05, 10_000, Sleeve.CORE_GROWTH, price=85)
    out = _run(
        [pos],
        [PositionObservation(symbol="NVDA", current_price=85, reference_price=100, price_move_pct=-0.15)],
        _ai("NVDA", status="INVALIDATED", state="THESIS_INVALIDATED", action="SELL", alloc=0),
        reports={"NVDA": _report("NVDA", price=100)},
        theses=theses,
        tmp_path=tmp_path,
    )
    row = out.positions[0]
    kinds = {t.kind for t in row.triggers}
    assert TriggerKind.PRICE_MOVE in kinds
    assert row.reassessment.core_price_not_used_as_invalidation is True
    assert row.reassessment.new_status != ThesisStatus.INVALIDATED
    assert row.state != MonitoringState.THESIS_INVALIDATED
    assert row.recommended_action != Decision.SELL
    loaded = theses.get(rec.thesis_id)
    assert loaded.status != ThesisStatus.INVALIDATED
    assert row.broker_stop_orders_created == 0
    assert "core_price_move_cannot_invalidate" in out.unsupported_claims


def test_core_fundamental_invalidation_can_invalidate(tmp_path):
    theses = ThesisRegistry(tmp_path / "theses.json")
    rec = _seed_thesis(theses, "NVDA", Sleeve.CORE_GROWTH)
    pos = _held("NVDA", 0.05, 10_000, Sleeve.CORE_GROWTH, price=100)
    out = _run(
        [pos],
        [
            PositionObservation(
                symbol="NVDA",
                current_price=100,
                reference_price=100,
                earnings_event=True,
                fundamental_invalidation_observed=True,
            )
        ],
        _ai("NVDA", status="INVALIDATED", state="THESIS_INVALIDATED", action="SELL", alloc=0),
        reports={"NVDA": _report("NVDA", price=100)},
        theses=theses,
        tmp_path=tmp_path,
    )
    row = out.positions[0]
    assert TriggerKind.EARNINGS_EVENT in {t.kind for t in row.triggers}
    assert TriggerKind.THESIS_INVALIDATION_CANDIDATE in {t.kind for t in row.triggers}
    assert row.state == MonitoringState.THESIS_INVALIDATED
    assert row.recommended_action == Decision.SELL
    assert theses.get(rec.thesis_id).status == ThesisStatus.INVALIDATED
    assert row.gated_actions
    assert row.gated_actions[0].proposed_action.decision == Decision.SELL
    assert row.gated_actions[0].risk.execution_permitted is False
    assert out.broker_stop_orders_created == 0


def test_opportunistic_recovery_vs_deterioration(tmp_path):
    theses = ThesisRegistry(tmp_path / "theses.json")
    rec = _seed_thesis(theses, "NKE", Sleeve.OPPORTUNISTIC)
    pos = _held("NKE", 0.04, 10_000, Sleeve.OPPORTUNISTIC, price=60)
    out = _run(
        [pos],
        [
            PositionObservation(
                symbol="NKE",
                current_price=60,
                reference_price=70,
                price_move_pct=-0.14,
                earnings_event=True,
                major_news=True,
            )
        ],
        _ai(
            "NKE",
            status="WEAKENED",
            state="THESIS_WEAKENED",
            action="REDUCE",
            alloc=2.0,
            extra={"opportunistic_verdict": "LIKELY_DETERIORATION"},
        ),
        reports={"NKE": _report("NKE", sleeve=Sleeve.OPPORTUNISTIC, price=70)},
        theses=theses,
        tmp_path=tmp_path,
    )
    row = out.positions[0]
    assert TriggerKind.OPPORTUNISTIC_DISLOCATION_REVIEW in {t.kind for t in row.triggers}
    assert row.state == MonitoringState.THESIS_WEAKENED
    assert row.reassessment.opportunistic_verdict == "LIKELY_DETERIORATION"
    assert row.recommended_action == Decision.REDUCE
    assert theses.get(rec.thesis_id).status == ThesisStatus.WEAKENED
    assert row.gated_actions[0].risk.verdict == GateVerdict.PASS


def test_tactical_detects_predefined_technical_invalidation(tmp_path):
    theses = ThesisRegistry(tmp_path / "theses.json")
    _seed_thesis(
        theses,
        "ESTC",
        Sleeve.TACTICAL,
        exit_policy=_exit(technical_invalidation="close below 50-day SMA", price_invalidation="break of setup low"),
    )
    pos = _held("ESTC", 0.02, 10_000, Sleeve.TACTICAL, price=70)
    out = _run(
        [pos],
        [
            PositionObservation(
                symbol="ESTC",
                current_price=70,
                reference_price=80,
                technicals={"sma_50": 75},
                technical_invalidation_observed=True,
            )
        ],
        _ai(
            "ESTC",
            status="INVALIDATED",
            state="EXIT_CONDITION_TRIGGERED",
            action="SELL",
            alloc=0,
            extra={"tactical_invalidation_detected": True, "exit_condition_triggered": True},
        ),
        reports={"ESTC": _report("ESTC", sleeve=Sleeve.TACTICAL, price=80)},
        theses=theses,
        tmp_path=tmp_path,
    )
    row = out.positions[0]
    kinds = {t.kind for t in row.triggers}
    assert TriggerKind.TACTICAL_PRICE_OR_TECHNICAL in kinds
    assert TriggerKind.EXIT_POLICY_CONDITION in kinds
    assert row.preliminary_state == MonitoringState.EXIT_CONDITION_TRIGGERED
    assert row.state == MonitoringState.EXIT_CONDITION_TRIGGERED
    assert row.recommended_action == Decision.SELL
    assert row.gated_actions[0].risk.execution_permitted is False
    assert out.broker_stop_orders_created == 0


def test_speculative_detects_predefined_risk_catalyst_invalidation(tmp_path):
    theses = ThesisRegistry(tmp_path / "theses.json")
    _seed_thesis(
        theses,
        "IONQ",
        Sleeve.SPECULATIVE,
        exit_policy=_exit(risk_invalidation="catalyst fails or dilution/financing stress"),
    )
    pos = _held("IONQ", 0.01, 10_000, Sleeve.SPECULATIVE, price=8)
    out = _run(
        [pos],
        [PositionObservation(symbol="IONQ", current_price=8, reference_price=10, catalyst_failed=True, risk_event=True)],
        _ai(
            "IONQ",
            status="INVALIDATED",
            state="EXIT_CONDITION_TRIGGERED",
            action="SELL",
            alloc=0,
            extra={"speculative_invalidation_detected": True, "exit_condition_triggered": True},
        ),
        reports={"IONQ": _report("IONQ", sleeve=Sleeve.SPECULATIVE, price=10)},
        theses=theses,
        tmp_path=tmp_path,
    )
    row = out.positions[0]
    kinds = {t.kind for t in row.triggers}
    assert TriggerKind.SPECULATIVE_RISK_OR_CATALYST in kinds
    assert TriggerKind.EXIT_POLICY_CONDITION in kinds
    assert row.state == MonitoringState.EXIT_CONDITION_TRIGGERED
    assert row.recommended_action == Decision.SELL
    assert out.broker_stop_orders_created == 0


def test_exit_condition_is_not_a_broker_stop(tmp_path):
    theses = ThesisRegistry(tmp_path / "theses.json")
    rec = _seed_thesis(
        theses,
        "ESTC",
        Sleeve.TACTICAL,
        exit_policy=_exit(technical_invalidation="close below 50-day SMA"),
    )
    pos = _held("ESTC", 0.02, 10_000, Sleeve.TACTICAL, price=70)
    payload = _ai("ESTC", status="INVALIDATED", state="EXIT_CONDITION_TRIGGERED", action="SELL", alloc=0)
    payload["broker_stop_orders_created"] = True
    out = _run(
        [pos],
        [PositionObservation(symbol="ESTC", current_price=70, technical_invalidation_observed=True)],
        payload,
        reports={"ESTC": _report("ESTC", sleeve=Sleeve.TACTICAL, price=80)},
        theses=theses,
        tmp_path=tmp_path,
    )
    row = out.positions[0]
    assert row.recommended_action == Decision.NO_ACTION
    assert row.state == MonitoringState.REVIEW_REQUIRED
    assert theses.get(rec.thesis_id).status == ThesisStatus.ACTIVE
    assert out.broker_stop_orders_created == 0


def test_research_refresh_and_decision_hit_risk_gate(tmp_path):
    theses = ThesisRegistry(tmp_path / "theses.json")
    _seed_thesis(theses, "NVDA", Sleeve.CORE_GROWTH)
    pos = _held("NVDA", 0.05, 10_000, Sleeve.CORE_GROWTH, price=100)
    report = _report("NVDA", price=100)
    decision = _decision_payload(
        "NVDA",
        decision="HOLD",
        alloc=5.0,
        theses=[_decision_thesis("NVDA")],
        decisions=[
            {"symbol": "NVDA", "decision": "HOLD", "desired_allocation_pct": 5.0, "rationale": "Thesis intact after earnings."},
            {"symbol": "CASH", "decision": "HOLD", "desired_allocation_pct": 95.0, "rationale": "Residual."},
        ],
    )
    out = _run(
        [pos],
        [PositionObservation(symbol="NVDA", current_price=100, earnings_event=True, material_filing=True)],
        _ai("NVDA", status="UNCHANGED", state="REVIEW_REQUIRED", action="HOLD", alloc=5.0),
        reports={"NVDA": report},
        theses=theses,
        tmp_path=tmp_path,
        decision_reasoner=ScriptedDecisionReasoner(decision),
        store=MonitoringStore(tmp_path),
    )
    row = out.positions[0]
    assert row.research_refresh_requested is True
    assert TriggerKind.EARNINGS_EVENT in {t.kind for t in row.triggers}
    assert row.recommended_action == Decision.HOLD
    assert row.gated_actions
    assert row.gated_actions[0].risk.execution_permitted is False
    assert out.execution_attempted is False


def test_ai_cannot_rewrite_nav(tmp_path):
    theses = ThesisRegistry(tmp_path / "theses.json")
    _seed_thesis(theses, "NVDA", Sleeve.CORE_GROWTH)
    pos = _held("NVDA", 0.05, 25_000, Sleeve.CORE_GROWTH, price=100)
    payload = _ai("NVDA", state="REVIEW_REQUIRED", action="HOLD")
    payload["current_nav"] = 1.0
    payload["positions"] = []
    out = _run(
        [pos],
        [PositionObservation(symbol="NVDA", current_price=100, earnings_event=True)],
        payload,
        nav=25_000,
        reports={"NVDA": _report("NVDA")},
        theses=theses,
        tmp_path=tmp_path,
    )
    assert "attempted_override:current_nav" in out.unsupported_claims
    assert out.context.current_nav == 25_000


def test_scale_invariant_monitoring_state(tmp_path):
    states = []
    pcts = []
    for i, nav in enumerate((1_000, 10_000, 100_000, 1_000_000)):
        theses = ThesisRegistry(tmp_path / f"theses-{i}.json")
        _seed_thesis(theses, "NVDA", Sleeve.CORE_GROWTH)
        pos = _held("NVDA", 0.05, nav, Sleeve.CORE_GROWTH, price=85)
        out = run_position_monitor(
            ctx(nav, [pos]),
            [PositionObservation(symbol="NVDA", current_price=85, reference_price=100, price_move_pct=-0.15)],
            reasoner=ScriptedMonitoringReasoner(_ai("NVDA", status="INVALIDATED", state="THESIS_INVALIDATED", action="SELL", alloc=0)),
            reports={"NVDA": _report("NVDA", price=100)},
            theses=theses,
            persist=False,
            now=NOW,
            journal=None,
        )
        row = out.positions[0]
        states.append(row.state)
        if row.gated_actions:
            pcts.append(row.gated_actions[0].proposed_action.expected_resulting_position_pct)
    assert len(set(s.value for s in states)) == 1
    if pcts:
        assert len(set(pcts)) == 1


def test_missing_thesis_requires_review():
    pos = _held("NVDA", 0.05, 10_000, Sleeve.CORE_GROWTH, price=100)
    out = run_position_monitor(
        ctx(10_000, [pos]),
        [PositionObservation(symbol="NVDA", current_price=100)],
        reasoner=None,
        reports={"NVDA": _report("NVDA")},
        persist=False,
        now=NOW,
        journal=None,
    )
    row = out.positions[0]
    assert TriggerKind.MISSING_THESIS in {t.kind for t in row.triggers}
    assert row.state == MonitoringState.REVIEW_REQUIRED
    assert row.recommended_action == Decision.NO_ACTION


def test_portfolio_risk_state_is_a_trigger():
    pos = _held("NVDA", 0.05, 10_000, Sleeve.CORE_GROWTH, price=100)
    context = ctx(10_000, [pos], prior_hwm=12_000)
    out = run_position_monitor(
        context,
        [PositionObservation(symbol="NVDA", current_price=100, reference_price=100)],
        reasoner=None,
        reports={"NVDA": _report("NVDA")},
        persist=False,
        now=NOW,
        journal=None,
    )
    row = out.positions[0]
    if context.risk_state.value in {"RISK_REDUCTION", "DEFENSIVE", "HALTED"} or context.daily_risk_halt:
        assert TriggerKind.PORTFOLIO_RISK_STATE in {t.kind for t in row.triggers}
    else:
        # drawdown from 12k to 10k is ~16.7% → DEFENSIVE
        assert context.current_drawdown <= -0.15
        assert TriggerKind.PORTFOLIO_RISK_STATE in {t.kind for t in row.triggers}


def test_no_execution_tools_reachable_from_monitoring():
    hits = inspect_monitoring_module_for_forbidden_tools()
    assert hits == []
    for tool in ("review_equity_order", "place_equity_order", "cancel_equity_order"):
        assert tool in MONITORING_FORBIDDEN_TOOLS
    with pytest.raises(MonitoringSafetyError):
        from agentic_portfolio.monitoring.safety import assert_no_forbidden_tools

        assert_no_forbidden_tools(["place_equity_order"])


def test_monitoring_refuses_execution_source_names():
    pos = _held("NVDA", 0.05, 10_000, Sleeve.CORE_GROWTH)
    with pytest.raises(MonitoringSafetyError):
        run_position_monitor(
            ctx(10_000, [pos]),
            [PositionObservation(symbol="NVDA", sources_observed=["place_equity_order"])],
            persist=False,
            now=NOW,
            journal=None,
        )


def test_paper_existing_research_and_theses(tmp_path):
    store = ResearchStore()
    nvda = store.latest_for_symbol("NVDA")
    nke = store.latest_for_symbol("NKE")
    estc = store.latest_for_symbol("ESTC")
    if any(r is None for r in (nvda, nke, estc)):
        pytest.skip("existing ResearchReports required")
    theses = ThesisRegistry(tmp_path / "theses.json")
    _seed_thesis(theses, "NVDA", Sleeve.CORE_GROWTH, extra={"research_id": nvda.research_id})
    _seed_thesis(theses, "NKE", Sleeve.OPPORTUNISTIC, extra={"research_id": nke.research_id})
    _seed_thesis(
        theses,
        "ESTC",
        Sleeve.TACTICAL,
        extra={"research_id": estc.research_id},
        exit_policy=_exit(technical_invalidation="close below 50-day SMA"),
    )
    nav = 10_000
    positions = [
        _held("NVDA", 0.05, nav, Sleeve.CORE_GROWTH, price=180),
        _held("NKE", 0.04, nav, Sleeve.OPPORTUNISTIC, price=60),
        _held("ESTC", 0.02, nav, Sleeve.TACTICAL, price=70),
    ]
    observations = [
        PositionObservation(symbol="NVDA", current_price=180, reference_price=210, price_move_pct=-0.14),
        PositionObservation(symbol="NKE", current_price=60, reference_price=70, earnings_event=True, major_news=True),
        PositionObservation(symbol="ESTC", current_price=70, technicals={"sma_50": 80}, technical_invalidation_observed=True),
    ]

    def _monitor(request):
        symbol = request.facts["symbol"]
        return {
            "NVDA": _ai("NVDA", status="UNCHANGED", state="RESEARCH_REFRESH_REQUIRED", action="HOLD", alloc=5.0),
            "NKE": _ai(
                "NKE",
                status="WEAKENED",
                state="THESIS_WEAKENED",
                action="REDUCE",
                alloc=2.0,
                extra={"opportunistic_verdict": "MIXED"},
            ),
            "ESTC": _ai(
                "ESTC",
                status="INVALIDATED",
                state="EXIT_CONDITION_TRIGGERED",
                action="SELL",
                alloc=0,
                extra={"tactical_invalidation_detected": True, "exit_condition_triggered": True},
            ),
        }[symbol]

    out = run_position_monitor(
        ctx(nav, positions),
        observations,
        reasoner=ScriptedMonitoringReasoner(_monitor),
        reports={"NVDA": nvda, "NKE": nke, "ESTC": estc},
        theses=theses,
        sleeves=SleeveRegistry(tmp_path / "sleeves.json"),
        store=MonitoringStore(tmp_path),
        persist=True,
        now=NOW,
        journal=tmp_path / "journal.jsonl",
    )
    by = {r.symbol: r for r in out.positions}
    assert by["NVDA"].state == MonitoringState.RESEARCH_REFRESH_REQUIRED
    assert by["NVDA"].recommended_action == Decision.HOLD
    assert by["NVDA"].reassessment.core_price_not_used_as_invalidation is True
    assert by["NKE"].state == MonitoringState.THESIS_WEAKENED
    assert by["NKE"].recommended_action == Decision.REDUCE
    assert by["ESTC"].state == MonitoringState.EXIT_CONDITION_TRIGGERED
    assert by["ESTC"].recommended_action == Decision.SELL
    assert all(g.risk.execution_permitted is False for r in out.positions for g in r.gated_actions)
    assert out.execution_attempted is False
    assert out.broker_stop_orders_created == 0
    journal = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert "review_equity_order" not in journal
    assert "place_equity_order" not in journal
    assert "cancel_equity_order" not in journal
    assert MonitoringStore(tmp_path).get(out.run_id) is not None
