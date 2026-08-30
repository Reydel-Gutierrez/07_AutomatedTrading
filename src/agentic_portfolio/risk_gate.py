from __future__ import annotations

from uuid import uuid4

from agentic_portfolio.liquidity import liquidity_ok
from agentic_portfolio.policy import load_account_rules, load_policy
from agentic_portfolio.schemas import (
    BreachKind,
    ClassificationStatus,
    Decision,
    GateReason,
    GateVerdict,
    PortfolioContext,
    PositionRegistryStatus,
    ProposedAction,
    RiskGateResult,
    RiskState,
    SecurityClass,
    Sleeve,
    ThesisStatus,
    to_dict,
)
from agentic_portfolio.sectors import CanonicalSector, map_sector

_SEVERITY = {
    GateVerdict.PASS: 0,
    GateVerdict.REQUIRES_ENHANCED_REVIEW: 1,
    GateVerdict.RISK_REDUCING_ONLY: 2,
    GateVerdict.HALTED: 3,
    GateVerdict.FAIL: 4,
}

_RISK_UP = {Decision.BUY, Decision.ADD}


def _bump(current: GateVerdict, new: GateVerdict) -> GateVerdict:
    return new if _SEVERITY[new] > _SEVERITY[current] else current


def position_ceiling_pct(security_class: SecurityClass, sleeve: Sleeve | None) -> float | None:
    if sleeve == Sleeve.SPECULATIVE:
        return 3.0
    if security_class == SecurityClass.BROAD_MARKET_INDEX_ETF:
        return 40.0
    if security_class == SecurityClass.OTHER_DIVERSIFIED_ETF:
        return 25.0
    if sleeve == Sleeve.CORE_GROWTH:
        return 20.0
    if sleeve == Sleeve.OPPORTUNISTIC:
        return 15.0
    if sleeve == Sleeve.TACTICAL:
        return 10.0
    return None


def _existing_mv(ctx: PortfolioContext, symbol: str) -> float:
    return sum(p.market_value for p in ctx.positions if p.symbol == symbol)


def _is_existing(ctx: PortfolioContext, symbol: str) -> bool:
    return any(p.symbol == symbol for p in ctx.positions)


def resulting_position_pct(ctx: PortfolioContext, action: ProposedAction) -> float:
    if action.expected_resulting_position_pct is not None:
        return action.expected_resulting_position_pct
    nav = ctx.current_nav
    if nav <= 0:
        return 0.0
    existing = _existing_mv(ctx, action.symbol)
    n = action.proposed_notional or 0.0
    if action.decision in _RISK_UP:
        return (existing + n) / nav
    if action.decision in {Decision.SELL, Decision.REDUCE}:
        return max(0.0, existing - n) / nav
    return existing / nav


def resulting_sleeve_pct(ctx: PortfolioContext, action: ProposedAction) -> float:
    if action.expected_resulting_sleeve_pct is not None:
        return action.expected_resulting_sleeve_pct
    if not action.sleeve:
        return 0.0
    nav = ctx.current_nav
    cur = ctx.sleeve_market_values.get(action.sleeve.value, 0.0)
    existing = _existing_mv(ctx, action.symbol)
    n = action.proposed_notional or 0.0
    if action.decision in _RISK_UP:
        delta = n
        if existing and action.decision == Decision.ADD:
            delta = n
        return (cur + delta) / nav if nav else 0.0
    if action.decision in {Decision.SELL, Decision.REDUCE}:
        return max(0.0, cur - n) / nav if nav else 0.0
    return cur / nav if nav else 0.0


def resulting_sector_pct(ctx: PortfolioContext, action: ProposedAction) -> float | None:
    if action.expected_resulting_sector_pct is not None:
        return action.expected_resulting_sector_pct
    if not action.sector:
        return None
    nav = ctx.current_nav
    cur = ctx.sector_exposure.get(action.sector, 0.0)
    n = action.proposed_notional or 0.0
    if nav <= 0:
        return 0.0
    if action.decision in _RISK_UP:
        return (cur + n) / nav
    if action.decision in {Decision.SELL, Decision.REDUCE}:
        return max(0.0, cur - n) / nav
    return cur / nav


_ACTIVE_THESIS = {
    ThesisStatus.ACTIVE,
    ThesisStatus.STRENGTHENED,
    ThesisStatus.UNCHANGED,
    ThesisStatus.WEAKENED,
}


def _canonical_sector_label(label: str | None) -> str | None:
    if not label:
        return None
    sector, status = map_sector(label)
    if status.value == "CONFLICTING" or sector == CanonicalSector.UNKNOWN:
        return CanonicalSector.UNKNOWN.value if label else None
    return sector.value


def evaluate(ctx: PortfolioContext, action: ProposedAction, sleeves=None, theses=None) -> RiskGateResult:
    """Is this proposed action permitted under policy? Not: is it a good stock?"""
    policy = load_policy()
    rules = load_account_rules()
    if action.sector:
        action.sector = _canonical_sector_label(action.sector) or action.sector
    reasons: list[GateReason] = []
    reviews: list[str] = []
    verdict = GateVerdict.PASS
    rec_ok = True
    exec_ok = bool(rules["execution"].get("live_trade_actions_allowed")) and bool(
        rules["execution"].get("auto_execution")
    )

    def fail(code: str, msg: str, kind: BreachKind = BreachKind.PROPOSED_ACTION_BREACH) -> None:
        nonlocal verdict
        reasons.append(GateReason(code, msg, kind))
        verdict = _bump(verdict, GateVerdict.FAIL)

    def need_review(flag: str, code: str, msg: str) -> None:
        nonlocal verdict
        reviews.append(flag)
        reasons.append(GateReason(code, msg, BreachKind.PROPOSED_ACTION_BREACH))
        verdict = _bump(verdict, GateVerdict.REQUIRES_ENHANCED_REVIEW)

    # A. execution-state (paper PASS still allowed; live exec off)
    if not rules["execution"].get("live_trade_actions_allowed"):
        exec_ok = False
        reasons.append(
            GateReason("LIVE_TRADE_ACTIONS_DISABLED", "Paper evaluation only; execution controller is off.", BreachKind.NONE)
        )

    # B. Agentic account
    if ctx.account_number != rules["account"]["account_number"]:
        fail("WRONG_ACCOUNT", "Context account is not the dedicated Agentic account.")

    # C. sufficient context
    if ctx.current_nav <= 0:
        fail("INSUFFICIENT_CONTEXT", "NAV missing or non-positive.")

    decision = action.decision
    if decision == Decision.BUY and _is_existing(ctx, action.symbol):
        decision = Decision.ADD

    risk_up = decision in _RISK_UP
    pos_pct = resulting_position_pct(ctx, action)
    pos_pct_pts = pos_pct * 100.0
    ceiling = position_ceiling_pct(action.security_class, action.sleeve)

    # D. classification confidence
    if risk_up and action.classification_status in {
        ClassificationStatus.INSUFFICIENT_EVIDENCE,
        ClassificationStatus.CONFLICTING_EVIDENCE,
    }:
        fail("CLASSIFICATION_INSUFFICIENT_EVIDENCE", "Fail closed: security class is not validated.")
    if risk_up and action.classification_status == ClassificationStatus.CONFLICTING_EVIDENCE:
        fail("CLASSIFICATION_CONFLICTING_EVIDENCE", "Fail closed: classification evidence conflicts.")
    if risk_up and action.security_class == SecurityClass.BROAD_MARKET_INDEX_ETF:
        if action.classification_status != ClassificationStatus.VALIDATED:
            fail("BROAD_MARKET_NOT_VALIDATED", "40% bucket requires validated broad-market evidence.")

    # E. sleeve
    if risk_up and action.sleeve is None:
        fail("SLEEVE_REQUIRED", "Risk-increasing actions require a persisted sleeve.")

    unregistered = action.position_registry_status == PositionRegistryStatus.UNREGISTERED_POSITION
    if sleeves is not None and _is_existing(ctx, action.symbol) and sleeves.get(action.symbol) is None:
        unregistered = True
        action.position_registry_status = PositionRegistryStatus.UNREGISTERED_POSITION
    if unregistered and risk_up:
        fail(
            "UNREGISTERED_POSITION",
            "Robinhood position has no sleeve/thesis registry entry. Do not invent a sleeve. ADD/BUY blocked until registered.",
        )

    if sleeves is not None and action.sleeve is not None:
        rec = sleeves.get(action.symbol)
        if rec is not None and rec.sleeve != action.sleeve:
            if action.sleeve_reclassification_pending or not getattr(rec, "status", None):
                fail(
                    "SLEEVE_RECLASSIFICATION_PENDING",
                    "Risk checks use the existing sleeve until SLEEVE_RECLASSIFICATION_REVIEW is approved.",
                )
            else:
                fail(
                    "SLEEVE_RECLASSIFICATION_REQUIRED",
                    f"Action sleeve {action.sleeve.value} differs from persisted {rec.sleeve.value}.",
                )

    if theses is not None and decision == Decision.ADD:
        thesis = theses.get(action.thesis_id) if action.thesis_id else theses.active_for_symbol(action.symbol)
        if thesis is None or thesis.status not in _ACTIVE_THESIS:
            fail("ADD_NO_ACTIVE_THESIS", "ADD requires an existing ACTIVE thesis in the thesis registry.")
        else:
            if not theses.has_fresh_review(thesis.thesis_id, "INVESTMENT_THESIS_REVIEW", session_id=ctx.trading_session_id):
                fail("ADD_STALE_THESIS_REVIEW", "ADD requires a fresh INVESTMENT_THESIS_REVIEW in the thesis registry.")
            if not theses.has_fresh_review(thesis.thesis_id, "RISK_REVIEW", session_id=ctx.trading_session_id):
                fail("ADD_STALE_RISK_REVIEW", "ADD requires a fresh RISK_REVIEW in the thesis registry.")

    # F/G. concentration matrix
    current_pos_pct = _existing_mv(ctx, action.symbol) / ctx.current_nav if ctx.current_nav else 0.0
    if ceiling is None and risk_up:
        fail("NO_POSITION_CEILING", "Cannot resolve class+sleeve ceiling.")
    elif ceiling is not None:
        if pos_pct_pts > ceiling + 1e-9 and risk_up:
            fail(
                "POSITION_CEILING",
                f"Resulting {pos_pct_pts:.4f}% exceeds hard ceiling {ceiling}% for {action.security_class.value}+{action.sleeve}.",
            )
        elif current_pos_pct * 100.0 > ceiling + 1e-9 and not risk_up:
            reasons.append(
                GateReason(
                    "PASSIVE_CONCENTRATION_DRIFT",
                    f"Existing position {current_pos_pct*100:.4f}% is above ceiling {ceiling}% via market drift. No add. No forced sale.",
                    BreachKind.PASSIVE_MARKET_DRIFT_BREACH,
                )
            )
            if decision in {Decision.BUY, Decision.ADD}:
                fail("DRIFT_NO_ADD", "Cannot increase a drifted over-ceiling position.", BreachKind.PASSIVE_MARKET_DRIFT_BREACH)

    # Speculative sleeve total
    if action.sleeve == Sleeve.SPECULATIVE:
        sleeve_pct = resulting_sleeve_pct(ctx, action) * 100.0
        if risk_up and sleeve_pct > 5.0 + 1e-9:
            fail("SPECULATIVE_SLEEVE_CEILING", f"Speculative sleeve would be {sleeve_pct:.4f}% > 5%.")

    # H. >10% enhanced concentration review
    if risk_up and pos_pct > 0.10 + 1e-12:
        reviews.append("ENHANCED_CONCENTRATION_REVIEW")
        if not action.enhanced_concentration_review_complete:
            need_review(
                "ENHANCED_CONCENTRATION_REVIEW",
                "ENHANCED_CONCENTRATION_REVIEW_REQUIRED",
                "Resulting individual security > 10% NAV requires enhanced concentration review.",
            )

    # I. >15% individual equity HIGH
    if (
        risk_up
        and action.security_class == SecurityClass.INDIVIDUAL_EQUITY
        and pos_pct > 0.15 + 1e-12
    ):
        reviews.append("HIGH_CONCENTRATION_REVIEW")
        if not action.high_concentration_review_complete:
            need_review(
                "HIGH_CONCENTRATION_REVIEW",
                "HIGH_CONCENTRATION_REVIEW_REQUIRED",
                "Individual equity > 15% NAV requires HIGH concentration justification.",
            )

    # J/K. sector
    sec_pct = resulting_sector_pct(ctx, action)
    current_sec = None
    if action.sector:
        current_sec = ctx.sector_allocation_pct.get(action.sector, 0.0)
    hard_sec = float(policy["sector_concentration"]["hard_ceiling_percent_of_nav"])
    soft_sec = float(policy["sector_concentration"]["review_threshold_percent_of_nav"])

    if risk_up and action.security_class == SecurityClass.INDIVIDUAL_EQUITY and (
        not action.sector or action.sector == CanonicalSector.UNKNOWN.value
    ):
        fail("SECTOR_UNKNOWN", "Fail closed: individual-equity new risk requires a sector.")

    if sec_pct is not None:
        sec_pts = sec_pct * 100.0
        if risk_up and sec_pts > hard_sec + 1e-9:
            fail("SECTOR_HARD_CEILING", f"Resulting sector exposure {sec_pts:.4f}% exceeds {hard_sec}%.")
        elif current_sec is not None and current_sec * 100.0 > hard_sec + 1e-9 and not risk_up:
            reasons.append(
                GateReason(
                    "PASSIVE_SECTOR_DRIFT",
                    "Sector exposure is above 45% via market drift. No further increase. No forced liquidation.",
                    BreachKind.PASSIVE_MARKET_DRIFT_BREACH,
                )
            )
        elif risk_up and sec_pts > soft_sec + 1e-9:
            reviews.append("SECTOR_CONCENTRATION_REVIEW")
            if not action.sector_concentration_review_complete:
                need_review(
                    "SECTOR_CONCENTRATION_REVIEW",
                    "SECTOR_CONCENTRATION_REVIEW_REQUIRED",
                    "Resulting sector exposure > 30% NAV requires sector concentration review.",
                )

    # M. liquidity
    if risk_up:
        ok, codes, liq_reviews = liquidity_ok(
            sleeve=action.sleeve,
            decision=decision,
            proposed_notional=action.proposed_notional,
            liq=action.liquidity,
            speculative_review_complete=action.speculative_liquidity_review_complete,
        )
        reviews.extend(liq_reviews)
        if not ok:
            for c in codes:
                if c.endswith("_REQUIRED"):
                    need_review("SPECULATIVE_LIQUIDITY_REVIEW", c, "Speculative liquidity review required.")
                else:
                    fail(c, "Liquidity policy failed (fail closed if data missing).")

    # N. daily halt
    if ctx.daily_risk_halt and risk_up:
        fail("DAILY_RISK_HALT", "Start-of-day NAV loss at/over 2%; no risk-increasing BUY/ADD.")

    # O/P/Q/R. HWM states
    state = ctx.risk_state
    if state == RiskState.RISK_REDUCTION and risk_up:
        if action.sleeve in {Sleeve.SPECULATIVE, Sleeve.TACTICAL}:
            fail("RISK_REDUCTION_BLOCK", "New Speculative/Tactical exposure is prohibited in RISK_REDUCTION.")
        if action.sleeve == Sleeve.OPPORTUNISTIC and not action.opportunistic_enhanced_risk_review_complete:
            need_review(
                "OPPORTUNISTIC_ENHANCED_RISK_REVIEW",
                "OPPORTUNISTIC_REVIEW_REQUIRED",
                "Opportunistic adds in RISK_REDUCTION require enhanced risk review.",
            )
        verdict = _bump(verdict, GateVerdict.RISK_REDUCING_ONLY) if action.sleeve == Sleeve.OPPORTUNISTIC else verdict

    if state == RiskState.DEFENSIVE and risk_up:
        if action.sleeve in {Sleeve.SPECULATIVE, Sleeve.TACTICAL}:
            fail("DEFENSIVE_BLOCK", "New Speculative/Tactical exposure is prohibited in DEFENSIVE.")
        if action.sleeve == Sleeve.OPPORTUNISTIC and not action.explicitly_risk_reducing:
            fail("DEFENSIVE_OPP_NOT_RISK_REDUCING", "Opportunistic adds in DEFENSIVE only if explicitly risk-reducing.")
        if action.sleeve == Sleeve.CORE_GROWTH and action.security_class != SecurityClass.BROAD_MARKET_INDEX_ETF:
            fail("DEFENSIVE_CORE_ETF_ONLY", "New Core exposure in DEFENSIVE is restricted to BROAD_MARKET_INDEX_ETF.")
        if action.sleeve == Sleeve.CORE_GROWTH and action.classification_status != ClassificationStatus.VALIDATED:
            fail("DEFENSIVE_CORE_ETF_NOT_VALIDATED", "Defensive Core ETF must be validated broad-market.")

    if state == RiskState.HALTED:
        exec_ok = False
        if risk_up:
            fail("HALTED_NO_NEW_RISK", "HALTED: no new autonomous risk.")
        elif decision in {Decision.SELL, Decision.REDUCE, Decision.HOLD, Decision.NO_ACTION, Decision.WATCH}:
            verdict = _bump(verdict, GateVerdict.HALTED)
            rec_ok = True
            if not action.human_authorized_halted_execution:
                exec_ok = False
                reasons.append(
                    GateReason(
                        "HALTED_RECOMMEND_ONLY",
                        "SELL/REDUCE may be recommended; live execution requires explicit human authorization.",
                        BreachKind.NONE,
                    )
                )
        else:
            rec_ok = True
            verdict = _bump(verdict, GateVerdict.HALTED)

    # S/T. buying power / no leverage
    if risk_up and action.proposed_notional is not None:
        if action.proposed_notional > ctx.buying_power + 1e-9:
            fail("BUYING_POWER", "Proposed notional exceeds buying power.")
        if action.proposed_notional > ctx.cash + 1e-9:
            fail("NO_LEVERAGE", "Proposed notional exceeds cash; borrowing/margin is prohibited.")

    # U/V. ADD reviews
    if decision == Decision.ADD:
        if not action.investment_thesis_review_complete or not action.risk_review_complete:
            fail("ADD_REVIEW_REQUIRED", "ADD requires completed INVESTMENT_THESIS_REVIEW and RISK_REVIEW.")
        if action.add_justified_only_by_lower_price:
            fail("ADD_ONLY_TO_LOWER_COST", "Cannot add merely to reduce average cost.")

    # W. overlap/correlation — informational only. No invented numeric cap.
    if action.correlation_with_book is not None:
        reasons.append(
            GateReason(
                "OVERLAP_OBSERVED",
                f"Correlation with book provided: {action.correlation_with_book}. No hard cap; construction layer must justify.",
                BreachKind.NONE,
            )
        )
    if action.correlation is not None:
        for code, msg in action.correlation.as_warning_codes():
            reasons.append(GateReason(code, msg, BreachKind.NONE))

    # X. immutability: no override channel exists on ProposedAction.

    if decision in {Decision.HOLD, Decision.WATCH, Decision.REJECT, Decision.NO_ACTION} and verdict == GateVerdict.PASS:
        rec_ok = True

    snapshot_id = str(uuid4())
    record = {
        "snapshot_id": snapshot_id,
        "timestamp": ctx.timestamp,
        "proposed_action": to_dict(action),
        "nav": ctx.current_nav,
        "current_position_pct": current_pos_pct,
        "proposed_position_pct": pos_pct,
        "sleeve": action.sleeve.value if action.sleeve else None,
        "security_class": action.security_class.value,
        "applicable_ceiling_pct": ceiling,
        "sector": action.sector,
        "sector_pct": sec_pct,
        "drawdown_state": ctx.risk_state.value,
        "daily_risk_halt": ctx.daily_risk_halt,
        "verdict": verdict.value,
        "reasons": to_dict(reasons),
        "required_reviews": reviews,
        "execution_permitted": exec_ok,
        "recommendation_permitted": rec_ok,
    }
    return RiskGateResult(
        verdict=verdict,
        execution_permitted=exec_ok,
        recommendation_permitted=rec_ok,
        reasons=reasons,
        required_reviews=reviews,
        applicable_position_ceiling_pct=ceiling,
        snapshot_id=snapshot_id,
        journal_record=record,
    )
