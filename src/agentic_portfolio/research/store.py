"""Persist ResearchReports without overwriting history."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentic_portfolio.paths import project_root
from agentic_portfolio.research.types import (
    DislocationAssessment,
    DislocationVerdict,
    EvidenceItem,
    EvidenceKind,
    ResearchComparison,
    ResearchConclusion,
    ResearchConfidence,
    ResearchFreshness,
    ResearchReport,
    ResearchStatus,
    ResearchSubjectKind,
    ScenarioCase,
)
from agentic_portfolio.schemas import SecurityClass, Sleeve, to_dict


def research_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "state" / "research_reports"


def comparisons_path(root: Path | None = None) -> Path:
    return research_dir(root) / "comparisons.json"


class ResearchStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = research_dir(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._packets = self.root / "packets"
        self._packets.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.json"
        self._index = self._load_index()

    def _load_index(self) -> dict[str, Any]:
        if self._index_path.exists():
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        return {"by_id": {}, "by_symbol": {}, "by_candidate": {}, "by_thesis": {}, "by_date": {}}

    def _save_index(self) -> None:
        self._index_path.write_text(json.dumps(self._index, indent=2, default=str), encoding="utf-8")

    def path_for(self, research_id: str) -> Path:
        return self.root / f"{research_id}.json"

    def save(self, report: ResearchReport) -> Path:
        """Always write a new id file. Never overwrite a prior report in place."""
        path = self.path_for(report.research_id)
        if path.exists():
            # History is preserved by new research_id. Refuse silent overwrite.
            raise FileExistsError(f"research report already exists: {report.research_id}")
        path.write_text(json.dumps(to_dict(report), indent=2, default=str), encoding="utf-8")
        self._index.setdefault("by_id", {})[report.research_id] = {
            "symbol": report.symbol,
            "candidate_id": report.candidate_id,
            "thesis_id": report.thesis_id,
            "completed_at": report.completed_at,
            "path": path.name,
        }
        self._index.setdefault("by_symbol", {}).setdefault(report.symbol.upper(), []).append(report.research_id)
        self._index.setdefault("by_candidate", {}).setdefault(report.candidate_id, []).append(report.research_id)
        if report.thesis_id:
            self._index.setdefault("by_thesis", {}).setdefault(report.thesis_id, []).append(report.research_id)
        day = (report.completed_at or report.started_at or "")[:10]
        if day:
            self._index.setdefault("by_date", {}).setdefault(day, []).append(report.research_id)
        self._save_index()
        return path

    def save_packet(self, packet) -> Path:
        """Persist the evidence packet that was sent (or would have been sent) to Terra."""
        from agentic_portfolio.research.types import ResearchEvidencePacket

        if not isinstance(packet, ResearchEvidencePacket):
            raise TypeError("save_packet expects ResearchEvidencePacket")
        path = self._packets / f"{packet.packet_id}.json"
        path.write_text(json.dumps(to_dict(packet), indent=2, default=str), encoding="utf-8")
        self._index.setdefault("packets_by_symbol", {}).setdefault(packet.symbol.upper(), []).append(packet.packet_id)
        self._index.setdefault("packets_by_id", {})[packet.packet_id] = {
            "symbol": packet.symbol,
            "candidate_id": packet.candidate_id,
            "assembled_at": packet.assembled_at,
            "completeness": packet.completeness,
            "path": f"packets/{path.name}",
        }
        self._save_index()
        return path

    def get_packet(self, packet_id: str):
        path = self._packets / f"{packet_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def packets_for_symbol(self, symbol: str) -> list[dict[str, Any]]:
        ids = self._index.get("packets_by_symbol", {}).get(symbol.upper(), [])
        return [p for pid in ids if (p := self.get_packet(pid))]

    def get(self, research_id: str) -> ResearchReport | None:
        path = self.path_for(research_id)
        if not path.exists():
            return None
        return report_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def by_symbol(self, symbol: str) -> list[ResearchReport]:
        ids = self._index.get("by_symbol", {}).get(symbol.upper(), [])
        return [r for rid in ids if (r := self.get(rid))]

    def by_candidate(self, candidate_id: str) -> list[ResearchReport]:
        ids = self._index.get("by_candidate", {}).get(candidate_id, [])
        return [r for rid in ids if (r := self.get(rid))]

    def by_thesis(self, thesis_id: str) -> list[ResearchReport]:
        ids = self._index.get("by_thesis", {}).get(thesis_id, [])
        return [r for rid in ids if (r := self.get(rid))]

    def by_date(self, day: str) -> list[ResearchReport]:
        ids = self._index.get("by_date", {}).get(day, [])
        return [r for rid in ids if (r := self.get(rid))]

    def latest_for_symbol(self, symbol: str) -> ResearchReport | None:
        reports = self.by_symbol(symbol)
        if not reports:
            return None
        return sorted(reports, key=lambda r: r.started_at, reverse=True)[0]

    def latest_valid_for_symbol(self, symbol: str) -> ResearchReport | None:
        from agentic_portfolio.research.operational import last_valid_investment_report

        return last_valid_investment_report(self.by_symbol(symbol))

    def all_ids(self) -> list[str]:
        return list(self._index.get("by_id") or {})

    def all_reports(self) -> list[ResearchReport]:
        return [r for research_id in self.all_ids() if (r := self.get(research_id))]

    def save_comparison(self, comparison: ResearchComparison) -> Path:
        path = self.root / "comparisons.json"
        data = {"records": {}}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("records", {})[comparison.comparison_id] = to_dict(comparison)
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return path


def report_from_dict(raw: dict[str, Any]) -> ResearchReport:
    def items(key: str) -> list[EvidenceItem]:
        out = []
        for item in raw.get(key) or []:
            out.append(
                EvidenceItem(
                    evidence_id=item["evidence_id"],
                    kind=EvidenceKind(item["kind"]),
                    name=item["name"],
                    value=item.get("value"),
                    source=item.get("source"),
                    observed_at=item.get("observed_at"),
                    data_type=item.get("data_type") or "unknown",
                    raw_ref=item.get("raw_ref"),
                    derived=bool(item.get("derived")),
                    freshness=item.get("freshness"),
                    notes=list(item.get("notes") or []),
                    evidence_refs=list(item.get("evidence_refs") or []),
                )
            )
        return out

    def case(key: str) -> ScenarioCase | None:
        item = raw.get(key)
        if not item:
            return None
        return ScenarioCase(
            case=item.get("case") or key,
            summary=item.get("summary") or "",
            major_assumptions=list(item.get("major_assumptions") or []),
            expected_business_outcome=item.get("expected_business_outcome"),
            major_risk=item.get("major_risk"),
            attractiveness_implication=item.get("attractiveness_implication"),
            evidence_refs=list(item.get("evidence_refs") or []),
            price_target=item.get("price_target"),
            notes=list(item.get("notes") or []),
        )

    def loc(key: str) -> DislocationAssessment | None:
        item = raw.get(key)
        if not item:
            return None
        return DislocationAssessment(
            verdict=DislocationVerdict(item["verdict"]),
            reasoning=item.get("reasoning") or "",
            evidence_refs=list(item.get("evidence_refs") or []),
        )

    sc = raw.get("security_class")
    conclusion = raw.get("research_conclusion")
    return ResearchReport(
        research_id=raw["research_id"],
        candidate_id=raw["candidate_id"],
        symbol=raw["symbol"],
        started_at=raw["started_at"],
        completed_at=raw.get("completed_at"),
        provisional_sleeve=Sleeve(raw["provisional_sleeve"]),
        security_class=SecurityClass(sc) if sc else None,
        sector=raw.get("sector"),
        industry=raw.get("industry"),
        market_price=raw.get("market_price"),
        research_status=ResearchStatus(raw.get("research_status") or "RESEARCH_PENDING"),
        subject_kind=ResearchSubjectKind(raw.get("subject_kind") or "NEW_CANDIDATE"),
        executive_summary=raw.get("executive_summary"),
        business_summary=raw.get("business_summary"),
        investment_question=raw.get("investment_question"),
        fundamental_analysis=raw.get("fundamental_analysis"),
        financial_analysis=raw.get("financial_analysis"),
        valuation_analysis=raw.get("valuation_analysis"),
        earnings_analysis=raw.get("earnings_analysis"),
        competitive_analysis=raw.get("competitive_analysis"),
        technical_context=raw.get("technical_context"),
        market_context=raw.get("market_context"),
        sector_context=raw.get("sector_context"),
        news_analysis=raw.get("news_analysis"),
        filing_analysis=raw.get("filing_analysis"),
        catalyst_analysis=raw.get("catalyst_analysis"),
        risk_analysis=raw.get("risk_analysis"),
        bull_case=case("bull_case"),
        base_case=case("base_case"),
        bear_case=case("bear_case"),
        temporary_dislocation_assessment=loc("temporary_dislocation_assessment"),
        fundamental_deterioration_assessment=loc("fundamental_deterioration_assessment"),
        key_catalysts=list(raw.get("key_catalysts") or []),
        key_risks=list(raw.get("key_risks") or []),
        invalidation_candidates=list(raw.get("invalidation_candidates") or []),
        expected_horizon=raw.get("expected_horizon"),
        missing_information=list(raw.get("missing_information") or []),
        conflicting_evidence=list(raw.get("conflicting_evidence") or []),
        evidence_refs=list(raw.get("evidence_refs") or []),
        facts=items("facts"),
        derived_metrics=items("derived_metrics"),
        ai_interpretations=items("ai_interpretations"),
        confidence=ResearchConfidence(raw.get("confidence") or "LOW"),
        research_conclusion=ResearchConclusion(conclusion) if conclusion else None,
        recommended_next_step=raw.get("recommended_next_step"),
        observed_at=raw.get("observed_at"),
        freshness=ResearchFreshness(raw.get("freshness") or "FRESH"),
        thesis_id=raw.get("thesis_id"),
        packet_id=raw.get("packet_id"),
        comparison_group_id=raw.get("comparison_group_id"),
        discovery_score=raw.get("discovery_score"),
        unsupported_claims=list(raw.get("unsupported_claims") or []),
        validation_errors=list(raw.get("validation_errors") or []),
        sources_observed=list(raw.get("sources_observed") or []),
        sources_unavailable=list(raw.get("sources_unavailable") or []),
        earnings_effect_kind=raw.get("earnings_effect_kind"),
        refresh_triggers=list(raw.get("refresh_triggers") or []),
        proposed_actions_created=int(raw.get("proposed_actions_created") or 0),
        buy_actions_created=int(raw.get("buy_actions_created") or 0),
        execution_attempted=bool(raw.get("execution_attempted") or False),
        risk_limits_unchanged=raw.get("risk_limits_unchanged", True),
        portfolio_facts_unchanged=raw.get("portfolio_facts_unchanged", True),
        classification_unchanged=raw.get("classification_unchanged", True),
        stale_after=raw.get("stale_after"),
        research_source=raw.get("research_source"),
        provider=raw.get("provider"),
        model=raw.get("model"),
        ai_call_id=raw.get("ai_call_id"),
        estimated_cost=float(raw["estimated_cost"]) if raw.get("estimated_cost") is not None else None,
        actual_cost=float(raw["actual_cost"]) if raw.get("actual_cost") is not None else None,
        evidence_fingerprint=raw.get("evidence_fingerprint"),
        research_generation=int(raw.get("research_generation") or 1),
        runtime_mode=raw.get("runtime_mode"),
        production_artifact=raw.get("production_artifact", True),
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
