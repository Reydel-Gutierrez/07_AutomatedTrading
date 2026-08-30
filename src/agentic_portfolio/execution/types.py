"""Execution Controller types. Mechanical paper OrderPlan only."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from agentic_portfolio.schemas import Decision, OpenOrder, PortfolioContext, ProposedAction, RiskGateResult


class OrderSide(str, Enum):
    BUY = "buy"
    SELL_TO_CLOSE = "sell_to_close"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(str, Enum):
    GFD = "gfd"
    GTC = "gtc"


class ExecutionStatus(str, Enum):
    PAPER_ONLY = "PAPER_ONLY"
    BLOCKED_FROM_LIVE = "BLOCKED_FROM_LIVE"


EXECUTABLE_ACTIONS = {Decision.BUY, Decision.ADD, Decision.REDUCE, Decision.SELL}
NON_EXECUTABLE_ACTIONS = {Decision.HOLD, Decision.WATCH, Decision.REJECT, Decision.NO_ACTION}
BUY_ACTIONS = {Decision.BUY, Decision.ADD}
SELL_ACTIONS = {Decision.REDUCE, Decision.SELL}

SIDE_FOR_ACTION = {
    Decision.BUY: OrderSide.BUY,
    Decision.ADD: OrderSide.BUY,
    Decision.REDUCE: OrderSide.SELL_TO_CLOSE,
    Decision.SELL: OrderSide.SELL_TO_CLOSE,
}


@dataclass
class QuoteSnapshot:
    """Current quote used for planning. Missing/stale quotes fail closed."""

    symbol: str
    last_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    spread_pct: float | None = None
    observed_at: str | None = None
    stale: bool = False
    source: str | None = None


@dataclass
class TradabilitySnapshot:
    symbol: str
    tradable: bool = False
    state: str | None = None
    observed_at: str | None = None
    source: str | None = None


@dataclass
class LiquidityCheck:
    ok: bool
    spread_pct: float | None = None
    notional: float | None = None
    adv: float | None = None
    notional_adv_fraction: float | None = None
    max_spread_pct: float | None = None
    max_notional_adv_fraction: float | None = None
    codes: list[str] = field(default_factory=list)


@dataclass
class SlippageCheck:
    ok: bool
    estimated_slippage_pct: float | None = None
    max_slippage_pct: float | None = None
    spread_pct: float | None = None
    codes: list[str] = field(default_factory=list)


@dataclass
class OrderPlan:
    order_plan_id: str
    symbol: str
    action: Decision
    quantity: float | None
    notional: float | None
    estimated_price: float | None
    estimated_position_quantity_after: float | None
    estimated_position_notional_after: float | None
    estimated_position_pct_after: float | None
    order_side: OrderSide | None
    order_type: OrderType
    time_in_force: TimeInForce
    slippage_check: SlippageCheck
    liquidity_check: LiquidityCheck
    source_decision_id: str | None
    thesis_id: str | None
    risk_evaluation_id: str | None
    execution_status: ExecutionStatus
    live_execution_blocked: bool = True
    blocked_reasons: list[str] = field(default_factory=list)
    created_at: str | None = None
    stop_orders_created: int = 0
    broker_submitted: bool = False
    live_trade_actions_allowed: bool = False
    auto_execution: bool = False


@dataclass
class SkippedAction:
    symbol: str
    action: Decision
    reason: str


@dataclass
class ExecutionResult:
    run_id: str
    plans: list[OrderPlan] = field(default_factory=list)
    skipped: list[SkippedAction] = field(default_factory=list)
    context: PortfolioContext | None = None
    execution_attempted: bool = False
    broker_orders_submitted: int = 0
    broker_stop_orders_created: int = 0
    live_execution_attempted: bool = False
    validation_errors: list[str] = field(default_factory=list)

    @property
    def paper_plans(self) -> list[OrderPlan]:
        return [p for p in self.plans if p.execution_status == ExecutionStatus.PAPER_ONLY]

    @property
    def blocked_plans(self) -> list[OrderPlan]:
        return [p for p in self.plans if p.execution_status == ExecutionStatus.BLOCKED_FROM_LIVE]


@dataclass
class ExecutionRequest:
    action: ProposedAction
    risk: RiskGateResult
    quote: QuoteSnapshot | None = None
    tradability: TradabilitySnapshot | None = None
    open_orders: list[OpenOrder] = field(default_factory=list)
    source_decision_id: str | None = None
    thesis_id: str | None = None
