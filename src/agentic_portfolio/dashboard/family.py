"""Simple family share of the paper book. Not a separate trading portfolio."""

from __future__ import annotations

import json
from typing import Any

from agentic_portfolio.dashboard.accounts import public_user
from agentic_portfolio.dashboard.queries import (
    DashboardState,
    allocation_slices,
    candidate_rows,
    paper_book,
    paper_context,
    spy_benchmark,
)
from agentic_portfolio.dashboard.settings import LIVE_ACCOUNT_LABEL, PAPER_BOOK_LABEL, resolve_ui_flags


def _usd(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${float(value):,.2f}"


def _signed_usd(value: float | None) -> str:
    if value is None:
        return "—"
    number = float(value)
    if number > 0:
        return f"+${number:,.2f}"
    if number < 0:
        return f"-${abs(number):,.2f}"
    return f"${number:,.2f}"


def _pct_fraction(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100.0:.2f}%"


def _signed_pct(value: float | None) -> str:
    if value is None:
        return "—"
    number = float(value) * 100.0
    if number > 0:
        return f"+{number:.2f}%"
    return f"{number:.2f}%"


def current_paper_nav(state: DashboardState) -> float | None:
    ctx = paper_context(state)
    nav = ctx.get("current_nav")
    if nav is None:
        return None
    return float(nav)


def scaled_share(
    assigned_amount: float | None,
    baseline_nav: float | None,
    current_nav: float | None,
) -> dict[str, Any]:
    """Family dollars track paper NAV from the assignment baseline.

    Example: $2,000 assigned when NAV=$10,000; later NAV=$11,000 → $2,200.
    Changing the assigned amount resets the baseline at that moment.
    """
    starting = None if assigned_amount is None else float(assigned_amount)
    payload = {
        "starting": starting,
        "starting_display": _usd(starting),
        "current_value": None,
        "current_value_display": "—",
        "gain_loss": None,
        "gain_loss_display": "—",
        "return_pct": None,
        "return_display": "—",
        "assigned": starting is not None and baseline_nav is not None,
    }
    if starting is None or baseline_nav is None or current_nav is None:
        return payload
    baseline = float(baseline_nav)
    if baseline <= 0:
        return payload
    current_value = starting * (float(current_nav) / baseline)
    gain_loss = current_value - starting
    return_pct = None if starting == 0 else (current_value / starting) - 1.0
    payload.update(
        {
            "current_value": current_value,
            "current_value_display": _usd(current_value),
            "gain_loss": gain_loss,
            "gain_loss_display": _signed_usd(gain_loss),
            "return_pct": return_pct,
            "return_display": _signed_pct(return_pct),
            "assigned": True,
        }
    )
    return payload


def parse_amount(raw: Any) -> float:
    text = str(raw or "").strip().replace(",", "").replace("$", "")
    if not text:
        raise ValueError("amount is required")
    amount = float(text)
    if amount < 0:
        raise ValueError("assigned amount cannot be negative")
    return amount


def nav_history(state: DashboardState) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    seen: set[str] = set()
    snap_dir = state.root / "state" / "paper_book" / "snapshots"

    def _add(at: Any, nav: Any) -> None:
        if nav is None or at is None:
            return
        stamp = str(at)
        if stamp in seen:
            return
        seen.add(stamp)
        points.append({"at": stamp, "nav": float(nav)})

    if snap_dir.is_dir():
        paths = sorted(snap_dir.glob("*.json"), key=lambda path: path.name)
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            ctx = data.get("context") or {}
            _add(ctx.get("timestamp") or data.get("created_at"), ctx.get("current_nav"))
    book = paper_book(state) or {}
    ctx = paper_context(state)
    _add(ctx.get("timestamp") or book.get("created_at"), ctx.get("current_nav"))
    points.sort(key=lambda row: str(row.get("at") or ""))
    return points


def performance_series(user: dict[str, Any], state: DashboardState) -> list[dict[str, Any]]:
    assigned = user.get("assigned_amount")
    baseline = user.get("baseline_nav")
    assigned_at = user.get("assigned_at")
    if assigned is None or baseline is None:
        return []
    series: list[dict[str, Any]] = []

    def _append(stamp: str, nav: float | None) -> None:
        share = scaled_share(assigned, baseline, nav)
        if share["current_value"] is None:
            return
        point = {
            "at": stamp,
            "value": share["current_value"],
            "value_display": share["current_value_display"],
        }
        if series and series[-1]["at"] == stamp:
            series[-1] = point
            return
        series.append(point)

    if assigned_at is not None:
        _append(str(assigned_at), baseline)
    for point in nav_history(state):
        stamp = str(point["at"])
        if assigned_at and stamp < str(assigned_at):
            continue
        _append(stamp, point["nav"])
    current = current_paper_nav(state)
    if current is not None:
        _append("current", current)
    return series


def chart_from_series(series: list[dict[str, Any]], *, width: int = 420, height: int = 140, pad: int = 14) -> dict[str, Any] | None:
    if len(series) < 2:
        return None
    values = [float(point["value"]) for point in series]
    vmin = min(values)
    vmax = max(values)
    span = vmax - vmin
    if span == 0:
        span = 1.0
        vmin -= 0.5
    inner_w = width - (2 * pad)
    inner_h = height - (2 * pad)
    last = max(len(series) - 1, 1)
    pairs: list[str] = []
    for index, point in enumerate(series):
        x = pad + inner_w * (index / last)
        y = pad + inner_h * (1.0 - ((float(point["value"]) - vmin) / span))
        pairs.append(f"{x:.1f},{y:.1f}")
    return {
        "width": width,
        "height": height,
        "polyline": " ".join(pairs),
        "start_display": series[0]["value_display"],
        "end_display": series[-1]["value_display"],
        "start_at": series[0]["at"],
        "end_at": series[-1]["at"],
        "point_count": len(series),
    }


def allocation_percentages(state: DashboardState) -> dict[str, Any]:
    ctx = paper_context(state)
    sleeves = []
    for name, value in (ctx.get("sleeve_allocation_pct") or {}).items():
        sleeves.append({"sleeve": name, "pct": value, "display": _pct_fraction(value)})
    nav = ctx.get("current_nav")
    positions = []
    for pos in ctx.get("positions") or []:
        mv = pos.get("market_value")
        pct = (mv / nav) if nav and mv is not None else None
        positions.append(
            {
                "symbol": pos.get("symbol"),
                "sleeve": pos.get("sleeve"),
                "allocation_display": _pct_fraction(pct),
            }
        )
    cash_pct = ctx.get("cash_allocation_pct")
    return {
        "sleeves": sleeves,
        "positions": positions,
        "cash_pct": cash_pct,
        "cash_pct_display": _pct_fraction(cash_pct),
    }


def family_spy(state: DashboardState) -> dict[str, Any]:
    ctx = paper_context(state)
    spy = ctx.get("spy")
    if isinstance(spy, dict) and spy:
        observed = spy_benchmark(ctx, [])
        payload = dict(observed)
        payload.pop("payload", None)
        note = payload.get("note") or "Observed SPY from the paper book. Not fabricated."
        payload["note"] = note
        if "price" not in payload and spy.get("price") is not None:
            payload["price"] = spy.get("price")
        if spy.get("return_pct") is not None:
            payload["return_pct"] = spy.get("return_pct")
            payload["return_display"] = _signed_pct(spy.get("return_pct"))
        return payload
    return {
        "observed": False,
        "source": None,
        "note": "SPY comparison is not on the current paper book snapshot. Not fabricated.",
    }


def member_row(user: dict[str, Any], current_nav: float | None) -> dict[str, Any]:
    share = scaled_share(user.get("assigned_amount"), user.get("baseline_nav"), current_nav)
    row = public_user(user) or {}
    row.update(share)
    row.pop("baseline_nav", None)
    return row


def family_admin_view(state: DashboardState, users: list[dict[str, Any]]) -> dict[str, Any]:
    ui = resolve_ui_flags()
    nav = current_paper_nav(state)
    members = [member_row(user, nav) for user in users]
    return {
        "members": members,
        "member_count": len(members),
        "environment": ui["environment"],
        "paper_book_label": PAPER_BOOK_LABEL,
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "live_order_placement_enabled": False,
        "nav_available": nav is not None and nav > 0,
    }


def _public_candidates(state: DashboardState) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in candidate_rows(state):
        rows.append(
            {
                "symbol": raw.get("symbol"),
                "score_display": raw.get("score_display"),
                "priority": raw.get("priority"),
                "priority_label": raw.get("priority_label"),
                "provisional_sleeve": raw.get("provisional_sleeve"),
                "sleeve_label": raw.get("sleeve_label"),
                "status": raw.get("status"),
                "status_label": raw.get("status_label"),
                "research_status": raw.get("research_status"),
                "research_status_label": raw.get("research_status_label"),
            }
        )
    return rows


def family_member_view(state: DashboardState, user: dict[str, Any]) -> dict[str, Any]:
    ui = resolve_ui_flags()
    nav = current_paper_nav(state)
    row = member_row(user, nav)
    series = performance_series(user, state)
    allocations = allocation_percentages(state)
    slices = allocation_slices(paper_context(state))
    candidates = _public_candidates(state)
    return {
        "name": row.get("name"),
        "username": row.get("username"),
        "starting": row.get("starting"),
        "starting_display": row.get("starting_display"),
        "current_value": row.get("current_value"),
        "current_value_display": row.get("current_value_display"),
        "gain_loss": row.get("gain_loss"),
        "gain_loss_display": row.get("gain_loss_display"),
        "return_pct": row.get("return_pct"),
        "return_display": row.get("return_display"),
        "assigned": row.get("assigned"),
        "chart": chart_from_series(series),
        "series": series,
        "allocation": allocations,
        "allocation_chart": {
            "labels": [item["label"] for item in slices],
            "values": [round(item["pct"] * 100.0, 4) for item in slices],
            "keys": [item["key"] for item in slices],
            "slices": slices,
        },
        "candidates": candidates,
        "candidate_count": len(candidates),
        "spy": family_spy(state),
        "environment": ui["environment"],
        "paper_book_label": PAPER_BOOK_LABEL,
        "live_account_label": LIVE_ACCOUNT_LABEL,
        "live_order_placement_enabled": False,
    }
