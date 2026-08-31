"""Production object-graph tests. Mock only the MCP HTTP boundary."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from agentic_portfolio.adapters.portfolio_facts import LiveDataUnavailable, LiveErrorCode
from agentic_portfolio.adapters.readonly_mcp_auth import (
    load_or_refresh_access_token,
    oauth_home,
    oauth_store_path,
    save_oauth_store,
)
from agentic_portfolio.adapters.readonly_runtime import (
    GuardedReadonlyTransport,
    StreamableHttpMcpTransport,
    bootstrap_readonly_broker_runtime,
    reset_readonly_broker_runtime,
    unwrap_mcp_tool_result,
)
from agentic_portfolio.adapters.robinhood_read import (
    MCP_TOOL_ARGUMENTS,
    MCP_TOOL_REQUIRED,
    AuthorizedMcpReadAdapter,
)
from agentic_portfolio.agent.connection import ConnectionManager
from agentic_portfolio.agent.heartbeat import load_health
from agentic_portfolio.agent.jobs import specs_by_name
from agentic_portfolio.agent.runtime import AgentRuntime
from agentic_portfolio.agent.session import MarketPhase, classify_market_phase
from agentic_portfolio.calendar import EASTERN
from agentic_portfolio.dashboard.app import create_app
from agentic_portfolio.dashboard.queries import dashboard_state, dashboard_view
from agentic_portfolio.discovery.live_readonly import LIVE_DISCOVERY_SKIP_REASON, LIVE_DISCOVERY_WIRED
from agentic_portfolio.live.store import LivePortfolioStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, RuntimeMode
from scripts.pi_production_smoke import run_smoke
from tests.fake_mcp_http import ACCOUNT, WRITE_TOOLS, FakeMcpHttp
from tests.test_family import _admin
from tests.test_live_mode import _write_paper

FRIDAY_OPEN = datetime(2026, 8, 28, 10, 0, tzinfo=EASTERN)
FRIDAY_POST = datetime(2026, 8, 28, 16, 30, tzinfo=EASTERN)
FRIDAY_NIGHT = datetime(2026, 8, 28, 21, 0, tzinfo=EASTERN)
SATURDAY = datetime(2026, 8, 29, 14, 0, tzinfo=EASTERN)
SUNDAY = datetime(2026, 8, 30, 14, 0, tzinfo=EASTERN)
MONDAY_PRE = datetime(2026, 8, 31, 7, 0, tzinfo=EASTERN)
MONDAY_OPEN = datetime(2026, 8, 31, 10, 0, tzinfo=EASTERN)


def _oauth_store(tmp_path: Path, *, access: str = "access-live", refresh: str = "refresh-live", expires_at: float | None = None) -> None:
    save_oauth_store(
        {
            "endpoint": "https://agent.robinhood.com/mcp/trading",
            "resource": "https://agent.robinhood.com/mcp/trading",
            "mode": "READ_ONLY",
            "client": {"client_id": "public-client-1"},
            "authorization_server": {"token_endpoint": "https://api.robinhood.com/oauth2/token/"},
            "tokens": {
                "access_token": access,
                "refresh_token": refresh,
                "expires_at": expires_at if expires_at is not None else time.time() + 3600,
            },
        }
    )


def _patch_http(monkeypatch, fake: FakeMcpHttp) -> None:
    monkeypatch.setattr(StreamableHttpMcpTransport, "_default_post", lambda self, body: fake.post(body))


def _live_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTIC_RUNTIME_MODE", "LIVE")
    monkeypatch.setenv("DASHBOARD_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("HOME", str(tmp_path / "home" / "agentic"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)


def _runtime(tmp_path: Path, fake: FakeMcpHttp, *, now=None, max_cycles=None) -> AgentRuntime:
    reset_readonly_broker_runtime()
    bound = bootstrap_readonly_broker_runtime(force=True)

    def bootstrap(**kwargs):
        return bootstrap_readonly_broker_runtime(force=bool(kwargs.get("force")))

    conn = ConnectionManager(bootstrap=bootstrap, root=tmp_path, now_fn=now or (lambda: SATURDAY))
    conn.runtime = bound
    conn.connected = bound.bound
    return AgentRuntime(
        tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        now_fn=now or (lambda: SATURDAY),
        sleep_fn=lambda _s: None,
        max_cycles=max_cycles,
        connection=conn,
        ai_allowed=False,
    )


def test_authorized_adapter_implements_portfolio_fetcher_contract():
    names = (
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_equity_quotes",
        "get_equity_orders",
        "get_equity_tradability",
        "get_equity_fundamentals",
        "search_instrument",
    )
    for name in names:
        assert callable(getattr(AuthorizedMcpReadAdapter, name))
    import inspect

    quotes = inspect.signature(AuthorizedMcpReadAdapter.get_equity_quotes)
    assert "symbols" in quotes.parameters


def test_mcp_request_bodies_match_verified_schemas(monkeypatch, tmp_path):
    _live_env(monkeypatch, tmp_path)
    _oauth_store(tmp_path)
    fake = FakeMcpHttp()
    _patch_http(monkeypatch, fake)
    reset_readonly_broker_runtime()
    runtime = bootstrap_readonly_broker_runtime(force=True)
    fetcher = runtime.fetcher
    assert fetcher is not None
    fetcher.get_accounts()
    fetcher.get_portfolio(ACCOUNT)
    fetcher.get_equity_positions(ACCOUNT)
    fetcher.get_equity_quotes(["MSFT", "SPY"])
    fetcher.get_equity_orders(ACCOUNT)
    fetcher.get_equity_tradability("MSFT")
    fetcher.get_equity_fundamentals("MSFT")
    fetcher.search_instrument("MSFT")
    expected = {
        "get_accounts": {},
        "get_portfolio": {"account_number": ACCOUNT},
        "get_equity_positions": {"account_number": ACCOUNT},
        "get_equity_quotes": {"symbols": ["MSFT", "SPY"]},
        "get_equity_orders": {"account_number": ACCOUNT},
        "get_equity_tradability": {"account_number": ACCOUNT, "symbols": ["MSFT"]},
        "get_equity_fundamentals": {"symbols": ["MSFT"]},
        "search": {"query": "MSFT", "asset_type": "instrument", "limit": 10},
    }
    got = dict(fake.tool_calls())
    for tool, arguments in expected.items():
        assert got[tool] == arguments, tool
        allowed = MCP_TOOL_ARGUMENTS[tool]
        assert set(arguments) <= allowed
        assert MCP_TOOL_REQUIRED[tool] <= set(arguments)
    methods = [body.get("method") for body in fake.calls]
    assert methods[0] == "initialize"
    assert "notifications/initialized" in methods
    assert methods.count("tools/call") >= 8
    for name, _args in fake.tool_calls():
        assert name not in WRITE_TOOLS


def test_unwrap_mcp_tool_result_shapes():
    structured = unwrap_mcp_tool_result({"result": {"structuredContent": {"data": {"ok": True}}}})
    assert structured == {"data": {"ok": True}}
    text = unwrap_mcp_tool_result({"result": {"content": [{"type": "text", "text": json.dumps({"data": {"results": []}})}]}})
    assert text == {"data": {"results": []}}
    with pytest.raises(LiveDataUnavailable):
        unwrap_mcp_tool_result({"error": {"message": "nope"}})
    with pytest.raises(LiveDataUnavailable):
        unwrap_mcp_tool_result({"result": {"isError": True, "content": []}})


def test_http_sse_and_status_errors(monkeypatch, tmp_path):
    _live_env(monkeypatch, tmp_path)
    _oauth_store(tmp_path)
    fake = FakeMcpHttp()
    fake.result_mode = "sse"
    _patch_http(monkeypatch, fake)
    reset_readonly_broker_runtime()
    runtime = bootstrap_readonly_broker_runtime(force=True)
    payload = runtime.fetcher.get_accounts()
    assert payload["data"]["accounts"]

    fake.status_override["get_portfolio"] = 500
    with pytest.raises(LiveDataUnavailable) as exc:
        runtime.fetcher.get_portfolio("549688554")
    assert exc.value.code in {LiveErrorCode.MCP_HTTP_ERROR, LiveErrorCode.MCP_GET_PORTFOLIO_FAILED}

    fake.status_override.clear()
    fake.status_override["get_accounts"] = 401
    with pytest.raises(LiveDataUnavailable) as exc401:
        runtime.fetcher.get_accounts()
    assert exc401.value.code in {LiveErrorCode.MCP_HTTP_401, LiveErrorCode.OAUTH_REFRESH_FAILED}


def test_real_stack_refresh_persists_and_dashboard_reads(monkeypatch, tmp_path):
    _live_env(monkeypatch, tmp_path)
    _oauth_store(tmp_path)
    fake = FakeMcpHttp()
    _patch_http(monkeypatch, fake)
    _write_paper(tmp_path, 10000.0)
    runtime = _runtime(tmp_path, fake)
    assert runtime.services.refresh_fn is not None
    assert runtime.services.quotes_fn is not None
    assert runtime.services.candidates_fn is None
    row = runtime.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    assert row["status"] == "OK"
    assert row["nav"] == 1513.67
    assert row.get("skipped") != "no_refresh"
    book = LivePortfolioStore(tmp_path).current_book()
    assert book["context"]["current_nav"] == 1513.67
    assert book["context"]["positions"][0]["symbol"] == "MSFT"
    view = dashboard_view(dashboard_state(tmp_path))
    assert view["live_data_unavailable"] is False
    assert view["nav"] == 1513.67
    assert view["cash"] == 1000.0
    assert view["buying_power"] == 1000.0
    assert view["positions"][0]["symbol"] == "MSFT"
    html = _admin(create_app(tmp_path).test_client()).get("/").get_data(as_text=True)
    assert "LIVE DATA UNAVAILABLE" not in html or "halt-banner\">LIVE DATA UNAVAILABLE" not in html
    assert "$1,513.67" in html
    assert "MSFT" in html
    assert "$10,000.00" not in html
    tools = [name for name, _ in fake.tool_calls()]
    assert "get_accounts" in tools
    assert "get_portfolio" in tools
    assert "get_equity_positions" in tools
    assert "get_equity_quotes" in tools
    for tool in WRITE_TOOLS:
        assert tool not in tools


def test_malformed_mcp_fails_closed_never_paper(monkeypatch, tmp_path):
    _live_env(monkeypatch, tmp_path)
    _oauth_store(tmp_path)
    fake = FakeMcpHttp()
    fake.malformed_portfolio = True
    _patch_http(monkeypatch, fake)
    _write_paper(tmp_path, 10000.0)
    runtime = _runtime(tmp_path, fake)
    row = runtime.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    assert row["status"] == "FAIL_CLOSED"
    assert row.get("error_code") in {LiveErrorCode.MCP_GET_PORTFOLIO_FAILED, LiveErrorCode.LIVE_DATA_UNAVAILABLE}
    view = dashboard_view(dashboard_state(tmp_path))
    assert view["live_data_unavailable"] is True
    assert view["nav"] is None
    assert view["nav"] != 10000.0
    assert view["live_error_code"]
    html = _admin(create_app(tmp_path).test_client()).get("/").get_data(as_text=True)
    assert "LIVE DATA UNAVAILABLE" in html
    assert "$10,000.00" not in html


def test_edge_cases_fail_closed_without_killing_runtime(monkeypatch, tmp_path):
    _live_env(monkeypatch, tmp_path)
    _oauth_store(tmp_path)

    fake = FakeMcpHttp()
    fake.set_zero_positions()
    _patch_http(monkeypatch, fake)
    runtime = _runtime(tmp_path / "zero", fake)
    row = runtime.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    assert row["status"] == "OK"
    assert row["holdings_count"] == 0

    fake2 = FakeMcpHttp()
    fake2.set_fractional()
    _patch_http(monkeypatch, fake2)
    reset_readonly_broker_runtime()
    runtime2 = _runtime(tmp_path / "frac", fake2)
    row2 = runtime2.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    assert row2["status"] == "OK"
    pos = LivePortfolioStore(tmp_path / "frac").current_book()["context"]["positions"][0]
    assert pos["quantity"] == pytest.approx(0.15)

    fake3 = FakeMcpHttp()
    fake3.drop_quote("MSFT")
    _patch_http(monkeypatch, fake3)
    reset_readonly_broker_runtime()
    runtime3 = _runtime(tmp_path / "missq", fake3)
    row3 = runtime3.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    assert row3["status"] == "FAIL_CLOSED"

    fake4 = FakeMcpHttp()
    fake4.drop_quote("SPY")
    _patch_http(monkeypatch, fake4)
    reset_readonly_broker_runtime()
    runtime4 = _runtime(tmp_path / "spy", fake4)
    row4 = runtime4.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    assert row4["status"] == "OK"

    fake5 = FakeMcpHttp()
    fake5.orders = [
        {"id": "1", "symbol": "MSFT", "state": "confirmed", "side": "buy", "quantity": "1"},
        {"id": "2", "symbol": "AAPL", "state": "queued", "side": "buy", "quantity": "2"},
    ]
    _patch_http(monkeypatch, fake5)
    reset_readonly_broker_runtime()
    runtime5 = _runtime(tmp_path / "orders", fake5)
    row5 = runtime5.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    assert row5["status"] == "OK"

    fake6 = FakeMcpHttp()
    fake6.wrong_account = True
    _patch_http(monkeypatch, fake6)
    reset_readonly_broker_runtime()
    runtime6 = _runtime(tmp_path / "acct", fake6)
    row6 = runtime6.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    assert row6["status"] == "FAIL_CLOSED"
    assert row6.get("error_code") == LiveErrorCode.ACCOUNT_IDENTITY_MISMATCH

    fake7 = FakeMcpHttp()
    fake7.missing_cash = True
    _patch_http(monkeypatch, fake7)
    reset_readonly_broker_runtime()
    runtime7 = _runtime(tmp_path / "cash", fake7)
    row7 = runtime7.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    assert row7["status"] == "FAIL_CLOSED"

    fake8 = FakeMcpHttp()
    fake8.timeout_tools.add("get_accounts")
    _patch_http(monkeypatch, fake8)
    reset_readonly_broker_runtime()
    runtime8 = _runtime(tmp_path / "to", fake8)
    row8 = runtime8.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    assert row8["status"] == "FAIL_CLOSED"
    runtime8.max_cycles = 2
    runtime8.run()
    assert runtime8.cycles == 2
    assert runtime8.fatal is False


def test_write_tools_refused_even_if_mcp_advertises_them(monkeypatch, tmp_path):
    _live_env(monkeypatch, tmp_path)
    _oauth_store(tmp_path)
    fake = FakeMcpHttp()
    fake.advertise_write_tools = True
    _patch_http(monkeypatch, fake)
    reset_readonly_broker_runtime()
    runtime = bootstrap_readonly_broker_runtime(force=True)
    guarded = runtime.transport
    assert isinstance(guarded, GuardedReadonlyTransport)
    for tool in WRITE_TOOLS:
        with pytest.raises(LiveDataUnavailable, match="refused forbidden MCP tool"):
            runtime.invoke(tool, symbol="MSFT")
        assert not callable(getattr(runtime.fetcher, tool, None))
    assert all(name not in WRITE_TOOLS for name, _ in fake.tool_calls())


def test_restart_reloads_state_and_oauth(monkeypatch, tmp_path):
    _live_env(monkeypatch, tmp_path)
    _oauth_store(tmp_path)
    fake = FakeMcpHttp()
    _patch_http(monkeypatch, fake)
    first = _runtime(tmp_path, fake)
    first.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    snap = LivePortfolioStore(tmp_path).current_book()["snapshot_id"]
    reset_readonly_broker_runtime()
    second = _runtime(tmp_path, fake)
    assert LivePortfolioStore(tmp_path).current_book()["snapshot_id"] == snap
    row = second.orchestrator.run_job("LIVE_ACCOUNT_REFRESH", now=SATURDAY)
    assert row["status"] == "OK"
    assert second.connection.connected is True


def test_expired_token_refreshes_then_recovers(monkeypatch, tmp_path):
    _live_env(monkeypatch, tmp_path)
    _oauth_store(tmp_path, access="old-access", expires_at=10.0)
    fake = FakeMcpHttp()
    _patch_http(monkeypatch, fake)

    def oauth_http(method, url, *, headers=None, json_body=None, form=None, timeout=30.0):
        if "oauth2/token" in url:
            assert form["grant_type"] == "refresh_token"
            return 200, {}, {"access_token": "access-refreshed", "refresh_token": "refresh-2", "expires_in": 3600, "token_type": "Bearer"}
        raise AssertionError(url)

    monkeypatch.setattr("agentic_portfolio.adapters.readonly_mcp_auth._default_http", oauth_http)
    access, err = load_or_refresh_access_token(now=1_000_000.0)
    assert err is None
    assert access == "access-refreshed"
    stored = json.loads(oauth_store_path().read_text(encoding="utf-8"))
    assert stored["tokens"]["access_token"] == "access-refreshed"
    reset_readonly_broker_runtime()
    runtime = bootstrap_readonly_broker_runtime(force=True)
    assert runtime.bound is True
    assert runtime.fetcher.get_accounts()["data"]["accounts"]


def test_401_then_refresh_on_tools_call(monkeypatch, tmp_path):
    _live_env(monkeypatch, tmp_path)
    _oauth_store(tmp_path)
    fake = FakeMcpHttp()
    fake.status_override["get_accounts"] = 401
    _patch_http(monkeypatch, fake)

    def refresh() -> str:
        fake.status_override.pop("get_accounts", None)
        return "access-refreshed"

    http = StreamableHttpMcpTransport("https://example.invalid/mcp", "old-access", post=fake.post, refresh_token_fn=refresh)
    adapter = AuthorizedMcpReadAdapter(transport=GuardedReadonlyTransport(http))
    payload = adapter.get_accounts()
    assert payload["data"]["accounts"]


def test_systemd_like_home_and_foreign_cwd(monkeypatch, tmp_path):
    home = tmp_path / "home" / "agentic"
    home.mkdir(parents=True)
    cwd = tmp_path / "not-the-project"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    env = {
        "HOME": str(home),
        "USER": "agentic",
        "LOGNAME": "agentic",
        "PATH": "/usr/bin",
        "AGENTIC_RUNTIME_MODE": "LIVE",
        "DASHBOARD_ENVIRONMENT": "LIVE",
    }
    assert oauth_home(environ=env) == home / ".agentic-portfolio" / "readonly-mcp"
    assert "LOCALAPPDATA" not in env
    assert project_root().joinpath("src", "agentic_portfolio").is_dir()
    _live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(home))
    _oauth_store(tmp_path)
    fake = FakeMcpHttp()
    _patch_http(monkeypatch, fake)
    runtime = _runtime(tmp_path, fake)
    row = runtime.orchestrator.run_job("WEEKEND_PORTFOLIO_REVIEW", now=SATURDAY)
    assert row["status"] == "OK"
    assert (tmp_path / "state" / "live_book" / "current.json").exists()
    assert not (cwd / "state").exists()


def test_scheduler_phases_and_long_simulated_service(monkeypatch, tmp_path):
    _live_env(monkeypatch, tmp_path)
    _oauth_store(tmp_path)
    fake = FakeMcpHttp()
    _patch_http(monkeypatch, fake)
    stamps = [FRIDAY_OPEN, FRIDAY_POST, FRIDAY_NIGHT, SATURDAY, SUNDAY, MONDAY_PRE, MONDAY_OPEN]
    clock = {"i": 0}

    def now():
        return stamps[min(clock["i"], len(stamps) - 1)]

    def sleep(_s):
        clock["i"] += 1

    runtime = _runtime(tmp_path, fake, now=now, max_cycles=len(stamps))
    runtime._sleep = sleep
    runtime.run()
    assert runtime.cycles == len(stamps)
    assert runtime.fatal is False
    health = load_health(tmp_path)
    assert health["LIVE_ORDER_PLACEMENT"] is False
    assert health["runtime_mode"] == "LIVE"
    assert LivePortfolioStore(tmp_path).current_book() is not None
    tools = [name for name, _ in fake.tool_calls()]
    for tool in WRITE_TOOLS:
        assert tool not in tools
    assert runtime.services.refresh_fn is not None
    assert runtime.services.quotes_fn is not None
    assert runtime.services.candidates_fn is None
    discovery = [row for row in runtime.last_results if row.get("job") == "CANDIDATE_DISCOVERY"]
    if discovery:
        assert discovery[0].get("skipped") == LIVE_DISCOVERY_SKIP_REASON
    assert LIVE_DISCOVERY_WIRED is False
    assert float(health.get("ai_budget", {}).get("cap") or 10) == 10

    phases = {
        FRIDAY_OPEN: MarketPhase.MARKET_OPEN,
        FRIDAY_POST: MarketPhase.AFTER_CLOSE,
        FRIDAY_NIGHT: MarketPhase.OVERNIGHT,
        SATURDAY: MarketPhase.WEEKEND,
        SUNDAY: MarketPhase.WEEKEND,
        MONDAY_PRE: MarketPhase.PREMARKET,
        MONDAY_OPEN: MarketPhase.MARKET_OPEN,
    }
    for stamp, phase in phases.items():
        assert classify_market_phase(stamp).phase is phase
    names = specs_by_name()
    for job in names:
        assert job in runtime.orchestrator.handlers
    assert names["LIVE_ACCOUNT_REFRESH"].requires_broker is True
    assert names["WEEKEND_PORTFOLIO_REVIEW"].requires_broker is True
    assert MarketPhase.WEEKEND in names["LIVE_ACCOUNT_REFRESH"].phases


def test_candidate_discovery_not_static_and_quotes_wired(monkeypatch, tmp_path):
    _live_env(monkeypatch, tmp_path)
    _oauth_store(tmp_path)
    fake = FakeMcpHttp()
    _patch_http(monkeypatch, fake)
    runtime = _runtime(tmp_path, fake, now=lambda: FRIDAY_OPEN)
    disc = runtime.orchestrator.run_job("CANDIDATE_DISCOVERY", now=FRIDAY_OPEN)
    assert disc["status"] == "SKIPPED"
    assert disc["skipped"] == LIVE_DISCOVERY_SKIP_REASON
    quotes = runtime.orchestrator.run_job("QUOTE_REFRESH", now=FRIDAY_OPEN)
    assert quotes.get("skipped") != "no_quotes_fn"
    assert quotes["status"] in {"OK", "FAIL_CLOSED"}


def test_guarded_transport_is_production_fetcher(monkeypatch, tmp_path):
    _live_env(monkeypatch, tmp_path)
    _oauth_store(tmp_path)
    fake = FakeMcpHttp()
    _patch_http(monkeypatch, fake)
    reset_readonly_broker_runtime()
    bound = bootstrap_readonly_broker_runtime(force=True)
    assert bound.bound is True
    assert isinstance(bound.fetcher, AuthorizedMcpReadAdapter) or hasattr(bound.fetcher, "get_accounts")
    assert callable(bound.fetcher.get_accounts)
    assert callable(bound.fetcher.get_portfolio)
    assert callable(bound.fetcher.get_equity_positions)
    payload = bound.fetcher.get_equity_quotes(["MSFT", "SPY"])
    symbols = {item["quote"]["symbol"] for item in payload["data"]["results"]}
    assert "MSFT" in symbols
    assert "SPY" in symbols


def test_pi_smoke_uses_real_stack(monkeypatch, tmp_path):
    _live_env(monkeypatch, tmp_path)
    _oauth_store(tmp_path)
    fake = FakeMcpHttp()
    _patch_http(monkeypatch, fake)
    reset_readonly_broker_runtime()
    http = StreamableHttpMcpTransport("https://example.invalid/mcp", "access-live", post=fake.post)
    checks = run_smoke(root=tmp_path, environ=os.environ, transport=http, persist=True, connect=True)
    failed = [item.name for item in checks if not item.ok]
    assert failed == [], failed
    for tool in WRITE_TOOLS:
        assert tool not in [name for name, _ in fake.tool_calls()]
    assert LIVE_ORDER_PLACEMENT is False
