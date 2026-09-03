"""Observed NAV snapshots for dashboard charts. Never fabricates prices."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import RuntimeMode, resolve_runtime_mode

HISTORY_NAME = "dashboard_nav_history.json"
MIN_CHART_POINTS = 2


def history_path(root: Path | None = None, *, mode: str | None = None) -> Path:
    base = Path(root) if root is not None else project_root()
    current = (mode or resolve_runtime_mode().value).upper()
    if current == RuntimeMode.LIVE.value:
        return base / "state" / "live_book" / "nav_history.json"
    return base / "state" / HISTORY_NAME


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _spy_level(spy: Any) -> float | None:
    if not isinstance(spy, dict) or not spy:
        return None
    for key in ("return_pct", "close", "price", "last", "value"):
        raw = spy.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _point(at: Any, nav: Any, spy: Any = None, **extra: Any) -> dict[str, Any] | None:
    if at is None or nav is None:
        return None
    try:
        nav_f = float(nav)
    except (TypeError, ValueError):
        return None
    spy_f: float | None
    if isinstance(spy, (int, float)):
        spy_f = float(spy)
    else:
        spy_f = _spy_level(spy)
    point = {"at": str(at), "nav": nav_f, "spy": spy_f}
    cash = extra.get("cash")
    if cash is not None:
        try:
            point["cash"] = float(cash)
        except (TypeError, ValueError):
            pass
    flow = extra.get("external_capital_flow")
    if flow is not None:
        try:
            point["external_capital_flow"] = float(flow)
        except (TypeError, ValueError):
            pass
    positions = extra.get("positions")
    if isinstance(positions, list) and positions:
        compact = []
        for row in positions:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            compact.append(
                {
                    "symbol": symbol,
                    "quantity": row.get("quantity"),
                    "market_value": row.get("market_value"),
                    "current_price": row.get("current_price"),
                }
            )
        if compact:
            point["positions"] = compact
    return point


def _load_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = raw.get("points") if isinstance(raw, dict) else raw
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        point = _point(
            row.get("at"),
            row.get("nav"),
            row.get("spy"),
            cash=row.get("cash"),
            external_capital_flow=row.get("external_capital_flow"),
            positions=row.get("positions"),
        )
        if point:
            out.append(point)
    return out


def _context_point(data: dict[str, Any]) -> dict[str, Any] | None:
    ctx = data.get("context") or {}
    return _point(
        ctx.get("timestamp") or data.get("created_at"),
        ctx.get("current_nav"),
        ctx.get("spy"),
        cash=ctx.get("cash"),
        external_capital_flow=ctx.get("external_capital_flow"),
        positions=ctx.get("positions"),
    )


def _snapshot_dir_points(snap_dir: Path, current: Path) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    if snap_dir.is_dir():
        for path in sorted(snap_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            point = _context_point(data)
            if point:
                points.append(point)
    if current.is_file():
        try:
            data = json.loads(current.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        point = _context_point(data)
        if point:
            points.append(point)
    return points


def _paper_snapshot_points(root: Path) -> list[dict[str, Any]]:
    return _snapshot_dir_points(root / "state" / "paper_book" / "snapshots", root / "state" / "paper_book" / "current.json")


def _merge(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_at: dict[str, dict[str, Any]] = {}
    for point in points:
        stamp = str(point["at"])
        prior = by_at.get(stamp)
        if prior is None:
            by_at[stamp] = dict(point)
            continue
        if prior.get("spy") is None and point.get("spy") is not None:
            prior["spy"] = point["spy"]
        prior["nav"] = point["nav"]
        for key in ("cash", "external_capital_flow", "positions"):
            if prior.get(key) in (None, [], "") and point.get(key) not in (None, [], ""):
                prior[key] = point[key]
    return [by_at[key] for key in sorted(by_at)]


def _save(path: Path, points: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"points": points}, indent=2, default=str), encoding="utf-8")


def _live_snapshot_points(root: Path) -> list[dict[str, Any]]:
    return _snapshot_dir_points(root / "state" / "live_book" / "snapshots", root / "state" / "live_book" / "current.json")


def load_nav_history(root: Path | None = None, *, mode: str | None = None) -> list[dict[str, Any]]:
    base = Path(root) if root is not None else project_root()
    current = (mode or resolve_runtime_mode().value).upper()
    if current == RuntimeMode.LIVE.value:
        merged = _merge(_load_file(history_path(base, mode=current)) + _live_snapshot_points(base))
        return merged
    merged = _merge(_load_file(history_path(base, mode=current)) + _paper_snapshot_points(base))
    return merged


def record_nav_snapshot(
    root: Path | None = None,
    *,
    nav: float | None,
    spy: Any = None,
    at: str | None = None,
    mode: str | None = None,
    cash: Any = None,
    external_capital_flow: Any = None,
    positions: Any = None,
) -> list[dict[str, Any]]:
    """Persist an observed NAV. Skips if NAV is missing. Does not invent SPY."""
    base = Path(root) if root is not None else project_root()
    current = (mode or resolve_runtime_mode().value).upper()
    existing = load_nav_history(base, mode=current)
    incoming = _point(
        at or utc_now(),
        nav,
        spy,
        cash=cash,
        external_capital_flow=external_capital_flow,
        positions=positions,
    )
    if incoming is None:
        return existing
    last = existing[-1] if existing else None
    if last is not None and last["nav"] == incoming["nav"] and last.get("spy") == incoming.get("spy"):
        last_at = str(last.get("at") or "")
        new_at = str(incoming["at"])
        if last_at[:16] == new_at[:16]:
            return existing
    merged = _merge(existing + [incoming])
    _save(history_path(base, mode=current), merged)
    return merged


def chart_ready(points: list[dict[str, Any]]) -> bool:
    return len(points) >= MIN_CHART_POINTS


def total_return(points: list[dict[str, Any]]) -> float | None:
    if not chart_ready(points):
        return None
    from agentic_portfolio.cash_flow import cash_flow_adjusted_total_return

    adjusted = cash_flow_adjusted_total_return(points)
    if adjusted is not None:
        return adjusted
    first = points[0]["nav"]
    last = points[-1]["nav"]
    if not first:
        return None
    return (float(last) / float(first)) - 1.0


def spy_return(points: list[dict[str, Any]]) -> float | None:
    series = [p for p in points if p.get("spy") is not None]
    if len(series) < MIN_CHART_POINTS:
        return None
    first = series[0]["spy"]
    last = series[-1]["spy"]
    if first in (None, 0):
        return None
    return (float(last) / float(first)) - 1.0
