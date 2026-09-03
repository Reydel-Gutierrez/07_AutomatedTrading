"""Deterministic live BUY/ADD/REDUCE/SELL sizing. FakeBroker only. No real broker."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_portfolio.live_approval import LiveApprovalEngine, LiveApprovalStatus, LiveApprovalStore
from agentic_portfolio.live_execution import ExecutionStore, FakeBroker, LiveOrderExecutor
from agentic_portfolio.live_execution.sizing import (
    MISSING_NAV,
    MISSING_QUOTE,
    MISSING_TARGET_ALLOCATION,
    NO_LIVE_POSITION,
    NON_EXECUTABLE_ACTION,
    OVERSELL_BLOCKED,
    REDUCE_BELOW_MINIMUM,
    REDUCE_TARGET_NOT_BELOW_CURRENT,
    ZERO_QUANTITY,
    cap_quantity,
    resolve_live_sizing,
)
from agentic_portfolio.live_execution.types import ExecutionIntentStatus
from agentic_portfolio.runtime import live_placement_enabled
from agentic_portfolio.schemas import Position
from tests.conftest import ctx

NOW = datetime(2026, 9, 3, 14, 30, tzinfo=timezone.utc)


def _enable_placement(monkeypatch):
    monkeypatch.setenv("AGENTIC_LIVE_ORDER_PLACEMENT", "true")
    assert live_placement_enabled() is True


def _msft(*, quantity: float, quote: float = 100.0, nav: float = 1000.0) -> Position:
    return Position(
        symbol="MSFT",
        quantity=quantity,
        market_value=quantity * quote,
        current_price=quote,
    )


def _book(nav: float, positions: list[Position] | None = None, *, cash: float | None = None, bp: float | None = None):
    positions = positions or []
    invested = sum(p.market_value for p in positions)
    cash = nav - invested if cash is None else cash
    return ctx(nav, positions, cash=cash, buying_power=bp if bp is not None else cash)


class _Live:
    def __init__(self, context):
        self.context = context

    def get(self):
        return self.context


def _stack(
    tmp_path: Path,
    broker: FakeBroker,
    live: _Live,
    *,
    quantity_decimal_places: int | None = None,
    min_order_notional_usd: float | None = None,
):
    store = ExecutionStore(tmp_path, runtime_mode="LIVE")
    executor = LiveOrderExecutor(
        store,
        broker,
        root=tmp_path,
        runtime_mode="LIVE",
        context_fn=live.get,
        regular_hours_fn=lambda: True,
        now_fn=lambda: NOW,
    )
    if quantity_decimal_places is not None:
        executor.cfg["quantity_decimal_places"] = quantity_decimal_places
    if min_order_notional_usd is not None:
        executor.cfg["min_order_notional_usd"] = min_order_notional_usd
    approvals = LiveApprovalEngine(
        LiveApprovalStore(tmp_path, runtime_mode="LIVE"),
        journal=tmp_path / "logs" / "approval.jsonl",
        now_fn=lambda: NOW,
        executor=executor,
    )
    return store, executor, approvals


def _approve(
    engine: LiveApprovalEngine,
    *,
    action: str,
    ticker: str = "MSFT",
    dollars: float | None = None,
    pct: float | None = None,
    quote: float = 100.0,
    nav: float = 1000.0,
    cash: float = 800.0,
    bp: float | None = None,
):
    item = engine.create(
        ticker=ticker,
        proposed_action=action,
        proposed_dollar_amount=dollars,
        proposed_allocation_pct=pct,
        current_quote=quote,
        risk_gate_result={"verdict": "PASS"},
        portfolio_impact={"nav": nav, "cash": cash, "buying_power": bp if bp is not None else cash},
        supporting_thesis="Test thesis.",
        reason="Test.",
    )
    return engine.record_decision(item.approval_id, LiveApprovalStatus.APPROVED, note="human")


def _qty(payload: dict) -> float:
    return float(payload.get("quantity"))


# --- helper unit tests (no broker) -------------------------------------------------

def test_helper_reduce_target_allocation():
    sized = resolve_live_sizing(
        action="REDUCE",
        symbol="MSFT",
        context=_book(1000, [_msft(quantity=2.0)]),
        quote=100.0,
        proposed_dollar_amount=200.0,
        proposed_allocation_pct=12.0,
    )
    assert sized.ok
    assert sized.action == "REDUCE"
    assert sized.side == "sell"
    assert sized.quantity == pytest.approx(0.8)
    assert sized.notional == pytest.approx(80.0)


def test_helper_reduce_target_equal_and_above_fail_closed():
    context = _book(1000, [_msft(quantity=2.0)])
    equal = resolve_live_sizing(
        action="REDUCE",
        symbol="MSFT",
        context=context,
        quote=100.0,
        proposed_dollar_amount=None,
        proposed_allocation_pct=20.0,
    )
    above = resolve_live_sizing(
        action="REDUCE",
        symbol="MSFT",
        context=context,
        quote=100.0,
        proposed_dollar_amount=None,
        proposed_allocation_pct=25.0,
    )
    assert equal.reason == REDUCE_TARGET_NOT_BELOW_CURRENT
    assert above.reason == REDUCE_TARGET_NOT_BELOW_CURRENT


def test_helper_reduce_fail_closed_missing_inputs():
    held = _book(1000, [_msft(quantity=2.0)])
    empty = _book(1000, [])
    assert (
        resolve_live_sizing(
            action="REDUCE", symbol="MSFT", context=held, quote=None, proposed_dollar_amount=None, proposed_allocation_pct=12.0
        ).reason
        == MISSING_QUOTE
    )
    assert (
        resolve_live_sizing(
            action="REDUCE",
            symbol="MSFT",
            context=replace(held, current_nav=0.0),
            quote=100.0,
            proposed_dollar_amount=None,
            proposed_allocation_pct=12.0,
        ).reason
        == MISSING_NAV
    )
    assert (
        resolve_live_sizing(
            action="REDUCE", symbol="MSFT", context=held, quote=100.0, proposed_dollar_amount=80.0, proposed_allocation_pct=None
        ).reason
        == MISSING_TARGET_ALLOCATION
    )
    assert (
        resolve_live_sizing(
            action="REDUCE", symbol="MSFT", context=empty, quote=100.0, proposed_dollar_amount=None, proposed_allocation_pct=12.0
        ).reason
        == NO_LIVE_POSITION
    )
    assert (
        resolve_live_sizing(
            action="SELL", symbol="MSFT", context=empty, quote=100.0, proposed_dollar_amount=200.0, proposed_allocation_pct=0.0
        ).reason
        == NO_LIVE_POSITION
    )


def test_helper_non_executable_cannot_size():
    context = _book(1000, [_msft(quantity=2.0)])
    for action in ("HOLD", "WATCH", "NO_ACTION"):
        sized = resolve_live_sizing(
            action=action,
            symbol="MSFT",
            context=context,
            quote=100.0,
            proposed_dollar_amount=80.0,
            proposed_allocation_pct=12.0,
        )
        assert sized.reason == NON_EXECUTABLE_ACTION
        assert sized.quantity is None


def test_helper_cap_quantity_never_oversells():
    assert cap_quantity(1.015, held=1.015, decimals=2) == pytest.approx(1.01)
    assert cap_quantity(1.015, held=1.015, decimals=2) <= 1.015
    assert cap_quantity(0.8, held=0.8, decimals=6) == pytest.approx(0.8)
    too_small = cap_quantity(0.0000004, held=1.0, decimals=6)
    assert too_small in {None, 0.0} or too_small == pytest.approx(0.0)


def test_helper_reduce_to_zero_is_full_exit():
    """REDUCE to 0% is allowed by the decision model and sizes a full liquidation."""
    sized = resolve_live_sizing(
        action="REDUCE",
        symbol="MSFT",
        context=_book(1000, [_msft(quantity=2.0)]),
        quote=100.0,
        proposed_dollar_amount=None,
        proposed_allocation_pct=0.0,
    )
    assert sized.ok
    assert sized.quantity == pytest.approx(2.0)
    assert sized.action == "REDUCE"
    assert sized.side == "sell"


# --- executor integration (FakeBroker) --------------------------------------------

def test_01_buy_fractional_dollar_sizing(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, cash=1000, bp=1000))
    broker = FakeBroker(nav=1000, cash=1000, buying_power=1000, quotes={"MSFT": 350.0})
    store, _executor, engine = _stack(tmp_path, broker, live)
    decided = _approve(engine, action="BUY", ticker="MSFT", dollars=100.0, pct=10.0, quote=350.0, nav=1000.0, cash=1000.0)
    assert decided.placed_order is True
    assert len(broker.place_calls) == 1
    assert broker.place_calls[0]["side"] == "buy"
    assert "dollar_amount" in broker.place_calls[0]
    assert float(broker.place_calls[0]["dollar_amount"]) == pytest.approx(100.0)
    assert "quantity" not in broker.place_calls[0]


def test_02_sell_full_position(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, [_msft(quantity=1.234567)]))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={"MSFT": 100.0})
    store, _executor, engine = _stack(tmp_path, broker, live)
    decided = _approve(engine, action="SELL", dollars=999.0, pct=0.0, quote=100.0)
    assert decided.placed_order is True
    qty = _qty(broker.place_calls[0])
    assert qty == pytest.approx(1.234567)
    assert qty <= 1.234567 + 1e-12
    assert broker.place_calls[0]["side"] == "sell"
    assert store.intents()[0].action == "SELL"
    assert store.intents()[0].side == "sell"


def test_03_reduce_partial_by_target_allocation(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, [_msft(quantity=2.0)]))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={"MSFT": 100.0})
    store, _executor, engine = _stack(tmp_path, broker, live)
    decided = _approve(engine, action="REDUCE", dollars=200.0, pct=12.0, quote=100.0)
    assert decided.placed_order is True
    assert len(broker.reviews) == 1
    assert len(broker.place_calls) == 1
    assert _qty(broker.reviews[0]) == pytest.approx(0.8)
    assert _qty(broker.place_calls[0]) == pytest.approx(0.8)
    assert _qty(broker.reviews[0]) == _qty(broker.place_calls[0])
    assert broker.place_calls[0]["side"] == "sell"
    intent = store.intents()[0]
    order = store.orders()[0]
    assert intent.action == "REDUCE"
    assert intent.side == "sell"
    assert intent.quantity == pytest.approx(0.8)
    assert intent.notional == pytest.approx(80.0)
    assert order.quantity == pytest.approx(0.8)
    assert order.notional == pytest.approx(80.0)
    assert order.side == "sell"


def test_04_reduce_fractional_expensive_stock(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, [_msft(quantity=0.5, quote=400.0)]))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={"MSFT": 400.0})
    store, _executor, engine = _stack(tmp_path, broker, live)
    decided = _approve(engine, action="REDUCE", dollars=200.0, pct=10.0, quote=400.0)
    assert decided.placed_order is True
    assert _qty(broker.place_calls[0]) == pytest.approx(0.25)


def test_05_reduce_target_equal_fails_closed(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, [_msft(quantity=2.0)]))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={"MSFT": 100.0})
    _store, _executor, engine = _stack(tmp_path, broker, live)
    decided = _approve(engine, action="REDUCE", dollars=0.0, pct=20.0, quote=100.0)
    assert decided.placed_order is False
    assert broker.place_calls == []
    assert broker.reviews == []
    assert REDUCE_TARGET_NOT_BELOW_CURRENT in (_store.intents()[0].block_reasons if _store.intents() else [])


def test_06_reduce_target_above_fails_closed(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, [_msft(quantity=2.0)]))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={"MSFT": 100.0})
    store, _executor, engine = _stack(tmp_path, broker, live)
    decided = _approve(engine, action="REDUCE", dollars=80.0, pct=25.0, quote=100.0)
    assert decided.placed_order is False
    assert broker.place_calls == []
    assert REDUCE_TARGET_NOT_BELOW_CURRENT in store.intents()[0].block_reasons


def test_07_reduce_missing_nav_fails_closed(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(replace(_book(1000, [_msft(quantity=2.0)]), current_nav=0.0))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={"MSFT": 100.0})
    store, executor, engine = _stack(tmp_path, broker, live)
    item = engine.create(
        ticker="MSFT",
        proposed_action="REDUCE",
        proposed_dollar_amount=80.0,
        proposed_allocation_pct=12.0,
        current_quote=100.0,
        risk_gate_result={"verdict": "PASS"},
        portfolio_impact={"cash": 800, "buying_power": 800},
    )
    item.status = LiveApprovalStatus.APPROVED
    engine.store.save(item)
    outcome = executor.execute_approved(item)
    assert outcome.placed is False
    assert broker.place_calls == []
    assert MISSING_NAV in outcome.reasons or "NAV_CHANGED_MATERIALLY" in outcome.reasons


def test_08_reduce_missing_quote_fails_closed(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, [_msft(quantity=2.0)]))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={})
    store, executor, engine = _stack(tmp_path, broker, live)
    item = engine.create(
        ticker="MSFT",
        proposed_action="REDUCE",
        proposed_dollar_amount=80.0,
        proposed_allocation_pct=12.0,
        current_quote=100.0,
        risk_gate_result={"verdict": "PASS"},
        portfolio_impact={"nav": 1000, "cash": 800, "buying_power": 800},
    )
    item.status = LiveApprovalStatus.APPROVED
    engine.store.save(item)
    outcome = executor.execute_approved(item)
    assert outcome.placed is False
    assert broker.place_calls == []
    assert "QUOTE_UNAVAILABLE" in outcome.reasons or MISSING_QUOTE in outcome.reasons


def test_09_reduce_no_position_fails_closed(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, []))
    broker = FakeBroker(nav=1000, cash=1000, buying_power=1000, quotes={"MSFT": 100.0})
    store, _executor, engine = _stack(tmp_path, broker, live)
    decided = _approve(engine, action="REDUCE", dollars=80.0, pct=12.0, quote=100.0, cash=1000.0)
    assert decided.placed_order is False
    assert broker.place_calls == []
    reasons = store.intents()[0].block_reasons if store.intents() else []
    assert NO_LIVE_POSITION in reasons or "POSITION_CHANGED" in reasons


def test_10_sell_no_position_fails_closed(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, []))
    broker = FakeBroker(nav=1000, cash=1000, buying_power=1000, quotes={"MSFT": 100.0})
    store, _executor, engine = _stack(tmp_path, broker, live)
    decided = _approve(engine, action="SELL", dollars=200.0, pct=0.0, quote=100.0, cash=1000.0)
    assert decided.placed_order is False
    assert broker.place_calls == []
    reasons = store.intents()[0].block_reasons if store.intents() else []
    assert NO_LIVE_POSITION in reasons or "POSITION_CHANGED" in reasons


def test_11_reduce_cannot_oversell_rounding(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    held = 1.015
    live = _Live(_book(1000, [_msft(quantity=held, quote=100.0)]))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={"MSFT": 100.0})
    store, _executor, engine = _stack(tmp_path, broker, live, quantity_decimal_places=2)
    decided = _approve(engine, action="REDUCE", dollars=held * 100.0, pct=0.0, quote=100.0)
    assert decided.placed_order is True
    qty = _qty(broker.place_calls[0])
    assert qty <= held + 1e-12
    assert qty == pytest.approx(1.01)


def test_12_sell_cannot_oversell_live_quantity(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    held = 1.015
    live = _Live(_book(1000, [_msft(quantity=held)]))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={"MSFT": 100.0})
    store, _executor, engine = _stack(tmp_path, broker, live, quantity_decimal_places=2)
    decided = _approve(engine, action="SELL", dollars=999.0, pct=0.0, quote=100.0)
    assert decided.placed_order is True
    qty = _qty(broker.place_calls[0])
    assert qty <= held + 1e-12
    assert qty == pytest.approx(1.01)


def test_13_reduce_uses_current_live_quantity_after_shrink(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, [_msft(quantity=1.2)]))
    broker = FakeBroker(nav=1000, cash=880, buying_power=880, quotes={"MSFT": 100.0})
    store, _executor, engine = _stack(tmp_path, broker, live)
    # Approval-time size assumed 2 shares / $80 sell. Live holding is 1.2.
    # Target 5% of $1000 = $50 → sell $70 = 0.7 from current 1.2, not 1.5 from stale 2.0.
    decided = _approve(engine, action="REDUCE", dollars=80.0, pct=5.0, quote=100.0, cash=880.0)
    assert decided.placed_order is True
    assert _qty(broker.place_calls[0]) == pytest.approx(0.7)
    assert _qty(broker.place_calls[0]) != pytest.approx(1.5)
    assert _qty(broker.place_calls[0]) != pytest.approx(0.8)


def test_14_reduce_uses_current_live_quantity_after_increase(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, [_msft(quantity=3.0)]))
    broker = FakeBroker(nav=1000, cash=700, buying_power=700, quotes={"MSFT": 100.0})
    store, _executor, engine = _stack(tmp_path, broker, live)
    decided = _approve(engine, action="REDUCE", dollars=80.0, pct=12.0, quote=100.0, cash=700.0)
    assert decided.placed_order is True
    # 3 shares = $300, target $120, sell $180 = 1.8. Stale 2-share math would be 0.8.
    assert _qty(broker.place_calls[0]) == pytest.approx(1.8)


def test_15_quantity_decimal_places(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    held = 1.234567
    live = _Live(_book(1000, [_msft(quantity=held)]))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={"MSFT": 100.0})
    store, _executor, engine = _stack(tmp_path, broker, live, quantity_decimal_places=4)
    decided = _approve(engine, action="SELL", dollars=held * 100.0, pct=0.0, quote=100.0)
    assert decided.placed_order is True
    qty = _qty(broker.place_calls[0])
    assert qty == pytest.approx(1.2345)
    assert qty <= held
    text = broker.place_calls[0]["quantity"]
    assert len(text.split(".")[-1]) <= 4


def test_16_very_small_reduce_fails_closed(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, [_msft(quantity=2.0)]))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={"MSFT": 100.0})
    store, _executor, engine = _stack(tmp_path, broker, live, min_order_notional_usd=1.0)
    # 20% → 19.95% is a $0.50 reduction, below $1 minimum. Must not dump the whole position.
    decided = _approve(engine, action="REDUCE", dollars=0.5, pct=19.95, quote=100.0)
    assert decided.placed_order is False
    assert broker.place_calls == []
    assert REDUCE_BELOW_MINIMUM in store.intents()[0].block_reasons
    assert store.intents()[0].action == "REDUCE"


def test_17_reduce_target_zero_full_exit(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, [_msft(quantity=2.0)]))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={"MSFT": 100.0})
    store, _executor, engine = _stack(tmp_path, broker, live)
    decided = _approve(engine, action="REDUCE", dollars=200.0, pct=0.0, quote=100.0)
    assert decided.placed_order is True
    assert _qty(broker.place_calls[0]) == pytest.approx(2.0)
    assert store.intents()[0].action == "REDUCE"
    assert store.intents()[0].side == "sell"


def test_18_sell_ignores_proposed_dollar_amount(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, [_msft(quantity=2.0)]))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={"MSFT": 100.0})
    store, _executor, engine = _stack(tmp_path, broker, live)
    decided = _approve(engine, action="SELL", dollars=10.0, pct=12.0, quote=100.0)
    assert decided.placed_order is True
    assert _qty(broker.place_calls[0]) == pytest.approx(2.0)


def test_19_hold_watch_no_action_cannot_sell(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, [_msft(quantity=2.0)]))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={"MSFT": 100.0})
    store, executor, engine = _stack(tmp_path, broker, live)
    for action in ("HOLD", "WATCH", "NO_ACTION"):
        item = engine.create(
            ticker="MSFT",
            proposed_action=action,
            proposed_dollar_amount=80.0,
            proposed_allocation_pct=12.0,
            current_quote=100.0,
            risk_gate_result={"verdict": "PASS"},
            portfolio_impact={"nav": 1000, "cash": 800, "buying_power": 800},
        )
        item.status = LiveApprovalStatus.APPROVED
        engine.store.save(item)
        outcome = executor.execute_approved(item)
        assert outcome.placed is False
        assert NON_EXECUTABLE_ACTION in outcome.reasons
    assert broker.place_calls == []
    assert broker.reviews == []


def test_20_reduce_idempotent_same_approval(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, [_msft(quantity=2.0)]))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={"MSFT": 100.0})
    store, executor, engine = _stack(tmp_path, broker, live)
    first = _approve(engine, action="REDUCE", dollars=80.0, pct=12.0, quote=100.0)
    second = engine.record_decision(first.approval_id, LiveApprovalStatus.APPROVED, note="again")
    retry = executor.execute_approved(engine.store.get(first.approval_id))
    assert first.placed_order is True
    assert second.approval_id == first.approval_id
    assert retry.placed is False
    assert "already_submitted" in retry.reasons
    assert len(broker.place_calls) == 1
    assert len(store.intents()) == 1
    assert len(store.orders()) == 1


def test_21_reduce_stale_quote_still_blocks(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, [_msft(quantity=2.0)]))
    broker = FakeBroker(nav=1000, cash=800, buying_power=800, quotes={"MSFT": 150.0})
    store, _executor, engine = _stack(tmp_path, broker, live)
    decided = _approve(engine, action="REDUCE", dollars=80.0, pct=12.0, quote=100.0)
    assert decided.placed_order is False
    assert broker.place_calls == []
    assert any("QUOTE_MOVED" in r for r in store.intents()[0].block_reasons)


def test_reduce_does_not_require_buying_power(monkeypatch, tmp_path):
    """REDUCE must not be blocked because sell notional exceeds cash/buying power."""
    _enable_placement(monkeypatch)
    live = _Live(_book(1000, [_msft(quantity=2.0)], cash=0.0, bp=0.0))
    broker = FakeBroker(nav=1000, cash=0, buying_power=0, quotes={"MSFT": 100.0})
    store, _executor, engine = _stack(tmp_path, broker, live)
    decided = _approve(engine, action="REDUCE", dollars=80.0, pct=12.0, quote=100.0, cash=0.0, bp=0.0)
    assert decided.placed_order is True
    assert _qty(broker.place_calls[0]) == pytest.approx(0.8)
    assert store.intents()[0].action == "REDUCE"


def test_oversell_blocked_constant_available():
    assert OVERSELL_BLOCKED == "oversell_blocked"
    assert ZERO_QUANTITY == "zero_quantity"
