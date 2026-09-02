"""Thesis + Portfolio Decision engine.

ResearchReport → DRAFT Thesis → Portfolio Decision → ProposedAction → Risk Gate.
Theses stay DRAFT. No broker stops. No execution tools.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentic_portfolio.decision.reasoner import COMMITTEE_REASONER_INSTRUCTIONS, REASONER_INSTRUCTIONS, DecisionReasoner
from agentic_portfolio.decision.residuals import build_broad_market_residual, resolve_cash_yield, spy_snapshot_usable
from agentic_portfolio.decision.safety import assert_draft, assert_no_forbidden_tools
from agentic_portfolio.decision.store import DecisionStore
from agentic_portfolio.decision.types import (
    CASH_SYMBOL,
    RISK_UP,
    SPY_SYMBOL,
    DecisionPacket,
    DecisionReasoningRequest,
    DecisionResult,
    GatedAction,
    NameDecision,
    PortfolioComparison,
    sleeve_of,
)
from agentic_portfolio.decision.validate import DecisionValidationError, parse_exit_policy, validate_payload
from agentic_portfolio.journal import append_jsonl, append_risk_decision
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_decision_config, load_policy
from agentic_portfolio.research.packet import freeze_portfolio, freeze_risk_limits
from agentic_portfolio.research.types import ResearchReport
from agentic_portfolio.risk_gate import evaluate
from agentic_portfolio.schemas import (
    ClassificationStatus,
    Decision,
    LiquidityInputs,
    PortfolioContext,
    ProposedAction,
    SecurityClass,
    Sleeve,
    SleeveAssignmentStatus,
    ThesisRecord,
    ThesisStatus,
    to_dict,
)
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry


def journal_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "logs" / "thesis_decision.jsonl"


def run_thesis_and_decision(
    report: ResearchReport,
    context: PortfolioContext,
    reasoner: DecisionReasoner,
    **kwargs: Any,
) -> DecisionResult:
    return run_portfolio_decision([report], context, reasoner, **kwargs)


def run_portfolio_decision(
    reports: list[ResearchReport],
    context: PortfolioContext,
    reasoner: DecisionReasoner,
    *,
    theses: ThesisRegistry | None = None,
    sleeves: SleeveRegistry | None = None,
    store: DecisionStore | None = None,
    persist: bool = True,
    now: datetime | None = None,
    config: dict | None = None,
    journal: Path | None = None,
    committee: bool = False,
    policy: dict | None = None,
    market_evidence_reports: list[ResearchReport] | None = None,
) -> DecisionResult:
    """Form DRAFT theses, decide, convert to ProposedAction, send to Risk Gate."""
    if not reports:
        raise DecisionValidationError("at least one ResearchReport is required")
    cfg = config or load_decision_config()
    assert_no_forbidden_tools(cfg.get("forbidden_tools") or [])
    now = now or datetime.now(timezone.utc)
    batch_id = str(uuid4())
    theses = theses or (ThesisRegistry() if persist else None)
    sleeves = sleeves or (SleeveRegistry() if persist else None)

    packet = build_packet(
        reports,
        context,
        theses,
        cfg,
        now=now,
        committee=committee,
        policy=policy,
        market_evidence_reports=market_evidence_reports,
    )
    request = DecisionReasoningRequest(
        packet=to_dict(packet),
        reports=list(packet.reports),
        portfolio_context=to_dict(packet.portfolio_facts) if packet.portfolio_facts else {},
        existing_theses=list(packet.existing_theses),
        policy_context=dict(packet.policy_context),
        alternatives=list(packet.alternatives),
        instructions=COMMITTEE_REASONER_INSTRUCTIONS if committee else REASONER_INSTRUCTIONS,
    )

    try:
        raw = reasoner.reason(request)
        normalized, unsupported, errors = validate_payload(raw, reports, current_nav=context.current_nav)
    except DecisionValidationError as exc:
        _journal(
            {
                "type": "THESIS_DECISION_INCONCLUSIVE",
                "batch_id": batch_id,
                "reason": str(exc),
                "symbols": [r.symbol for r in reports],
            },
            journal,
            persist=persist,
        )
        return DecisionResult(
            packet=packet,
            reports=list(reports),
            context=context,
            batch_id=batch_id,
            validation_errors=[str(exc)],
        )

    comparison = _comparison(normalized.get("comparison") or {})
    report_by = {r.symbol.upper(): r for r in reports}
    dec_by = {d["symbol"]: d for d in normalized["decisions"]}
    thesis_records: list[ThesisRecord] = []
    thesis_by_symbol: dict[str, ThesisRecord] = {}
    for item in normalized.get("theses") or []:
        rec = _persist_draft_thesis(
            item,
            report_by.get(item["symbol"]),
            theses,
            now,
            decision_item=dec_by.get(item["symbol"]),
        )
        thesis_records.append(rec)
        thesis_by_symbol[rec.symbol] = rec

    decisions: list[NameDecision] = []
    gated: list[GatedAction] = []
    for item in normalized["decisions"]:
        nd = _name_decision(item, thesis_by_symbol)
        decisions.append(nd)
        if nd.symbol == CASH_SYMBOL:
            continue
        action = to_proposed_action(nd, context, report_by.get(nd.symbol), thesis_by_symbol.get(nd.symbol))
        if action is None:
            continue
        if sleeves is not None and action.sleeve and nd.decision in RISK_UP | {Decision.HOLD}:
            _assign_proposed_sleeve(sleeves, action.symbol, action.sleeve, nd.thesis_id)
        risk = evaluate(context, action, sleeves=sleeves, theses=theses)
        gated.append(GatedAction(proposed_action=action, risk=risk, thesis_id=nd.thesis_id))
        if persist:
            append_risk_decision(risk, journal)

    result = DecisionResult(
        packet=packet,
        theses=thesis_records,
        decisions=decisions,
        comparison=comparison,
        gated_actions=gated,
        reports=list(reports),
        context=context,
        batch_id=batch_id,
        unsupported_claims=list(unsupported),
        validation_errors=list(errors),
    )
    if persist:
        (store or DecisionStore()).save(batch_id, _batch_record(result, now))
    _journal(
        {
            "type": "PORTFOLIO_DECISION_COMPLETED",
            "batch_id": batch_id,
            "symbols": [d.symbol for d in decisions],
            "decisions": {d.symbol: d.decision.value for d in decisions},
            "theses_created": len(thesis_records),
            "theses_activated": 0,
            "proposed_actions": len(gated),
            "execution_attempted": False,
            "broker_stop_orders_created": 0,
        },
        journal,
        persist=persist,
    )
    return result


def _spy_evidence_report(
    reports: list[ResearchReport],
    extra: list[ResearchReport] | None,
) -> ResearchReport | None:
    candidates = [r for r in list(reports) + list(extra or []) if str(r.symbol or "").upper() == SPY_SYMBOL]
    if not candidates:
        return None
    return sorted(candidates, key=lambda r: r.started_at or "", reverse=True)[0]


def build_packet(
    reports: list[ResearchReport],
    context: PortfolioContext,
    theses: ThesisRegistry | None,
    config: dict,
    *,
    now: datetime | None = None,
    committee: bool = False,
    policy: dict | None = None,
    market_evidence_reports: list[ResearchReport] | None = None,
) -> DecisionPacket:
    now = now or datetime.now(timezone.utc)
    pol = policy if policy is not None else load_policy()
    alternatives = list(config.get("alternatives") or [CASH_SYMBOL, SPY_SYMBOL])
    for report in reports:
        if report.symbol.upper() not in alternatives:
            alternatives.append(report.symbol.upper())
    if CASH_SYMBOL not in alternatives:
        alternatives.insert(0, CASH_SYMBOL)
    if SPY_SYMBOL not in alternatives:
        alternatives.insert(1, SPY_SYMBOL)
    existing = [to_dict(t) for t in theses.all_records()] if theses is not None else []
    cash_pol = dict(pol.get("cash") or {})
    cash_alt = resolve_cash_yield(cash_pol, now=now, context=context)
    residual = build_broad_market_residual(
        context,
        spy_report=_spy_evidence_report(reports, market_evidence_reports),
        now=now,
    )
    spy_included = spy_snapshot_usable(residual)
    core_alloc = (context.sleeve_allocation_pct or {}).get("CORE_GROWTH")
    return DecisionPacket(
        packet_id=str(uuid4()),
        assembled_at=now.isoformat(),
        reports=[_report_brief(r) for r in reports],
        portfolio_facts=freeze_portfolio(context),
        risk_limits=freeze_risk_limits(pol),
        existing_theses=existing,
        existing_holdings=[to_dict(p) for p in context.positions],
        alternatives=alternatives,
        committee=committee,
        policy_context={
            "cash_is_valid_alternative": True,
            "spy_is_valid_alternative": True,
            "spy_included": spy_included,
            "no_action_always_valid": True,
            "unused_sleeve_capacity_is_not_a_mandate": True,
            "never_force_deployment": bool(cash_pol.get("never_force_deployment", True)),
            "thesis_remains_draft_until_real_execution": True,
            "consider_cash_yield_and_opportunity_cost": bool(
                cash_pol.get("consider_cash_yield_and_opportunity_cost", True)
            ),
            "cash_alternative": {
                "valid_position": True,
                "never_force_deployment": True,
                "not_a_free_no_risk_default": True,
                "may_win": True,
                "current_yield": cash_alt["current_yield"],
                "yield_known": cash_alt["yield_known"],
                "yield_source": cash_alt["yield_source"],
                "yield_as_of": cash_alt["yield_as_of"],
                "yield_status": cash_alt["yield_status"],
                "yield_unit": cash_alt["yield_unit"],
                "optionality": True,
                "inflation_and_opportunity_cost": True,
                "evaluate_expected_return_foregone": True,
            },
            "broad_market_residual": residual,
            "spy_alternative": residual,
            "starter_position": {
                "allowed": True,
                "not_mandatory": True,
                "empty_book_does_not_require_deployment": True,
                "unused_sleeve_capacity_is_not_a_reason_to_buy": True,
                "size_from_conviction_valuation_construction_and_risk_gate": True,
            },
            "committee": committee,
            "residual_allocation_question": committee,
            "concentration": pol.get("concentration"),
            "sleeves": {k: {"target_percent_of_nav": v.get("target_percent_of_nav")} for k, v in (pol.get("sleeves") or {}).items()},
            "core_allocation_pct": core_alloc,
            "sector_exposure": dict(context.sector_allocation_pct or {}),
            "risk_state": context.risk_state.value if context.risk_state else None,
            "daily_risk_halt": context.daily_risk_halt,
            "buying_power": context.buying_power,
            "cash": context.cash,
            "holdings_count": context.holdings_count,
        },
        sleeve_exit_requirements=dict(config.get("exit_policy") or {}),
    )


def to_proposed_action(
    nd: NameDecision,
    context: PortfolioContext,
    report: ResearchReport | None,
    thesis: ThesisRecord | None,
) -> ProposedAction | None:
    """Convert a valid AI decision into a ProposedAction. Does not trade."""
    if nd.symbol == CASH_SYMBOL:
        return None
    if report is None:
        return None
    nav = context.current_nav
    existing_mv = sum(p.market_value for p in context.positions if p.symbol.upper() == nd.symbol)
    current_pct = (existing_mv / nav) if nav else 0.0
    desired_frac = (nd.desired_allocation_pct / 100.0) if nd.desired_allocation_pct is not None else current_pct
    decision = nd.decision
    if decision == Decision.BUY and existing_mv > 0:
        decision = Decision.ADD
    elif decision == Decision.ADD and existing_mv <= 0:
        decision = Decision.BUY

    notional: float | None = None
    resulting = current_pct
    if decision in RISK_UP:
        resulting = desired_frac
        notional = max(0.0, desired_frac * nav - existing_mv)
    elif decision in {Decision.SELL, Decision.REDUCE}:
        resulting = desired_frac
        notional = max(0.0, existing_mv - desired_frac * nav)
    else:
        notional = None
        resulting = current_pct

    sleeve = thesis.sleeve if thesis else report.provisional_sleeve
    security_class = report.security_class or SecurityClass.INDIVIDUAL_EQUITY
    status = ClassificationStatus.VALIDATED if report.security_class else ClassificationStatus.INSUFFICIENT_EVIDENCE
    sector = None if not report.sector or report.sector.upper() in {"UNKNOWN", "MISCELLANEOUS"} else report.sector
    return ProposedAction(
        symbol=nd.symbol,
        decision=decision,
        security_class=security_class,
        classification_status=status,
        sleeve=sleeve,
        current_price=report.market_price,
        proposed_notional=notional,
        expected_resulting_position_pct=resulting,
        sector=sector,
        thesis_id=nd.thesis_id or (thesis.thesis_id if thesis else None),
        investment_thesis_review_complete=False,
        risk_review_complete=False,
        liquidity=_liquidity_from_report(report),
    )


def fact_value(report: ResearchReport, name: str) -> Any:
    for item in list(report.facts) + list(report.derived_metrics):
        if item.name == name:
            return item.value
    return None


def _liquidity_from_report(report: ResearchReport) -> LiquidityInputs:
    price = report.market_price or fact_value(report, "market_price")
    avg_vol = fact_value(report, "average_volume")
    recent_vol = fact_value(report, "volume")
    spread = fact_value(report, "spread_pct")
    adv = float(avg_vol) * float(price) if avg_vol and price else None
    recent = float(recent_vol) * float(price) if recent_vol and price else None
    return LiquidityInputs(
        median_daily_dollar_volume_20d=adv,
        recent_dollar_volume=recent,
        bid_ask_spread_pct=float(spread) if spread is not None else None,
    )


def _persist_draft_thesis(
    item: dict[str, Any],
    report: ResearchReport | None,
    theses: ThesisRegistry | None,
    now: datetime,
    *,
    decision_item: dict[str, Any] | None = None,
) -> ThesisRecord:
    assert_draft(item.get("status"))
    sleeve = sleeve_of(item.get("sleeve"), report.provisional_sleeve if report else Sleeve.CORE_GROWTH)
    decision = Decision(decision_item["decision"]) if decision_item and decision_item.get("decision") else None
    alloc = decision_item.get("desired_allocation_pct") if decision_item else None
    kwargs = dict(
        symbol=item["symbol"],
        sleeve=sleeve,
        status=ThesisStatus.DRAFT,
        decision=decision,
        expected_horizon=item.get("horizon"),
        thesis_summary=item.get("thesis_summary"),
        bull_case=item.get("bull_case"),
        base_case=item.get("base_case"),
        bear_case=item.get("bear_case"),
        catalysts=item.get("catalysts"),
        risks=item.get("risks"),
        invalidation_conditions=item.get("invalidation_conditions"),
        review_triggers=item.get("review_triggers"),
        exit_policy=parse_exit_policy(item.get("exit_policy")),
        why_position_should_exist=item.get("why_position_should_exist"),
        research_id=item.get("research_id") or (report.research_id if report else None),
        desired_allocation_pct=alloc,
        confidence=item.get("confidence"),
        created_at=now.isoformat(),
    )
    if theses is not None:
        rec = theses.create(**kwargs)
    else:
        ts = now.isoformat()
        rec = ThesisRecord(
            thesis_id=str(uuid4()),
            symbol=str(kwargs["symbol"]).upper(),
            sleeve=kwargs["sleeve"],
            created_at=ts,
            updated_at=ts,
            status=ThesisStatus.DRAFT,
            decision=kwargs.get("decision"),
            expected_horizon=kwargs.get("expected_horizon"),
            thesis_summary=kwargs.get("thesis_summary"),
            bull_case=kwargs.get("bull_case"),
            base_case=kwargs.get("base_case"),
            bear_case=kwargs.get("bear_case"),
            catalysts=list(kwargs.get("catalysts") or []),
            risks=list(kwargs.get("risks") or []),
            invalidation_conditions=list(kwargs.get("invalidation_conditions") or []),
            review_triggers=list(kwargs.get("review_triggers") or []),
            exit_policy=kwargs.get("exit_policy"),
            why_position_should_exist=kwargs.get("why_position_should_exist"),
            research_id=kwargs.get("research_id"),
            desired_allocation_pct=kwargs.get("desired_allocation_pct"),
            confidence=kwargs.get("confidence"),
        )
    if rec.status != ThesisStatus.DRAFT:
        raise RuntimeError("thesis persistence must force DRAFT")
    return rec


def _name_decision(item: dict[str, Any], thesis_by: dict[str, ThesisRecord]) -> NameDecision:
    sym = item["symbol"]
    thesis = thesis_by.get(sym)
    return NameDecision(
        symbol=sym,
        decision=Decision(item["decision"]),
        desired_allocation_pct=item.get("desired_allocation_pct"),
        rationale=item.get("rationale"),
        why_preferable_to_cash=item.get("why_preferable_to_cash"),
        why_preferable_to_spy=item.get("why_preferable_to_spy"),
        why_preferable_to_alternatives=item.get("why_preferable_to_alternatives"),
        thesis_id=thesis.thesis_id if thesis else None,
        research_id=thesis.research_id if thesis else item.get("research_id"),
        reconsideration=item.get("reconsideration") if isinstance(item.get("reconsideration"), dict) else None,
        starter_position=bool(item.get("starter_position")),
    )


def _assign_proposed_sleeve(
    sleeves: SleeveRegistry,
    symbol: str,
    sleeve: Sleeve,
    thesis_id: str | None,
) -> None:
    existing = sleeves.get(symbol)
    if existing is None:
        sleeves.assign(
            symbol=symbol,
            sleeve=sleeve,
            thesis_id=thesis_id,
            status=SleeveAssignmentStatus.PROPOSED,
            source_decision_id=thesis_id,
        )
        return
    if existing.sleeve != sleeve:
        # Do not silently reclassify. Risk Gate uses the persisted sleeve.
        return
    if existing.status == SleeveAssignmentStatus.PROPOSED and thesis_id:
        sleeves.assign(
            symbol=symbol,
            sleeve=sleeve,
            thesis_id=thesis_id,
            status=SleeveAssignmentStatus.PROPOSED,
            source_decision_id=thesis_id,
        )


def _comparison(raw: dict[str, Any]) -> PortfolioComparison:
    dims = raw.get("ranking_dimensions") or {}
    if not isinstance(dims, dict):
        dims = {}
    return PortfolioComparison(
        ranking=[str(s).upper() for s in (raw.get("ranking") or [])],
        vs_cash=raw.get("vs_cash"),
        vs_spy=raw.get("vs_spy"),
        notes=raw.get("notes"),
        ranking_dimensions=dims,
    )


def _report_brief(report: ResearchReport) -> dict[str, Any]:
    return {
        "research_id": report.research_id,
        "symbol": report.symbol,
        "provisional_sleeve": report.provisional_sleeve.value,
        "security_class": report.security_class.value if report.security_class else None,
        "sector": report.sector,
        "market_price": report.market_price,
        "research_status": report.research_status.value,
        "research_conclusion": report.research_conclusion.value if report.research_conclusion else None,
        "confidence": report.confidence.value,
        "freshness": report.freshness.value,
        "executive_summary": report.executive_summary,
        "bull_case": report.bull_case.summary if report.bull_case else None,
        "base_case": report.base_case.summary if report.base_case else None,
        "bear_case": report.bear_case.summary if report.bear_case else None,
        "key_catalysts": list(report.key_catalysts),
        "key_risks": list(report.key_risks),
        "invalidation_candidates": list(report.invalidation_candidates),
        "expected_horizon": report.expected_horizon,
        "existing_thesis_id": report.thesis_id,
        "average_volume": fact_value(report, "average_volume"),
        "spread_pct": fact_value(report, "spread_pct"),
        "is_etf": bool(report.security_class and "ETF" in report.security_class.value),
        "is_broad_market_etf": bool(
            report.security_class and report.security_class.value == "BROAD_MARKET_INDEX_ETF"
        ),
    }


def _batch_record(result: DecisionResult, now: datetime) -> dict[str, Any]:
    return {
        "batch_id": result.batch_id,
        "created_at": now.isoformat(),
        "symbols": [d.symbol for d in result.decisions],
        "comparison": to_dict(result.comparison) if result.comparison else None,
        "theses": [to_dict(t) for t in result.theses],
        "decisions": [to_dict(d) for d in result.decisions],
        "gated_actions": [
            {
                "proposed_action": to_dict(g.proposed_action),
                "risk": {
                    "verdict": g.risk.verdict.value,
                    "execution_permitted": g.risk.execution_permitted,
                    "recommendation_permitted": g.risk.recommendation_permitted,
                    "reasons": to_dict(g.risk.reasons),
                    "required_reviews": list(g.risk.required_reviews),
                },
                "thesis_id": g.thesis_id,
            }
            for g in result.gated_actions
        ],
        "execution_attempted": False,
        "theses_activated": 0,
        "broker_stop_orders_created": 0,
        "unsupported_claims": list(result.unsupported_claims),
        "nav": result.context.current_nav if result.context else None,
    }


def _journal(row: dict, path: Path | None, *, persist: bool = True) -> None:
    if path is None and not persist:
        return
    append_jsonl(row, path or journal_path())
