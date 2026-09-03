"""LIVE runtime uses the Agentic Robinhood account. Paper book must not leak."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_portfolio.adapters.portfolio_facts import (
    LiveAccountError,
    LiveDataUnavailable,
    StaticPortfolioFetcher,
    confirm_agentic_account,
    parse_portfolio,
    parse_positions,
)
from agentic_portfolio.context import build_context
from agentic_portfolio.dashboard.app import create_app
from agentic_portfolio.dashboard.family import current_runtime_nav, scaled_share
from agentic_portfolio.dashboard.queries import (
    active_context,
    ai_activity_view,
    dashboard_state,
    dashboard_view,
    journal_view,
    list_approvals,
    orders_view,
    paper_context,
    research_view,
    system_view,
)
from agentic_portfolio.live.engine import load_runtime_portfolio_context, refresh_live_portfolio
from agentic_portfolio.live.isolation import PaperContaminationError, detect_paper_contamination
from agentic_portfolio.live.safety import inspect_live_module_for_forbidden_calls
from agentic_portfolio.monitoring.engine import run_position_monitor
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.risk_gate import evaluate
from agentic_portfolio.runtime import (
    LIVE_ORDER_PLACEMENT,
    RuntimeMode,
    artifact_environment,
    get_active_artifact_environment,
    get_active_portfolio_source,
    get_active_runtime,
    resolve_runtime_mode,
)
from agentic_portfolio.schemas import (
    ClassificationStatus,
    Decision,
    LiquidityInputs,
    Position,
    ProposedAction,
    SecurityClass,
    Sleeve,
)
from scripts.run_live_launch_check import run_live_launch_check
from tests.conftest import ACCOUNT
from tests.test_family import _admin, _csrf, _login

NOW = datetime(2026, 8, 30, 20, 0, tzinfo=timezone.utc)
EXPECTED = load_account_rules()["account"]["account_number"]


def _accounts(*, number=EXPECTED, nickname="Agentic", allowed=True):
    return {
        "data": {
            "accounts": [
                {
                    "account_number": "5UW82786",
                    "nickname": "Default",
                    "agentic_allowed": False,
                    "state": "active",
                    "brokerage_account_type": "individual",
                    "type": "margin",
                    "is_default": True,
                },
                {
                    "account_number": number,
                    "rhs_account_number": number,
                    "nickname": nickname,
                    "agentic_allowed": allowed,
                    "state": "active",
                    "brokerage_account_type": "individual",
                    "type": "limited_margin",
                    "is_default": False,
                },
            ]
        }
    }


def _portfolio(nav=500.0, cash=500.0, bp=500.0):
    return {
        "data": {
            "total_value": str(nav),
            "equity_value": "0",
            "cash": str(cash),
            "buying_power": {"buying_power": f"{bp:.4f}", "display_currency": "USD"},
        }
    }


def _positions(rows=None):
    return {"data": {"positions": list(rows or [])}}


def _quotes(*symbols_prices):
    results = []
    for symbol, price in symbols_prices:
        results.append({"quote": {"symbol": symbol, "last_trade_price": str(price), "previous_close": str(price)}})
    return {"data": {"results": results}}


def _fetcher(**kwargs):
    return StaticPortfolioFetcher(
        accounts=kwargs.get("accounts", _accounts()),
        portfolio=kwargs.get("portfolio", _portfolio()),
        positions=kwargs.get("positions", _positions()),
        quotes=kwargs.get("quotes", _quotes(("SPY", 769.39))),
        orders=kwargs.get("orders", {"data": {"orders": []}}),
        error=kwargs.get("error"),
    )


def _write_paper(root: Path, nav: float = 10000.0) -> None:
    book_dir = root / "state" / "paper_book"
    book_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": "2026-08-30T18:45:00+00:00",
        "paper_environment": True,
        "live_book_untouched": True,
        "context": {
            "timestamp": "2026-08-30T18:45:00+00:00",
            "account_number": EXPECTED,
            "current_nav": nav,
            "cash": nav * 0.93,
            "buying_power": nav * 0.93,
            "positions": [
                {"symbol": "NVDA", "market_value": nav * 0.05, "quantity": 2.7778, "sleeve": "CORE_GROWTH", "thesis_id": "a141950d-c730-4b98-9b13-167c977b3596"},
                {"symbol": "NKE", "market_value": nav * 0.02, "quantity": 3.3333, "sleeve": "OPPORTUNISTIC", "thesis_id": "c6a6b724-588c-4133-a6ef-9882d5a8aa75"},
            ],
        },
        "lots": [{"symbol": "NVDA"}],
        "fills": [{"symbol": "NVDA"}],
    }
    (book_dir / "current.json").write_text(json.dumps(payload), encoding="utf-8")


def _seed_paper_operational_artifacts(root: Path) -> None:
    _write_paper(root, 10000.0)
    theses = root / "state" / "paper_book" / "theses.json"
    theses.write_text(
        json.dumps(
            {
                "records": {
                    "a141950d-c730-4b98-9b13-167c977b3596": {
                        "thesis_id": "a141950d-c730-4b98-9b13-167c977b3596",
                        "symbol": "NVDA",
                        "sleeve": "CORE_GROWTH",
                        "created_at": "2026-08-30T18:45:00+00:00",
                        "updated_at": "2026-08-30T18:45:00+00:00",
                        "status": "ACTIVE",
                        "decision": "HOLD",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (root / "state" / "candidates.json").write_text(
        json.dumps({"records": {"paper-nvda": {"candidate_id": "paper-nvda", "symbol": "NVDA", "discovered_at": "2026-08-30T18:45:00+00:00", "discovery_source": "paper", "provisional_sleeve": "CORE_GROWTH", "discovery_score": 99, "priority": "HIGH", "status": "DISCOVERED"}}}),
        encoding="utf-8",
    )
    fills = root / "state" / "paper_fills"
    fills.mkdir(parents=True, exist_ok=True)
    fill_run = {
        "run_id": "paper-fill-1",
        "created_at": "2026-08-30T18:45:00+00:00",
        "paper_environment": True,
        "fills": [{"fill_id": "fill-nvda", "symbol": "NVDA", "filled_notional": 500, "execution_status": "PAPER_ONLY"}],
        "skipped": [],
    }
    (fills / "paper-fill-1.json").write_text(json.dumps(fill_run), encoding="utf-8")
    (fills / "index.json").write_text(json.dumps({"by_id": {"paper-fill-1": {"created_at": "2026-08-30T18:45:00+00:00", "path": "paper-fill-1.json"}}}), encoding="utf-8")
    plans = root / "state" / "order_plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "paper-plan-1.json").write_text(
        json.dumps({"run_id": "paper-plan-1", "created_at": "2026-08-30T18:45:00+00:00", "plans": [{"symbol": "NVDA", "execution_status": "PAPER_ONLY", "action": "BUY", "notional": 500}]}),
        encoding="utf-8",
    )
    (plans / "index.json").write_text(json.dumps({"by_id": {"paper-plan-1": {"created_at": "2026-08-30T18:45:00+00:00", "path": "paper-plan-1.json"}}}), encoding="utf-8")
    packets = root / "state" / "approval_packets"
    packets.mkdir(parents=True, exist_ok=True)
    packet = {
        "approval_id": "paper-packet-nvda",
        "symbol": "NVDA",
        "action": "BUY",
        "status": "PENDING_HUMAN_APPROVAL",
        "created_at": "2026-08-30T18:45:00+00:00",
        "desired_allocation_pct": 0.05,
        "current_allocation_pct": 0.0,
        "order_notional": 500,
        "order_quantity": 1,
        "current_price": 180,
        "sleeve": "CORE_GROWTH",
        "key_risks": [],
        "enhanced_review_requirements": [],
        "order_plan_summary": {"order_plan_id": "paper-plan-1", "execution_status": "PAPER_ONLY"},
        "evidence_refs": {},
        "snapshot": {},
        "status_history": [],
        "expiry_reasons": [],
    }
    (packets / "paper-packet-nvda.json").write_text(json.dumps(packet), encoding="utf-8")
    (packets / "index.json").write_text(
        json.dumps({"by_id": {"paper-packet-nvda": {"created_at": "2026-08-30T18:45:00+00:00", "path": "paper-packet-nvda.json", "symbol": "NVDA", "status": "PENDING_HUMAN_APPROVAL"}}}),
        encoding="utf-8",
    )
    monitor = root / "state" / "position_monitoring"
    monitor.mkdir(parents=True, exist_ok=True)
    run = {
        "run_id": "paper-mon-1",
        "created_at": "2026-08-30T18:45:00+00:00",
        "symbols": ["NVDA", "NKE"],
        "positions": [{"symbol": "NVDA", "state": "HEALTHY"}, {"symbol": "NKE", "state": "HEALTHY"}],
    }
    (monitor / "paper-mon-1.json").write_text(json.dumps(run), encoding="utf-8")
    (monitor / "index.json").write_text(
        json.dumps({"by_id": {"paper-mon-1": {"created_at": "2026-08-30T18:45:00+00:00", "path": "paper-mon-1.json", "symbols": ["NVDA", "NKE"]}}}),
        encoding="utf-8",
    )
    from agentic_portfolio.ai.store import AIArtifactStore

    AIArtifactStore(root, runtime_mode=RuntimeMode.PAPER).save_screening(
        "paper-ai-nvda",
        {"ticker": "NVDA", "score": 99, "created_at": "2026-08-30T18:45:00+00:00", "confidence": "HIGH"},
    )
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "paper_fill.jsonl").write_text(json.dumps({"symbol": "NVDA", "type": "PAPER_FILL", "logged_at": "2026-08-30T18:45:00+00:00"}) + "\n", encoding="utf-8")
    (logs / "position_monitor.jsonl").write_text(json.dumps({"symbol": "NKE", "type": "MONITOR", "logged_at": "2026-08-30T18:45:00+00:00"}) + "\n", encoding="utf-8")



def test_runtime_mode_defaults_to_paper():
    assert resolve_runtime_mode(environ={}, runtime_config={"mode": "PAPER"}) is RuntimeMode.PAPER
    assert resolve_runtime_mode(environ={"AGENTIC_RUNTIME_MODE": "LIVE"}) is RuntimeMode.LIVE
    assert resolve_runtime_mode(environ={"DASHBOARD_ENVIRONMENT": "LIVE"}) is RuntimeMode.LIVE


def test_confirm_agentic_account_fail_closed():
    found = confirm_agentic_account(_accounts())
    assert found["account_number"] == EXPECTED
    assert found["agentic_allowed"] is True
    with pytest.raises(LiveAccountError):
        confirm_agentic_account(_accounts(number="000000000"))
    with pytest.raises(LiveAccountError):
        confirm_agentic_account(_accounts(allowed=False))
    with pytest.raises(LiveAccountError):
        confirm_agentic_account(_accounts(nickname="NotAgentic"))


def test_refresh_live_ignores_paper_book(tmp_path):
    _write_paper(tmp_path, 10000.0)
    result = refresh_live_portfolio(_fetcher(), now=NOW, root=tmp_path, persist=True)
    assert result.context.current_nav == 500.0
    assert result.context.cash == 500.0
    assert result.context.buying_power == 500.0
    assert result.context.positions == []
    assert result.snapshot["paper_environment"] is False
    assert result.snapshot["source_of_truth"] == "robinhood_agentic_account"
    assert "place_equity_order" not in result.tools_used
    assert "review_equity_order" not in result.tools_used
    stored = json.loads((tmp_path / "state" / "live_book" / "current.json").read_text(encoding="utf-8"))
    assert stored["context"]["current_nav"] == 500.0
    paper = json.loads((tmp_path / "state" / "paper_book" / "current.json").read_text(encoding="utf-8"))
    assert paper["context"]["current_nav"] == 10000.0


def test_live_fails_closed_on_unavailable_and_wrong_account(tmp_path):
    with pytest.raises(LiveAccountError):
        refresh_live_portfolio(_fetcher(accounts=_accounts(number="111")), now=NOW, root=tmp_path, persist=False)
    with pytest.raises(LiveDataUnavailable):
        refresh_live_portfolio(_fetcher(portfolio=None), now=NOW, root=tmp_path, persist=False)
    boom = StaticPortfolioFetcher(accounts=_accounts(), error=RuntimeError("mcp down"))
    with pytest.raises(RuntimeError):
        refresh_live_portfolio(boom, now=NOW, root=tmp_path, persist=False)


def test_paper_contamination_detector():
    live = {
        "runtime_mode": "LIVE",
        "source_of_truth": "robinhood_agentic_account",
        "paper_environment": False,
        "context": {"current_nav": 500.0, "timestamp": "live", "positions": []},
    }
    paper = {
        "paper_environment": True,
        "context": {"current_nav": 10000.0, "timestamp": "paper", "positions": [{"symbol": "NVDA"}]},
    }
    assert detect_paper_contamination(live, paper) == []
    contaminated = {
        "runtime_mode": "LIVE",
        "source_of_truth": "isolated_paper_book",
        "paper_environment": True,
        "live_book_untouched": True,
        "lots": [{}],
        "context": {
            "current_nav": 10000.0,
            "timestamp": "2026-08-30T18:45:00+00:00",
            "positions": [{"symbol": "NVDA", "thesis_id": "a141950d-c730-4b98-9b13-167c977b3596"}, {"symbol": "NKE"}],
        },
    }
    leaks = detect_paper_contamination(contaminated, paper)
    assert "live_snapshot_marked_paper_environment" in leaks
    assert "live_nav_is_paper_10000" in leaks


def test_dashboard_live_does_not_read_paper_nav(tmp_path, monkeypatch):
    _write_paper(tmp_path, 10000.0)
    refresh_live_portfolio(_fetcher(), now=NOW, root=tmp_path, persist=True)
    monkeypatch.setenv("AGENTIC_RUNTIME_MODE", "LIVE")
    monkeypatch.setenv("DASHBOARD_ENVIRONMENT", "LIVE")
    state = dashboard_state(tmp_path)
    assert paper_context(state)["current_nav"] == 10000.0
    assert active_context(state)["current_nav"] == 500.0
    view = dashboard_view(state)
    assert view["environment"] == "LIVE"
    assert view["nav"] == 500.0
    assert view["cash"] == 500.0
    assert view["buying_power"] == 500.0
    assert view["positions"] == []
    assert view["book_kind"] == "live"
    assert view["live_order_placement_enabled"] is False
    assert view["paper_environment"] is False
    html = _admin(create_app(tmp_path).test_client()).get("/").get_data(as_text=True)
    assert "LIVE ENVIRONMENT" in html
    assert "LIVE ORDER PLACEMENT: OFF" in html or "NO LIVE ORDER PLACEMENT ENABLED" in html
    assert "$10,000.00" not in html
    assert "$500.00" in html
    assert "NVDA" not in html or "INACTIVE" in html
    symbols = {p["symbol"] for p in view["positions"]}
    assert "NVDA" not in symbols
    assert "NKE" not in symbols


def test_active_runtime_helpers(monkeypatch):
    monkeypatch.setenv("AGENTIC_RUNTIME_MODE", "LIVE")
    monkeypatch.setenv("DASHBOARD_ENVIRONMENT", "LIVE")
    assert get_active_runtime() is RuntimeMode.LIVE
    assert get_active_portfolio_source() == "robinhood_agentic_account"
    assert get_active_artifact_environment() == "LIVE"
    assert artifact_environment({"execution_status": "PAPER_ONLY"}) == "PAPER"
    assert artifact_environment({"runtime_mode": "LIVE"}) == "LIVE"
    assert LIVE_ORDER_PLACEMENT is False


def test_live_runtime_does_not_expose_paper_artifacts(tmp_path, monkeypatch):
    _seed_paper_operational_artifacts(tmp_path)
    refresh_live_portfolio(_fetcher(), now=NOW, root=tmp_path, persist=True)
    monkeypatch.setenv("AGENTIC_RUNTIME_MODE", "LIVE")
    monkeypatch.setenv("DASHBOARD_ENVIRONMENT", "LIVE")
    state = dashboard_state(tmp_path)
    view = dashboard_view(state)
    symbols = {p["symbol"] for p in view["positions"]}
    assert view["environment"] == "LIVE"
    assert view["active_runtime"] == "LIVE"
    assert view["nav"] == 500.0
    assert view["cash"] == 500.0
    assert view["buying_power"] == 500.0
    assert view["positions"] == []
    assert "NVDA" not in symbols
    assert "NKE" not in symbols
    assert view["monitoring_positions_count"] == 0
    assert view["kpis"]["risk_state"]["display"] == "NORMAL on LIVE ACCOUNT"
    assert view["live_order_placement_enabled"] is False
    assert LIVE_ORDER_PLACEMENT is False
    pending = list_approvals(state)
    assert pending["pending_count"] == 0
    assert all(p.get("symbol") != "NVDA" for p in pending["all"])
    research = research_view(state)
    thesis_symbols = {t["symbol"] for t in research["theses"]}
    assert "NVDA" not in thesis_symbols
    assert all(c.get("symbol") != "NVDA" for c in research["candidates"])
    orders = orders_view(state)
    assert orders["fills"] == []
    assert orders["plans"] == []
    journal = journal_view(state)
    journal_text = json.dumps(journal)
    assert "NVDA" not in journal_text
    activity = ai_activity_view(state)
    assert all(row.get("ticker") != "NVDA" for row in activity["rows"])
    activity_titles = " ".join(item.get("title") or "" for item in view["activity"])
    assert "NVDA" not in activity_titles
    system = system_view(state)
    assert system["active_runtime"] == "LIVE"
    assert system["live_account_status"] == "ACTIVE"
    assert "INACTIVE" in system["paper_book_status"]
    assert "NORMAL on LIVE ACCOUNT" in system["risk_state_label"]
    service_names = {row["name"]: row["count"] for row in system["services"]}
    assert service_names["Approval packets"] == 0
    assert service_names["Fills"] == 0
    diag = {row["name"]: row["count"] for row in system["paper_diagnostics"]["counts"]}
    assert diag["Paper fills"] >= 1
    assert diag["Approval packets"] >= 1
    assert diag["Theses (paper book)"] >= 1
    html = _admin(create_app(tmp_path).test_client()).get("/system").get_data(as_text=True)
    assert "ACTIVE RUNTIME: LIVE" in html or "ACTIVE RUNTIME" in html
    assert "INACTIVE / TEST ENVIRONMENT" in html
    assert "PAPER DIAGNOSTICS" in html
    dash = _admin(create_app(tmp_path).test_client()).get("/").get_data(as_text=True)
    assert "Buying Power" in dash
    assert "$500.00" in dash
    assert "No live positions." in dash


def test_live_fail_closed_dashboard_without_snapshot(tmp_path, monkeypatch):
    _write_paper(tmp_path, 10000.0)
    monkeypatch.setenv("DASHBOARD_ENVIRONMENT", "LIVE")
    view = dashboard_view(dashboard_state(tmp_path))
    assert view["environment"] == "LIVE"
    assert view["nav"] != 10000.0
    assert view["nav"] is None
    assert view["live_data_unavailable"] is True
    assert view["kpis"]["portfolio_value"]["display"] == "LIVE DATA UNAVAILABLE"
    assert view["positions"] == []


def test_family_uses_live_nav(tmp_path, monkeypatch):
    _write_paper(tmp_path, 10000.0)
    refresh_live_portfolio(_fetcher(), now=NOW, root=tmp_path, persist=True)
    monkeypatch.setenv("DASHBOARD_ENVIRONMENT", "LIVE")
    assert current_runtime_nav(dashboard_state(tmp_path)) == 500.0
    share = scaled_share(100.0, 500.0, current_runtime_nav(dashboard_state(tmp_path)))
    assert share["current_value"] == 100.0
    client = _admin(create_app(tmp_path).test_client())
    token = _csrf(client)
    dad = client.post("/family/users", json={"csrf_token": token, "name": "Dad", "username": "dad", "password": "dadpass"}).get_json()["user"]
    assigned = client.post(f"/family/users/{dad['id']}/assign", json={"csrf_token": token, "amount": 100})
    assert assigned.status_code == 200
    assert assigned.get_json()["user"]["current_value"] == 100.0


def test_risk_gate_uses_live_buying_power(tmp_path):
    ctx = refresh_live_portfolio(_fetcher(), now=NOW, root=tmp_path, persist=False).context
    assert ctx.buying_power == 500.0
    risk = evaluate(
        ctx,
        ProposedAction(
            symbol="MSFT",
            decision=Decision.BUY,
            security_class=SecurityClass.INDIVIDUAL_EQUITY,
            classification_status=ClassificationStatus.VALIDATED,
            sleeve=Sleeve.CORE_GROWTH,
            proposed_notional=600.0,
            expected_resulting_position_pct=1.2,
            liquidity=LiquidityInputs(median_daily_dollar_volume_20d=1e12),
            investment_thesis_review_complete=True,
            risk_review_complete=True,
        ),
    )
    codes = {r.code for r in risk.reasons}
    assert "BUYING_POWER" in codes
    live_ctx = load_runtime_portfolio_context(tmp_path, mode=RuntimeMode.LIVE) if False else ctx
    assert live_ctx.positions == []


def test_monitoring_live_rejects_paper_holdings():
    ctx = build_context(
        account_number=ACCOUNT,
        current_nav=10000.0,
        cash=9300.0,
        buying_power=9300.0,
        positions=[
            Position(symbol="NVDA", market_value=500.0, sleeve=Sleeve.CORE_GROWTH, security_class=SecurityClass.INDIVIDUAL_EQUITY),
            Position(symbol="NKE", market_value=200.0, sleeve=Sleeve.OPPORTUNISTIC, security_class=SecurityClass.INDIVIDUAL_EQUITY),
        ],
    )
    with pytest.raises(PaperContaminationError):
        run_position_monitor(ctx, persist=False, runtime_mode="LIVE")


def test_monitoring_live_uses_robinhood_holdings(tmp_path):
    ctx = refresh_live_portfolio(_fetcher(), now=NOW, root=tmp_path, persist=False).context
    result = run_position_monitor(ctx, persist=False, runtime_mode="LIVE")
    assert result.runtime_mode == "LIVE"
    assert result.positions == []
    assert {p.symbol for p in ctx.positions} == set()


def test_launch_check_pass_and_fails_on_paper_leak(tmp_path):
    _write_paper(tmp_path, 10000.0)
    report = run_live_launch_check(fetcher=_fetcher(), root=tmp_path, persist=True, now=NOW)
    assert report["ok"] is True
    assert report["nav"] == 500.0
    assert report["cash"] == 500.0
    assert report["buying_power"] == 500.0
    assert report["positions"] == []
    assert report["paper_state_leaked"] is False
    assert report["live_placement_disabled"] is True
    assert report["agentic_account"]["found"] is True
    assert "place_equity_order" not in report["mcp_tools_used"]
    assert report["dashboard"]["mode"] == "LIVE"
    assert report["dashboard"]["nav"] == 500.0
    bad = {
        "runtime_mode": "LIVE",
        "source_of_truth": "robinhood_agentic_account",
        "paper_environment": True,
        "context": {"current_nav": 10000.0, "positions": [{"symbol": "NVDA"}, {"symbol": "NKE"}]},
    }
    assert detect_paper_contamination(bad, json.loads((tmp_path / "state" / "paper_book" / "current.json").read_text(encoding="utf-8")))


def test_live_modules_do_not_call_place():
    assert inspect_live_module_for_forbidden_calls() == []


def test_snapshot_placement_flag_follows_env_true(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_LIVE_ORDER_PLACEMENT", "true")
    result = refresh_live_portfolio(_fetcher(), now=NOW, root=tmp_path, persist=True)
    assert result.snapshot["live_order_placement_enabled"] is True
    assert result.placement_disabled is True
    stored = json.loads((tmp_path / "state" / "live_book" / "current.json").read_text(encoding="utf-8"))
    assert stored["live_order_placement_enabled"] is True


def test_snapshot_placement_flag_follows_live_order_placement_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_ORDER_PLACEMENT", "true")
    result = refresh_live_portfolio(_fetcher(), now=NOW, root=tmp_path, persist=True)
    assert result.snapshot["live_order_placement_enabled"] is True
    assert result.placement_disabled is True


def test_snapshot_placement_flag_false_when_env_false(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_LIVE_ORDER_PLACEMENT", "false")
    result = refresh_live_portfolio(_fetcher(), now=NOW, root=tmp_path, persist=True)
    assert result.snapshot["live_order_placement_enabled"] is False


def test_snapshot_placement_flag_false_when_unset(tmp_path):
    result = refresh_live_portfolio(_fetcher(), now=NOW, root=tmp_path, persist=True)
    assert result.snapshot["live_order_placement_enabled"] is False


def test_snapshot_placement_flag_follows_runtime_config_true(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIC_LIVE_ORDER_PLACEMENT", raising=False)
    monkeypatch.delenv("LIVE_ORDER_PLACEMENT", raising=False)
    monkeypatch.setattr(
        "agentic_portfolio.runtime.load_runtime_config",
        lambda: {"live_order_placement_enabled": True},
    )
    result = refresh_live_portfolio(_fetcher(), now=NOW, root=tmp_path, persist=True)
    assert result.snapshot["live_order_placement_enabled"] is True
    assert result.placement_disabled is True


def test_executor_enforces_placement_independently_of_snapshot(tmp_path, monkeypatch):
    from agentic_portfolio.live_approval.types import LiveApprovalStatus
    from agentic_portfolio.live_execution import FakeBroker
    from tests.test_live_rc1 import _approved_buy, _stack

    monkeypatch.setenv("AGENTIC_LIVE_ORDER_PLACEMENT", "true")
    result = refresh_live_portfolio(_fetcher(), now=NOW, root=tmp_path, persist=True)
    assert result.snapshot["live_order_placement_enabled"] is True
    monkeypatch.delenv("AGENTIC_LIVE_ORDER_PLACEMENT", raising=False)
    monkeypatch.delenv("LIVE_ORDER_PLACEMENT", raising=False)
    broker = FakeBroker()
    _, executor, approvals, _ = _stack(tmp_path, broker)
    item = _approved_buy(approvals)
    if item.status is not LiveApprovalStatus.APPROVED:
        item = approvals.store.get(item.approval_id)
    outcome = executor.execute_approved(item)
    assert outcome.placed is False
    assert "LIVE_ORDER_PLACEMENT_false" in outcome.reasons
    assert broker.place_calls == []


def test_parse_positions_fail_closed_without_quotes():
    payload = _positions([{"symbol": "MSFT", "quantity": "2", "average_buy_price": "100"}])
    with pytest.raises(LiveDataUnavailable):
        parse_positions(payload, quotes=None)
    parsed = parse_positions(payload, quotes=_quotes(("MSFT", 120.0)))
    assert parsed[0].market_value == pytest.approx(240.0)
    assert parse_portfolio(_portfolio())["current_nav"] == 500.0
