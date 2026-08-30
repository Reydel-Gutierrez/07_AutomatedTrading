"""Paper fill + blotter types. Mechanical simulator only."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from agentic_portfolio.execution.types import (
    ExecutionStatus,
    LiquidityCheck,
    OrderPlan,
    OrderSide,
    OrderType,
    SlippageCheck,
    TimeInForce,
)
from agentic_portfolio.schemas import (
    Decision,
    PortfolioContext,
    ReconciliationFinding,
)


class FillStatus(str, Enum):
    FILLED = "FILLED"
    REJECTED = "REJECTED"


class FillSkipReason(str, Enum):
    NON_EXECUTABLE_ACTION = "NON_EXECUTABLE_ACTION"
    BLOCKED_FROM_LIVE = "BLOCKED_FROM_LIVE"
    NOT_PAPER_ONLY = "NOT_PAPER_ONLY"


@dataclass
class PaperLot:
    """One paper lot. FIFO consume on REDUCE/SELL. Not a broker tax lot."""

    lot_id: str
    symbol: str
    quantity: float
    cost_price: float
    opened_at: str
    thesis_id: str | None = None


@dataclass
class PaperFill:
    fill_id: str
    order_plan_id: str
    symbol: str
    side: OrderSide | None
    quantity: float | None
    fill_price: float | None
    filled_notional: float | None
    timestamp: str
    source_decision_id: str | None
    thesis_id: str | None
    status: FillStatus
    reject_reasons: list[str] = field(default_factory=list)


@dataclass
class BlotterEntry:
    blotter_id: str
    fill_id: str
    order_plan_id: str
    symbol: str
    action: Decision
    side: OrderSide | None
    quantity: float
    fill_price: float
    filled_notional: float
    timestamp: str
    cash_before: float
    cash_after: float
    quantity_before: float
    quantity_after: float
    average_cost_before: float | None
    average_cost_after: float | None
    realized_pnl: float
    position_closed: bool
    estimated_slippage_pct: float | None
    source_decision_id: str | None
    thesis_id: str | None
    sleeve: str | None
    status: FillStatus


@dataclass
class ReconciliationResult:
    ok: bool
    findings: list[ReconciliationFinding] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def codes(self) -> list[str]:
        return [f.code for f in self.findings]


@dataclass
class SkippedFill:
    symbol: str
    action: Decision
    reason: str
    order_plan_id: str | None = None


@dataclass
class BookDelta:
    symbol: str
    action: Decision
    side: OrderSide | None
    quantity: float
    fill_price: float
    filled_notional: float
    cash_before: float
    cash_after: float
    quantity_before: float
    quantity_after: float
    average_cost_before: float | None
    average_cost_after: float | None
    realized_pnl: float
    position_closed: bool
    nav_before: float
    nav_after: float
    thesis_id: str | None
    sleeve_before: str | None
    sleeve_after: str | None
    lots_after: list[PaperLot] = field(default_factory=list)


@dataclass
class PaperFillResult:
    run_id: str
    fills: list[PaperFill] = field(default_factory=list)
    blotter: list[BlotterEntry] = field(default_factory=list)
    skipped: list[SkippedFill] = field(default_factory=list)
    reconciliation: ReconciliationResult = field(default_factory=lambda: ReconciliationResult(ok=True))
    context_before: PortfolioContext | None = None
    context_after: PortfolioContext | None = None
    lots: list[PaperLot] = field(default_factory=list)
    paper_environment: bool = True
    live_book_untouched: bool = True
    execution_attempted: bool = False
    broker_orders_submitted: int = 0
    broker_stop_orders_created: int = 0
    live_execution_attempted: bool = False
    validation_errors: list[str] = field(default_factory=list)

    @property
    def filled(self) -> list[PaperFill]:
        return [f for f in self.fills if f.status == FillStatus.FILLED]

    @property
    def rejected(self) -> list[PaperFill]:
        return [f for f in self.fills if f.status == FillStatus.REJECTED]


def order_plan_from_dict(raw: dict) -> OrderPlan:
    """Rebuild an OrderPlan from persisted JSON. No broker calls."""
    slip = raw.get("slippage_check") or {}
    liq = raw.get("liquidity_check") or {}
    side = raw.get("order_side")
    action = Decision(raw["action"])
    status = ExecutionStatus(raw["execution_status"])
    return OrderPlan(
        order_plan_id=str(raw["order_plan_id"]),
        symbol=str(raw["symbol"]).upper(),
        action=action,
        quantity=raw.get("quantity"),
        notional=raw.get("notional"),
        estimated_price=raw.get("estimated_price"),
        estimated_position_quantity_after=raw.get("estimated_position_quantity_after"),
        estimated_position_notional_after=raw.get("estimated_position_notional_after"),
        estimated_position_pct_after=raw.get("estimated_position_pct_after"),
        order_side=OrderSide(side) if side else None,
        order_type=OrderType(raw.get("order_type") or "market"),
        time_in_force=TimeInForce(raw.get("time_in_force") or "gfd"),
        slippage_check=SlippageCheck(
            ok=bool(slip.get("ok", False)),
            estimated_slippage_pct=slip.get("estimated_slippage_pct"),
            max_slippage_pct=slip.get("max_slippage_pct"),
            spread_pct=slip.get("spread_pct"),
            codes=list(slip.get("codes") or []),
        ),
        liquidity_check=LiquidityCheck(
            ok=bool(liq.get("ok", False)),
            spread_pct=liq.get("spread_pct"),
            notional=liq.get("notional"),
            adv=liq.get("adv"),
            notional_adv_fraction=liq.get("notional_adv_fraction"),
            max_spread_pct=liq.get("max_spread_pct"),
            max_notional_adv_fraction=liq.get("max_notional_adv_fraction"),
            codes=list(liq.get("codes") or []),
        ),
        source_decision_id=raw.get("source_decision_id"),
        thesis_id=raw.get("thesis_id"),
        risk_evaluation_id=raw.get("risk_evaluation_id"),
        execution_status=status,
        live_execution_blocked=bool(raw.get("live_execution_blocked", True)),
        blocked_reasons=list(raw.get("blocked_reasons") or []),
        created_at=raw.get("created_at"),
        stop_orders_created=int(raw.get("stop_orders_created") or 0),
        broker_submitted=bool(raw.get("broker_submitted", False)),
        live_trade_actions_allowed=False,
        auto_execution=False,
    )
