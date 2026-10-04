"""Deterministic ProposedAction for a watch item whose entry conditions just passed.

The watch path used to create approvals with a hard-coded ``{"verdict": "PASS"}``.
Now it rebuilds the ProposedAction from the persisted research report (class,
sector, sleeve, liquidity) and the persisted sizing, and runs the real Risk Gate.
No AI call. Missing research fails closed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentic_portfolio.schemas import ClassificationStatus, Decision, ProposedAction, SecurityClass, Sleeve


def _report_for(item: Any, root: Path):
    from agentic_portfolio.research.store import ResearchStore

    store = ResearchStore(Path(root))
    report = store.get(item.research_id) if getattr(item, "research_id", None) else None
    if report is None:
        rows = [r for r in store.by_symbol(item.ticker) if r.research_status is not None]
        rows.sort(key=lambda r: r.started_at or "", reverse=True)
        report = rows[0] if rows else None
    return report


def build_watch_proposal(item: Any, context: Any, *, dollars: float | None, price: float | None, root: Path) -> tuple[ProposedAction | None, str | None]:
    from agentic_portfolio.decision.engine import _liquidity_from_report

    report = _report_for(item, root)
    if report is None:
        return None, "missing_research_for_risk_gate"
    sleeve = None
    raw_sleeve = getattr(item, "sleeve", None)
    if raw_sleeve:
        try:
            sleeve = raw_sleeve if isinstance(raw_sleeve, Sleeve) else Sleeve(str(raw_sleeve).upper())
        except ValueError:
            sleeve = None
    sleeve = sleeve or report.provisional_sleeve
    existing = sum(p.market_value for p in (context.positions or []) if p.symbol.upper() == item.ticker.upper())
    security_class = report.security_class or SecurityClass.INDIVIDUAL_EQUITY
    status = ClassificationStatus.VALIDATED if report.security_class else ClassificationStatus.INSUFFICIENT_EVIDENCE
    sector = None if not report.sector or str(report.sector).upper() in {"UNKNOWN", "MISCELLANEOUS"} else report.sector
    action = ProposedAction(
        symbol=item.ticker.upper(),
        decision=Decision.ADD if existing > 0 else Decision.BUY,
        security_class=security_class,
        classification_status=status,
        sleeve=sleeve,
        current_price=price if price is not None else report.market_price,
        proposed_notional=dollars,
        sector=sector,
        thesis_id=getattr(item, "thesis_id", None),
        liquidity=_liquidity_from_report(report),
    )
    return action, None


def evaluate_watch_proposal(action: ProposedAction, context: Any, *, root: Path):
    from agentic_portfolio.risk_gate import evaluate
    from agentic_portfolio.sleeve_registry import SleeveRegistry
    from agentic_portfolio.thesis_registry import ThesisRegistry

    base = Path(root)
    return evaluate(
        context,
        action,
        sleeves=SleeveRegistry(base / "state" / "sleeve_registry.json"),
        theses=ThesisRegistry(base / "state" / "thesis_registry.json"),
    )
