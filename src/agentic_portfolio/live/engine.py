"""Refresh LIVE portfolio context from Robinhood. Fail closed. Never places."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from agentic_portfolio.adapters.portfolio_facts import (
    LiveAccountError,
    LiveDataUnavailable,
    LiveErrorCode,
    PortfolioFetcher,
    confirm_agentic_account,
    parse_filled_orders,
    parse_open_orders,
    parse_portfolio,
    parse_positions,
    parse_spy,
)
from agentic_portfolio.calendar import EASTERN, NyseEquityCalendar, REGULAR_OPEN
from agentic_portfolio.cash_flow import (
    FLOW_EPS,
    TradeFill,
    observation_from_context_dict,
    observation_from_facts,
    reconcile_external_flow,
    reconstruct_session_external_flow,
    select_session_external_flow,
)
from agentic_portfolio.context import build_context, portfolio_context_from_dict
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.live.isolation import assert_live_isolated, detect_paper_contamination
from agentic_portfolio.live.safety import assert_no_forbidden_tools, assert_placement_disabled
from agentic_portfolio.live.store import LivePortfolioStore
from agentic_portfolio.paper_fill.store import PaperFillStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.runtime import LIVE_SOURCE_OF_TRUTH, RuntimeMode, live_placement_enabled, resolve_runtime_mode
from agentic_portfolio.schemas import PortfolioContext, RiskState, to_dict
from agentic_portfolio.session import SessionNavState, load_session_state, observe_nav_for_session, save_session_state
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


def _recover_session_capital_flow(
    *,
    store: LivePortfolioStore,
    session: SessionNavState,
    session_prior: SessionNavState | None,
    current_obs,
    persist: bool,
) -> tuple[SessionNavState, float]:
    """Rebuild same-session external flow from LIVE snapshots when legacy state missed it.

    Does not mutate historical snapshot files. Returns the recovered delta versus
    persisted session flow (0 when accounted state is already correct).
    """
    persisted = float(session_prior.session_external_capital_flow or 0.0) if session_prior else 0.0
    if session.fail_safe or not session.session_id:
        return session, 0.0
    if session_prior and session_prior.session_id and session.session_id != session_prior.session_id:
        return session, 0.0
    reconstructed = reconstruct_session_external_flow(
        store.list_snapshot_records(),
        session_id=session.session_id,
        current=current_obs,
    )
    accounted = float(session.session_external_capital_flow or 0.0)
    chosen = select_session_external_flow(
        accounted=accounted,
        reconstructed=reconstructed,
        sod_nav=session.sod_nav,
    )
    if abs(chosen - accounted) <= FLOW_EPS:
        return session, 0.0
    session.session_external_capital_flow = chosen
    if persist:
        save_session_state(session, store.session_path())
    return session, chosen - persisted


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
    placement_enabled = bool(live_placement_enabled())
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

    try:
        accounts_payload = fetcher.get_accounts()
    except LiveDataUnavailable:
        raise
    except AttributeError as exc:
        raise LiveDataUnavailable(
            f"production adapter is missing get_accounts: {exc}",
            code=LiveErrorCode.MCP_GET_ACCOUNTS_FAILED,
        ) from exc
    except Exception as exc:
        raise LiveDataUnavailable(f"get_accounts failed: {type(exc).__name__}: {exc}", code=LiveErrorCode.MCP_GET_ACCOUNTS_FAILED) from exc
    used.append("get_accounts")
    if not accounts_payload:
        raise LiveDataUnavailable("get_accounts unavailable", code=LiveErrorCode.MCP_GET_ACCOUNTS_FAILED)
    try:
        account = confirm_agentic_account(accounts_payload, expected_number=expected, rules=rules)
    except LiveAccountError as exc:
        raise LiveAccountError(str(exc), code=LiveErrorCode.ACCOUNT_IDENTITY_MISMATCH) from exc

    try:
        portfolio_payload = fetcher.get_portfolio(expected)
    except LiveDataUnavailable:
        raise
    except Exception as exc:
        raise LiveDataUnavailable(f"get_portfolio failed: {type(exc).__name__}: {exc}", code=LiveErrorCode.MCP_GET_PORTFOLIO_FAILED) from exc
    used.append("get_portfolio")
    if not portfolio_payload:
        raise LiveDataUnavailable("get_portfolio unavailable", code=LiveErrorCode.MCP_GET_PORTFOLIO_FAILED)
    try:
        book = parse_portfolio(portfolio_payload)
    except LiveDataUnavailable as exc:
        raise LiveDataUnavailable(str(exc), code=exc.code if exc.code != LiveErrorCode.LIVE_DATA_UNAVAILABLE else LiveErrorCode.MCP_GET_PORTFOLIO_FAILED) from exc

    try:
        positions_payload = fetcher.get_equity_positions(expected)
    except LiveDataUnavailable:
        raise
    except Exception as exc:
        raise LiveDataUnavailable(f"get_equity_positions failed: {type(exc).__name__}: {exc}", code=LiveErrorCode.MCP_GET_POSITIONS_FAILED) from exc
    used.append("get_equity_positions")
    if not positions_payload:
        raise LiveDataUnavailable("get_equity_positions unavailable", code=LiveErrorCode.MCP_GET_POSITIONS_FAILED)

    quotes_payload = None
    spy = None
    pos_preview = (positions_payload.get("data") or positions_payload).get("positions") if isinstance(positions_payload, dict) else None
    symbols = [str(p.get("symbol") or "").upper() for p in (pos_preview or []) if isinstance(p, dict) and p.get("symbol")]
    quote_symbols = list(dict.fromkeys(symbols + ["SPY"]))
    try:
        quotes_payload = fetcher.get_equity_quotes(quote_symbols)
        used.append("get_equity_quotes")
        spy = parse_spy(quotes_payload)
    except LiveDataUnavailable as exc:
        quotes_payload = None
        if symbols:
            raise LiveDataUnavailable(
                str(exc) if str(exc) else "LIVE quotes unavailable for open positions",
                code=exc.code if exc.code != LiveErrorCode.LIVE_DATA_UNAVAILABLE else LiveErrorCode.MCP_QUOTES_FAILED,
            ) from exc
        spy = None
    except Exception:
        quotes_payload = None
        if symbols:
            raise LiveDataUnavailable("LIVE quotes unavailable for open positions", code=LiveErrorCode.MCP_QUOTES_FAILED) from None

    sleeves = sleeves or SleeveRegistry(base / "state" / "sleeve_registry.json")
    theses = theses or ThesisRegistry(base / "state" / "thesis_registry.json")
    try:
        positions = parse_positions(positions_payload, quotes=quotes_payload, sleeves=sleeves, theses=theses)
        _overlay_live_position_links(positions, base)
    except LiveDataUnavailable as exc:
        if "market value unavailable" in str(exc).lower():
            raise LiveDataUnavailable(str(exc), code=LiveErrorCode.MCP_QUOTES_FAILED) from exc
        raise LiveDataUnavailable(str(exc), code=exc.code if getattr(exc, "code", None) else LiveErrorCode.MCP_GET_POSITIONS_FAILED) from exc

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
    prior_snapshot = store.current_book() if persist else None
    prior_ctx = dict((prior_snapshot or {}).get("context") or {}) if prior_snapshot else {}
    prior_obs = observation_from_context_dict(prior_ctx)
    current_obs = observation_from_facts(nav=book["current_nav"], cash=book["cash"], positions=positions)
    fills = [
        TradeFill(symbol=str(row["symbol"]), side=str(row["side"]), quantity=float(row["quantity"]), price=float(row["price"]))
        for row in parse_filled_orders(orders_payload, since=str(prior_ctx.get("timestamp") or "") or None)
    ]
    recon = reconcile_external_flow(prior_obs, current_obs, fills=fills)
    session = observe_nav_for_session(
        current_nav=book["current_nav"],
        now=stamp,
        prior=session_prior,
        persist_path=store.session_path() if persist else None,
        incremental_external_flow=recon.external_capital_flow,
        current_cash=book["cash"],
    )
    session, recovered_delta = _recover_session_capital_flow(
        store=store,
        session=session,
        session_prior=session_prior,
        current_obs=current_obs,
        persist=persist,
    )
    prior_nav, prior_hwm = _prior_hwm(store=store, account_number=expected, paper_ctx=paper_ctx)
    hwm_flow = recon.external_capital_flow
    if (
        abs(recovered_delta) > FLOW_EPS
        and prior_nav is not None
        and abs((float(book["current_nav"]) - float(prior_nav)) - recovered_delta) <= FLOW_EPS
    ):
        hwm_flow = recovered_delta

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
        external_capital_flow=hwm_flow,
        session_external_capital_flow=float(session.session_external_capital_flow or 0.0),
        spy=spy,
        timestamp=stamp.isoformat(),
        trading_session_id=session.session_id,
        session_fail_safe=session.fail_safe,
    )
    if abs(recovered_delta) > FLOW_EPS and abs(hwm_flow - recovered_delta) > FLOW_EPS:
        context = replace(context, external_capital_flow=recovered_delta)

    snapshot_id = str(uuid4())
    snapshot = {
        "snapshot_id": snapshot_id,
        "created_at": stamp.isoformat(),
        "runtime_mode": RuntimeMode.LIVE.value,
        "source_of_truth": LIVE_SOURCE_OF_TRUTH,
        "paper_environment": False,
        "live_order_placement_enabled": placement_enabled,
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
        try:
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
                    "external_capital_flow": context.external_capital_flow,
                    "session_external_capital_flow": context.session_external_capital_flow,
                    "daily_portfolio_return": context.daily_portfolio_return,
                    "source_of_truth": LIVE_SOURCE_OF_TRUTH,
                    "live_order_placement_enabled": placement_enabled,
                    "mcp_tools_used": snapshot["mcp_tools_used"],
                    "mcp_not_called": snapshot["mcp_not_called"],
                },
                journal or journal_path(base),
            )
            store.clear_error()
        except LiveDataUnavailable:
            raise
        except OSError as exc:
            raise LiveDataUnavailable(
                f"LIVE snapshot persist failed: {exc}",
                code=LiveErrorCode.LIVE_SNAPSHOT_PERSIST_FAILED,
            ) from exc

    return LiveRefreshResult(
        snapshot_id=snapshot_id,
        account=dict(snapshot["account"]),
        context=context,
        snapshot=snapshot,
        tools_used=list(snapshot["mcp_tools_used"]),
        leaks=[],
        placement_disabled=True,
    )


def _overlay_live_position_links(positions, root: Path) -> None:
    """Preserve thesis/sleeve from filled-order links when the broker book has only a symbol."""
    try:
        from agentic_portfolio.live_execution.positions import load_links
        from dataclasses import replace

        links = load_links(root, mode=RuntimeMode.LIVE)
    except Exception:  # noqa: BLE001 — missing links must not fail the refresh
        return
    if not links:
        return
    for idx, pos in enumerate(list(positions)):
        link = links.get(pos.symbol.upper())
        if link is None:
            continue
        updates: dict[str, Any] = {}
        thesis_id = pos.thesis_id or link.thesis_id
        if thesis_id and thesis_id != pos.thesis_id:
            updates["thesis_id"] = thesis_id
        if pos.sleeve is None and link.sleeve:
            from agentic_portfolio.schemas import Sleeve

            try:
                updates["sleeve"] = Sleeve(str(link.sleeve).upper())
            except ValueError:
                pass
        if updates:
            positions[idx] = replace(pos, **updates)


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
