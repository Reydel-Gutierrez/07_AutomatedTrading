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


def _point(at: Any, nav: Any, spy: Any = None) -> dict[str, Any] | None:
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
    return {"at": str(at), "nav": nav_f, "spy": spy_f}


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
        point = _point(row.get("at"), row.get("nav"), row.get("spy"))
        if point:
            out.append(point)
    return out


def _paper_snapshot_points(root: Path) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    snap_dir = root / "state" / "paper_book" / "snapshots"
    if snap_dir.is_dir():
        for path in sorted(snap_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            ctx = data.get("context") or {}
            point = _point(
                ctx.get("timestamp") or data.get("created_at"),
                ctx.get("current_nav"),
                ctx.get("spy"),
            )
            if point:
                points.append(point)
    current = root / "state" / "paper_book" / "current.json"
    if current.is_file():
        try:
            data = json.loads(current.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        ctx = data.get("context") or {}
        point = _point(
            ctx.get("timestamp") or data.get("created_at"),
            ctx.get("current_nav"),
            ctx.get("spy"),
        )
        if point:
            points.append(point)
    return points


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
    return [by_at[key] for key in sorted(by_at)]


def _save(path: Path, points: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"points": points}, indent=2, default=str), encoding="utf-8")


def _live_snapshot_points(root: Path) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    snap_dir = root / "state" / "live_book" / "snapshots"
    if snap_dir.is_dir():
        for path in sorted(snap_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            ctx = data.get("context") or {}
            point = _point(
                ctx.get("timestamp") or data.get("created_at"),
                ctx.get("current_nav"),
                ctx.get("spy"),
            )
            if point:
                points.append(point)
    current = root / "state" / "live_book" / "current.json"
    if current.is_file():
        try:
            data = json.loads(current.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        ctx = data.get("context") or {}
        point = _point(
            ctx.get("timestamp") or data.get("created_at"),
            ctx.get("current_nav"),
            ctx.get("spy"),
        )
        if point:
            points.append(point)
    return points


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
) -> list[dict[str, Any]]:
    """Persist an observed NAV. Skips if NAV is missing. Does not invent SPY."""
    base = Path(root) if root is not None else project_root()
    current = (mode or resolve_runtime_mode().value).upper()
    existing = load_nav_history(base, mode=current)
    incoming = _point(at or utc_now(), nav, spy)
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
