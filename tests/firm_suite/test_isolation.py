"""Static guarantees: only the Execution Desk can reach broker write methods."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "firm"
WRITE_METHODS = {"place", "review", "place_equity_order", "review_equity_order", "cancel_equity_order"}
ALLOWED = {"desk.py", "broker/robinhood.py"}


def _calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


def test_only_the_desk_calls_broker_write_methods():
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED:
            continue
        hits = _calls(path) & WRITE_METHODS
        if hits:
            offenders.append(f"{rel}: {sorted(hits)}")
    assert offenders == []


def test_managers_and_ai_cannot_import_the_desk_writer_or_ledger_writes():
    for folder in ("managers", "cio"):
        for path in (SRC / folder).rglob("*.py") if (SRC / folder).exists() else []:
            text = path.read_text(encoding="utf-8")
            assert "connect_writer" not in text, path
            assert "from firm.desk" not in text, path
