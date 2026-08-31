"""LIVE approval queue engine. APPROVE never auto-executes; LiveOrderExecutor is optional."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from agentic_portfolio.agent.safety import AgentSafetyError, assert_auto_execution_disabled
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.live_approval.store import LiveApprovalStore
from agentic_portfolio.live_approval.types import APPROVED_STATES, LiveApproval, LiveApprovalStatus
from agentic_portfolio.policy import load_account_rules, load_agent_config
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, RuntimeMode, live_placement_enabled


APPROVED_HOLD = LiveApprovalStatus.APPROVED_EXECUTION_DISABLED


class LiveApprovalEngine:
    def __init__(
        self,
        store: LiveApprovalStore,
        *,
        config: dict[str, Any] | None = None,
        account_rules: dict[str, Any] | None = None,
        journal=None,
        now_fn=None,
        executor=None,
    ) -> None:
        self.store = store
        self.config = config or load_agent_config()
        self.rules = account_rules or load_account_rules()
        self.journal = journal
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.executor = executor
        self._assert_flags()

    def now(self) -> datetime:
        stamp = self._now()
        if stamp.tzinfo is None:
            return stamp.replace(tzinfo=timezone.utc)
        return stamp

    def _assert_flags(self) -> None:
        exec_cfg = dict(self.rules.get("execution") or {})
        assert_auto_execution_disabled(
            live_trade_actions_allowed=bool(exec_cfg.get("live_trade_actions_allowed")),
            auto_execution=bool(exec_cfg.get("auto_execution")),
        )

    def create(
        self,
        *,
        ticker: str,
        proposed_action: str,
        proposed_dollar_amount: float | None = None,
        proposed_allocation_pct: float | None = None,
        reason: str | None = None,
        ai_rationale: str | None = None,
        supporting_thesis: str | None = None,
        current_quote: float | None = None,
        current_spread_bps: float | None = None,
        risk_gate_result: Mapping[str, Any] | None = None,
        portfolio_impact: Mapping[str, Any] | None = None,
        watch_id: str | None = None,
        ttl_hours: int | None = None,
    ) -> LiveApproval:
        self._assert_flags()
        stamp = self.now()
        hours = ttl_hours if ttl_hours is not None else int((self.config.get("approval") or {}).get("ttl_hours") or 24)
        mode = self.store.runtime_mode.value if isinstance(self.store.runtime_mode, RuntimeMode) else str(self.store.runtime_mode)
        item = LiveApproval(
            approval_id=str(uuid4()),
            ticker=str(ticker).upper(),
            proposed_action=str(proposed_action).upper(),
            proposed_dollar_amount=proposed_dollar_amount,
            proposed_allocation_pct=proposed_allocation_pct,
            reason=reason,
            ai_rationale=ai_rationale,
            supporting_thesis=supporting_thesis,
            current_quote=current_quote,
            quote_at_proposal=current_quote,
            nav_at_proposal=_f((portfolio_impact or {}).get("nav")),
            current_spread_bps=current_spread_bps,
            risk_gate_result=dict(risk_gate_result or {}),
            portfolio_impact=dict(portfolio_impact or {}),
            created_at=stamp.isoformat(),
            expires_at=(stamp + timedelta(hours=hours)).isoformat(),
            status=LiveApprovalStatus.PENDING,
            watch_id=watch_id,
            runtime_mode=mode,
            paper_environment=mode == RuntimeMode.PAPER.value,
            LIVE_ORDER_PLACEMENT=live_placement_enabled(),
        )
        self.store.save(item)
        self._log("APPROVAL_CREATED", item)
        return item

    def record_decision(
        self,
        approval_id: str,
        status: LiveApprovalStatus | str,
        *,
        note: str | None = None,
        decided_by: str | None = None,
    ) -> LiveApproval:
        """APPROVE/REJECT. Double APPROVE is idempotent. Placement goes through LiveOrderExecutor."""
        self._assert_flags()
        item = self.store.get(approval_id)
        if item is None:
            raise KeyError(approval_id)
        wanted = LiveApprovalStatus(str(status.value if isinstance(status, LiveApprovalStatus) else status))
        if wanted is LiveApprovalStatus.APPROVED_AWAITING_EXECUTION_IMPLEMENTATION:
            wanted = LiveApprovalStatus.APPROVED
        if item.status is not LiveApprovalStatus.PENDING:
            if wanted in {LiveApprovalStatus.APPROVED, LiveApprovalStatus.APPROVED_EXECUTION_DISABLED} and item.status in APPROVED_STATES:
                return item
            if wanted is LiveApprovalStatus.REJECTED and item.status is LiveApprovalStatus.REJECTED:
                return item
            raise AgentSafetyError(f"approval {approval_id} is {item.status.value} and cannot be decided")
        if wanted is LiveApprovalStatus.APPROVED:
            wanted = LiveApprovalStatus.APPROVED
        if wanted not in {LiveApprovalStatus.APPROVED, LiveApprovalStatus.REJECTED}:
            raise AgentSafetyError(f"unsupported live approval decision {wanted.value}")
        stamp = self.now().isoformat()
        item.decided_at = stamp
        item.decided_by = decided_by
        item.decision_note = note
        item.LIVE_ORDER_PLACEMENT = live_placement_enabled()
        if wanted is LiveApprovalStatus.REJECTED:
            item.status = LiveApprovalStatus.REJECTED
            item.broker_submitted = False
            item.placed_order = False
            item.execution_attempted = False
            self.store.save(item)
            self._log("APPROVAL_REJECTED", item)
            return item
        item.status = LiveApprovalStatus.APPROVED
        item.broker_submitted = False
        item.placed_order = False
        item.execution_attempted = False
        item.approved_does_not_place_order = not live_placement_enabled()
        item.live_execution_blocked = not live_placement_enabled()
        self.store.save(item)
        self._log("APPROVAL_APPROVED", item)
        if self.executor is not None:
            outcome = self.executor.execute_approved(item)
            item = self.store.get(approval_id) or item
            item.status = _status_from_outcome(outcome, item)
            item.execution_attempted = True
            item.placed_order = bool(outcome.placed)
            item.broker_submitted = bool(outcome.placed)
            item.approved_does_not_place_order = not outcome.placed
            item.live_execution_blocked = not outcome.placed
            self.store.save(item)
            return item
        item.status = LiveApprovalStatus.APPROVED_EXECUTION_DISABLED
        item.approved_does_not_place_order = True
        item.live_execution_blocked = True
        self.store.save(item)
        return item

    def expire_due(self) -> list[LiveApproval]:
        now = self.now()
        expired: list[LiveApproval] = []
        for item in self.store.pending():
            if not item.expires_at:
                continue
            try:
                stamp = datetime.fromisoformat(item.expires_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if now >= stamp:
                item.status = LiveApprovalStatus.EXPIRED
                self.store.save(item)
                self._log("APPROVAL_EXPIRED", item)
                expired.append(item)
        return expired

    def attempt_place(self, approval_id: str) -> None:
        item = self.store.get(approval_id)
        if self.executor is None:
            raise AgentSafetyError(
                f"live approval {approval_id if item else approval_id} cannot place an order without LiveOrderExecutor"
            )
        if item is None:
            raise KeyError(approval_id)
        self.executor.execute_approved(item)

    def _log(self, kind: str, item: LiveApproval) -> None:
        if self.journal is None:
            return
        append_jsonl(
            {
                "type": kind,
                "approval_id": item.approval_id,
                "ticker": item.ticker,
                "status": item.status.value,
                "broker_submitted": item.broker_submitted,
                "placed_order": item.placed_order,
                "execution_attempted": item.execution_attempted,
                "LIVE_ORDER_PLACEMENT": live_placement_enabled(),
            },
            self.journal,
        )


def _status_from_outcome(outcome, item: LiveApproval) -> LiveApprovalStatus:
    status = outcome.intent.status.value if hasattr(outcome.intent.status, "value") else str(outcome.intent.status)
    if outcome.placed and status in {"FILLED"}:
        return LiveApprovalStatus.EXECUTED
    if outcome.placed:
        return LiveApprovalStatus.APPROVED
    if status in {"BLOCKED_DISABLED"}:
        return LiveApprovalStatus.APPROVED_EXECUTION_DISABLED
    if status in {"REVALIDATION_REQUIRED"}:
        return LiveApprovalStatus.REVALIDATION_REQUIRED
    if status in {"REJECTED", "BLOCKED"}:
        return LiveApprovalStatus.EXECUTION_FAILED
    if status == "UNKNOWN_RECONCILIATION_REQUIRED":
        return LiveApprovalStatus.APPROVED
    return item.status


def _f(raw) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
