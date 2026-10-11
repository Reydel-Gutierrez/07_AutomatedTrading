"""Legacy → firm migration: archive every legacy record, import what HQ shows.

The archive is a gzip tarball of the legacy ``state/`` (except ``state/firm``),
``logs/`` and ``reports/`` with a SHA-256, written under ``state/firm/archive``.
Nothing legacy is deleted here; legacy files stay in place until cleanup.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from collections import OrderedDict
from pathlib import Path
from typing import Any

from firm.clock import et_date, iso, parse
from firm.core import Firm
from firm.paths import project_root, state_root

LEGACY_DIRS = ("state", "logs", "reports")


def archive_legacy(root: Path | None = None, *, out_dir: Path | None = None, stamp: str) -> dict[str, Any]:
    base = root or project_root()
    out = out_dir or state_root() / "archive"
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"legacy-{stamp}.tar.gz"
    files = 0
    firm_state = (base / "state" / "firm").resolve()
    with tarfile.open(target, "w:gz") as tar:
        for name in LEGACY_DIRS:
            folder = base / name
            if not folder.is_dir():
                continue
            for path in sorted(folder.rglob("*")):
                resolved = path.resolve()
                if firm_state == resolved or firm_state in resolved.parents or not path.is_file():
                    continue
                tar.add(path, arcname=str(path.relative_to(base)))
                files += 1
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (out / f"legacy-{stamp}.tar.gz.sha256").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    return {"path": str(target), "sha256": digest, "files": files, "bytes": target.stat().st_size}


def legacy_nav_history(root: Path | None = None) -> list[dict[str, Any]]:
    """One point per ET date (the last LIVE snapshot that day) from state/live_book/snapshots."""
    folder = (root or project_root()) / "state" / "live_book" / "snapshots"
    points: dict[str, dict[str, Any]] = {}
    for path in folder.glob("*.json"):
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if snap.get("runtime_mode") != "LIVE":
            continue
        ctx = snap.get("context") or {}
        ts = snap.get("created_at") or ctx.get("timestamp")
        if not ts or ctx.get("current_nav") is None:
            continue
        day = et_date(parse(ts)).isoformat()
        if day not in points or points[day]["ts"] < ts:
            points[day] = {"ts": ts, "date": day, "nav": ctx.get("current_nav"), "cash": ctx.get("cash"), "positions": len(ctx.get("positions") or [])}
    return list(OrderedDict(sorted(points.items())).values())


def legacy_ai_spend(root: Path | None = None) -> dict[str, Any]:
    folder = (root or project_root()) / "state" / "ai_budget"
    out = {}
    for path in sorted(folder.glob("month_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out[path.stem.removeprefix("month_")] = data.get("spent") or data.get("total") or data.get("cumulative_cost")
    return out


def import_legacy(firm: Firm, archive: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    history = legacy_nav_history(root)
    summary = {
        "archive": archive,
        "nav_history": history,
        "ai_spend_by_month": legacy_ai_spend(root),
        "note": "Pre-firm records. The full legacy state, logs, and reports are in the archive.",
    }
    with firm.db.tx() as conn:
        conn.execute(
            "INSERT INTO legacy_import (key, source, sha256, imported_at, summary_json) VALUES (?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET source=excluded.source, sha256=excluded.sha256, imported_at=excluded.imported_at, summary_json=excluded.summary_json",
            ("legacy_v1", archive["path"], archive["sha256"], iso(firm.clock()), json.dumps(summary, default=str)),
        )
    firm.events.emit(
        "LEGACY_IMPORTED",
        f"Legacy records archived ({archive['files']} files, sha256 {archive['sha256'][:12]}…) and {len(history)} days of NAV history imported.",
        actor="migration",
        payload={"archive": archive, "history_days": len(history)},
    )
    return summary
