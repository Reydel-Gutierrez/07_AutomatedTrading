"""Validate AI thesis/decision output. Process/schema only — no stock-picking rules."""

from __future__ import annotations

from typing import Any

from agentic_portfolio.decision.compact import expand_compact_committee_payload, is_compact_committee_payload
from agentic_portfolio.decision.residuals import configured_residual_symbol, residual_strategic_buy_eligible
from agentic_portfolio.decision.types import CASH_SYMBOL, RISK_UP, SPY_SYMBOL
from agentic_portfolio.research.sufficiency import is_etf_class
from agentic_portfolio.research.types import ResearchConclusion, ResearchReport, ResearchStatus
from agentic_portfolio.risk_gate import position_ceiling_pct
from agentic_portfolio.schemas import Decision, ExitPolicy, SecurityClass, Sleeve

VALID_DECISIONS = {d.value for d in Decision}
VALID_DECISIONS.add("EXIT")
VALID_SLEEVES = {s.value for s in Sleeve}
VALID_CONFIDENCE = {"LOW", "MEDIUM", "HIGH"}
RISK_UP_VALUES = {d.value for d in RISK_UP}
ALTERNATIVE_SYMBOLS = {CASH_SYMBOL, SPY_SYMBOL}
NO_NAMED_DECISION = "no_named_decision"
CASH_SPY_ONLY_PAYLOAD = "cash_spy_only_payload"
REQUIRED_NAMED_DECISION_CONCLUSION = ResearchConclusion.ADVANCE_TO_THESIS
PROTECTED_KEYS = {
    "current_nav",
    "cash",
    "buying_power",
    "positions",
    "holdings_count",
    "high_water_mark",
    "risk_limits",
    "security_class",
    "classification_status",
}


class DecisionValidationError(ValueError):
    """Malformed AI output. Engine must not persist this as a valid decision."""


def is_no_named_decision_reason(reason: str | None) -> bool:
    """True when a stored/queue/watch reason is the missing researched-symbol decision."""
    text = str(reason or "").strip().lower().replace("-", "_")
    if not text:
        return False
    return NO_NAMED_DECISION in text or CASH_SPY_ONLY_PAYLOAD in text


def advance_to_thesis_symbols(reports: list[ResearchReport]) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for report in reports:
        if report.research_conclusion != REQUIRED_NAMED_DECISION_CONCLUSION:
            continue
        sym = str(report.symbol or "").upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        symbols.append(sym)
    return symbols


def named_decision_invariant_errors(seen: set[str], reports: list[ResearchReport]) -> list[str]:
    """Every ADVANCE_TO_THESIS report must have exactly one decisions[] row.

    CASH and SPY may coexist. A CASH/SPY-only payload that omits the researched
    ticker is malformed. Duplicates are rejected before this runs.
    """
    required = advance_to_thesis_symbols(reports)
    errors: list[str] = []
    for sym in required:
        if sym not in seen:
            errors.append(f"{NO_NAMED_DECISION}:{sym}")
    if required and not any(sym in seen for sym in required):
        decided = {str(s).upper() for s in seen}
        if decided and decided <= ALTERNATIVE_SYMBOLS:
            errors.append(CASH_SPY_ONLY_PAYLOAD)
    return errors


def validate_payload(
    payload: Any,
    reports: list[ResearchReport],
    *,
    current_nav: float,
    committee: bool = False,
    residual_snapshot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    if not isinstance(payload, dict):
        raise DecisionValidationError("AI output is not an object")
    if is_compact_committee_payload(payload):
        try:
            payload = expand_compact_committee_payload(payload)
        except (TypeError, ValueError) as exc:
            raise DecisionValidationError(f"malformed compact committee payload: {exc}") from exc
    missing = [k for k in ("decisions", "comparison") if k not in payload]
    if missing:
        raise DecisionValidationError(f"malformed AI response missing keys: {missing}")

    errors: list[str] = []
    unsupported: list[str] = []
    payload = dict(payload)
    by_symbol = {r.symbol.upper(): r for r in reports}

    for key in PROTECTED_KEYS:
        if key in payload and payload[key] is not None:
            unsupported.append(f"attempted_override:{key}")

    comparison = payload.get("comparison") or {}
    if not isinstance(comparison, dict):
        raise DecisionValidationError("comparison must be an object")
    if not comparison.get("vs_cash"):
        errors.append("missing_vs_cash")
    if not comparison.get("vs_spy"):
        errors.append("missing_vs_spy")
    ranking = comparison.get("ranking") or []
    if ranking and not isinstance(ranking, list):
        raise DecisionValidationError("comparison.ranking must be a list")
    ranking_u = [str(s).upper() for s in ranking]
    residual_symbol = str((residual_snapshot or {}).get("symbol") or configured_residual_symbol()).upper()
    if CASH_SYMBOL not in ranking_u:
        errors.append("cash_not_in_ranking")
    if residual_symbol not in ranking_u and SPY_SYMBOL not in ranking_u:
        errors.append("spy_not_in_ranking")

    theses_raw = payload.get("theses") or []
    if theses_raw and not isinstance(theses_raw, list):
        raise DecisionValidationError("theses must be a list")
    thesis_by_symbol: dict[str, dict[str, Any]] = {}
    for item in theses_raw:
        if not isinstance(item, dict) or not item.get("symbol"):
            errors.append("malformed_thesis")
            continue
        sym = str(item["symbol"]).upper()
        if item.get("status") not in (None, "DRAFT"):
            unsupported.append(f"attempted_active_thesis:{sym}")
            item = dict(item)
            item["status"] = "DRAFT"
        sleeve = str(item.get("sleeve") or "")
        if sleeve and sleeve not in VALID_SLEEVES:
            raise DecisionValidationError(f"invalid sleeve: {sleeve}")
        conf = str(item.get("confidence") or "LOW").upper()
        if conf not in VALID_CONFIDENCE:
            raise DecisionValidationError(f"invalid confidence: {item.get('confidence')}")
        item = dict(item)
        item["symbol"] = sym
        item["confidence"] = conf
        item["status"] = "DRAFT"
        thesis_by_symbol[sym] = item

    decisions_raw = payload.get("decisions")
    if not isinstance(decisions_raw, list) or not decisions_raw:
        raise DecisionValidationError("decisions must be a non-empty list")

    seen: set[str] = set()
    risk_up_pct = 0.0
    normalized_decisions: list[dict[str, Any]] = []
    for item in decisions_raw:
        if not isinstance(item, dict) or not item.get("symbol"):
            raise DecisionValidationError("each decision needs a symbol")
        sym = str(item["symbol"]).upper()
        if sym in seen:
            raise DecisionValidationError(f"duplicate decision for {sym}")
        seen.add(sym)
        raw_dec = str(item.get("decision") or "")
        if raw_dec == "EXIT":
            raw_dec = "SELL"
        if raw_dec not in VALID_DECISIONS:
            raise DecisionValidationError(f"invalid decision: {item.get('decision')}")
        decision = Decision(raw_dec)
        alloc = item.get("desired_allocation_pct")
        if alloc is not None:
            try:
                alloc = float(alloc)
            except (TypeError, ValueError) as exc:
                raise DecisionValidationError(f"desired_allocation_pct not numeric for {sym}") from exc
            if alloc < 0 or alloc > 100:
                errors.append(f"allocation_out_of_range:{sym}")
        item = dict(item)
        item["symbol"] = sym
        item["decision"] = decision.value
        item["desired_allocation_pct"] = alloc

        if sym == CASH_SYMBOL:
            if decision not in {Decision.HOLD, Decision.NO_ACTION, Decision.WATCH}:
                errors.append("cash_must_be_hold_or_no_action")
            recon = item.get("reconsideration")
            if recon is not None and not isinstance(recon, dict):
                errors.append(f"malformed_reconsideration:{sym}")
            elif isinstance(recon, dict):
                item["reconsideration"] = _normalize_reconsideration(recon)
            normalized_decisions.append(item)
            continue

        report = by_symbol.get(sym)
        strategic_residual = _is_strategic_residual_buy(
            sym,
            report,
            committee=committee,
            residual_snapshot=residual_snapshot,
            residual_symbol=residual_symbol,
        )
        if report is None and not strategic_residual and sym not in {SPY_SYMBOL, residual_symbol}:
            errors.append(f"no_research_report:{sym}")
        thesis = thesis_by_symbol.get(sym)

        if decision in RISK_UP:
            if alloc is None or alloc <= 0:
                errors.append(f"buy_add_requires_allocation:{sym}")
            if not item.get("why_preferable_to_cash") and not comparison.get("vs_cash"):
                errors.append(f"missing_vs_cash:{sym}")
            circular = _spy_comparison_is_circular(sym, report, residual_symbol=residual_symbol)
            if not circular and not item.get("why_preferable_to_spy") and not comparison.get("vs_spy"):
                errors.append(f"missing_vs_spy:{sym}")
            if strategic_residual:
                if not residual_strategic_buy_eligible(residual_snapshot):
                    errors.append(f"buy_add_requires_usable_residual_snapshot:{sym}")
            elif report is None:
                errors.append(f"buy_add_requires_research:{sym}")
            else:
                if report.research_status != ResearchStatus.RESEARCH_COMPLETE:
                    errors.append(f"buy_add_requires_complete_research:{sym}")
                if report.research_conclusion != ResearchConclusion.ADVANCE_TO_THESIS:
                    errors.append(f"buy_add_requires_advance_to_thesis:{sym}")
            if thesis is None:
                errors.append(f"buy_add_requires_thesis:{sym}")
            else:
                errors.extend(_thesis_field_errors(sym, thesis, report=report, strategic_residual=strategic_residual))
                errors.extend(_exit_policy_errors(sym, thesis, decision))
            if alloc:
                risk_up_pct += alloc
        elif decision in {Decision.SELL, Decision.REDUCE}:
            pass
        elif thesis is not None:
            # WATCH/REJECT/NO_ACTION/HOLD may carry a lighter draft thesis.
            if not thesis.get("thesis_summary"):
                errors.append(f"thesis_summary_required:{sym}")
            if thesis.get("exit_policy"):
                errors.extend(_exit_policy_errors(sym, thesis, decision))

        recon = item.get("reconsideration")
        if recon is not None and not isinstance(recon, dict):
            errors.append(f"malformed_reconsideration:{sym}")
        elif isinstance(recon, dict):
            item["reconsideration"] = _normalize_reconsideration(recon)

        normalized_decisions.append(item)

    errors.extend(named_decision_invariant_errors(seen, reports))
    errors.extend(
        _allocation_consistency_errors(
            normalized_decisions,
            by_symbol=by_symbol,
            residual_symbol=residual_symbol,
            committee=committee,
        )
    )
    if committee:
        errors.extend(_cash_justification_errors(normalized_decisions, comparison, residual_symbol=residual_symbol))
    if risk_up_pct > 100 + 1e-9:
        errors.append("risk_increasing_allocation_exceeds_100")
    if current_nav is not None and current_nav <= 0:
        errors.append("non_positive_nav")

    if errors:
        raise DecisionValidationError(f"malformed decision payload: {errors}")

    payload["theses"] = list(thesis_by_symbol.values())
    payload["decisions"] = normalized_decisions
    payload["comparison"] = comparison
    return payload, unsupported, errors


def parse_exit_policy(raw: Any) -> ExitPolicy:
    if raw is None:
        return ExitPolicy()
    if isinstance(raw, ExitPolicy):
        policy = raw
    elif isinstance(raw, dict):
        policy = ExitPolicy(
            thesis_based=bool(raw.get("thesis_based", True)),
            mandatory_fixed_stop_loss=bool(raw.get("mandatory_fixed_stop_loss", False)),
            price_invalidation=raw.get("price_invalidation") or None,
            event_invalidation=raw.get("event_invalidation") or None,
            technical_invalidation=raw.get("technical_invalidation") or None,
            risk_invalidation=raw.get("risk_invalidation") or None,
            broker_stop_orders_created=bool(raw.get("broker_stop_orders_created", False)),
            notes=raw.get("notes"),
        )
    else:
        raise DecisionValidationError("exit_policy malformed")
    if policy.broker_stop_orders_created:
        raise DecisionValidationError("broker stop orders are not allowed")
    return policy


def _spy_comparison_is_circular(
    symbol: str,
    report: ResearchReport | None,
    *,
    residual_symbol: str = SPY_SYMBOL,
) -> bool:
    """The configured broad-market residual must not be required to beat itself."""
    residual = str(residual_symbol or SPY_SYMBOL).upper()
    if symbol == residual or symbol == SPY_SYMBOL:
        return True
    if report is None:
        return False
    return report.security_class is SecurityClass.BROAD_MARKET_INDEX_ETF and str(report.symbol or "").upper() == residual


def _is_strategic_residual_buy(
    symbol: str,
    report: ResearchReport | None,
    *,
    committee: bool,
    residual_snapshot: dict[str, Any] | None,
    residual_symbol: str,
) -> bool:
    """BUY/ADD of the configured residual may skip company ADVANCE_TO_THESIS in committee mode only."""
    if not committee:
        return False
    if str(symbol or "").upper() != str(residual_symbol or "").upper():
        return False
    if not residual_strategic_buy_eligible(residual_snapshot):
        return False
    if report is not None and report.research_conclusion == REQUIRED_NAMED_DECISION_CONCLUSION:
        if report.research_status == ResearchStatus.RESEARCH_COMPLETE:
            return False
    return True


def _allocation_consistency_errors(
    decisions: list[dict[str, Any]],
    *,
    by_symbol: dict[str, ResearchReport],
    residual_symbol: str,
    committee: bool,
) -> list[str]:
    errors: list[str] = []
    weights: dict[str, float] = {}
    for item in decisions:
        alloc = item.get("desired_allocation_pct")
        if alloc is None:
            continue
        try:
            alloc_f = float(alloc)
        except (TypeError, ValueError):
            continue
        sym = str(item["symbol"]).upper()
        decision = Decision(item["decision"])
        if decision in RISK_UP or sym == CASH_SYMBOL or (decision == Decision.HOLD and alloc_f > 0):
            weights[sym] = alloc_f
        if decision in RISK_UP:
            report = by_symbol.get(sym)
            security_class = report.security_class if report is not None else None
            sleeve = report.provisional_sleeve if report is not None else Sleeve.CORE_GROWTH
            if security_class is None and sym == str(residual_symbol).upper():
                security_class = SecurityClass.BROAD_MARKET_INDEX_ETF
                sleeve = Sleeve.CORE_GROWTH
            if security_class is not None:
                ceiling = position_ceiling_pct(security_class, sleeve)
                if ceiling is not None and alloc_f > ceiling + 1e-9:
                    errors.append(f"allocation_exceeds_ceiling:{sym}:{alloc_f:g}>{ceiling:g}")
    total = sum(weights.values())
    if total > 100 + 1e-6:
        errors.append("allocation_weights_exceed_100")
    if committee and weights:
        cash_w = weights.get(CASH_SYMBOL)
        equity_w = sum(v for k, v in weights.items() if k != CASH_SYMBOL)
        if cash_w is not None and cash_w + equity_w > 100 + 1e-6:
            errors.append("allocation_weights_exceed_100")
    return errors


def _cash_justification_errors(
    decisions: list[dict[str, Any]],
    comparison: dict[str, Any],
    *,
    residual_symbol: str,
) -> list[str]:
    """100% cash / no equity must be an affirmative comparative choice, not a missing-ticker leftover."""
    errors: list[str] = []
    risk_up = [item for item in decisions if item.get("decision") in RISK_UP_VALUES]
    if risk_up:
        return errors
    cash = next((item for item in decisions if str(item.get("symbol") or "").upper() == CASH_SYMBOL), None)
    cash_pct = float(cash.get("desired_allocation_pct") or 100.0) if cash else 100.0
    if cash_pct < 99.0 - 1e-9:
        return errors
    vs_spy = str(comparison.get("vs_spy") or "").strip()
    why_market = str((cash or {}).get("why_preferable_to_spy") or vs_spy).strip()
    rationale = str((cash or {}).get("rationale") or comparison.get("vs_cash") or "").strip()
    recon = (cash or {}).get("reconsideration") if isinstance((cash or {}).get("reconsideration"), dict) else {}
    trigger = str(recon.get("next_review_reason") or "").strip()
    if not why_market:
        errors.append(f"cash_requires_why_{residual_symbol.lower()}_inferior")
    if not rationale:
        errors.append("cash_requires_comparative_rationale")
    if cash is not None and not trigger:
        recon = dict(recon or {})
        recon["next_review_reason"] = "scheduled_core_review"
        cash["reconsideration"] = _normalize_reconsideration(recon)
    return errors


def _is_core_or_etf_thesis(thesis: dict[str, Any], report: ResearchReport | None) -> bool:
    sleeve_raw = thesis.get("sleeve")
    if str(sleeve_raw or "") == Sleeve.CORE_GROWTH.value:
        return True
    if report is None:
        return False
    if report.provisional_sleeve is Sleeve.CORE_GROWTH:
        return True
    return is_etf_class(report.security_class)


def _has_nonempty_list(thesis: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        val = thesis.get(key)
        if isinstance(val, list) and any(str(item).strip() for item in val):
            return True
    return False


def _normalize_reconsideration(raw: dict[str, Any]) -> dict[str, Any]:
    lost_to = raw.get("lost_to") or raw.get("alternative_that_beat_it") or []
    if isinstance(lost_to, str):
        lost_to = [lost_to]
    if not isinstance(lost_to, list):
        lost_to = []
    return {
        "why_lost": raw.get("why_lost") or raw.get("why_it_lost_today"),
        "lost_to": [str(item).upper() for item in lost_to if str(item).strip()],
        "valuation_condition": raw.get("valuation_condition"),
        "thesis_condition": raw.get("thesis_condition"),
        "required_evidence_improvement": raw.get("required_evidence_improvement"),
        "next_review_reason": raw.get("next_review_reason"),
        "next_review_at": raw.get("next_review_at"),
        "not_an_auto_execution_condition": True,
    }


def _thesis_field_errors(
    symbol: str,
    thesis: dict[str, Any],
    *,
    report: ResearchReport | None = None,
    strategic_residual: bool = False,
) -> list[str]:
    errors = []
    for key in (
        "thesis_summary",
        "bull_case",
        "base_case",
        "bear_case",
        "horizon",
        "why_position_should_exist",
    ):
        if not thesis.get(key):
            errors.append(f"missing_{key}:{symbol}")
    for key in ("risks", "invalidation_conditions", "review_triggers"):
        val = thesis.get(key)
        if not isinstance(val, list) or not val:
            errors.append(f"missing_{key}:{symbol}")
    catalysts = thesis.get("catalysts")
    has_catalysts = isinstance(catalysts, list) and any(str(item).strip() for item in catalysts)
    if strategic_residual or _is_core_or_etf_thesis(thesis, report):
        # CORE ownership / ETF vehicle theses are not near-term event catalysts.
        if not has_catalysts and not _has_nonempty_list(thesis, "thesis_drivers"):
            if strategic_residual or is_etf_class(getattr(report, "security_class", None)):
                pass
            elif not thesis.get("why_position_should_exist"):
                errors.append(f"missing_catalysts:{symbol}")
    elif not has_catalysts:
        errors.append(f"missing_catalysts:{symbol}")
    if not thesis.get("exit_policy"):
        errors.append(f"missing_exit_policy:{symbol}")
    return errors


def _exit_policy_errors(symbol: str, thesis: dict[str, Any], decision: Decision) -> list[str]:
    try:
        policy = parse_exit_policy(thesis.get("exit_policy"))
    except DecisionValidationError as exc:
        return [f"{exc}:{symbol}"]
    sleeve_raw = thesis.get("sleeve")
    try:
        sleeve = Sleeve(sleeve_raw) if sleeve_raw else None
    except ValueError:
        return [f"invalid_sleeve:{symbol}"]
    errors: list[str] = []
    if policy.broker_stop_orders_created:
        errors.append(f"broker_stop_orders_not_allowed:{symbol}")
    if sleeve == Sleeve.CORE_GROWTH and policy.mandatory_fixed_stop_loss:
        errors.append(f"core_no_mandatory_fixed_stop_loss:{symbol}")
    if decision in RISK_UP:
        if sleeve in {Sleeve.CORE_GROWTH, Sleeve.OPPORTUNISTIC} and not policy.thesis_based:
            errors.append(f"thesis_based_exit_required:{symbol}")
        if sleeve == Sleeve.TACTICAL and not (policy.price_invalidation or policy.technical_invalidation):
            errors.append(f"tactical_requires_price_or_technical_invalidation:{symbol}")
        if sleeve == Sleeve.SPECULATIVE and not policy.risk_invalidation:
            errors.append(f"speculative_requires_risk_invalidation:{symbol}")
    return errors
