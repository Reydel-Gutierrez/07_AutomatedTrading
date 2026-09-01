"""Watch lifecycle, conditional plans, and AI-reassessment gates.

Most loops are deterministic. AI runs only on material events after cooldown.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from agentic_portfolio.agent.safety import assert_execution_disabled
from agentic_portfolio.calendar import NyseEquityCalendar, is_regular_hours, next_regular_open_at
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.policy import load_agent_config, load_research_config
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, RuntimeMode
from agentic_portfolio.watch.store import WatchStore
from agentic_portfolio.watch.types import (
    ACTIVE_WATCH,
    TERMINAL_WATCH,
    ConditionalPlan,
    ReassessTrigger,
    WatchItem,
    WatchStatus,
    parse_iso,
)

# Session-open is a scheduling event. It must not spend Terra by itself.
AI_REASSESS_TRIGGERS = {
    ReassessTrigger.PRICE_MOVE,
    ReassessTrigger.NEWS_CATALYST,
    ReassessTrigger.EARNINGS_UPDATE,
    ReassessTrigger.FUNDAMENTAL_UPDATE,
    ReassessTrigger.THESIS_EXPIRED,
    ReassessTrigger.ENTRY_APPROACHED,
    ReassessTrigger.RISK_STATE_CHANGE,
    ReassessTrigger.MANUAL,
}

# Conditional waits retry inside the session. They must not inherit a sleeve WATCH interval.
INTRASESSION_WAIT = {
    WatchStatus.WAITING_FOR_PRICE,
    WatchStatus.WAITING_FOR_LIQUIDITY,
    WatchStatus.WAITING_FOR_CATALYST,
    WatchStatus.READY_FOR_RISK_GATE,
}
SCHEDULE_ON_STATUS = INTRASESSION_WAIT | {
    WatchStatus.WAITING_FOR_OPEN,
    WatchStatus.WATCH,
    WatchStatus.APPROVAL_REQUIRED,
}


def context_hash(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(dict(payload), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def news_hash(headlines: list[str] | None) -> str:
    return context_hash({"headlines": list(headlines or [])})


class WatchEngine:
    def __init__(
        self,
        store: WatchStore,
        *,
        config: dict[str, Any] | None = None,
        journal=None,
        now_fn=None,
    ) -> None:
        self.store = store
        self.config = config or load_agent_config()
        self.watch_cfg = dict(self.config.get("watch") or {})
        self.cond_cfg = dict(self.config.get("conditions") or {})
        self.journal = journal
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.reconcile_waiting_for_open_schedules()

    def now(self) -> datetime:
        stamp = self._now()
        if stamp.tzinfo is None:
            return stamp.replace(tzinfo=timezone.utc)
        return stamp

    def upsert_from_candidate(
        self,
        *,
        ticker: str,
        score: float | None = None,
        security_identity: str | None = None,
        security_type: str | None = None,
        thesis: str | None = None,
        confidence: str | None = None,
        reasons: list[str] | None = None,
        risks: list[str] | None = None,
        screening: dict[str, Any] | None = None,
        ai_ids: list[str] | None = None,
        status: WatchStatus = WatchStatus.WATCH,
        last_price: float | None = None,
        session_id: str | None = None,
        next_session_id: str | None = None,
        off_hours: bool = False,
        prepare_conditional_plan: bool | None = None,
        context: Mapping[str, Any] | None = None,
        sleeve: str | None = None,
        proposed_notional: float | None = None,
        desired_allocation_pct: float | None = None,
    ) -> WatchItem:
        assert_execution_disabled()
        stamp = self.now()
        existing = self.store.by_ticker(ticker)
        if existing is None:
            existing = WatchItem(
                watch_id=str(uuid4()),
                ticker=str(ticker).upper(),
                created_at=stamp.isoformat(),
                runtime_mode=self.store.runtime_mode.value if isinstance(self.store.runtime_mode, RuntimeMode) else str(self.store.runtime_mode),
                paper_environment=self.store.runtime_mode is RuntimeMode.PAPER,
                LIVE_ORDER_PLACEMENT=LIVE_ORDER_PLACEMENT,
            )
        existing.security_identity = security_identity or existing.security_identity
        existing.security_type = security_type or existing.security_type or "equity"
        existing.source_candidate_score = score if score is not None else existing.source_candidate_score
        if thesis:
            existing.research_thesis = thesis
        if confidence:
            existing.confidence = confidence
        if reasons:
            existing.reasons = list(reasons)
        if risks:
            existing.risks = list(risks)
        if screening:
            existing.screening_result = dict(screening)
        if ai_ids:
            existing.ai_context_ids = list(dict.fromkeys(list(existing.ai_context_ids) + list(ai_ids)))
        if last_price is not None:
            existing.last_price = last_price
        if sleeve:
            existing.sleeve = sleeve
        if proposed_notional is not None:
            existing.proposed_notional = proposed_notional
        if desired_allocation_pct is not None:
            existing.desired_allocation_pct = desired_allocation_pct
        if existing.status in {WatchStatus.REJECTED, WatchStatus.EXPIRED, WatchStatus.INVALIDATED}:
            existing.status = status
        elif existing.status == WatchStatus.DISCOVERED:
            existing.status = status
        ttl_hours = int(self.watch_cfg.get("thesis_ttl_hours") or 168)
        existing.expiration = existing.expiration or (stamp + timedelta(hours=ttl_hours)).isoformat()
        make_plan = prepare_conditional_plan if prepare_conditional_plan is not None else off_hours
        if off_hours:
            existing.required_market_confirmation = True
            existing.status = WatchStatus.WAITING_FOR_OPEN if existing.status in ACTIVE_WATCH or status in ACTIVE_WATCH else existing.status
            if make_plan:
                existing.conditional_plan = existing.conditional_plan or self.default_plan(
                    last_price=last_price,
                    prepared_session_id=session_id,
                    target_session_id=next_session_id,
                    proposed_notional=existing.proposed_notional,
                    desired_allocation_pct=existing.desired_allocation_pct,
                )
            self._copy_sizing_onto_plan(existing)
            self.schedule_review(existing, waiting_for_open=True, sleeve=existing.sleeve)
        else:
            existing.status = status
            self._copy_sizing_onto_plan(existing)
            if status is WatchStatus.WATCH:
                self.schedule_review(existing, waiting_for_open=False, sleeve=existing.sleeve)
        hashed = context_hash(context or {"ticker": existing.ticker, "thesis": existing.research_thesis, "price": existing.last_price})
        existing.last_context_hash = hashed
        existing.last_updated = stamp.isoformat()
        self.store.save(existing)
        self._log("WATCH_UPSERTED", existing)
        return existing

    def default_plan(
        self,
        *,
        last_price: float | None,
        prepared_session_id: str | None,
        target_session_id: str | None,
        proposed_notional: float | None = None,
        desired_allocation_pct: float | None = None,
    ) -> ConditionalPlan:
        max_price = None
        if last_price is not None:
            max_price = round(float(last_price) * 1.01, 4)
        return ConditionalPlan(
            max_price=max_price,
            max_spread_bps=float(self.cond_cfg.get("max_spread_bps") or 50),
            min_dollar_volume=float(self.cond_cfg.get("min_dollar_volume") or 1_000_000),
            require_no_adverse_catalyst=True,
            require_cash_available=True,
            require_risk_gate_pass=True,
            require_regular_hours_quotes=True,
            notes="Next-session confirmation required. Off-hours liquidity is not executable.",
            prepared_session_id=prepared_session_id,
            target_session_id=target_session_id,
            proposed_notional=proposed_notional,
            desired_allocation_pct=desired_allocation_pct,
        )

    def _copy_sizing_onto_plan(self, item: WatchItem) -> None:
        plan = item.conditional_plan
        if plan is None:
            return
        if plan.proposed_notional is None and item.proposed_notional is not None:
            plan.proposed_notional = item.proposed_notional
        if plan.desired_allocation_pct is None and item.desired_allocation_pct is not None:
            plan.desired_allocation_pct = item.desired_allocation_pct

    def schedule_review(
        self,
        item: WatchItem,
        *,
        waiting_for_open: bool = False,
        sleeve: str | None = None,
    ) -> WatchItem:
        """Bind next_review_at to the current status. Clock ticks do not spend Terra."""
        target = self._review_datetime(item.status, sleeve or item.sleeve, waiting_for_open=waiting_for_open)
        if target is not None:
            item.next_review_at = target.isoformat()
        item.last_updated = self.now().isoformat()
        self.store.save(item)
        return item

    def _review_datetime(
        self,
        status: WatchStatus,
        sleeve: str | None,
        *,
        waiting_for_open: bool = False,
    ) -> datetime | None:
        if waiting_for_open or status is WatchStatus.WAITING_FOR_OPEN:
            return next_regular_open_at(self.now())
        if status in INTRASESSION_WAIT or status is WatchStatus.APPROVAL_REQUIRED:
            return self._intrasession_retry_at()
        if status is WatchStatus.WATCH:
            return self.now() + timedelta(hours=_watch_retry_hours(sleeve))
        if status in TERMINAL_WATCH:
            return None
        return None

    def _intrasession_retry_at(self) -> datetime:
        """Retry liquidity/price/catalyst inside RTH; otherwise the next regular open.

        Cadence matches WATCH_CONDITION_MONITOR (15 minutes). Deterministic quote
        checks only — not a Terra event.
        """
        minutes = float(self.watch_cfg.get("intrasession_retry_minutes") or 15)
        proposed = self.now() + timedelta(minutes=minutes)
        if is_regular_hours(proposed):
            return proposed
        cal = NyseEquityCalendar()
        nxt = cal.next_session(self.now())
        if nxt is None:
            return next_regular_open_at(self.now())
        open_local = datetime.combine(nxt.session_date, nxt.regular_open, tzinfo=cal.tz)
        return open_local.astimezone(timezone.utc)

    def reconcile_waiting_for_open_schedules(self) -> list[WatchItem]:
        """Rewrite persisted WAITING_FOR_OPEN next_review_at onto the next regular open.

        Thesis, invalidation, sleeve, score, expiry, and evidence are left alone.
        A timestamp correction is not a Terra event.
        """
        assert_execution_disabled()
        migrated: list[WatchItem] = []
        for item in self.store.active():
            if item.status is WatchStatus.WATCH:
                continue
            if item.status not in SCHEDULE_ON_STATUS:
                continue
            target = self._review_datetime(item.status, item.sleeve)
            if target is None:
                continue
            current = parse_iso(item.next_review_at)
            if _same_review_instant(current, target):
                continue
            item.next_review_at = target.isoformat()
            item.last_updated = self.now().isoformat()
            self.store.save(item)
            self._log(
                "WATCH_SCHEDULE_RECONCILED",
                item,
                extra={"reason": "status_next_review", "ai_spent": False},
            )
            migrated.append(item)
        return migrated

    def promote_waiting_for_open(self, *, regular_hours_open: bool) -> list[WatchItem]:
        """At the open, KEEP_WATCHING items become WATCH. Conditional buy plans stay until evaluated."""
        if not regular_hours_open:
            return []
        promoted: list[WatchItem] = []
        for item in self.store.active():
            if item.status is not WatchStatus.WAITING_FOR_OPEN:
                continue
            if item.conditional_plan is not None:
                continue
            self.set_status(item, WatchStatus.WATCH, reason="regular_session_open")
            promoted.append(item)
        return promoted

    def should_spend_ai(
        self,
        item: WatchItem,
        *,
        context: Mapping[str, Any],
        triggers: list[ReassessTrigger],
        ai_allowed: bool,
        budget_exhausted: bool,
    ) -> tuple[bool, str]:
        if not ai_allowed or budget_exhausted:
            return False, "ai_blocked"
        material = [t for t in triggers if t in AI_REASSESS_TRIGGERS]
        hashed = context_hash(context)
        if not material:
            return False, "unchanged_context" if hashed == item.last_context_hash else "no_material_change"
        return True, "material_event"

    def detect_triggers(
        self,
        item: WatchItem,
        *,
        price: float | None = None,
        headlines: list[str] | None = None,
        risk_state: str | None = None,
        regular_hours_open: bool = False,
        earnings_update: bool = False,
        fundamental_update: bool = False,
    ) -> list[ReassessTrigger]:
        triggers: list[ReassessTrigger] = []
        threshold = float(self.watch_cfg.get("price_move_pct") or 0.03)
        if price is not None and item.last_price:
            if item.last_price and abs(price - item.last_price) / abs(item.last_price) >= threshold:
                triggers.append(ReassessTrigger.PRICE_MOVE)
        hashed = news_hash(headlines)
        if headlines and item.last_news_hash and hashed != item.last_news_hash:
            triggers.append(ReassessTrigger.NEWS_CATALYST)
        elif headlines and not item.last_news_hash:
            triggers.append(ReassessTrigger.NEWS_CATALYST)
        if earnings_update:
            triggers.append(ReassessTrigger.EARNINGS_UPDATE)
        if fundamental_update:
            triggers.append(ReassessTrigger.FUNDAMENTAL_UPDATE)
        exp = parse_iso(item.expiration)
        if exp is not None and self.now() >= exp:
            triggers.append(ReassessTrigger.THESIS_EXPIRED)
        plan = item.conditional_plan
        if price is not None and plan and plan.max_price is not None:
            approach = float(self.watch_cfg.get("entry_approach_pct") or 0.015)
            if price <= plan.max_price * (1 + approach):
                triggers.append(ReassessTrigger.ENTRY_APPROACHED)
        if regular_hours_open and item.status == WatchStatus.WAITING_FOR_OPEN:
            triggers.append(ReassessTrigger.MARKET_OPEN_AFTER_OFFHOURS)
        if risk_state and item.last_risk_state and risk_state != item.last_risk_state:
            triggers.append(ReassessTrigger.RISK_STATE_CHANGE)
        return triggers

    def mark_ai_spent(self, item: WatchItem, *, context: Mapping[str, Any], cost: float = 0.0, context_id: str | None = None) -> WatchItem:
        item.last_ai_at = self.now().isoformat()
        item.last_context_hash = context_hash(context)
        item.last_ai_cost = float(cost)
        item.last_reassessed_at = item.last_ai_at
        item.last_updated = item.last_ai_at
        if context_id:
            item.ai_context_ids = list(dict.fromkeys(list(item.ai_context_ids) + [context_id]))
        self.store.save(item)
        return item

    def mark_reassessed(self, item: WatchItem, *, price: float | None = None, headlines: list[str] | None = None, risk_state: str | None = None) -> WatchItem:
        stamp = self.now().isoformat()
        item.last_reassessed_at = stamp
        item.last_updated = stamp
        if price is not None:
            item.last_price = price
        if headlines is not None:
            item.last_news_hash = news_hash(headlines)
        if risk_state is not None:
            item.last_risk_state = risk_state
        self.store.save(item)
        return item

    def set_status(self, item: WatchItem, status: WatchStatus, *, reason: str | None = None) -> WatchItem:
        item.status = status
        item.last_updated = self.now().isoformat()
        if reason:
            item.reasons = list(dict.fromkeys(list(item.reasons) + [reason]))
        if status in SCHEDULE_ON_STATUS:
            self.schedule_review(item, sleeve=item.sleeve)
        else:
            self.store.save(item)
        self._log("WATCH_STATUS", item, extra={"reason": reason})
        return item

    def expire_stale(self) -> list[WatchItem]:
        expired: list[WatchItem] = []
        now = self.now()
        for item in self.store.active():
            exp = parse_iso(item.expiration)
            if exp is not None and now >= exp:
                expired.append(self.set_status(item, WatchStatus.EXPIRED, reason="thesis_expiration"))
        return expired

    def evaluate_conditions(
        self,
        item: WatchItem,
        *,
        regular_hours_open: bool,
        price: float | None,
        spread_bps: float | None,
        dollar_volume: float | None,
        adverse_catalyst: bool,
        cash_available: bool,
        risk_pass: bool | None,
    ) -> dict[str, Any]:
        """Deterministic next-session check. Off-hours liquidity is never treated as executable."""
        plan = item.conditional_plan
        if plan is None:
            return {"ok": False, "reason": "no_conditional_plan", "create_approval": False}
        if not regular_hours_open:
            return {
                "ok": False,
                "reason": "off_hours_liquidity_not_executable",
                "create_approval": False,
                "remain": WatchStatus.WAITING_FOR_OPEN.value,
            }
        failures: list[str] = []
        if plan.max_price is not None and (price is None or price > plan.max_price):
            failures.append("price")
        if plan.max_spread_bps is not None and (spread_bps is None or spread_bps > plan.max_spread_bps):
            failures.append("spread")
        if plan.min_dollar_volume is not None and (dollar_volume is None or dollar_volume < plan.min_dollar_volume):
            failures.append("liquidity")
        if plan.require_no_adverse_catalyst and adverse_catalyst:
            failures.append("catalyst")
        if plan.require_cash_available and not cash_available:
            failures.append("cash")
        if plan.require_risk_gate_pass and risk_pass is not True:
            failures.append("risk_gate")
        if failures:
            remain = WatchStatus.WATCH
            if "price" in failures:
                remain = WatchStatus.WAITING_FOR_PRICE
            elif "liquidity" in failures or "spread" in failures:
                remain = WatchStatus.WAITING_FOR_LIQUIDITY
            elif "catalyst" in failures:
                remain = WatchStatus.WAITING_FOR_CATALYST
            self.set_status(item, remain, reason="conditions_failed:" + ",".join(failures))
            return {"ok": False, "reason": ",".join(failures), "create_approval": False, "remain": remain.value}
        self.set_status(item, WatchStatus.READY_FOR_RISK_GATE, reason="conditions_passed")
        return {"ok": True, "reason": "conditions_passed", "create_approval": True, "remain": WatchStatus.READY_FOR_RISK_GATE.value}

    def _log(self, kind: str, item: WatchItem, extra: dict[str, Any] | None = None) -> None:
        if self.journal is None:
            return
        payload = {"type": kind, "watch_id": item.watch_id, "ticker": item.ticker, "status": item.status.value, "placement_attempted": False}
        if extra:
            payload.update(extra)
        append_jsonl(payload, self.journal)


def _same_review_instant(left: datetime | None, right: datetime) -> bool:
    if left is None:
        return False
    try:
        a = left.astimezone(timezone.utc).replace(microsecond=0)
        b = right.astimezone(timezone.utc).replace(microsecond=0)
    except (ValueError, OverflowError, OSError):
        return False
    return a == b


def _watch_retry_hours(sleeve: str | None) -> float:
    try:
        cfg = load_research_config()
        hours = ((cfg.get("reassessment") or {}).get("watch_min_retry_hours") or {}).get(sleeve or "")
        if hours is not None:
            return float(hours)
    except Exception:  # noqa: BLE001
        pass
    return 12.0
