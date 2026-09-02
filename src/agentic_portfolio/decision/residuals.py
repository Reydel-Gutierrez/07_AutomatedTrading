"""Deterministic CASH yield and SPY comparison snapshot for portfolio decision.

These are comparison facts. They do not authorize BUY/ADD and do not call Terra.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from agentic_portfolio.decision.types import SPY_SYMBOL
from agentic_portfolio.research.types import ResearchReport
from agentic_portfolio.schemas import PortfolioContext

YIELD_UNAVAILABLE = "unavailable"
YIELD_KNOWN = "known"
YIELD_STALE = "stale"
DEFAULT_YIELD_MAX_AGE_DAYS = 90
CONFIGURED_YIELD_SOURCE = "configured_risk_free_proxy"


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def _parse_as_of(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        text = text + "T00:00:00+00:00"
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _report_metric(report: ResearchReport | None, *names: str) -> Any:
    if report is None:
        return None
    want = {str(n) for n in names}
    for item in list(getattr(report, "facts", None) or []) + list(getattr(report, "derived_metrics", None) or []):
        if str(getattr(item, "name", "") or "") in want and getattr(item, "value", None) not in (None, ""):
            return item.value
    return None


def resolve_cash_yield(
    cash_pol: Mapping[str, Any] | None,
    *,
    now: datetime,
    context: PortfolioContext | None = None,
) -> dict[str, Any]:
    """Return explicit cash-yield fields. Never invent a rate.

    Known = numeric yield present and not stale.
    Unavailable / stale remain non-fatal; the committee still runs.
    """
    pol = dict(cash_pol or {})
    live = _as_float(getattr(context, "cash_yield", None) if context is not None else None)
    configured = _as_float(pol.get("current_yield"))
    if live is not None:
        current = live
        source = str(pol.get("live_yield_source") or "portfolio_context") or "portfolio_context"
        as_of = pol.get("yield_as_of") or (context.timestamp if context is not None else None)
    else:
        current = configured
        source = pol.get("yield_source")
        as_of = pol.get("yield_as_of")
        if current is not None and not source:
            source = CONFIGURED_YIELD_SOURCE

    max_age = pol.get("yield_max_age_days")
    try:
        max_age_days = float(max_age) if max_age is not None else float(DEFAULT_YIELD_MAX_AGE_DAYS)
    except (TypeError, ValueError):
        max_age_days = float(DEFAULT_YIELD_MAX_AGE_DAYS)

    stamp = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    as_of_dt = _parse_as_of(as_of)
    stale = False
    if current is not None and as_of_dt is not None and max_age_days >= 0:
        stale = (stamp - as_of_dt).total_seconds() > max_age_days * 86400.0

    if current is None:
        status = YIELD_UNAVAILABLE
        known = False
    elif stale:
        status = YIELD_STALE
        known = False
    else:
        status = YIELD_KNOWN
        known = True

    return {
        "current_yield": current,
        "yield_known": known,
        "yield_source": str(source) if source else None,
        "yield_as_of": str(as_of) if as_of else None,
        "yield_status": status,
        "yield_unit": str(pol.get("yield_unit") or "annualized_decimal"),
    }


def spy_snapshot_usable(snapshot: Mapping[str, Any] | None) -> bool:
    if not snapshot:
        return False
    return snapshot.get("current_price") is not None or snapshot.get("return_1d") is not None


def build_broad_market_residual(
    context: PortfolioContext | None,
    *,
    spy_report: ResearchReport | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Deterministic SPY comparison snapshot. Insufficient for purchase."""
    spy = getattr(context, "spy", None) if context is not None else None
    price = _as_float(getattr(spy, "price", None))
    return_1d = _as_float(getattr(spy, "period_return", None))
    sources: list[str] = []
    if price is not None or return_1d is not None:
        sources.append("live_quote")

    report_price = _as_float(getattr(spy_report, "market_price", None) if spy_report is not None else None)
    if report_price is None:
        report_price = _as_float(_report_metric(spy_report, "market_price"))
    if price is None and report_price is not None:
        price = report_price
        sources.append("research_facts")
    elif spy_report is not None and (
        _report_metric(
            spy_report,
            "return_21d",
            "return_63d",
            "return_252d",
            "drawdown_from_52w_high",
            "fund_pe_ratio",
            "pe_ratio",
            "sma_alignment",
        )
        is not None
        or report_price is not None
    ):
        if "research_facts" not in sources:
            sources.append("research_facts")

    if return_1d is None:
        return_1d = _as_float(_report_metric(spy_report, "return_1d", "period_return"))

    trend = _report_metric(spy_report, "sma_alignment", "spy_trend")
    as_of = None
    if context is not None and getattr(context, "timestamp", None):
        as_of = context.timestamp
    elif spy_report is not None:
        as_of = spy_report.completed_at or spy_report.observed_at or spy_report.started_at
    elif now is not None:
        as_of = now.isoformat()

    snapshot = {
        "symbol": SPY_SYMBOL,
        "current_price": price,
        "return_1d": return_1d,
        "return_21d": _as_float(_report_metric(spy_report, "return_21d")),
        "return_63d": _as_float(_report_metric(spy_report, "return_63d")),
        "return_252d": _as_float(_report_metric(spy_report, "return_252d")),
        "trend": str(trend) if trend not in (None, "") else None,
        "sma_alignment": str(_report_metric(spy_report, "sma_alignment") or "") or None,
        "drawdown_from_52w_high": _as_float(_report_metric(spy_report, "drawdown_from_52w_high")),
        "fund_pe": _as_float(_report_metric(spy_report, "fund_pe_ratio", "pe_ratio")),
        "source": "+".join(sources) if sources else None,
        "as_of": as_of,
        "usable_for_comparison": False,
        "comparison_only": True,
        "insufficient_for_purchase": True,
        "does_not_authorize_buy": True,
    }
    snapshot["usable_for_comparison"] = spy_snapshot_usable(snapshot)
    if not snapshot["usable_for_comparison"] and snapshot["source"] is None:
        snapshot["source"] = None
    return snapshot
