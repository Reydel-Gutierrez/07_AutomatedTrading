"""OrderAuthority — deterministic order authorization. No AI. No broker calls.

Two checkpoints:

* ``review_proposal`` — when a Risk-Gate-permitted proposal enters the queue.
  In AUTONOMOUS mode a clean proposal is AUTO_APPROVED; anything unusual is
  ESCALATED to the human queue with explicit reasons. HUMAN_APPROVAL escalates
  everything. OBSERVE never creates orders.
* ``pre_send`` — immediately before broker submission (all modes). Kill switch,
  autonomy limits (for autonomously authorized orders), trading window, spread.

The Risk Gate is re-run separately at send time by the executor; this module
adds circuit breakers on top of it and never relaxes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from agentic_portfolio.agent.persist import atomic_write_json, read_json
from agentic_portfolio.autonomy.kill_switch import KillSwitch
from agentic_portfolio.autonomy.policy import (
    RISK_DOWN_ACTIONS,
    RISK_UP_ACTIONS,
    ExecutionMode,
    ExecutionPolicy,
    load_execution_policy,
)
from agentic_portfolio.calendar import EASTERN, NyseEquityCalendar

AUTO_APPROVE = "AUTO_APPROVE"
ESCALATE = "ESCALATE"
OBSERVE_ONLY = "OBSERVE_ONLY"

AUTONOMOUS_DECIDER = "autonomous_policy"

SUBMITTED_STATES = {"PENDING_SUBMISSION", "SUBMITTED", "OPEN", "PARTIALLY_FILLED", "FILLED", "UNKNOWN_RECONCILIATION_REQUIRED"}


@dataclass
class AuthorizationDecision:
    outcome: str
    reasons: list[str] = field(default_factory=list)
    mode: str = ExecutionMode.HUMAN_APPROVAL.value
    decided_by: str | None = None
    policy_version: str | None = None
    decided_at: str | None = None

    @property
    def auto_approved(self) -> bool:
        return self.outcome == AUTO_APPROVE

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "mode": self.mode,
            "decided_by": self.decided_by,
            "policy_version": self.policy_version,
            "decided_at": self.decided_at,
        }


@dataclass
class DailyUsage:
    session_date: str
    orders_submitted: int = 0
    buy_notional: float = 0.0
    sell_notional: float = 0.0


def is_autonomous_approval(approval: Any) -> bool:
    return str(getattr(approval, "decided_by", "") or "").startswith(AUTONOMOUS_DECIDER)


def _f(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return None if value != value else value


class OrderAuthority:
    def __init__(
        self,
        root: Path,
        *,
        policy: ExecutionPolicy | None = None,
        kill_switch: KillSwitch | None = None,
        execution_store: Any = None,
        now_fn: Callable[[], datetime] | None = None,
        notify: Any = None,
    ) -> None:
        self.root = Path(root)
        self._policy = policy
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.kill_switch = kill_switch or KillSwitch(self.root, policy=policy, notify=notify, now_fn=self._now)
        self.execution_store = execution_store
        self.notify = notify
        self.health_path = self.root / "state" / "runtime" / "execution_health.json"
        self.calendar = NyseEquityCalendar()

    # ------------------------------------------------------------------ helpers
    @property
    def policy(self) -> ExecutionPolicy:
        # Re-read each time unless injected: an operator can switch modes without a restart.
        return self._policy if self._policy is not None else load_execution_policy()

    def now(self) -> datetime:
        stamp = self._now()
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)

    def _session_date(self, stamp: datetime) -> str:
        return stamp.astimezone(EASTERN).date().isoformat()

    def daily_usage(self, now: datetime | None = None) -> DailyUsage:
        """Count orders actually sent to the broker during this ET trading day."""
        stamp = now or self.now()
        day = self._session_date(stamp)
        usage = DailyUsage(session_date=day)
        if self.execution_store is None:
            return usage
        for intent in self.execution_store.intents():
            status = getattr(intent.status, "value", intent.status)
            if not intent.place_attempted and status not in SUBMITTED_STATES:
                continue
            when = intent.submitted_at or intent.updated_at or intent.created_at
            try:
                at = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            if self._session_date(at) != day:
                continue
            usage.orders_submitted += 1
            notional = _f(intent.notional) or 0.0
            if str(intent.side).lower() == "buy":
                usage.buy_notional += notional
            else:
                usage.sell_notional += notional
        return usage

    def failure_streak(self) -> int:
        data = read_json(self.health_path, {})
        try:
            return int(data.get("consecutive_failures") or 0)
        except (TypeError, ValueError):
            return 0

    def note_execution_outcome(self, *, success: bool, reason: str | None = None, symbol: str | None = None) -> int:
        """Track consecutive broker-side failures. Trips the kill switch at the limit."""
        data = read_json(self.health_path, {})
        streak = 0 if success else int(data.get("consecutive_failures") or 0) + 1
        payload = {
            "consecutive_failures": streak,
            "last_outcome": "SUCCESS" if success else "FAILURE",
            "last_reason": reason,
            "last_symbol": symbol,
            "updated_at": self.now().isoformat(),
        }
        atomic_write_json(self.health_path, payload)
        limit = self.policy.limits.max_consecutive_execution_failures
        if not success and streak >= limit and self.policy.auto_trip_enabled("consecutive_execution_failures"):
            self.kill_switch.engage(f"{streak} consecutive execution failures (last: {reason})", by="executor", automatic=True)
        return streak

    def trip(self, reason: str, *, rule: str) -> None:
        if self.policy.auto_trip_enabled(rule):
            self.kill_switch.engage(reason, by="executor", automatic=True)

    # --------------------------------------------------------- authorization
    def review_proposal(
        self,
        *,
        action: str,
        risk_verdict: str | None,
        notional: float | None,
        nav: float | None,
        cash: float | None,
        risk_reasons: Iterable[str] = (),
        human_only_reasons: Iterable[str] = (),
        queued_orders: int = 0,
        queued_buy_notional: float = 0.0,
    ) -> AuthorizationDecision:
        """Authorize or escalate. ``queued_*`` = already-authorized orders not yet sent today."""
        policy = self.policy
        stamp = self.now().isoformat()
        decision = AuthorizationDecision(outcome=ESCALATE, mode=policy.mode.value, policy_version=policy.version, decided_at=stamp)
        if policy.mode is ExecutionMode.OBSERVE:
            decision.outcome = OBSERVE_ONLY
            decision.reasons = ["observe_mode"]
            return decision
        if policy.mode is ExecutionMode.HUMAN_APPROVAL:
            decision.reasons = ["human_approval_mode"] + [str(r) for r in human_only_reasons if r]
            return decision
        usage = self.daily_usage()
        usage.orders_submitted += max(0, int(queued_orders))
        usage.buy_notional += max(0.0, float(queued_buy_notional or 0.0))
        reasons = self._autonomy_reasons(
            action=action,
            risk_verdict=risk_verdict,
            notional=notional,
            nav=nav,
            cash=cash,
            usage=usage,
        )
        if queued_buy_notional and str(action).upper() in RISK_UP_ACTIONS and cash is not None and notional:
            # Cash already promised to queued buys is not available to this one.
            if float(cash) - float(queued_buy_notional) - float(notional) < (float(nav or 0) * self.policy.limits.min_cash_reserve_pct_nav / 100.0) - 1e-9:
                reasons.append("cash_committed_to_queued_orders")
        reasons.extend(str(r) for r in human_only_reasons if r)
        if reasons:
            decision.reasons = list(dict.fromkeys(reasons))
            return decision
        decision.outcome = AUTO_APPROVE
        decision.decided_by = f"{AUTONOMOUS_DECIDER}:{policy.version}"
        decision.reasons = ["all_autonomy_checks_passed"]
        return decision

    def _autonomy_reasons(
        self,
        *,
        action: str,
        risk_verdict: str | None,
        notional: float | None,
        nav: float | None,
        cash: float | None,
        usage: DailyUsage | None = None,
        counting_this_order: bool = True,
    ) -> list[str]:
        limits = self.policy.limits
        act = str(action or "").upper()
        reasons: list[str] = []
        if self.kill_switch.engaged():
            reasons.append("kill_switch_engaged")
        if act not in limits.allowed_actions:
            reasons.append(f"action_not_autonomous:{act}")
        verdict = str(risk_verdict or "").upper()
        if verdict != limits.required_risk_verdict:
            reasons.append(f"risk_verdict_requires_human:{verdict or 'MISSING'}")
        streak = self.failure_streak()
        if streak >= limits.max_consecutive_execution_failures:
            reasons.append(f"execution_failure_streak:{streak}")
        nav_v = _f(nav)
        amount = _f(notional)
        if nav_v is None or nav_v <= 0:
            reasons.append("nav_unavailable")
            return reasons
        if act in RISK_UP_ACTIONS or act in RISK_DOWN_ACTIONS:
            if amount is None or amount <= 0:
                reasons.append("order_unsized")
            elif act in RISK_UP_ACTIONS and amount / nav_v * 100.0 > limits.max_single_order_pct_nav + 1e-9:
                reasons.append(f"single_order_above_{limits.max_single_order_pct_nav:g}pct_nav")
        used = usage or self.daily_usage()
        if counting_this_order and used.orders_submitted + 1 > limits.max_orders_per_day:
            reasons.append(f"daily_order_limit:{limits.max_orders_per_day}")
        if act in RISK_UP_ACTIONS and amount:
            projected = (used.buy_notional + amount) / nav_v * 100.0
            if projected > limits.max_buy_notional_pct_nav_per_day + 1e-9:
                reasons.append(f"daily_buy_limit:{limits.max_buy_notional_pct_nav_per_day:g}pct_nav")
            cash_v = _f(cash)
            if cash_v is None:
                reasons.append("cash_unavailable")
            else:
                reserve = nav_v * limits.min_cash_reserve_pct_nav / 100.0
                if cash_v - amount < reserve - 1e-9:
                    reasons.append(f"cash_reserve_below_{limits.min_cash_reserve_pct_nav:g}pct_nav")
        return reasons

    # ------------------------------------------------------------- send time
    def pre_send(
        self,
        approval: Any,
        *,
        nav: float | None,
        cash: float | None,
        notional: float | None,
        bid: float | None,
        ask: float | None,
        now: datetime | None = None,
    ) -> list[str]:
        """Checks run right before broker submission. Empty list means proceed."""
        stamp = now or self.now()
        codes: list[str] = []
        ks = self.kill_switch.status()
        if ks.engaged:
            codes.append("KILL_SWITCH_ENGAGED")
        policy = self.policy
        if policy.mode is ExecutionMode.OBSERVE:
            codes.append("OBSERVE_MODE")
        spread_bps = _spread_bps(bid, ask)
        if spread_bps is not None and spread_bps > policy.limits.max_spread_bps + 1e-9:
            codes.append("SPREAD_TOO_WIDE")
        if is_autonomous_approval(approval):
            if policy.mode is not ExecutionMode.AUTONOMOUS:
                codes.append("AUTONOMOUS_MODE_DISABLED_REQUIRES_HUMAN")
            if spread_bps is None and str(getattr(approval, "proposed_action", "")).upper() in RISK_UP_ACTIONS:
                codes.append("SPREAD_UNAVAILABLE")
            window = self._window_code(stamp)
            if window:
                codes.append(window)
            decided = _parse(getattr(approval, "decided_at", None))
            if decided is not None and self._authorization_stale(decided, stamp):
                codes.append("AUTHORIZATION_STALE")
            reasons = self._autonomy_reasons(
                action=str(approval.proposed_action),
                risk_verdict=str((approval.risk_gate_result or {}).get("verdict") or ""),
                notional=notional,
                nav=nav,
                cash=cash,
            )
            # Map the shared checks onto send-time codes (kill switch already covered).
            for reason in reasons:
                if reason == "kill_switch_engaged":
                    continue
                codes.append("AUTONOMY_LIMIT:" + reason)
        return list(dict.fromkeys(codes))

    def _authorization_stale(self, decided: datetime, stamp: datetime) -> bool:
        """An autonomous authorization is good for the first session it could execute in.

        Decided before that session's send cutoff → that session; otherwise the next one.
        Never longer than ``authorization_ttl_hours`` either. Stale orders are consumed and
        the pipeline must re-propose on fresh facts.
        """
        limits = self.policy.limits
        if limits.authorization_ttl_hours and stamp - decided > timedelta(hours=float(limits.authorization_ttl_hours) + 24 * 4):
            return True  # hard backstop (long weekends included)
        close_at = self.first_eligible_close(decided)
        if close_at is None:
            return True
        return stamp > close_at

    def first_eligible_close(self, decided: datetime) -> datetime | None:
        """Close (UTC) of the first session in which an order decided at ``decided`` may be sent."""
        limits = self.policy.limits
        session = self.calendar.session_for(decided)
        if session is not None:
            cutoff = datetime.combine(session.session_date, session.close_time, tzinfo=EASTERN) - timedelta(minutes=limits.min_minutes_before_close)
            if decided.astimezone(EASTERN) >= cutoff:
                session = None
        if session is None:
            session = self.calendar.next_session(decided)
        if session is None:
            return None
        return datetime.combine(session.session_date, session.close_time, tzinfo=EASTERN).astimezone(timezone.utc)

    def _window_code(self, stamp: datetime) -> str | None:
        session = self.calendar.session_for(stamp)
        if session is None:
            return "MARKET_CLOSED"
        et = stamp.astimezone(EASTERN)
        open_at = datetime.combine(session.session_date, session.regular_open, tzinfo=EASTERN)
        close_at = datetime.combine(session.session_date, session.close_time, tzinfo=EASTERN)
        limits = self.policy.limits
        if et < open_at + timedelta(minutes=limits.min_minutes_after_open):
            return "TOO_CLOSE_TO_OPEN"
        if et > close_at - timedelta(minutes=limits.min_minutes_before_close):
            return "TOO_CLOSE_TO_CLOSE"
        return None

    def status(self) -> dict[str, Any]:
        usage = self.daily_usage()
        policy = self.policy
        return {
            "policy": policy.as_dict(),
            "kill_switch": self.kill_switch.status().as_dict(),
            "today": {
                "session_date": usage.session_date,
                "orders_submitted": usage.orders_submitted,
                "buy_notional": round(usage.buy_notional, 2),
                "sell_notional": round(usage.sell_notional, 2),
            },
            "consecutive_failures": self.failure_streak(),
        }


def _spread_bps(bid: float | None, ask: float | None) -> float | None:
    b, a = _f(bid), _f(ask)
    if b is None or a is None or b <= 0 or a <= 0 or a < b:
        return None
    mid = (a + b) / 2.0
    return (a - b) / mid * 10_000.0


def _parse(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
