"""Deterministic CASH yield and broad-market residual snapshot.

Comparison facts for CASH vs diversified CORE exposure vs individual names.
The configured residual vehicle may be proposed via the strategic residual path.
This module does not call Terra and does not invent observed market facts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from agentic_portfolio.decision.types import SPY_SYMBOL, STRATEGIC_RESIDUAL_SOURCE
from agentic_portfolio.policy import load_decision_config, load_policy
from agentic_portfolio.research.types import (
    EvidenceItem,
    EvidenceKind,
    ResearchConfidence,
    ResearchFreshness,
    ResearchReport,
    ResearchStatus,
    ResearchSubjectKind,
)
from agentic_portfolio.schemas import ClassificationStatus, PortfolioContext, SecurityClass, Sleeve

YIELD_UNAVAILABLE = "unavailable"
YIELD_KNOWN = "known"
YIELD_STALE = "stale"
DEFAULT_YIELD_MAX_AGE_DAYS = 90
CONFIGURED_YIELD_SOURCE = "configured_actual_account_cash_yield"
CONFIGURED_ADV_SOURCE = "configured_supporting_adv"


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


def residual_config(decision_cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(decision_cfg or load_decision_config())
    committee = dict(cfg.get("core_committee") or {})
    raw = committee.get("broad_market_residual")
    if not isinstance(raw, dict):
        raw = cfg.get("broad_market_residual")
    return dict(raw or {})


def configured_residual_symbol(
    *,
    policy: Mapping[str, Any] | None = None,
    decision_cfg: Mapping[str, Any] | None = None,
    residual_cfg: Mapping[str, Any] | None = None,
) -> str:
    """Configured CORE broad-market residual. Defaults to the CORE_GROWTH benchmark."""
    raw = dict(residual_cfg) if residual_cfg is not None else residual_config(decision_cfg)
    symbol = str(raw.get("symbol") or "").upper().strip()
    if symbol:
        return symbol
    pol = dict(policy) if policy is not None else load_policy()
    bench = str(((pol.get("sleeves") or {}).get("CORE_GROWTH") or {}).get("benchmark") or "").upper().strip()
    return bench or SPY_SYMBOL


def is_configured_residual_symbol(
    symbol: str | None,
    *,
    policy: Mapping[str, Any] | None = None,
    decision_cfg: Mapping[str, Any] | None = None,
    residual_cfg: Mapping[str, Any] | None = None,
) -> bool:
    if not symbol:
        return False
    return str(symbol).upper() == configured_residual_symbol(
        policy=policy, decision_cfg=decision_cfg, residual_cfg=residual_cfg
    )


def is_strategic_residual_report(report: ResearchReport | None) -> bool:
    if report is None:
        return False
    return str(getattr(report, "research_source", "") or "") == STRATEGIC_RESIDUAL_SOURCE


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


def residual_strategic_buy_eligible(snapshot: Mapping[str, Any] | None) -> bool:
    """True when the configured residual vehicle may be proposed without a company thesis."""
    if not spy_snapshot_usable(snapshot):
        return False
    if snapshot.get("allows_strategic_allocation") is False:
        return False
    return True


def build_broad_market_residual(
    context: PortfolioContext | None,
    *,
    spy_report: ResearchReport | None = None,
    now: datetime | None = None,
    residual_cfg: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    decision_cfg: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic residual snapshot. Usable snapshots may support the strategic residual path."""
    cfg = dict(residual_cfg) if residual_cfg is not None else residual_config(decision_cfg)
    symbol = configured_residual_symbol(policy=policy, decision_cfg=decision_cfg, residual_cfg=cfg)
    spy = getattr(context, "spy", None) if context is not None else None
    use_live_quote = symbol == SPY_SYMBOL or str(getattr(spy, "symbol", "") or "").upper() == symbol
    price = _as_float(getattr(spy, "price", None)) if use_live_quote else None
    return_1d = _as_float(getattr(spy, "period_return", None)) if use_live_quote else None
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
            "average_volume",
            "median_daily_dollar_volume_20d",
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

    adv = _as_float(_report_metric(spy_report, "median_daily_dollar_volume_20d"))
    avg_vol = _as_float(_report_metric(spy_report, "average_volume"))
    if adv is None and avg_vol is not None and price is not None:
        adv = avg_vol * price
        if "research_facts" not in sources:
            sources.append("research_facts")
    configured_adv = _as_float(cfg.get("median_daily_dollar_volume_20d"))
    adv_source = None
    if adv is not None:
        adv_source = "research_facts"
    elif configured_adv is not None:
        adv = configured_adv
        adv_source = str(cfg.get("volume_source") or CONFIGURED_ADV_SOURCE)
        if adv_source not in sources:
            sources.append(adv_source)

    snapshot = {
        "symbol": symbol,
        "current_price": price,
        "return_1d": return_1d,
        "return_21d": _as_float(_report_metric(spy_report, "return_21d")),
        "return_63d": _as_float(_report_metric(spy_report, "return_63d")),
        "return_252d": _as_float(_report_metric(spy_report, "return_252d")),
        "trend": str(trend) if trend not in (None, "") else None,
        "sma_alignment": str(_report_metric(spy_report, "sma_alignment") or "") or None,
        "drawdown_from_52w_high": _as_float(_report_metric(spy_report, "drawdown_from_52w_high")),
        "fund_pe": _as_float(_report_metric(spy_report, "fund_pe_ratio", "pe_ratio")),
        "median_daily_dollar_volume_20d": adv,
        "volume_source": adv_source,
        "source": "+".join(sources) if sources else None,
        "as_of": as_of,
        "usable_for_comparison": False,
        "comparison_snapshot": True,
        "comparison_only": True,
        "insufficient_for_purchase": True,
        "does_not_authorize_buy": True,
        "does_not_authorize_individual_stock_buy": True,
        "requires_company_thesis": False,
        "purchase_requires_advance_to_thesis": False,
        "strategic_allocation_vehicle": True,
        "strategic_residual_eligible": False,
        "allows_strategic_allocation": bool(cfg.get("allows_strategic_allocation_without_company_thesis", True)),
        "security_class": str(cfg.get("security_class") or SecurityClass.BROAD_MARKET_INDEX_ETF.value),
        "classification_status": ClassificationStatus.VALIDATED.value,
        "classification_basis": "configured_core_benchmark_vehicle",
        "portfolio_role": "strategic_core_market_exposure",
    }
    snapshot["usable_for_comparison"] = spy_snapshot_usable(snapshot)
    eligible = residual_strategic_buy_eligible(snapshot)
    snapshot["strategic_residual_eligible"] = eligible
    snapshot["comparison_only"] = not eligible
    snapshot["insufficient_for_purchase"] = not eligible
    snapshot["does_not_authorize_buy"] = not eligible
    if not snapshot["usable_for_comparison"] and snapshot["source"] is None:
        snapshot["source"] = None
    return snapshot


def _evidence(name: str, value: Any, *, source: str | None, observed_at: str | None, derived: bool = False) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"residual:{name}",
        kind=EvidenceKind.DETERMINISTIC_DERIVED_METRIC if derived else EvidenceKind.OBSERVED_FACT,
        name=name,
        value=value,
        source=source,
        observed_at=observed_at,
        derived=derived,
    )


def build_strategic_residual_report(
    snapshot: Mapping[str, Any] | None,
    *,
    now: datetime,
    candidate_id: str | None = None,
) -> ResearchReport | None:
    """Wrap snapshot facts as a residual vehicle report. Does not invent prices or yields."""
    if not residual_strategic_buy_eligible(snapshot):
        return None
    snap = dict(snapshot or {})
    symbol = str(snap.get("symbol") or SPY_SYMBOL).upper()
    stamp = now.isoformat() if now.tzinfo else now.replace(tzinfo=timezone.utc).isoformat()
    as_of = str(snap.get("as_of") or stamp)
    source = str(snap.get("source") or "broad_market_residual")
    facts: list[EvidenceItem] = []
    derived: list[EvidenceItem] = []
    price = _as_float(snap.get("current_price"))
    if price is not None:
        facts.append(_evidence("market_price", price, source=source, observed_at=as_of))
    adv = _as_float(snap.get("median_daily_dollar_volume_20d"))
    if adv is not None:
        facts.append(
            _evidence(
                "median_daily_dollar_volume_20d",
                adv,
                source=str(snap.get("volume_source") or source),
                observed_at=as_of,
            )
        )
    for name, derived_flag in (
        ("return_1d", True),
        ("return_21d", True),
        ("return_63d", True),
        ("return_252d", True),
        ("drawdown_from_52w_high", True),
        ("fund_pe", False),
        ("sma_alignment", True),
    ):
        value = snap.get(name) if name != "fund_pe" else snap.get("fund_pe")
        if value in (None, ""):
            continue
        item = _evidence(name if name != "fund_pe" else "fund_pe_ratio", value, source=source, observed_at=as_of, derived=derived_flag)
        (derived if derived_flag else facts).append(item)

    return ResearchReport(
        research_id=f"strategic-residual-{symbol}",
        candidate_id=candidate_id or f"strategic-residual-{symbol.lower()}",
        symbol=symbol,
        started_at=as_of,
        completed_at=as_of,
        observed_at=as_of,
        provisional_sleeve=Sleeve.CORE_GROWTH,
        security_class=SecurityClass.BROAD_MARKET_INDEX_ETF,
        sector="UNKNOWN",
        market_price=price,
        research_status=ResearchStatus.RESEARCH_COMPLETE,
        subject_kind=ResearchSubjectKind.NEW_CANDIDATE,
        executive_summary=(
            f"{symbol} is the configured diversified CORE market vehicle. "
            "This is a strategic residual allocation path, not a company 10-K thesis."
        ),
        confidence=ResearchConfidence.MEDIUM,
        freshness=ResearchFreshness.FRESH,
        facts=facts,
        derived_metrics=derived,
        research_source=STRATEGIC_RESIDUAL_SOURCE,
        sources_observed=[source] if source else [],
        key_risks=["market_drawdown", "valuation_compression", "opportunity_cost_if_cash_is_preferable"],
        invalidation_candidates=["risk_regime_makes_cash_preferable", "residual_snapshot_no_longer_usable"],
        expected_horizon="multi-year CORE market exposure",
    )
