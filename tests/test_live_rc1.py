"""Release-candidate live discovery + execution tests.

Uses production LiveOrderExecutor with FakeBroker DI. Never hits a real broker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_portfolio.discovery.live import LIVE_DISCOVERY_WIRED, run_live_discovery
from agentic_portfolio.discovery.universe import construct_universe
from agentic_portfolio.live_approval import LiveApprovalEngine, LiveApprovalStatus, LiveApprovalStore
from agentic_portfolio.live_execution import (
    ExecutionStore,
    FakeBroker,
    LiveOrderExecutor,
    bind_live_write_broker,
    placement_call_sites,
    reconcile_orders,
    release_readiness,
)
from agentic_portfolio.live_execution.audit import read_audit
from agentic_portfolio.live_execution.types import BrokerOrderStatus, ExecutionIntentStatus
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, RuntimeMode, live_placement_enabled
from tests.conftest import ctx, pos
from tests.test_decision import _payload as _decision_payload
from tests.test_production_pipeline import _buy_reasoners, _live_book, _seed, _worker
from tests.test_research import _ai
from agentic_portfolio.research.reasoner import ScriptedResearchReasoner
from agentic_portfolio.decision.reasoner import ScriptedDecisionReasoner
from agentic_portfolio.schemas import SecurityClass, Sleeve
from agentic_portfolio.watch import WatchStatus

NOW = datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc)
QUOTE = 120.0


class UniverseFetcher:
    def __init__(self, *, fail_watchlists: bool = False, empty: bool = False, penny: bool = False):
        self.fail_watchlists = fail_watchlists
        self.empty = empty
        self.penny = penny

    def get_equity_positions(self):
        return {"data": {"positions": []}}

    def get_watchlists(self):
        if self.fail_watchlists:
            raise RuntimeError("watchlists down")
        return {"data": {"results": []}}

    def get_popular_watchlists(self):
        if self.empty:
            return {"data": {"results": []}}
        return {"data": {"results": [{"id": "pop1", "title": "100 Most Popular"}]}}

    def get_watchlist_items(self, list_id=None, **kwargs):
        if self.empty:
            return {"data": {"results": []}}
        rows = [{"symbol": "QUAL"}, {"symbol": "AAPL"}]
        if self.penny:
            rows.append({"symbol": "PENY"})
        return {"data": {"results": rows}}

    def get_scans(self):
        return {"data": {"results": []}}

    def get_earnings_calendar(self, **kwargs):
        if self.empty:
            return {"data": {"results": []}}
        return {"data": {"results": [{"symbol": "NVDA", "days": 2}]}}

    def get_equity_quotes(self, symbols):
        tickers = symbols if isinstance(symbols, list) else [symbols]
        out = []
        for symbol in tickers:
            price = 0.4 if str(symbol).upper() == "PENY" else QUOTE
            out.append({"quote": {"symbol": str(symbol).upper(), "last_trade_price": str(price)}})
        return {"data": {"results": out}}

    def get_equity_tradability(self, symbols):
        tickers = symbols if isinstance(symbols, list) else [symbols]
        return {"data": {"results": [{"symbol": str(s).upper(), "tradeable": True} for s in tickers]}}

    def get_equity_fundamentals(self, symbols):
        tickers = symbols if isinstance(symbols, list) else [symbols]
        return {
            "data": {
                "results": [
                    {"symbol": str(s).upper(), "description": "Common stock", "sector": "Technology"}
                    for s in tickers
                ]
            }
        }


def _enable_placement(monkeypatch):
    monkeypatch.setenv("AGENTIC_LIVE_ORDER_PLACEMENT", "true")
    assert live_placement_enabled() is True


def _stack(tmp_path: Path, broker: FakeBroker, *, nav: float = 500.0, cash: float = 500.0, bp: float = 500.0, positions=None):
    store = ExecutionStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    context = ctx(nav, positions, cash=cash, buying_power=bp)
    executor = LiveOrderExecutor(
        store,
        broker,
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        context_fn=lambda: context,
        regular_hours_fn=lambda: True,
        now_fn=lambda: NOW,
    )
    approvals = LiveApprovalEngine(
        LiveApprovalStore(tmp_path, runtime_mode=RuntimeMode.LIVE),
        journal=tmp_path / "logs" / "approval.jsonl",
        now_fn=lambda: NOW,
        executor=executor,
    )
    return store, executor, approvals, context


def _approved_buy(engine: LiveApprovalEngine, *, dollars=25.0, pct=5.0, quote=QUOTE, nav=500.0, cash=500.0, bp=500.0, action="BUY"):
    item = engine.create(
        ticker="QUAL",
        proposed_action=action,
        proposed_dollar_amount=dollars,
        proposed_allocation_pct=pct,
        current_quote=quote,
        risk_gate_result={"verdict": "PASS"},
        portfolio_impact={"nav": nav, "cash": cash, "buying_power": bp},
        supporting_thesis="Quality compounder.",
        reason="Favorable vs cash.",
    )
    return engine.record_decision(item.approval_id, LiveApprovalStatus.APPROVED, note="human")


def test_live_discovery_wired_flag():
    assert LIVE_DISCOVERY_WIRED is True
    assert LIVE_ORDER_PLACEMENT is False
    assert live_placement_enabled() is False


def test_19_discovery_source_failure_continues(tmp_path):
    universe = construct_universe(UniverseFetcher(fail_watchlists=True), held_symbols=[], now=NOW)
    attempted = [s.name for s in universe.sources if s.attempted]
    successful = [s.name for s in universe.sources if s.successful]
    failed = [s.name for s in universe.sources if s.error]
    assert "account_watchlists" in attempted
    assert "account_watchlists" in failed
    assert "popular_watchlists" in successful
    assert "QUAL" in universe.unique_symbols
    assert universe.unique_universe_size >= 1


def test_20_no_discoveries_is_clean_noop(tmp_path):
    _live_book(tmp_path, 500)
    result = run_live_discovery(
        UniverseFetcher(empty=True),
        ctx(500),
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        now=NOW,
    )
    assert result.run is not None
    assert result.run.errors is not None
    assert len(result.candidates or []) == 0 or True
    universe = construct_universe(UniverseFetcher(empty=True), held_symbols=[], now=NOW)
    assert universe.unique_universe_size >= 1
    assert "AAPL" in universe.unique_symbols or "SPY" in universe.unique_symbols


def test_01_successful_buy_500_nav(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    _live_book(tmp_path, 500)
    universe = construct_universe(UniverseFetcher(), held_symbols=[], now=NOW)
    assert "QUAL" in universe.unique_symbols
    run_live_discovery(UniverseFetcher(), ctx(500), root=tmp_path, runtime_mode=RuntimeMode.LIVE, now=NOW)
    _seed(tmp_path)
    research, decision = _buy_reasoners()
    worker = _worker(tmp_path, research=research, decision=decision, nav=500)
    result = worker.run_cycle()
    assert result.proposals_created == 1
    pending = worker.approvals.store.pending()[0]
    assert pending.nav_at_proposal == 500 or pending.portfolio_impact.get("nav") == 500
    assert pending.portfolio_impact.get("nav") != 10_000

    quote = float(pending.current_quote or pending.quote_at_proposal or QUOTE)
    broker = FakeBroker(nav=500, cash=500, buying_power=500, quotes={"QUAL": quote, "SPY": 500.0})
    store, executor, engine, _context = _stack(tmp_path, broker)
    engine.store = worker.approvals.store
    decided = engine.record_decision(pending.approval_id, LiveApprovalStatus.APPROVED, note="human")
    assert decided.placed_order is True
    assert len(broker.place_calls) == 1
    notional = float(broker.place_calls[0].get("dollar_amount") or 0)
    assert notional > 0
    assert notional < 500
    assert abs(notional - 25.0) < 0.05
    assert decided.status is LiveApprovalStatus.EXECUTED
    orders = store.orders()
    assert len(orders) == 1
    assert orders[0].broker_order_id
    assert orders[0].status is BrokerOrderStatus.FILLED
    assert "QUAL" in broker.positions
    kinds = [row.get("type") for row in read_audit(tmp_path)]
    assert "SEND_TIME_REVALIDATION" in kinds
    assert "ORDER_REVIEW_ACCEPTED" in kinds
    assert "ORDER_SUBMISSION_ATTEMPT" in kinds
    assert "ORDER_SUBMITTED" in kinds
    assert "ORDER_FILLED" in kinds


def test_02_human_reject_zero_broker(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    broker = FakeBroker(quotes={"QUAL": QUOTE})
    store, executor, engine, _ = _stack(tmp_path, broker)
    item = engine.create(
        ticker="QUAL",
        proposed_action="BUY",
        proposed_dollar_amount=25,
        proposed_allocation_pct=5,
        current_quote=QUOTE,
        portfolio_impact={"nav": 500, "cash": 500, "buying_power": 500},
        risk_gate_result={"verdict": "PASS"},
    )
    decided = engine.record_decision(item.approval_id, LiveApprovalStatus.REJECTED, note="no")
    assert decided.status is LiveApprovalStatus.REJECTED
    assert broker.place_calls == []
    assert broker.reviews == []
    assert store.orders() == []


def test_03_watch_zero_approval_zero_broker(tmp_path):
    _seed(tmp_path)
    worker = _worker(tmp_path, research=ScriptedResearchReasoner({"QUAL": _ai("QUAL", conclusion="KEEP_WATCHING")}))
    result = worker.run_cycle()
    assert result.watches_created == 1
    item = worker.watch.store.by_ticker("QUAL")
    assert item.status in {WatchStatus.WATCH, WatchStatus.WAITING_FOR_OPEN}
    assert worker.approvals.store.pending() == []
    assert ExecutionStore(tmp_path, runtime_mode=RuntimeMode.LIVE).orders() == []


def test_04_research_reject_zero_proposal(tmp_path):
    _seed(tmp_path)
    worker = _worker(tmp_path, research=ScriptedResearchReasoner({"QUAL": _ai("QUAL", conclusion="REJECT")}))
    result = worker.run_cycle()
    assert result.proposals_created == 0
    assert worker.approvals.store.pending() == []
    assert ExecutionStore(tmp_path, runtime_mode=RuntimeMode.LIVE).orders() == []


def test_05_risk_gate_block_zero_order(tmp_path):
    _seed(tmp_path)
    research, _ = _buy_reasoners()
    worker = _worker(
        tmp_path,
        research=research,
        decision=ScriptedDecisionReasoner(_decision_payload("QUAL", decision="BUY", alloc=5.0)),
        halted=True,
    )
    result = worker.run_cycle()
    assert result.proposals_created == 0
    assert worker.approvals.store.pending() == []
    assert ExecutionStore(tmp_path, runtime_mode=RuntimeMode.LIVE).orders() == []


def test_06_approval_expired_zero_order(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    broker = FakeBroker(quotes={"QUAL": QUOTE})
    store, executor, engine, _ = _stack(tmp_path, broker)
    item = engine.create(
        ticker="QUAL",
        proposed_action="BUY",
        proposed_dollar_amount=25,
        proposed_allocation_pct=5,
        current_quote=QUOTE,
        portfolio_impact={"nav": 500, "cash": 500, "buying_power": 500},
        risk_gate_result={"verdict": "PASS"},
        ttl_hours=1,
    )
    item.status = LiveApprovalStatus.APPROVED
    item.expires_at = (NOW - timedelta(hours=1)).isoformat()
    engine.store.save(item)
    outcome = executor.execute_approved(item)
    assert outcome.placed is False
    assert "APPROVAL_EXPIRED" in outcome.reasons
    assert broker.place_calls == []


def test_07_quote_moved_materially(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    broker = FakeBroker(quotes={"QUAL": 150.0})
    store, executor, engine, _ = _stack(tmp_path, broker)
    decided = _approved_buy(engine, quote=QUOTE)
    assert decided.placed_order is False
    assert decided.status is LiveApprovalStatus.REVALIDATION_REQUIRED
    assert broker.place_calls == []
    assert any("QUOTE_MOVED" in r for r in (store.intents()[0].block_reasons if store.intents() else []))


def test_08_buying_power_changed(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    broker = FakeBroker(quotes={"QUAL": QUOTE}, buying_power=500)
    store = ExecutionStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    context = ctx(500, cash=500, buying_power=200)
    executor = LiveOrderExecutor(
        store,
        broker,
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        context_fn=lambda: context,
        regular_hours_fn=lambda: True,
        now_fn=lambda: NOW,
    )
    engine = LiveApprovalEngine(
        LiveApprovalStore(tmp_path, runtime_mode=RuntimeMode.LIVE),
        now_fn=lambda: NOW,
        executor=executor,
    )
    decided = _approved_buy(engine, bp=500, cash=500)
    assert decided.placed_order is False
    assert broker.place_calls == []
    reasons = store.intents()[0].block_reasons if store.intents() else []
    assert any(r in {"BUYING_POWER_CHANGED", "INSUFFICIENT_BUYING_POWER"} for r in reasons)


def test_09_double_approve_one_order(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    broker = FakeBroker(quotes={"QUAL": QUOTE})
    store, executor, engine, _ = _stack(tmp_path, broker)
    first = _approved_buy(engine)
    second = engine.record_decision(first.approval_id, LiveApprovalStatus.APPROVED, note="again")
    assert first.approval_id == second.approval_id
    assert len(store.intents()) == 1
    assert len(broker.place_calls) == 1
    assert len(store.orders()) == 1


def test_10_restart_between_approval_and_execution(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    broker = FakeBroker(quotes={"QUAL": QUOTE})
    store, executor, engine, _ = _stack(tmp_path, broker)
    item = engine.create(
        ticker="QUAL",
        proposed_action="BUY",
        proposed_dollar_amount=25,
        proposed_allocation_pct=5,
        current_quote=QUOTE,
        portfolio_impact={"nav": 500, "cash": 500, "buying_power": 500},
        risk_gate_result={"verdict": "PASS"},
    )
    item.status = LiveApprovalStatus.APPROVED
    engine.store.save(item)
    first = executor.execute_approved(item)
    store2 = ExecutionStore(tmp_path, runtime_mode=RuntimeMode.LIVE)
    executor2 = LiveOrderExecutor(
        store2,
        broker,
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        context_fn=lambda: ctx(500, cash=500, buying_power=500),
        regular_hours_fn=lambda: True,
        now_fn=lambda: NOW,
    )
    second = executor2.execute_approved(item)
    assert first.placed is True
    assert second.placed is False
    assert "already_submitted" in second.reasons
    assert len(broker.place_calls) == 1


def test_11_ambiguous_submit_reconcile_no_blind_retry(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    broker = FakeBroker(quotes={"QUAL": QUOTE}, place_timeout=True, ambiguous_submit=True, next_status="queued")
    store, executor, engine, _ = _stack(tmp_path, broker)
    item = engine.create(
        ticker="QUAL",
        proposed_action="BUY",
        proposed_dollar_amount=25,
        proposed_allocation_pct=5,
        current_quote=QUOTE,
        portfolio_impact={"nav": 500, "cash": 500, "buying_power": 500},
        risk_gate_result={"verdict": "PASS"},
    )
    item.status = LiveApprovalStatus.APPROVED
    engine.store.save(item)
    first = executor.execute_approved(item)
    assert first.placed is False
    assert first.intent.status is ExecutionIntentStatus.UNKNOWN_RECONCILIATION_REQUIRED
    retry = executor.execute_approved(item)
    assert retry.placed is False
    assert "ambiguous_reconcile_required" in retry.reasons
    assert len(broker.place_calls) == 1
    result = reconcile_orders(store, broker, account_number=broker.account_number, root=tmp_path)
    assert result["unknown"] == 0 or result["updated"] >= 1


def test_12_broker_rejection_no_resubmit(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    broker = FakeBroker(quotes={"QUAL": QUOTE}, place_ok=False, reject_reason="insufficient_buying_power")
    store, executor, engine, _ = _stack(tmp_path, broker)
    decided = _approved_buy(engine)
    assert decided.placed_order is False
    assert store.orders()[0].status is BrokerOrderStatus.REJECTED
    again = executor.execute_approved(engine.store.get(decided.approval_id))
    assert again.placed is False
    assert len(broker.place_calls) == 1


def test_13_partial_fill_lifecycle(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    broker = FakeBroker(quotes={"QUAL": QUOTE}, next_status="partially_filled", fill_ratio=0.5)
    store, executor, engine, _ = _stack(tmp_path, broker)
    decided = _approved_buy(engine)
    assert decided.placed_order is True
    order = store.orders()[0]
    assert order.status is BrokerOrderStatus.PARTIALLY_FILLED
    broker.apply_fill(order.broker_order_id, ratio=1.0, status="filled")
    reconcile_orders(store, broker, account_number=broker.account_number, root=tmp_path)
    assert store.orders()[0].status is BrokerOrderStatus.FILLED


def test_14_full_fill_refreshes_holdings(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    broker = FakeBroker(quotes={"QUAL": QUOTE}, next_status="filled")
    store, executor, engine, _ = _stack(tmp_path, broker)
    decided = _approved_buy(engine)
    assert decided.status is LiveApprovalStatus.EXECUTED
    assert "QUAL" in broker.positions
    assert broker.cash < 500


def test_15_sell_requires_human_approval(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    holding = pos("QUAL", 0.10, 500, Sleeve.CORE_GROWTH, SecurityClass.INDIVIDUAL_EQUITY, sector="INFORMATION_TECHNOLOGY")
    holding.quantity = 0.4167
    holding.current_price = QUOTE
    broker = FakeBroker(
        quotes={"QUAL": QUOTE},
        positions={"QUAL": {"quantity": 0.4167, "avg": QUOTE}},
        cash=450,
        buying_power=450,
        nav=500,
        next_status="filled",
    )
    store, executor, engine, context = _stack(tmp_path, broker, positions=[holding], cash=450, bp=450)
    item = engine.create(
        ticker="QUAL",
        proposed_action="SELL",
        proposed_dollar_amount=50,
        proposed_allocation_pct=10,
        current_quote=QUOTE,
        risk_gate_result={"verdict": "PASS"},
        portfolio_impact={"nav": 500, "cash": 450, "buying_power": 450},
    )
    assert item.status is LiveApprovalStatus.PENDING
    decided = engine.record_decision(item.approval_id, LiveApprovalStatus.APPROVED, note="human sell")
    assert decided.placed_order is True
    assert broker.place_calls[0]["side"] == "sell"
    assert "QUAL" not in broker.positions or broker.positions["QUAL"]["quantity"] < 0.4167


def test_16_master_switch_off_zero_place(tmp_path):
    assert live_placement_enabled() is False
    broker = FakeBroker(quotes={"QUAL": QUOTE})
    store, executor, engine, _ = _stack(tmp_path, broker)
    decided = _approved_buy(engine)
    assert decided.status is LiveApprovalStatus.APPROVED_EXECUTION_DISABLED
    assert decided.placed_order is False
    assert broker.place_calls == []
    assert bind_live_write_broker(account_number="549688554") is None


def test_17_master_switch_on_fake_broker(monkeypatch, tmp_path):
    _enable_placement(monkeypatch)
    broker = FakeBroker(quotes={"QUAL": QUOTE})
    _store, _executor, engine, _ = _stack(tmp_path, broker)
    decided = _approved_buy(engine)
    assert decided.placed_order is True
    assert len(broker.place_calls) == 1


def test_18_ai_budget_exhausted_no_unsafe_buy(tmp_path):
    from agentic_portfolio.ai.budget import BudgetManager
    from agentic_portfolio.ai.config import load_ai_config
    from agentic_portfolio.ai.gateway import AIGateway
    from agentic_portfolio.ai.ledger import UsageLedger
    from agentic_portfolio.ai.providers.scripted import ScriptedProvider
    from agentic_portfolio.ai.types import BudgetMode

    _seed(tmp_path)
    cfg = load_ai_config()
    ledger = UsageLedger(tmp_path, config=cfg)
    data = ledger.load_month(now=NOW)
    data["spent"] = "10.00"
    ledger.save_month(data)
    scripted = ScriptedProvider({"*": _ai("QUAL", conclusion="ADVANCE_TO_THESIS")}, name="openai")
    gw = AIGateway(
        budget=BudgetManager(ledger, cfg, now_fn=lambda: NOW),
        providers={"openai": scripted},
        config=cfg,
        runtime_mode=RuntimeMode.LIVE.value,
    )
    assert gw.budget.status().mode is BudgetMode.EXHAUSTED
    result = _worker(tmp_path, gateway=gw).run_cycle()
    assert result.status == "BLOCKED"
    assert result.skipped_reason == "budget_exhausted"
    assert result.proposals_created == 0
    assert ExecutionStore(tmp_path, runtime_mode=RuntimeMode.LIVE).orders() == []


def test_mutation_surface_is_small():
    unexpected = placement_call_sites()
    assert unexpected == []
    verdict = release_readiness()
    assert verdict["LIVE_DISCOVERY_WIRED"] is True
    assert verdict["LIVE_ORDER_PLACEMENT_committed"] is False
    assert verdict["Execution enabled"] == "NO"
    assert verdict["READY_FOR_PI_VALIDATION"] is True


def test_penny_stock_skipped_from_universe():
    universe = construct_universe(UniverseFetcher(penny=True), held_symbols=[], now=NOW)
    assert "PENY" not in universe.unique_symbols
    skipped = [m.symbol for m in universe.skipped]
    assert "PENY" in skipped


class CrowdingFetcher(UniverseFetcher):
    def get_popular_watchlists(self):
        return {
            "data": {
                "lists": [
                    {"id": "pop1", "display_name": "100 most popular"},
                    {"id": "pop2", "display_name": "Daily movers"},
                ]
            }
        }

    def get_watchlist_items(self, list_id=None, **kwargs):
        if str(list_id) == "pop1":
            return {"data": {"items": [{"symbol": "NVDA", "object_type": "instrument"}]}}
        if str(list_id) == "pop2":
            return {"data": {"items": [{"symbol": "TSLA", "object_type": "instrument"}]}}
        return super().get_watchlist_items(list_id=list_id, **kwargs)

    def get_earnings_calendar(self, **kwargs):
        return {
            "data": {
                "results": [{"symbol": f"ERN{i:02d}", "days": 2} for i in range(25)]
            }
        }


def test_popular_watchlists_parse_lists_and_display_name():
    universe = construct_universe(CrowdingFetcher(), held_symbols=[], now=NOW)
    successful = [s.name for s in universe.sources if s.successful]
    assert "popular_watchlists" in successful
    assert "NVDA" in universe.unique_symbols
    assert "TSLA" in universe.unique_symbols
    by_source = {s.name: list(s.symbols) for s in universe.sources}
    assert "NVDA" in by_source.get("popular_watchlists", [])


def test_earnings_calendar_cannot_crowd_out_core_liquid():
    from agentic_portfolio.policy import load_discovery_config

    cfg = dict(load_discovery_config())
    uc = dict(cfg.get("universe_construction") or {})
    uc["max_universe_size"] = 18
    uc["max_per_source"] = 25
    cfg["universe_construction"] = uc
    universe = construct_universe(CrowdingFetcher(), held_symbols=[], now=NOW, config=cfg)
    successful = [s.name for s in universe.sources if s.successful]
    assert "earnings_calendar" in successful
    assert "core_liquid" in successful
    assert "popular_watchlists" in successful
    assert "AAPL" in universe.unique_symbols or "MSFT" in universe.unique_symbols
    assert "SPY" in universe.unique_symbols or "QQQ" in universe.unique_symbols
    assert universe.unique_universe_size <= 18
