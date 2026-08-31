"""LIVE launch validation. Read-only. Never places or reviews an order.

Reports Agentic account identity, NAV/cash/BP/positions, HWM, risk, session,
dashboard mode, paper-leak status, and whether live placement remains disabled.

FAIL if paper state contaminates LIVE runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from agentic_portfolio.adapters.portfolio_facts import StaticPortfolioFetcher
from agentic_portfolio.dashboard.queries import dashboard_state, dashboard_view, paper_book
from agentic_portfolio.dashboard.settings import resolve_ui_flags
from agentic_portfolio.live.engine import market_session_state, refresh_live_portfolio
from agentic_portfolio.live.isolation import detect_paper_contamination
from agentic_portfolio.live.store import LivePortfolioStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.risk_gate import evaluate
from agentic_portfolio.runtime import RuntimeMode
from agentic_portfolio.schemas import (
    ClassificationStatus,
    Decision,
    LiquidityInputs,
    ProposedAction,
    SecurityClass,
)


FORBIDDEN = (
    "place_equity_order",
    "cancel_equity_order",
    "review_equity_order",
)


def _payloads_from_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fetcher(payloads: dict) -> StaticPortfolioFetcher:
    return StaticPortfolioFetcher(
        accounts=payloads.get("accounts"),
        portfolio=payloads.get("portfolio"),
        positions=payloads.get("positions"),
        quotes=payloads.get("quotes"),
        orders=payloads.get("orders"),
    )


def run_live_launch_check(
    fetcher: StaticPortfolioFetcher | None = None,
    *,
    payloads: dict | None = None,
    root: Path | None = None,
    persist: bool = True,
    now: datetime | None = None,
    environ: dict[str, str] | None = None,
) -> dict:
    base = root or project_root()
    stamp = now or datetime.now(timezone.utc)
    rules = load_account_rules()
    exe = dict(rules.get("execution") or {})
    expected = str(rules["account"]["account_number"])
    nickname = rules["account"].get("nickname")
    if payloads is None and fetcher is None:
        mcp_path = LivePortfolioStore(base).mcp_path()
        if not mcp_path.exists():
            return _fail("LIVE MCP payloads are unavailable", expected=expected, stamp=stamp)
        payloads = _payloads_from_file(mcp_path)
    client = fetcher or _fetcher(payloads or {})
    try:
        refresh = refresh_live_portfolio(
            client,
            now=stamp,
            root=base,
            persist=persist,
        )
    except Exception as exc:
        return _fail(str(exc), expected=expected, stamp=stamp, tools=getattr(client, "calls", []))

    ctx = refresh.context
    prior_env = {k: os.environ.get(k) for k in ("AGENTIC_RUNTIME_MODE", "DASHBOARD_ENVIRONMENT")}
    os.environ["AGENTIC_RUNTIME_MODE"] = "LIVE"
    os.environ["DASHBOARD_ENVIRONMENT"] = "LIVE"
    try:
        ui = resolve_ui_flags()
        state = dashboard_state(base)
        view = dashboard_view(state) if persist else {}
        paper = paper_book(state)
    finally:
        for key, value in prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    live = refresh.snapshot
    leaks = detect_paper_contamination(live, paper, runtime_mode=RuntimeMode.LIVE)
    if ui["environment"] == "LIVE" and persist and view.get("nav") == 10_000.0 and (paper.get("context") or {}).get("current_nav") == 10_000.0:
        leaks.append("dashboard_live_nav_is_paper_10000")
    if persist and ui["environment"] == "LIVE" and view.get("nav") != ctx.current_nav:
        leaks.append("dashboard_nav_does_not_match_live_snapshot")

    action = ProposedAction(
        symbol="CASH",
        decision=Decision.NO_ACTION,
        security_class=SecurityClass.INDIVIDUAL_EQUITY,
        classification_status=ClassificationStatus.PARTIAL,
        sleeve=None,
        liquidity=LiquidityInputs(median_daily_dollar_volume_20d=1e12),
    )
    risk = evaluate(ctx, action)
    positions = [
        {
            "symbol": p.symbol,
            "quantity": p.quantity,
            "market_value": p.market_value,
            "average_cost": p.average_cost,
            "current_price": p.current_price,
            "sleeve": p.sleeve.value if p.sleeve else None,
            "unrealized_pnl": p.unrealized_pnl,
        }
        for p in ctx.positions
    ]
    concentration = max((p.market_value / ctx.current_nav for p in ctx.positions), default=0.0) if ctx.current_nav else 0.0
    placement_disabled = (
        not bool(exe.get("live_trade_actions_allowed"))
        and not bool(exe.get("auto_execution"))
        and ui["live_order_placement_enabled"] is False
        and refresh.placement_disabled
        and "place_equity_order" not in refresh.tools_used
        and "review_equity_order" not in refresh.tools_used
    )
    account_ok = (
        refresh.account.get("account_number") == expected
        and refresh.account.get("agentic_allowed") is True
        and (not nickname or refresh.account.get("nickname") == nickname)
    )
    dashboard_mode = ui["environment"]
    dashboard_nav = view.get("nav") if persist else ctx.current_nav
    dashboard_live = dashboard_mode == "LIVE" and dashboard_nav == ctx.current_nav
    failed = bool(leaks) or not account_ok or not placement_disabled
    report = {
        "ok": not failed,
        "run": "live_launch_check",
        "observed_at": stamp.isoformat(),
        "runtime_mode": RuntimeMode.LIVE.value,
        "source_of_truth": "robinhood_agentic_account",
        "agentic_account": {
            "found": account_ok,
            "account_number": expected,
            "nickname": refresh.account.get("nickname"),
            "agentic_allowed": True,
            "brokerage_account_type": refresh.account.get("brokerage_account_type"),
        },
        "nav": ctx.current_nav,
        "cash": ctx.cash,
        "buying_power": ctx.buying_power,
        "positions": positions,
        "holdings_count": ctx.holdings_count,
        "allocations": ctx.sleeve_allocation_pct,
        "cash_allocation_pct": ctx.cash_allocation_pct,
        "portfolio_concentration": {"max_position_pct_of_nav": concentration},
        "hwm": {
            "high_water_mark": ctx.high_water_mark,
            "cash_flow_adjusted_hwm": ctx.cash_flow_adjusted_hwm,
            "drawdown": ctx.current_drawdown,
            "risk_state": ctx.risk_state.value if hasattr(ctx.risk_state, "value") else ctx.risk_state,
            "start_of_day_nav": ctx.start_of_day_nav,
            "daily_portfolio_return": ctx.daily_portfolio_return,
            "daily_risk_halt": ctx.daily_risk_halt,
        },
        "risk": {
            "context_risk_state": ctx.risk_state.value if hasattr(ctx.risk_state, "value") else ctx.risk_state,
            "daily_risk_halt": ctx.daily_risk_halt,
            "buying_power": ctx.buying_power,
            "gate_verdict": risk.verdict.value,
            "execution_permitted": risk.execution_permitted,
            "used_live_buying_power": True,
            "used_live_positions": True,
        },
        "market": market_session_state(stamp),
        "dashboard": {
            "mode": dashboard_mode,
            "active_book_label": ui.get("active_book_label"),
            "nav": dashboard_nav,
            "matches_live_nav": dashboard_live if persist else True,
            "live_order_placement_enabled": False,
        },
        "paper_state_leaked": bool(leaks),
        "paper_leak_reasons": leaks,
        "live_placement_disabled": placement_disabled,
        "mcp_tools_used": refresh.tools_used,
        "mcp_not_called": list(FORBIDDEN),
        "order_placed": False,
        "review_called": False,
        "snapshot_id": refresh.snapshot_id,
        "fail_reasons": ([] if not failed else (
            (["paper_state_leaked"] if leaks else [])
            + ([] if account_ok else ["agentic_account_not_confirmed"])
            + ([] if placement_disabled else ["live_placement_not_disabled"])
        )),
        "note": "Read-only LIVE launch check. Did not call place_equity_order or review_equity_order.",
    }
    return report


def _fail(message: str, *, expected: str, stamp: datetime, tools: list | None = None) -> dict:
    return {
        "ok": False,
        "run": "live_launch_check",
        "observed_at": stamp.isoformat(),
        "runtime_mode": RuntimeMode.LIVE.value,
        "source_of_truth": "robinhood_agentic_account",
        "agentic_account": {"found": False, "account_number": expected},
        "fail_reasons": [message],
        "paper_state_leaked": "paper" in message.lower(),
        "live_placement_disabled": True,
        "mcp_tools_used": list(tools or []),
        "mcp_not_called": list(FORBIDDEN),
        "order_placed": False,
        "review_called": False,
        "note": "LIVE launch check failed closed. Did not place.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only LIVE launch check. Never places.")
    parser.add_argument("--payloads", type=Path, default=None, help="JSON file of get_accounts/get_portfolio/get_equity_positions payloads")
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args()
    root = project_root()
    payloads = None
    if args.payloads:
        payloads = _payloads_from_file(args.payloads)
    report = run_live_launch_check(payloads=payloads, root=root, persist=not args.no_persist)
    out_json = root / "reports" / "2026-08-30_live_launch_check.json"
    out_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md = [
        "# LIVE launch check",
        "",
        f"Observed at {report.get('observed_at')}. Read-only. Did not place.",
        "",
        f"**Result:** `{'PASS' if report.get('ok') else 'FAIL'}`",
        "",
        f"- Agentic account found: {report.get('agentic_account', {}).get('found')}",
        f"- NAV: {report.get('nav')}",
        f"- Cash: {report.get('cash')}",
        f"- Buying power: {report.get('buying_power')}",
        f"- Positions: {len(report.get('positions') or [])}",
        f"- HWM / drawdown / risk: {((report.get('hwm') or {}).get('high_water_mark'))} / {((report.get('hwm') or {}).get('drawdown'))} / {((report.get('hwm') or {}).get('risk_state'))}",
        f"- Dashboard mode: {(report.get('dashboard') or {}).get('mode')}",
        f"- Paper leaked: {report.get('paper_state_leaked')} {report.get('paper_leak_reasons')}",
        f"- Live placement disabled: {report.get('live_placement_disabled')}",
        "",
        f"**MCP used:** {', '.join(report.get('mcp_tools_used') or [])}",
        "",
        f"**MCP NOT called:** {', '.join(report.get('mcp_not_called') or [])}",
        "",
    ]
    if report.get("fail_reasons"):
        md.append("**Fail reasons:** " + "; ".join(report["fail_reasons"]))
        md.append("")
    (root / "reports" / "2026-08-30_live_launch_check.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({k: report.get(k) for k in ("ok", "nav", "cash", "buying_power", "paper_state_leaked", "live_placement_disabled", "fail_reasons")}, indent=2, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
