"""LIVE AI may analyze and propose. It must never place or cancel an order."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agentic_portfolio.ai.errors import MissingBrokerFacts, PaperContaminationError, PlacementForbidden, StaleSnapshotError
from agentic_portfolio.discovery.safety import DISCOVERY_FORBIDDEN_TOOLS
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.live.isolation import detect_paper_contamination
from agentic_portfolio.live.safety import LIVE_FORBIDDEN_TOOLS, assert_placement_disabled
from agentic_portfolio.paper_fill.store import PaperFillStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import LIVE_SOURCE_OF_TRUTH, RuntimeMode

_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+)", re.IGNORECASE)

LIVE_AI_ALLOWED = True
LIVE_PROPOSALS_ALLOWED = True
LIVE_ORDER_PLACEMENT = False

AI_FORBIDDEN_TOOLS = frozenset(LIVE_FORBIDDEN_TOOLS) | frozenset(DISCOVERY_FORBIDDEN_TOOLS) | {
    "place_equity_order",
    "cancel_equity_order",
    "review_equity_order",
}


class AISafetyError(RuntimeError):
    """Raised when the AI runtime would trade, mix books, or skip facts."""


def redact_secrets(value: Any) -> Any:
    """Strip API keys from nested report/log payloads. Never persist secrets."""
    if isinstance(value, str):
        return _SECRET.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in ("api_key", "apikey", "authorization", "secret", "password")):
                out[key] = "[REDACTED]" if item else item
            else:
                out[key] = redact_secrets(item)
        return out
    return value


def live_invariants() -> dict[str, bool]:
    return {
        "LIVE_AI_ALLOWED": LIVE_AI_ALLOWED,
        "LIVE_PROPOSALS_ALLOWED": LIVE_PROPOSALS_ALLOWED,
        "LIVE_ORDER_PLACEMENT": LIVE_ORDER_PLACEMENT,
    }


def assert_proposal_only(*, live_trade_actions_allowed: bool = False, auto_execution: bool = False) -> None:
    if LIVE_ORDER_PLACEMENT:
        raise PlacementForbidden("LIVE_ORDER_PLACEMENT must remain false")
    assert_placement_disabled(
        live_trade_actions_allowed=live_trade_actions_allowed,
        auto_execution=auto_execution,
        live_order_placement_enabled=False,
    )


def refuse_placement(tool: str, *, root: Path | None = None, extra: Mapping[str, Any] | None = None) -> None:
    """Any attempt to reach place/cancel fails closed and writes an audit event."""
    payload = {
        "type": "LIVE_PLACEMENT_REFUSED",
        "tool": tool,
        "LIVE_ORDER_PLACEMENT": False,
        "live_ai_allowed": LIVE_AI_ALLOWED,
        "live_proposals_allowed": LIVE_PROPOSALS_ALLOWED,
        **dict(extra or {}),
    }
    append_jsonl(payload, (root or project_root()) / "logs" / "ai_safety.jsonl")
    raise PlacementForbidden(f"AI runtime refused {tool}; LIVE_ORDER_PLACEMENT=false")


def assert_no_forbidden_tools(names: list[str] | tuple[str, ...] | set[str], *, root: Path | None = None) -> None:
    bad = set(names) & AI_FORBIDDEN_TOOLS
    if bad:
        for tool in sorted(bad):
            try:
                refuse_placement(tool, root=root)
            except PlacementForbidden:
                pass
        raise PlacementForbidden(f"AI runtime refused forbidden MCP tools: {sorted(bad)}")


def inspect_ai_module_for_forbidden_calls(root: Path | None = None) -> list[str]:
    base = (root or project_root()) / "src" / "agentic_portfolio" / "ai"
    hits: list[str] = []
    for path in base.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for tool in ("place_equity_order", "cancel_equity_order"):
            if f"{tool}(" in text:
                hits.append(f"{path.relative_to(base)}:{tool}")
    return hits


def inspect_src_for_direct_provider_calls(root: Path | None = None) -> list[str]:
    """No code outside the gateway adapters may call an AI HTTP API."""
    src = (root or project_root()) / "src" / "agentic_portfolio"
    allowed_names = {
        "openai.py",
        "anthropic.py",
        "safety.py",
    }
    hits: list[str] = []
    needles = ("api.openai.com", "api.anthropic.com")
    for path in src.rglob("*.py"):
        if path.name in allowed_names and path.parent.name in {"providers", "ai"}:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle in text:
                hits.append(f"{path.relative_to(src)}:{needle}")
    return hits


def assert_broker_facts(context: Any) -> None:
    nav = getattr(context, "current_nav", None)
    cash = getattr(context, "cash", None)
    bp = getattr(context, "buying_power", None)
    if nav is None or cash is None or bp is None:
        raise MissingBrokerFacts("LIVE pipeline requires NAV, cash, and buying power")
    try:
        if float(nav) <= 0:
            raise MissingBrokerFacts("LIVE NAV is missing or non-positive")
    except (TypeError, ValueError) as exc:
        raise MissingBrokerFacts("LIVE NAV is not a number") from exc


def assert_snapshot_fresh(
    snapshot: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    max_age_seconds: int = 3600,
) -> None:
    if not snapshot:
        raise StaleSnapshotError("LIVE snapshot is not available")
    created = str(snapshot.get("created_at") or (snapshot.get("context") or {}).get("timestamp") or "")
    if not created:
        raise StaleSnapshotError("LIVE snapshot has no timestamp")
    try:
        stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise StaleSnapshotError("LIVE snapshot timestamp is unreadable") from exc
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = (current - stamp).total_seconds()
    if age > max_age_seconds:
        raise StaleSnapshotError(f"LIVE snapshot is stale ({int(age)}s > {max_age_seconds}s)")


def assert_live_ai_isolated(
    artifact: Mapping[str, Any] | None,
    *,
    runtime_mode: RuntimeMode | str,
    paper_snapshot: Mapping[str, Any] | None = None,
    root: Path | None = None,
) -> None:
    mode = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode)
    if mode != RuntimeMode.LIVE.value:
        return
    row = dict(artifact or {})
    if str(row.get("runtime_mode") or "").upper() == RuntimeMode.PAPER.value:
        raise PaperContaminationError("PAPER AI artifact cannot be used as a LIVE decision")
    if row.get("paper_environment") is True:
        raise PaperContaminationError("paper_environment=true on a LIVE AI artifact")
    source = str(row.get("source_of_truth") or "")
    if source and source != LIVE_SOURCE_OF_TRUTH:
        raise PaperContaminationError(f"LIVE AI artifact source_of_truth is {source}")
    paper = paper_snapshot
    if paper is None and root is not None:
        paper = PaperFillStore(root).current_book()
    leaks = detect_paper_contamination(row, paper, runtime_mode=RuntimeMode.LIVE)
    if leaks:
        raise PaperContaminationError("paper state leaked into LIVE AI: " + ", ".join(leaks))
