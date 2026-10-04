"""ProposalIntake — the single door from "Risk Gate permits this" to "order queue".

Every order proposal (new research decision, position-monitor reassessment,
watch-condition trigger, CORE committee) enters here. Intake:

1. journals and stops in OBSERVE mode;
2. de-duplicates against open proposals for the same ticker/action;
3. persists the full ProposedAction snapshot (so the executor can re-run the
   Risk Gate at send time) and the proposal-time Risk Gate result;
4. asks the deterministic OrderAuthority whether it may be auto-authorized;
5. AUTO_APPROVE → status APPROVED (decided_by=autonomous_policy) and hands it to
   the executor; ESCALATE → stays PENDING for a human, with explicit reasons.

AI never reaches this module directly; it only produces the decision upstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from agentic_portfolio.autonomy.authority import AUTO_APPROVE, OBSERVE_ONLY, AuthorizationDecision, OrderAuthority
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.live_approval.types import LiveApproval, LiveApprovalStatus
from agentic_portfolio.schemas import GateVerdict, to_dict

PERMITTED_VERDICTS = {GateVerdict.PASS.value, GateVerdict.REQUIRES_ENHANCED_REVIEW.value}
RISK_DOWN = {"SELL", "REDUCE"}


@dataclass
class IntakeResult:
    status: str
    approval: LiveApproval | None = None
    created: bool = False
    authorization: AuthorizationDecision | None = None
    outcome: Any = None
    reasons: list[str] = field(default_factory=list)

    @property
    def auto_approved(self) -> bool:
        return self.status == "AUTHORIZED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "approval_id": self.approval.approval_id if self.approval else None,
            "created": self.created,
            "authorization": self.authorization.as_dict() if self.authorization else None,
            "placed": bool(getattr(self.outcome, "placed", False)),
            "reasons": list(self.reasons),
        }


def _verdict_of(risk: Any) -> tuple[str, list[str], dict[str, Any]]:
    if risk is None:
        return "", [], {}
    if isinstance(risk, Mapping):
        verdict = str(risk.get("verdict") or "").upper()
        reasons = [str(r) for r in (risk.get("reasons") or [])]
        return verdict, reasons, dict(risk)
    verdict_obj = getattr(risk, "verdict", None)
    verdict = str(getattr(verdict_obj, "value", verdict_obj) or "").upper()
    codes = [str(getattr(r, "code", r)) for r in (getattr(risk, "reasons", None) or [])]
    payload = {
        "verdict": verdict,
        "reasons": codes,
        "required_reviews": list(getattr(risk, "required_reviews", None) or []),
        "execution_permitted": bool(getattr(risk, "execution_permitted", False)),
        "snapshot_id": getattr(risk, "snapshot_id", None),
    }
    return verdict, codes, payload


class ProposalIntake:
    def __init__(
        self,
        approvals: Any,
        authority: OrderAuthority,
        *,
        root: Path,
        notify: Any = None,
        journal: Path | None = None,
    ) -> None:
        self.approvals = approvals
        self.authority = authority
        self.root = Path(root)
        self.notify = notify
        self.journal = journal or (self.root / "logs" / "order_intake.jsonl")

    def submit(
        self,
        *,
        ticker: str,
        action: str,
        risk: Any,
        proposed_action: Any = None,
        dollars: float | None,
        pct: float | None,
        context: Any = None,
        reason: str | None = None,
        ai_rationale: str | None = None,
        supporting_thesis: str | None = None,
        quote: float | None = None,
        spread_bps: float | None = None,
        watch_id: str | None = None,
        preferred_approval_id: str | None = None,
        portfolio_impact: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        origin: str,
    ) -> IntakeResult:
        ticker_u = str(ticker).upper()
        action_u = str(action).upper()
        verdict, risk_codes, risk_payload = _verdict_of(risk)
        policy = self.authority.policy
        base_row = {"ticker": ticker_u, "action": action_u, "origin": origin, "verdict": verdict, "mode": policy.mode.value, "dollars": dollars, "pct": pct}

        halted_recommendation = verdict == GateVerdict.HALTED.value and action_u in RISK_DOWN
        if verdict not in PERMITTED_VERDICTS and not halted_recommendation:
            self._journal({"type": "PROPOSAL_BLOCKED_BY_RISK", **base_row, "risk_reasons": risk_codes})
            return IntakeResult(status="BLOCKED", reasons=[f"risk_verdict:{verdict or 'MISSING'}"])

        if not policy.creates_orders:
            self._journal({"type": "PROPOSAL_OBSERVED", **base_row})
            return IntakeResult(status="OBSERVED", reasons=["observe_mode"])

        duplicate = self._open_approved(ticker_u, action_u)
        if duplicate is not None:
            return IntakeResult(status="REUSED", approval=duplicate, reasons=["approved_order_already_open"])

        snapshot = to_dict(proposed_action) if proposed_action is not None else {}
        meta = dict(metadata or {})
        meta.setdefault("origin", origin)
        if snapshot:
            meta["proposed_action_snapshot"] = snapshot
        nav = getattr(context, "current_nav", None) if context is not None else (portfolio_impact or {}).get("nav")
        cash = getattr(context, "cash", None) if context is not None else (portfolio_impact or {}).get("cash")
        impact = dict(portfolio_impact or {})
        impact.setdefault("nav", nav)
        impact.setdefault("cash", cash)
        if context is not None:
            impact.setdefault("buying_power", getattr(context, "buying_power", None))
        impact.setdefault("proposed_notional", dollars)
        impact.setdefault("desired_allocation_pct", pct)
        item, created = self.approvals.get_or_create(
            ticker=ticker_u,
            proposed_action=action_u,
            proposed_dollar_amount=dollars,
            proposed_allocation_pct=pct,
            reason=reason,
            ai_rationale=ai_rationale,
            supporting_thesis=supporting_thesis,
            current_quote=quote,
            current_spread_bps=spread_bps,
            risk_gate_result=risk_payload,
            portfolio_impact=impact,
            watch_id=watch_id,
            preferred_approval_id=preferred_approval_id,
            expected_order_type="market",
            metadata=meta,
        )
        if not created and item.authorization:
            return IntakeResult(status="REUSED", approval=item, reasons=["pending_proposal_exists"])

        human_only: list[str] = []
        if halted_recommendation:
            human_only.append("risk_state_halted_requires_human")
        if action_u in {"BUY", "ADD"} and not snapshot:
            human_only.append("missing_proposed_action_snapshot")
        queued_orders, queued_buy = self._queued_authorized(exclude=item.approval_id)
        decision = self.authority.review_proposal(
            action=action_u,
            risk_verdict=verdict,
            notional=dollars,
            nav=nav,
            cash=cash,
            risk_reasons=risk_codes,
            human_only_reasons=human_only,
            queued_orders=queued_orders,
            queued_buy_notional=queued_buy,
        )
        item.authorization = decision.as_dict()
        if decision.outcome == OBSERVE_ONLY:  # mode flipped between checks
            item.escalation_reasons = list(decision.reasons)
            self.approvals.store.save(item)
            return IntakeResult(status="OBSERVED", approval=item, created=created, authorization=decision)
        if decision.outcome == AUTO_APPROVE:
            item.escalation_reasons = []
            self.approvals.store.save(item)
            # Valid for the first session it can execute in (Friday evening → Monday), then stale.
            close_at = self.authority.first_eligible_close(self.authority.now())
            if close_at is not None:
                item.expires_at = close_at.isoformat()
                self.approvals.store.save(item)
            approved, outcome = self.approvals.authorize(item.approval_id, decided_by=decision.decided_by or "autonomous_policy", note="autonomous authorization")
            self._journal({"type": "ORDER_AUTHORIZED", **base_row, "approval_id": item.approval_id, "decided_by": decision.decided_by})
            self._emit_authorized(approved, outcome)
            return IntakeResult(status="AUTHORIZED", approval=approved, created=created, authorization=decision, outcome=outcome)

        item.escalation_reasons = list(decision.reasons)
        self.approvals.store.save(item)
        self._journal({"type": "ORDER_ESCALATED", **base_row, "approval_id": item.approval_id, "reasons": decision.reasons})
        if created:
            self._emit_escalated(item, decision)
        return IntakeResult(status="ESCALATED", approval=item, created=created, authorization=decision, reasons=list(decision.reasons))

    # ----------------------------------------------------------------- helpers
    def _open_approved(self, ticker: str, action: str) -> LiveApproval | None:
        for item in self.approvals.store.all():
            if item.ticker != ticker or str(item.proposed_action).upper() != action:
                continue
            # APPROVED = authorized and either waiting for a send window or already at the broker.
            # APPROVED_EXECUTION_DISABLED = authorized while broker writes were off; the drain owns it.
            if item.status in {LiveApprovalStatus.APPROVED, LiveApprovalStatus.APPROVED_EXECUTION_DISABLED}:
                return item
        return None

    def _queued_authorized(self, *, exclude: str | None = None) -> tuple[int, float]:
        """Authorized orders not yet at the broker (they count against today's limits)."""
        count = 0
        buys = 0.0
        for item in self.approvals.store.all():
            if item.approval_id == exclude or item.placed_order:
                continue
            if item.status not in {LiveApprovalStatus.APPROVED, LiveApprovalStatus.APPROVED_EXECUTION_DISABLED}:
                continue
            count += 1
            if str(item.proposed_action).upper() in {"BUY", "ADD"}:
                buys += float(item.proposed_dollar_amount or 0.0)
        return count, buys

    def _journal(self, row: dict[str, Any]) -> None:
        try:
            append_jsonl(row, self.journal)
        except OSError:
            pass

    def _emit_authorized(self, item: LiveApproval, outcome: Any) -> None:
        if self.notify is None:
            return
        from agentic_portfolio.notify import NotificationKind

        placed = bool(getattr(outcome, "placed", False))
        self.notify.emit(
            NotificationKind.ORDER_AUTHORIZED,
            title=f"ORDER AUTHORIZED — {item.ticker} {item.proposed_action}",
            body=(
                f"Autonomous policy authorized {item.proposed_action} {item.ticker}"
                + (f" ${item.proposed_dollar_amount:,.2f}" if item.proposed_dollar_amount else "")
                + (". Submitted to broker." if placed else ". Will execute at the next eligible send window.")
            ),
            payload={
                "approval_id": item.approval_id,
                "ticker": item.ticker,
                "action": item.proposed_action,
                "proposed_dollar_amount": item.proposed_dollar_amount,
                "proposed_allocation_pct": item.proposed_allocation_pct,
                "sleeve": item.sleeve,
                "decided_by": item.decided_by,
                "placed": placed,
            },
        )

    def _emit_escalated(self, item: LiveApproval, decision: AuthorizationDecision) -> None:
        if self.notify is None:
            return
        from agentic_portfolio.notify import NotificationKind

        why = ", ".join(decision.reasons) or "human approval required"
        self.notify.emit(
            NotificationKind.APPROVAL_REQUIRED,
            title=f"TRADE APPROVAL REQUIRED — {item.ticker}",
            body=f"{item.ticker} {item.proposed_action} needs a human decision ({why}).",
            payload={
                "approval_id": item.approval_id,
                "ticker": item.ticker,
                "action": item.proposed_action,
                "proposed_dollar_amount": item.proposed_dollar_amount,
                "proposed_allocation_pct": item.proposed_allocation_pct,
                "sleeve": item.sleeve,
                "reason": item.reason,
                "escalation_reasons": list(decision.reasons),
                "expires_at": item.expires_at,
            },
        )


def build_intake(approvals: Any, *, root: Path, notify: Any = None, now_fn: Any = None) -> ProposalIntake:
    """Intake bound to the same OrderAuthority (kill switch, limits) as the executor."""
    executor = getattr(approvals, "executor", None)
    authority = getattr(executor, "authority", None)
    if authority is None:
        store = getattr(executor, "store", None)
        authority = OrderAuthority(Path(root), execution_store=store, notify=notify, now_fn=now_fn)
    return ProposalIntake(approvals, authority, root=Path(root), notify=notify)
