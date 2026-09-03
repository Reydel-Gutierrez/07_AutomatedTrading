import pytest

from agentic_portfolio.context import build_context
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.schemas import (
    ClassificationStatus,
    Decision,
    LiquidityInputs,
    Position,
    ProposedAction,
    SecurityClass,
    Sleeve,
)

ACCOUNT = load_account_rules()["account"]["account_number"]

HUGE_ADV = LiquidityInputs(median_daily_dollar_volume_20d=1e12)


@pytest.fixture(autouse=True)
def _isolate_live_and_ai_env(monkeypatch, tmp_path):
    """Tests must not inherit LIVE shell secrets or runtime flags."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENTIC_RUNTIME_MODE", raising=False)
    monkeypatch.delenv("DASHBOARD_ENVIRONMENT", raising=False)
    monkeypatch.delenv("AGENTIC_READONLY_MCP_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_READONLY_MCP_URL", raising=False)
    monkeypatch.delenv("AGENTIC_READONLY_MCP_TOKEN_FILE", raising=False)
    monkeypatch.delenv("AGENTIC_LIVE_ORDER_PLACEMENT", raising=False)
    monkeypatch.delenv("LIVE_ORDER_PLACEMENT", raising=False)
    monkeypatch.delenv("TELEGRAM_NOTIFICATIONS", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("AGENTIC_PUBLIC_DASHBOARD_URL", raising=False)
    monkeypatch.setenv("AGENTIC_READONLY_MCP_HOME", str(tmp_path / "mcp-home"))
    from agentic_portfolio.runtime import reset_readonly_broker_runtime
    from agentic_portfolio.live_execution.broker import reset_live_write_broker

    reset_readonly_broker_runtime()
    reset_live_write_broker()


def ctx(
    nav: float,
    positions: list[Position] | None = None,
    *,
    start_of_day_nav: float | None = None,
    prior_hwm: float | None = None,
    prior_nav: float | None = None,
    external_capital_flow: float = 0.0,
    cash: float | None = None,
    buying_power: float | None = None,
    spy=None,
    session_external_capital_flow: float | None = None,
):
    positions = positions or []
    invested = sum(p.market_value for p in positions)
    cash = nav - invested if cash is None else cash
    bp = cash if buying_power is None else buying_power
    sod = start_of_day_nav if start_of_day_nav is not None else nav
    return build_context(
        account_number=ACCOUNT,
        current_nav=nav,
        cash=cash,
        buying_power=bp,
        positions=positions,
        start_of_day_nav=sod,
        prior_nav=prior_nav if prior_nav is not None else (nav if external_capital_flow == 0 else None),
        prior_hwm=prior_hwm if prior_hwm is not None else nav,
        external_capital_flow=external_capital_flow,
        session_external_capital_flow=session_external_capital_flow,
        spy=spy,
    )


def pos(symbol, pct, nav, sleeve, cls, sector=None, status=ClassificationStatus.VALIDATED):
    return Position(
        symbol=symbol,
        market_value=pct * nav,
        sleeve=sleeve,
        security_class=cls,
        classification_status=status,
        sector=sector,
    )


def act(**kwargs) -> ProposedAction:
    kwargs.setdefault("classification_status", ClassificationStatus.VALIDATED)
    kwargs.setdefault("liquidity", HUGE_ADV)
    kwargs.setdefault("decision", Decision.BUY)
    kwargs.setdefault("current_price", 100.0)
    return ProposedAction(**kwargs)
