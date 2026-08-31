"""Refresh LIVE portfolio context from Robinhood. Fail closed. Never places."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from agentic_portfolio.adapters.portfolio_facts import (
    LiveAccountError,
    LiveDataUnavailable,
    PortfolioFetcher,
    confirm_agentic_account,
    parse_open_orders,
    parse_portfolio,
    parse_positions,
    parse_spy,
)
from agentic_portfolio.calendar import EASTERN, NyseEquityCalendar, REGULAR_OPEN
from agentic_portfolio.context import build_context, portfolio_context_from_dict
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.live.isolation import assert_live_isolated, detect_paper_contamination
from agentic_portfolio.live.safety import assert_no_forbidden_tools, assert_placement_disabled
from agentic_portfolio.live.store import LivePortfolioStore
from agentic_portfolio.paper_fill.store import PaperFillStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.runtime import LIVE_SOURCE_OF_TRUTH, RuntimeMode, resolve_runtime_mode
from agentic_portfolio.schemas import PortfolioContext, RiskState, to_dict
from agentic_portfolio.session import load_session_state, observe_nav_for_session
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.state_store import load_hwm_state, save_hwm_state
from agentic_portfolio.thesis_registry import ThesisRegistry


FORBIDDEN_NOW = (
    "place_equity_order",
    "cancel_equity_order",
    "review_equity_order",
)


@dataclass
class LiveRefreshResult:
    snapshot_id: str
    account: dict[str, Any]
    context: PortfolioContext
    snapshot: dict[str, Any]
    tools_used: list[str]
    leaks: list[str] = field(default_factory=list)
    placement_disabled: bool = True


def journal_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "logs" / "live_portfolio.jsonl"


def market_session_state(now: datetime | None = None) -> dict[str, Any]:
    stamp = now or datetime.now(timezone.utc)
    cal = NyseEquityCalendar()
    current = cal.session_for(stamp)
    last = cal.current_or_last_session(stamp)
    et = stamp.astimezone(EASTERN)
    regular_open = False
    status = "closed"
    reason = "non_trading_day"
    if current is not None:
        close = current.close_time
        regular_open = REGULAR_OPEN <= et.time() < close
        status = "regular_hours" if regular_open else "closed"
        reason = "same_session" if regular_open else "outside_regular_hours"
        if et.time() < REGULAR_OPEN:
            reason = "pre_open"
    elif et.weekday() >= 5:
        reason = "weekend"
    completed = cal.latest_completed_session(stamp)
    return {
        "timezone": "America/New_York",
        "observed_at": stamp.isoformat(),
        "trading_session": current is not None,
        "regular_hours_open": regular_open,
        "status": status,
        "reason": reason,
        "session_id": current.session_id if current else (last.session_id if last else None),
        "session_date": (current.session_date.isoformat() if current else (last.session_date.isoformat() if last else None)),
        "latest_completed_session": completed.session_id if completed else None,
        "is_early_close": bool(current.is_early_close) if current else False,
        "weekday": et.strftime("%A"),
    }


def _looks_like_paper_hwm(legacy: Mapping[str, Any] | None, paper_ctx: Mapping[str, Any] | None) -> bool:
    if not legacy:
        return False
    try:
        nav = float(legacy.get("nav"))
        hwm = float(legacy.get("cash_flow_adjusted_hwm"))
    except (TypeError, ValueError):
        return False
    paper_nav = paper_ctx.get("current_nav") if paper_ctx else None
    if paper_nav is not None and nav == float(paper_nav) == 10_000.0:
        return True
    if nav == hwm == 10_000.0 and paper_nav is not None and float(paper_nav) == 10_000.0:
        return True
    return False


def _prior_hwm(
    *,
    store: LivePortfolioStore,
    account_number: str,
    paper_ctx: Mapping[str, Any] | None,
) -> tuple[float | None, float | None]:
    live_hwm = load_hwm_state(store.hwm_path())
    if live_hwm and str(live_hwm.get("account_number") or "") in {"", account_number}:
        return (
            float(live_hwm["nav"]) if live_hwm.get("nav") is not None else None,
            float(live_hwm["cash_flow_adjusted_hwm"]) if live_hwm.get("cash_flow_adjusted_hwm") is not None else None,
        )
    legacy = load_hwm_state(store.base / "state" / "hwm_state.json")
    if legacy and str(legacy.get("account_number") or "") == account_number and not _looks_like_paper_hwm(legacy, paper_ctx):
        return (
            float(legacy["nav"]) if legacy.get("nav") is not None else None,
            float(legacy["cash_flow_adjusted_hwm"]) if legacy.get("cash_flow_adjusted_hwm") is not None else None,
        )
    return None, None


def refresh_live_portfolio(
    fetcher: PortfolioFetcher,
    *,
    now: datetime | None = None,
    root: Path | None = None,
    persist: bool = True,
    store: LivePortfolioStore | None = None,
    sleeves: SleeveRegistry | None = None,
    theses: ThesisRegistry | None = None,
    account_rules: dict | None = None,
    journal: Path | None = None,
    sources_observed: list[str] | None = None,
) -> LiveRefreshResult:
    """Fetch Agentic Robinhood facts, build LIVE context, persist snapshot. Never places."""
    rules = account_rules or load_account_rules()
    exe = dict(rules.get("execution") or {})
    assert_placement_disabled(
        live_trade_actions_allowed=bool(exe.get("live_trade_actions_allowed")),
        auto_execution=bool(exe.get("auto_execution")),
        live_order_placement_enabled=False,
    )
    used = list(sources_observed or [])
    assert_no_forbidden_tools(used)
    for tool in FORBIDDEN_NOW:
        if tool in used:
            raise LiveDataUnavailable(f"LIVE refresh refused {tool}")

    stamp = now or datetime.now(timezone.utc)
    base = root or project_root()
    store = store or LivePortfolioStore(base)
    expected = str(rules["account"]["account_number"])
    paper_book = PaperFillStore(base).current_book() or {}
    paper_ctx = dict(paper_book.get("context") or {})

    accounts_payload = fetcher.get_accounts()
    used.append("get_accounts")
    if not accounts_payload:
        raise LiveDataUnavailable("get_accounts unavailable")
    account = confirm_agentic_account(accounts_payload, expected_number=expected, rules=rules)

    portfolio_payload = fetcher.get_portfolio(expected)
    used.append("get_portfolio")
    if not portfolio_payload:
        raise LiveDataUnavailable("get_portfolio unavailable")
    book = parse_portfolio(portfolio_payload)

    positions_payload = fetcher.get_equity_positions(expected)
    used.append("get_equity_positions")
    if not positions_payload:
        raise LiveDataUnavailable("get_equity_positions unavailable")

    quotes_payload = None
    spy = None
    pos_preview = (positions_payload.get("data") or positions_payload).get("positions") if isinstance(positions_payload, dict) else None
    symbols = [str(p.get("symbol") or "").upper() for p in (pos_preview or []) if isinstance(p, dict) and p.get("symbol")]
    quote_symbols = list(dict.fromkeys(symbols + ["SPY"]))
    try:
        quotes_payload = fetcher.get_equity_quotes(quote_symbols)
        used.append("get_equity_quotes")
        spy = parse_spy(quotes_payload)
    except Exception:
        quotes_payload = None
        if symbols:
            raise LiveDataUnavailable("LIVE quotes unavailable for open positions") from None

    sleeves = sleeves or SleeveRegistry(base / "state" / "sleeve_registry.json")
    theses = theses or ThesisRegistry(base / "state" / "thesis_registry.json")
    positions = parse_positions(positions_payload, quotes=quotes_payload, sleeves=sleeves, theses=theses)

    orders_payload = None
    open_orders = []
    if hasattr(fetcher, "get_equity_orders"):
        try:
            orders_payload = fetcher.get_equity_orders(expected)
            used.append("get_equity_orders")
            open_orders = parse_open_orders(orders_payload)
        except Exception:
            open_orders = []

    session_prior = load_session_state(store.session_path())
    session = observe_nav_for_session(
        current_nav=book["current_nav"],
        now=stamp,
        prior=session_prior,
        persist_path=store.session_path() if persist else None,
    )
    prior_nav, prior_hwm = _prior_hwm(store=store, account_number=expected, paper_ctx=paper_ctx)

    context = build_context(
        account_number=expected,
        current_nav=book["current_nav"],
        cash=book["cash"],
        buying_power=book["buying_power"],
        positions=positions,
        open_orders=open_orders,
        start_of_day_nav=session.sod_nav,
        prior_nav=prior_nav,
        prior_hwm=prior_hwm,
        spy=spy,
        timestamp=stamp.isoformat(),
        trading_session_id=session.session_id,
        session_fail_safe=session.fail_safe,
    )

    snapshot_id = str(uuid4())
    snapshot = {
        "snapshot_id": snapshot_id,
        "created_at": stamp.isoformat(),
        "runtime_mode": RuntimeMode.LIVE.value,
        "source_of_truth": LIVE_SOURCE_OF_TRUTH,
        "paper_environment": False,
        "live_order_placement_enabled": False,
        "account": {
            "account_number": expected,
            "nickname": account.get("nickname"),
            "agentic_allowed": True,
            "brokerage_account_type": account.get("brokerage_account_type"),
            "brokerage_trading_type": account.get("type"),
        },
        "mcp_tools_used": list(dict.fromkeys(used)),
        "mcp_not_called": list(FORBIDDEN_NOW),
        "context": to_dict(context),
        "session": session.to_dict(),
        "market": market_session_state(stamp),
    }
    leaks = detect_paper_contamination(snapshot, paper_book, runtime_mode=RuntimeMode.LIVE)
    if leaks:
        raise LiveDataUnavailable("paper state leaked into LIVE runtime: " + ", ".join(leaks))
    assert_live_isolated(snapshot, paper_book)

    if persist:
        store.save_snapshot(snapshot_id, snapshot)
        save_hwm_state(context, store.hwm_path())
        store.save_mcp(
            {
                "observed_at": stamp.isoformat(),
                "account_number": expected,
                "accounts": accounts_payload,
                "portfolio": portfolio_payload,
                "positions": positions_payload,
                "quotes": quotes_payload,
                "orders": orders_payload,
                "mcp_tools_used": snapshot["mcp_tools_used"],
                "mcp_not_called": snapshot["mcp_not_called"],
            }
        )
        append_jsonl(
            {
                "type": "LIVE_PORTFOLIO_REFRESHED",
                "snapshot_id": snapshot_id,
                "account_number": expected,
                "nav": context.current_nav,
                "cash": context.cash,
                "buying_power": context.buying_power,
                "holdings_count": context.holdings_count,
                "risk_state": context.risk_state.value if isinstance(context.risk_state, RiskState) else context.risk_state,
                "source_of_truth": LIVE_SOURCE_OF_TRUTH,
                "live_order_placement_enabled": False,
                "mcp_tools_used": snapshot["mcp_tools_used"],
                "mcp_not_called": snapshot["mcp_not_called"],
            },
            journal or journal_path(base),
        )

    return LiveRefreshResult(
        snapshot_id=snapshot_id,
        account=dict(snapshot["account"]),
        context=context,
        snapshot=snapshot,
        tools_used=list(snapshot["mcp_tools_used"]),
        leaks=[],
        placement_disabled=True,
    )


def load_live_context(root: Path | None = None) -> PortfolioContext:
    store = LivePortfolioStore(root)
    book = store.current_book()
    if not book:
        raise LiveDataUnavailable("LIVE snapshot is not available")
    paper = PaperFillStore(root).current_book()
    assert_live_isolated(book, paper)
    ctx = book.get("context")
    if not ctx:
        raise LiveDataUnavailable("LIVE snapshot has no portfolio context")
    return portfolio_context_from_dict(ctx)


def live_context_or_none(root: Path | None = None) -> PortfolioContext | None:
    try:
        return load_live_context(root)
    except (LiveDataUnavailable, LiveAccountError, FileNotFoundError):
        return None


def load_runtime_portfolio_context(
    root: Path | None = None,
    *,
    mode: RuntimeMode | None = None,
) -> PortfolioContext:
    """PAPER → isolated paper book. LIVE → Robinhood snapshot. Never mix."""
    current = mode or resolve_runtime_mode()
    if current is RuntimeMode.LIVE:
        return load_live_context(root)
    book = PaperFillStore(root).current_book() or {}
    ctx = book.get("context")
    if not ctx:
        raise LiveDataUnavailable("paper book NAV is not available")
    return portfolio_context_from_dict(ctx)
