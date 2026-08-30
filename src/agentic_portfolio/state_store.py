from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agentic_portfolio.paths import project_root
from agentic_portfolio.schemas import PortfolioContext
from agentic_portfolio.session import SessionNavState, save_session_state


def hwm_state_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "state" / "hwm_state.json"


def load_hwm_state(path: Path | None = None) -> dict | None:
    p = path or hwm_state_path()
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save_hwm_state(ctx: PortfolioContext, path: Path | None = None) -> Path:
    """Persist cash-flow-adjusted HWM. Do not call this to wipe a drawdown."""
    p = path or hwm_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "account_number": ctx.account_number,
        "nav": ctx.current_nav,
        "cash_flow_adjusted_hwm": ctx.cash_flow_adjusted_hwm,
        "drawdown": ctx.current_drawdown,
        "risk_state": ctx.risk_state.value,
        "start_of_day_nav": ctx.start_of_day_nav,
        "trading_session_id": ctx.trading_session_id,
        "session_fail_safe": ctx.session_fail_safe,
        "note": "Human-only reset. Agent must not rewrite this to escape HALTED/drawdown.",
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if ctx.trading_session_id:
        save_session_state(
            SessionNavState(
                session_id=ctx.trading_session_id,
                session_date=ctx.trading_session_id,
                timezone="America/New_York",
                sod_nav=ctx.start_of_day_nav,
                sod_anchored_at=ctx.timestamp,
                calendar_provider="nyse_builtin_v1",
                calendar_available=not ctx.session_fail_safe,
                fail_safe=ctx.session_fail_safe,
                fail_safe_reason=None,
                last_observed_nav=ctx.current_nav,
                last_observed_at=ctx.timestamp,
            ),
            p.parent / "session_state.json",
        )
    return p
