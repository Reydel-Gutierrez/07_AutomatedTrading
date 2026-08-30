"""Structure SEC filing evidence. Do not keyword-score investment quality."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4

from agentic_portfolio.research.metrics import as_float
from agentic_portfolio.research.types import EvidenceItem, EvidenceKind

# GAAP concepts requested when facts are available. Values remain OBSERVED_FACT.
DEFAULT_GAAP_CONCEPTS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "NetIncomeLoss",
    "GrossProfit",
    "OperatingIncomeLoss",
    "Assets",
    "Liabilities",
    "LongTermDebt",
    "LongTermDebtNoncurrent",
    "CashAndCashEquivalentsAtCarryingValue",
    "NetCashProvidedByUsedInOperatingActivities",
    "StockholdersEquity",
    "EarningsPerShareDiluted",
    "WeightedAverageNumberOfDilutedSharesOutstanding",
)


def filing_index_items(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not payload:
        return []
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return []
    rows = data.get("results") or data.get("filings") or data.get("items") or []
    if isinstance(rows, list):
        return [r for r in rows if isinstance(r, dict)]
    return []


def facts_from_sec(
    payload: Mapping[str, Any] | None,
    *,
    observed_at: str,
    source: str = "get_sec_filing_facts",
) -> list[EvidenceItem]:
    """GAAP tagged numbers as observed facts. No interpretation."""
    items: list[EvidenceItem] = []
    if not payload:
        return items
    data = payload.get("data", payload) if isinstance(payload, Mapping) else {}
    if not isinstance(data, dict):
        return items
    rows = data.get("results") or data.get("facts") or data.get("items") or []
    if isinstance(data.get("facts"), dict):
        rows = [{"concept": k, **(v if isinstance(v, dict) else {"value": v})} for k, v in data["facts"].items()]
    if not isinstance(rows, list):
        return items
    for row in rows:
        if not isinstance(row, dict):
            continue
        concept = str(row.get("concept") or row.get("name") or row.get("tag") or "")
        value = row.get("value") or row.get("amount") or row.get("fact")
        num = as_float(value)
        if not concept:
            continue
        items.append(
            EvidenceItem(
                evidence_id=f"fact:sec:{concept}:{uuid4().hex[:8]}",
                kind=EvidenceKind.OBSERVED_FACT,
                name=f"sec_fact.{concept}",
                value=num if num is not None else value,
                source=source,
                observed_at=observed_at,
                data_type="number" if num is not None else "text",
                raw_ref=str(row.get("filing_id") or row.get("id") or concept),
                notes=[str(row.get("period") or row.get("end_date") or "")],
            )
        )
    return items


def structured_filing_meta(
    index_payload: Mapping[str, Any] | None,
    *,
    observed_at: str,
    sections: list[Mapping[str, Any]] | None = None,
) -> list[EvidenceItem]:
    """Filing identity + optional section excerpts. Excerpts are facts, not scores."""
    items: list[EvidenceItem] = []
    for row in filing_index_items(index_payload):
        form = row.get("form_type") or row.get("form") or row.get("type")
        fid = row.get("filing_id") or row.get("id") or row.get("accession")
        filed = row.get("date_filed") or row.get("filed_at") or row.get("filing_date") or row.get("date")
        items.append(
            EvidenceItem(
                evidence_id=f"fact:filing:{fid or form}:{uuid4().hex[:8]}",
                kind=EvidenceKind.OBSERVED_FACT,
                name="sec_filing",
                value={
                    "form_type": form,
                    "filing_id": fid,
                    "filed_at": filed,
                    "title": row.get("title") or row.get("description"),
                },
                source="get_sec_filing_index",
                observed_at=observed_at,
                data_type="object",
                raw_ref=str(fid) if fid else None,
            )
        )
    for sec in sections or []:
        if not isinstance(sec, Mapping):
            continue
        sid = sec.get("section") or sec.get("id") or "section"
        text = sec.get("text") or sec.get("content") or sec.get("excerpt")
        if not text:
            continue
        excerpt = str(text)
        if len(excerpt) > 4000:
            excerpt = excerpt[:4000]
        items.append(
            EvidenceItem(
                evidence_id=f"fact:filing_section:{sid}:{uuid4().hex[:8]}",
                kind=EvidenceKind.OBSERVED_FACT,
                name=f"sec_section.{sid}",
                value=excerpt,
                source="get_sec_filing",
                observed_at=observed_at,
                data_type="text",
                raw_ref=str(sec.get("filing_id") or sid),
                notes=["excerpt_not_a_keyword_score"],
            )
        )
    return items
