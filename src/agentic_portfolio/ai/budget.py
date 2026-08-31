"""Hard global AI budget. Combined spend across every provider/model ≤ $10/month."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable
from uuid import uuid4

from agentic_portfolio.ai.config import load_ai_config, money
from agentic_portfolio.ai.errors import BudgetDenied, BudgetExhausted
from agentic_portfolio.ai.ledger import UsageLedger, day_key, month_key
from agentic_portfolio.ai.pricing import quantize
from agentic_portfolio.ai.types import BudgetMode, BudgetStatus, ModelRole, UsageRecord

ZERO = Decimal("0")


@dataclass
class Reservation:
    reservation_id: str
    estimated_cost: Decimal
    month: str
    purpose: str
    role: str
    provider: str
    model: str
    ticker: str | None
    created_at: str


class BudgetManager:
    """Every adapter must pass through this manager. No bypass."""

    def __init__(
        self,
        ledger: UsageLedger,
        config: dict[str, Any] | None = None,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.ledger = ledger
        self.config = config or load_ai_config()
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        budget = dict(self.config.get("budget") or {})
        self.cap = money(budget.get("monthly_cap") or 10)
        self.conservation = money(budget.get("conservation_threshold") or 8)
        self.critical = money(budget.get("critical_threshold") or 9.5)
        self.hard_stop = money(budget.get("hard_stop") or 10)
        self.reservation_ttl = int(budget.get("reservation_ttl_seconds") or 3600)
        self.critical_purposes = {str(p) for p in (budget.get("critical_purposes") or [])}
        self.conserving_roles = {str(p) for p in (budget.get("conserving_allowed_roles") or ["screening"])}

    def now(self) -> datetime:
        stamp = self._now()
        if stamp.tzinfo is None:
            return stamp.replace(tzinfo=timezone.utc)
        return stamp

    def mode_for(self, spent: Decimal, reserved: Decimal = ZERO) -> BudgetMode:
        total = spent + reserved
        if total >= self.hard_stop or total >= self.cap:
            return BudgetMode.EXHAUSTED
        if total >= self.critical:
            return BudgetMode.CRITICAL
        if total >= self.conservation:
            return BudgetMode.CONSERVING
        return BudgetMode.NORMAL

    def status(self) -> BudgetStatus:
        def _read() -> BudgetStatus:
            data = self._reconcile_locked(self.ledger.load_month(now=self.now()))
            spent = money(data.get("spent"))
            reserved = self._reserved_total(data)
            remaining = max(ZERO, self.cap - spent - reserved)
            pct = float((spent / self.cap) * 100) if self.cap else 0.0
            today = day_key(self.now())
            daily = dict(data.get("daily") or {})
            return BudgetStatus(
                month=str(data["month"]),
                mode=self.mode_for(spent, reserved),
                cap=self.cap,
                spent=spent,
                reserved=reserved,
                remaining=remaining,
                pct_used=round(pct, 4),
                calls_month=int(data.get("calls") or 0),
                calls_today=int((daily.get(today) or {}).get("calls") or 0) if isinstance(daily.get(today), dict) else 0,
                spend_by_provider={k: money(v) for k, v in dict(data.get("by_provider") or {}).items()},
                spend_by_model={k: money(v) for k, v in dict(data.get("by_model") or {}).items()},
                daily_spent=money((daily.get(today) or {}).get("spent") if isinstance(daily.get(today), dict) else daily.get(today) or 0),
            )

        return self.ledger.with_lock(_read)

    def authorize(
        self,
        estimated_cost: Decimal,
        *,
        purpose: str,
        role: str | ModelRole,
        provider: str,
        model: str,
        ticker: str | None = None,
        critical: bool = False,
        runtime_mode: str | None = None,
    ) -> Reservation:
        estimate = quantize(max(ZERO, estimated_cost))
        role_name = role.value if isinstance(role, ModelRole) else str(role)

        def _reserve() -> Reservation:
            now = self.now()
            data = self._reconcile_locked(self.ledger.load_month(now=now))
            spent = money(data.get("spent"))
            reserved = self._reserved_total(data)
            projected = spent + reserved + estimate
            mode = self.mode_for(spent, reserved)
            if mode is BudgetMode.EXHAUSTED or spent >= self.hard_stop or projected > self.cap:
                self._deny(
                    purpose=purpose,
                    role=role_name,
                    provider=provider,
                    model=model,
                    ticker=ticker,
                    estimated=estimate,
                    spent=spent,
                    reason="monthly_cap",
                    runtime_mode=runtime_mode,
                )
                raise BudgetExhausted(
                    f"AI monthly cap ${self.cap} reached (spent ${spent}). "
                    "External AI calls are blocked until next calendar month."
                )
            if mode is BudgetMode.CRITICAL and not (critical or purpose in self.critical_purposes):
                self._deny(
                    purpose=purpose,
                    role=role_name,
                    provider=provider,
                    model=model,
                    ticker=ticker,
                    estimated=estimate,
                    spent=spent,
                    reason="critical_mode",
                    runtime_mode=runtime_mode,
                )
                raise BudgetDenied(
                    f"budget mode CRITICAL: only reassessment/critical purposes allowed (got {purpose})"
                )
            if mode is BudgetMode.CONSERVING and role_name not in self.conserving_roles and not (
                critical or purpose in self.critical_purposes
            ):
                self._deny(
                    purpose=purpose,
                    role=role_name,
                    provider=provider,
                    model=model,
                    ticker=ticker,
                    estimated=estimate,
                    spent=spent,
                    reason="conservation_mode",
                    runtime_mode=runtime_mode,
                )
                raise BudgetDenied(
                    f"budget mode CONSERVING: role {role_name} is not allowed"
                )
            reservation_id = str(uuid4())
            created = now.isoformat()
            data.setdefault("reserved", []).append(
                {
                    "id": reservation_id,
                    "amount": str(estimate),
                    "purpose": purpose,
                    "role": role_name,
                    "provider": provider,
                    "model": model,
                    "ticker": ticker,
                    "created_at": created,
                }
            )
            self.ledger.save_month(data)
            return Reservation(
                reservation_id=reservation_id,
                estimated_cost=estimate,
                month=str(data["month"]),
                purpose=purpose,
                role=role_name,
                provider=provider,
                model=model,
                ticker=ticker,
                created_at=created,
            )

        return self.ledger.with_lock(_reserve)

    def record(
        self,
        reservation: Reservation,
        *,
        actual_cost: Decimal,
        input_tokens: int,
        output_tokens: int,
        timestamp: str | None = None,
        runtime_mode: str | None = None,
        blocked: bool = False,
        reason: str | None = None,
    ) -> UsageRecord:
        actual = quantize(max(ZERO, actual_cost))

        def _commit() -> UsageRecord:
            now = self.now()
            data = self._reconcile_locked(self.ledger.load_month(now=now))
            reserved_rows = list(data.get("reserved") or [])
            data["reserved"] = [row for row in reserved_rows if row.get("id") != reservation.reservation_id]
            spent = money(data.get("spent")) + actual
            if spent > self.cap:
                # Still record: the call already happened after a reservation. Cap remaining at 0.
                pass
            data["spent"] = str(quantize(spent))
            data["calls"] = int(data.get("calls") or 0) + (0 if blocked else 1)
            provider = reservation.provider
            model = reservation.model
            by_p = dict(data.get("by_provider") or {})
            by_m = dict(data.get("by_model") or {})
            by_p[provider] = str(quantize(money(by_p.get(provider)) + actual))
            by_m[model] = str(quantize(money(by_m.get(model)) + actual))
            data["by_provider"] = by_p
            data["by_model"] = by_m
            today = day_key(now)
            daily = dict(data.get("daily") or {})
            day_row = daily.get(today) if isinstance(daily.get(today), dict) else {"spent": str(daily.get(today) or 0), "calls": 0}
            day_row = {
                "spent": str(quantize(money(day_row.get("spent")) + actual)),
                "calls": int(day_row.get("calls") or 0) + (0 if blocked else 1),
            }
            daily[today] = day_row
            data["daily"] = daily
            self.ledger.save_month(data)
            record = UsageRecord(
                timestamp=timestamp or now.isoformat(),
                provider=provider,
                model=model,
                purpose=reservation.purpose,
                ticker=reservation.ticker,
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens),
                estimated_cost=reservation.estimated_cost,
                actual_cost=actual,
                cumulative_daily_cost=money(day_row["spent"]),
                cumulative_monthly_cost=money(data["spent"]),
                role=reservation.role,
                runtime_mode=runtime_mode,
                reservation_id=reservation.reservation_id,
                blocked=blocked,
                reason=reason,
            )
            self.ledger.append(record, now=now)
            return record

        return self.ledger.with_lock(_commit)

    def release(self, reservation: Reservation, *, reason: str = "released") -> None:
        def _release() -> None:
            data = self._reconcile_locked(self.ledger.load_month(now=self.now()))
            data["reserved"] = [row for row in list(data.get("reserved") or []) if row.get("id") != reservation.reservation_id]
            self.ledger.save_month(data)

        self.ledger.with_lock(_release)
        del reason

    def _reserved_total(self, data: dict[str, Any]) -> Decimal:
        total = ZERO
        for row in data.get("reserved") or []:
            total += money(row.get("amount"))
        return total

    def _reconcile_locked(self, data: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        kept = []
        for row in data.get("reserved") or []:
            created = str(row.get("created_at") or "")
            try:
                stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if (now - stamp).total_seconds() <= self.reservation_ttl:
                kept.append(row)
        data["reserved"] = kept
        data.setdefault("month", month_key(now))
        return data

    def _deny(
        self,
        *,
        purpose: str,
        role: str,
        provider: str,
        model: str,
        ticker: str | None,
        estimated: Decimal,
        spent: Decimal,
        reason: str,
        runtime_mode: str | None,
    ) -> None:
        now = self.now()
        today = day_key(now)
        data = self.ledger.load_month(now=now)
        daily = dict(data.get("daily") or {})
        day_row = daily.get(today) if isinstance(daily.get(today), dict) else {"spent": "0", "calls": 0}
        self.ledger.append(
            UsageRecord(
                timestamp=now.isoformat(),
                provider=provider,
                model=model,
                purpose=purpose,
                ticker=ticker,
                input_tokens=0,
                output_tokens=0,
                estimated_cost=estimated,
                actual_cost=ZERO,
                cumulative_daily_cost=money(day_row.get("spent")),
                cumulative_monthly_cost=spent,
                role=role,
                runtime_mode=runtime_mode,
                blocked=True,
                reason=reason,
            ),
            now=now,
        )
