"""Operational research failures are not investment conclusions.

Schema, provider, budget, timeout, truncation, and collector-bug reports must
never be treated as the canonical thesis for a symbol.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from agentic_portfolio.research.sufficiency import looks_like_pre_fix_need_more_data
from agentic_portfolio.research.types import ResearchConclusion, ResearchReport, ResearchStatus


SCHEMA_FAILURE_SUMMARY = "Reasoner output failed schema validation."
INVESTMENT_CONCLUSIONS = {
    ResearchConclusion.ADVANCE_TO_THESIS,
    ResearchConclusion.KEEP_WATCHING,
    ResearchConclusion.REJECT,
}
QUOTE_LIKE_FACTS = frozenset(
    {
        "market_price",
        "bid",
        "ask",
        "bid_price",
        "ask_price",
        "last_price",
        "previous_close",
        "mark_price",
        "mid_price",
        "last_trade_price",
        "rsi",
        "sma_50",
        "sma_200",
        "return_1d",
        "return_5d",
        "return_21d",
    }
)


def _as_dict(report: ResearchReport | dict[str, Any]) -> dict[str, Any]:
    if isinstance(report, dict):
        return report
    return {
        "executive_summary": getattr(report, "executive_summary", None),
        "thesis": getattr(report, "executive_summary", None),
        "recommended_next_step": getattr(report, "recommended_next_step", None),
        "recommended_action": getattr(report, "research_conclusion", None),
        "research_conclusion": getattr(report, "research_conclusion", None),
        "research_status": getattr(report, "research_status", None),
        "validation_errors": list(getattr(report, "validation_errors", None) or []),
        "bull_case": getattr(report, "bull_case", None),
        "missing_information": list(getattr(report, "missing_information", None) or []),
        "sources_unavailable": list(getattr(report, "sources_unavailable", None) or []),
        "symbol": getattr(report, "symbol", None),
        "security_class": getattr(report, "security_class", None),
    }


def _enum_value(raw: Any) -> str:
    if raw is None:
        return ""
    return str(getattr(raw, "value", raw) or "")


def looks_like_schema_failure_report(report: ResearchReport | dict[str, Any] | None) -> bool:
    """True when a stored report is the fabricated schema-validation fallback."""
    if report is None:
        return False
    data = _as_dict(report)
    summary = str(data.get("executive_summary") or data.get("thesis") or "")
    if SCHEMA_FAILURE_SUMMARY in summary:
        return True
    errors = [str(item) for item in (data.get("validation_errors") or [])]
    if not errors:
        return False
    conclusion = _enum_value(data.get("research_conclusion") or data.get("recommended_action"))
    next_step = str(data.get("recommended_next_step") or "")
    if conclusion == ResearchConclusion.NEED_MORE_DATA.value and next_step == "NEED_MORE_DATA" and not data.get("bull_case"):
        joined = " ".join(errors).lower()
        if "schema" in joined or "malformed" in joined or "missing keys" in joined or "material report" in joined:
            return True
    return False


def looks_like_operational_failure_report(report: ResearchReport | dict[str, Any] | None) -> bool:
    """True when the latest stored report is a software/AI failure, not a thesis."""
    if report is None:
        return False
    if looks_like_schema_failure_report(report):
        return True
    if isinstance(report, ResearchReport) and looks_like_pre_fix_need_more_data(report):
        return True
    return False


def is_canonical_investment_report(report: ResearchReport | None) -> bool:
    """Usable as the latest investment state. Operational failures never qualify."""
    if report is None:
        return False
    if looks_like_operational_failure_report(report):
        return False
    status = report.research_status
    if status in {ResearchStatus.RESEARCH_PENDING, ResearchStatus.RESEARCHING}:
        return False
    conclusion = report.research_conclusion
    if conclusion in INVESTMENT_CONCLUSIONS:
        return True
    # Legitimate NEED_MORE_DATA (missing core evidence) is a research outcome,
    # but it is not a replacement for a prior KEEP_WATCHING / ADVANCE_TO_THESIS.
    return False


def last_valid_investment_report(reports: list[ResearchReport]) -> ResearchReport | None:
    usable = [r for r in reports if is_canonical_investment_report(r)]
    if not usable:
        return None
    return sorted(usable, key=lambda r: r.started_at or "", reverse=True)[0]


def report_is_still_fresh(report: ResearchReport | None, *, now: datetime | None = None) -> bool:
    if report is None:
        return False
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    raw = report.stale_after
    if not raw:
        return report.freshness.value == "FRESH" if getattr(report, "freshness", None) else False
    try:
        until = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return stamp < until


def screening_is_fresh(row: dict[str, Any] | None, *, now: datetime | None = None, ttl_hours: float = 48.0) -> bool:
    if not row:
        return False
    raw = row.get("created_at") or row.get("timestamp")
    if not raw:
        return False
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    try:
        created = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (stamp - created) < timedelta(hours=float(ttl_hours))


def payload_needs_schema_retry(payload: Any) -> bool:
    """Cheap pre-check used by the Terra reasoner before the engine's full validator."""
    if not isinstance(payload, dict):
        return True
    required = ("research_conclusion", "confidence", "executive_summary")
    if any(payload.get(key) in (None, "") for key in required):
        return True
    conclusion = str(payload.get("research_conclusion") or "")
    if conclusion not in {c.value for c in ResearchConclusion}:
        return True
    if conclusion in {ResearchConclusion.ADVANCE_TO_THESIS.value, ResearchConclusion.KEEP_WATCHING.value}:
        for key in ("bull_case", "base_case", "bear_case"):
            case = payload.get(key)
            if not isinstance(case, dict) or not case.get("summary"):
                return True
    return False
