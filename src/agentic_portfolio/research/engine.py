"""Deep Research engine.

Selective: operates on ResearchQueue entries and existing-position refresh
requests. Does not buy, size, write ACTIVE theses, or call execution tools.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agentic_portfolio.discovery.store import CandidateStore, ResearchQueue
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_research_config
from agentic_portfolio.research.comparison import ComparisonReasoner, build_comparison
from agentic_portfolio.research.freshness import apply_freshness, evaluate_freshness, freshness_horizon
from agentic_portfolio.research.packet import ResearchPayload, build_packet
from agentic_portfolio.research.reasoner import (
    REASONER_INSTRUCTIONS,
    ResearchReasoner,
    packet_for_reasoner,
)
from agentic_portfolio.research.safety import (
    RESEARCH_FORBIDDEN_TOOLS,
    assert_no_forbidden_tools,
    research_cannot_become_buy,
)
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.types import (
    ResearchConclusion,
    ResearchConfidence,
    ResearchEvidencePacket,
    ResearchFreshness,
    ResearchReasoningRequest,
    ResearchReport,
    ResearchResult,
    ResearchStatus,
    ResearchSubjectKind,
)
from agentic_portfolio.research.validate import (
    ResearchValidationError,
    apply_validated_payload,
    validate_reasoning,
)
from agentic_portfolio.schemas import (
    Candidate,
    PortfolioContext,
    ResearchQueueEntry,
    ResearchQueueStatus,
    to_dict,
)


def journal_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "logs" / "research.jsonl"


def run_research(
    candidate: Candidate,
    payload: ResearchPayload,
    context: PortfolioContext,
    reasoner: ResearchReasoner,
    *,
    subject_kind: ResearchSubjectKind = ResearchSubjectKind.NEW_CANDIDATE,
    queue_entry: ResearchQueueEntry | None = None,
    store: ResearchStore | None = None,
    candidate_store: CandidateStore | None = None,
    queue_store: ResearchQueue | None = None,
    persist: bool = True,
    now: datetime | None = None,
    config: dict | None = None,
    comparison_peer_symbols: list[str] | None = None,
    existing_thesis_id: str | None = None,
    journal: Path | None = None,
) -> ResearchResult:
    """Collect packet → reason → validate → persist. Never creates ProposedAction."""
    cfg = config or load_research_config()
    assert_no_forbidden_tools(payload.sources_attempted or payload.sources_observed)
    now = now or datetime.now(timezone.utc)
    started = now.isoformat()
    research_id = str(uuid4())
    _journal(
        {
            "type": "RESEARCH_STARTED",
            "research_id": research_id,
            "candidate_id": candidate.candidate_id,
            "symbol": candidate.symbol,
            "queue_id": queue_entry.queue_id if queue_entry else None,
            "subject_kind": subject_kind.value,
        },
        journal,
        persist=persist,
    )

    packet = build_packet(
        payload,
        candidate,
        context,
        subject_kind=subject_kind,
        config=cfg,
        comparison_peer_symbols=comparison_peer_symbols,
        existing_thesis_id=existing_thesis_id,
    )
    report = _blank_report(research_id, candidate, packet, started, subject_kind, existing_thesis_id)
    report.research_status = ResearchStatus.RESEARCHING
    report.evidence_fingerprint = evidence_fingerprint(candidate, payload=payload)
    if queue_entry is not None:
        report.research_generation = int(getattr(queue_entry, "research_generation", None) or 1)
    if queue_entry and persist and queue_store:
        queue_store.set_status(queue_entry.queue_id, ResearchQueueStatus.RESEARCHING)

    request = ResearchReasoningRequest(
        candidate=to_dict(candidate),
        packet=packet_for_reasoner(to_dict(packet)),
        portfolio_context=to_dict(packet.portfolio_facts) if packet.portfolio_facts else {},
        policy_context=dict(packet.policy_context),
        sleeve_questions=list(packet.sleeve_research_questions),
        instructions=REASONER_INSTRUCTIONS,
        comparison_peers=[{"symbol": s} for s in (comparison_peer_symbols or [])],
    )

    # Fail closed: if the reasoner/gateway never returns, do not persist a synthetic report.
    raw = reasoner.reason(request)
    apply_research_provenance(report, reasoner, raw if isinstance(raw, dict) else None)

    try:
        normalized, unsupported, errors = validate_reasoning(raw, packet)
        report = apply_validated_payload(report, normalized, packet, unsupported=unsupported)
        apply_research_provenance(report, reasoner, normalized)
        report.validation_errors = list(errors)
        report.completed_at = now.isoformat()
        report.observed_at = payload.observed_at
        report.stale_after = (now + freshness_horizon(candidate.provisional_sleeve, cfg)).isoformat()
        report = _finalize_status(report, packet)
    except ResearchValidationError as exc:
        report.research_status = ResearchStatus.RESEARCH_INCONCLUSIVE
        report.research_conclusion = ResearchConclusion.NEED_MORE_DATA
        report.validation_errors = [str(exc)]
        report.completed_at = now.isoformat()
        report.observed_at = payload.observed_at
        report.facts = list(packet.facts)
        report.derived_metrics = list(packet.derived_metrics)
        report.executive_summary = "Reasoner output failed schema validation."
        report.recommended_next_step = "NEED_MORE_DATA"
        apply_research_provenance(report, reasoner, raw if isinstance(raw, dict) else None)
        _journal(
            {
                "type": "RESEARCH_FAILED",
                "research_id": research_id,
                "candidate_id": candidate.candidate_id,
                "symbol": candidate.symbol,
                "reason": str(exc),
                "research_source": report.research_source,
                "provider": report.provider,
                "model": report.model,
            },
            journal,
            persist=persist,
        )
        # Schema/API validation failures are not investment conclusions. Do not persist a fake report.
        raise

    event = {
        ResearchConclusion.REJECT: "RESEARCH_REJECTED",
        ResearchConclusion.NEED_MORE_DATA: "RESEARCH_INCONCLUSIVE",
        ResearchConclusion.ADVANCE_TO_THESIS: "RESEARCH_COMPLETED",
        ResearchConclusion.KEEP_WATCHING: "RESEARCH_COMPLETED",
    }.get(report.research_conclusion or ResearchConclusion.NEED_MORE_DATA, "RESEARCH_COMPLETED")
    _journal(
        {
            "type": event,
            "research_id": research_id,
            "candidate_id": candidate.candidate_id,
            "symbol": candidate.symbol,
            "conclusion": report.research_conclusion.value if report.research_conclusion else None,
            "status": report.research_status.value,
            "confidence": report.confidence.value,
            "packet_id": packet.packet_id,
            "unsupported_claim_count": len(report.unsupported_claims),
            "research_source": report.research_source,
            "provider": report.provider,
            "model": report.model,
            "ai_call_id": report.ai_call_id,
        },
        journal,
        persist=persist,
    )

    if persist:
        (store or ResearchStore()).save(report)
        if queue_entry and queue_store:
            queue_store.set_status(queue_entry.queue_id, ResearchQueueStatus.COMPLETED)

    return ResearchResult(report=report, packet=packet, candidate=candidate, context=context)


def request_refresh(
    report: ResearchReport,
    *,
    earnings_event: bool = False,
    major_news: bool = False,
    material_filing: bool = False,
    regime_changed: bool = False,
    price_move_pct: float | None = None,
    thesis_concern: bool = False,
    now: datetime | None = None,
    config: dict | None = None,
    journal: Path | None = None,
    store: ResearchStore | None = None,
    persist: bool = False,
) -> ResearchReport:
    freshness, triggers = evaluate_freshness(
        report,
        now=now,
        earnings_event=earnings_event,
        major_news=major_news,
        material_filing=material_filing,
        regime_changed=regime_changed,
        price_move_pct=price_move_pct,
        thesis_concern=thesis_concern,
        config=config,
    )
    updated = apply_freshness(report, freshness, triggers)
    if freshness == ResearchFreshness.RESEARCH_REFRESH_REQUIRED:
        _journal(
            {
                "type": "RESEARCH_REFRESH_REQUIRED",
                "research_id": report.research_id,
                "candidate_id": report.candidate_id,
                "symbol": report.symbol,
                "triggers": triggers,
                "subject_kind": report.subject_kind.value,
            },
            journal,
            persist=persist or journal is not None,
        )
    return updated


def compare_reports(
    reports: list[ResearchReport],
    *,
    reasoner: ComparisonReasoner | None = None,
    store: ResearchStore | None = None,
    persist: bool = True,
    portfolio_overlap_notes: str | None = None,
) -> ResearchResult:
    comparison = build_comparison(reports, reasoner=reasoner, portfolio_overlap_notes=portfolio_overlap_notes)
    if persist:
        (store or ResearchStore()).save_comparison(comparison)
    # Return the first report as anchor; comparison is the payload of interest.
    return ResearchResult(report=reports[0], packet=_empty_packet_anchor(reports[0]), comparison=comparison)


def _empty_packet_anchor(report: ResearchReport) -> ResearchEvidencePacket:
    return ResearchEvidencePacket(
        packet_id=report.packet_id or report.research_id,
        candidate_id=report.candidate_id,
        symbol=report.symbol,
        assembled_at=report.observed_at or report.started_at,
        subject_kind=report.subject_kind,
        provisional_sleeve=report.provisional_sleeve,
        facts=list(report.facts),
        derived_metrics=list(report.derived_metrics),
    )


def apply_research_provenance(
    report: ResearchReport,
    reasoner: ResearchReasoner,
    payload: dict | None = None,
) -> ResearchReport:
    """Stamp AI vs scripted vs deterministic source. Never invent a provider."""
    result = getattr(reasoner, "last_result", None)
    if result is not None:
        report.provider = getattr(result, "provider", None)
        report.model = getattr(result, "model", None)
        report.ai_call_id = getattr(result, "reservation_id", None)
        estimated = getattr(result, "estimated_cost", None)
        actual = getattr(result, "actual_cost", None)
        report.estimated_cost = float(estimated) if estimated is not None else None
        report.actual_cost = float(actual) if actual is not None else None
        report.research_source = "scripted" if str(report.provider or "").lower() == "scripted" else "AI"
        return report
    if payload:
        if payload.get("provider"):
            report.provider = str(payload.get("provider"))
        if payload.get("model"):
            report.model = str(payload.get("model"))
        if payload.get("ai_call_id"):
            report.ai_call_id = str(payload.get("ai_call_id"))
        if payload.get("estimated_cost") is not None:
            report.estimated_cost = float(payload.get("estimated_cost"))
        if payload.get("actual_cost") is not None:
            report.actual_cost = float(payload.get("actual_cost"))
        if payload.get("research_source"):
            report.research_source = str(payload.get("research_source"))
            return report
        if report.provider:
            report.research_source = "scripted" if report.provider.lower() == "scripted" else "AI"
            return report
    name = type(reasoner).__name__
    if name == "ScriptedResearchReasoner":
        report.research_source = "scripted"
    elif name == "GatewayResearchReasoner":
        report.research_source = "AI"
    else:
        report.research_source = "deterministic"
    return report


def evidence_fingerprint(candidate: Candidate, *, payload: ResearchPayload | None = None, packet: ResearchEvidencePacket | None = None) -> str:
    """Stable evidence-generation key. Unchanged evidence must not spawn a new report."""
    sources = []
    facts: list[str] = []
    observed = ""
    if packet is not None:
        sources = list(packet.sources_observed or [])
        facts = [e.name for e in (packet.facts or [])]
        observed = packet.assembled_at or ""
    elif payload is not None:
        sources = list(payload.sources_observed or payload.sources_attempted or [])
        observed = payload.observed_at or ""
    blob = json.dumps(
        {
            "symbol": candidate.symbol.upper(),
            "candidate_id": candidate.candidate_id,
            "day": observed[:10],
            "sources": sorted(str(s) for s in sources),
            "facts": sorted(str(f) for f in facts),
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def _blank_report(
    research_id: str,
    candidate: Candidate,
    packet: ResearchEvidencePacket,
    started: str,
    subject_kind: ResearchSubjectKind,
    existing_thesis_id: str | None,
) -> ResearchReport:
    cls = packet.classification
    sc = None
    if cls.security_class:
        from agentic_portfolio.schemas import SecurityClass

        try:
            sc = SecurityClass(cls.security_class)
        except ValueError:
            sc = candidate.security_class
    return ResearchReport(
        research_id=research_id,
        candidate_id=candidate.candidate_id,
        symbol=candidate.symbol.upper(),
        started_at=started,
        provisional_sleeve=candidate.provisional_sleeve,
        security_class=sc or candidate.security_class,
        sector=candidate.sector or cls.sector,
        industry=candidate.industry or cls.industry,
        market_price=candidate.current_price,
        research_status=ResearchStatus.RESEARCH_PENDING,
        subject_kind=subject_kind,
        thesis_id=existing_thesis_id,
        comparison_group_id=candidate.comparison_group_id,
        discovery_score=candidate.discovery_score,
        packet_id=packet.packet_id,
        observed_at=packet.assembled_at,
        facts=list(packet.facts),
        derived_metrics=list(packet.derived_metrics),
        sources_observed=list(packet.sources_observed),
        sources_unavailable=list(packet.sources_unavailable),
        missing_information=list(packet.missing_information),
    )


def _finalize_status(report: ResearchReport, packet: ResearchEvidencePacket) -> ResearchReport:
    conclusion = report.research_conclusion
    if packet.completeness == "INCOMPLETE" and conclusion == ResearchConclusion.ADVANCE_TO_THESIS:
        # Incomplete packets may still be interpreted, but cannot skip NEED_MORE_DATA
        # unless the reasoner explicitly had enough named facts. Prefer NEED_MORE_DATA
        # only when the reasoner already chose it; do not override REJECT.
        pass
    if conclusion == ResearchConclusion.NEED_MORE_DATA:
        report.research_status = ResearchStatus.RESEARCH_INCONCLUSIVE
        report.recommended_next_step = report.recommended_next_step or "NEED_MORE_DATA"
    elif conclusion == ResearchConclusion.REJECT:
        report.research_status = ResearchStatus.RESEARCH_REJECTED
    elif conclusion in {ResearchConclusion.ADVANCE_TO_THESIS, ResearchConclusion.KEEP_WATCHING}:
        report.research_status = ResearchStatus.RESEARCH_COMPLETE
    else:
        report.research_status = ResearchStatus.RESEARCH_INCONCLUSIVE
    if report.unsupported_claims and report.confidence == ResearchConfidence.HIGH:
        report.confidence = ResearchConfidence.MEDIUM
    return report


def _journal(row: dict, path: Path | None, *, persist: bool = True) -> None:
    if path is None and not persist:
        return
    append_jsonl(row, path or journal_path())


# Re-export for tests that assert execution tools are unreachable.
FORBIDDEN = RESEARCH_FORBIDDEN_TOOLS
assert_no_execution = assert_no_forbidden_tools
cannot_become_buy = research_cannot_become_buy
