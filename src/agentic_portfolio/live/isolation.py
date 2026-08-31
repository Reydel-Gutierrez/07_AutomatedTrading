"""Detect paper-book contamination of LIVE runtime. Fail closed if mixed."""

from __future__ import annotations

from typing import Any, Mapping

from agentic_portfolio.live.safety import LiveSafetyError
from agentic_portfolio.runtime import LIVE_SOURCE_OF_TRUTH, RuntimeMode

PAPER_NAV_SENTINEL = 10_000.0
PAPER_FILL_SYMBOLS = ("NVDA", "NKE", "ESTC", "IONQ")
PAPER_SNAPSHOT_FLAGS = ("paper_environment", "live_book_untouched")
PAPER_THESIS_MARKERS = (
    "a141950d-c730-4b98-9b13-167c977b3596",  # paper NVDA
    "c6a6b724-588c-4133-a6ef-9882d5a8aa75",  # paper NKE
)


class PaperContaminationError(LiveSafetyError):
    """LIVE runtime was built from paper book / paper fills / paper theses."""


def _ctx(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {}
    ctx = snapshot.get("context") if isinstance(snapshot.get("context"), dict) else snapshot
    return dict(ctx or {})


def _symbols(ctx: Mapping[str, Any]) -> set[str]:
    return {str(p.get("symbol") or "").upper() for p in (ctx.get("positions") or []) if isinstance(p, dict)}


def detect_paper_contamination(
    live_snapshot: Mapping[str, Any] | None,
    paper_snapshot: Mapping[str, Any] | None = None,
    *,
    runtime_mode: RuntimeMode | str | None = RuntimeMode.LIVE,
) -> list[str]:
    """Return leak reasons. Empty list means LIVE is isolated from paper."""
    mode = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode or "").upper()
    if mode != RuntimeMode.LIVE.value:
        return []
    leaks: list[str] = []
    live = dict(live_snapshot or {})
    live_ctx = _ctx(live)
    paper = dict(paper_snapshot or {})
    paper_ctx = _ctx(paper)

    if live.get("paper_environment") is True:
        leaks.append("live_snapshot_marked_paper_environment")
    if live.get("live_book_untouched") is True:
        leaks.append("live_snapshot_uses_paper_book_flag")
    source = str(live.get("source_of_truth") or live_ctx.get("source_of_truth") or "")
    if source and source != LIVE_SOURCE_OF_TRUTH:
        leaks.append(f"live_source_of_truth_is_{source}")
    if str(live.get("runtime_mode") or "").upper() == RuntimeMode.PAPER.value:
        leaks.append("live_snapshot_runtime_mode_paper")
    if live.get("lots"):
        leaks.append("live_snapshot_contains_paper_lots")
    if live.get("fills") or live.get("blotter"):
        leaks.append("live_snapshot_contains_paper_fills")

    live_nav = live_ctx.get("current_nav")
    paper_nav = paper_ctx.get("current_nav")
    live_ts = str(live_ctx.get("timestamp") or live.get("created_at") or "")
    paper_ts = str(paper_ctx.get("timestamp") or paper.get("created_at") or "")
    if paper_ctx and live_nav is not None and paper_nav is not None:
        if float(live_nav) == float(paper_nav) == PAPER_NAV_SENTINEL:
            leaks.append("live_nav_is_paper_10000")
        if live_ts and live_ts == paper_ts and float(live_nav) == float(paper_nav):
            leaks.append("live_context_timestamp_matches_paper_book")
        live_syms = _symbols(live_ctx)
        paper_syms = _symbols(paper_ctx)
        if live_syms and live_syms == paper_syms and float(live_nav) == float(paper_nav):
            leaks.append("live_positions_match_paper_book")
        if paper_syms and live_syms == paper_syms and paper_syms.issuperset(PAPER_FILL_SYMBOLS[:2]) and float(live_nav) == PAPER_NAV_SENTINEL:
            leaks.append("live_holdings_are_paper_fill_symbols")

    thesis_ids = {str(p.get("thesis_id") or "") for p in (live_ctx.get("positions") or []) if isinstance(p, dict)}
    if thesis_ids & set(PAPER_THESIS_MARKERS):
        leaks.append("live_positions_use_paper_thesis_ids")

    return leaks


def assert_live_isolated(
    live_snapshot: Mapping[str, Any] | None,
    paper_snapshot: Mapping[str, Any] | None = None,
) -> None:
    leaks = detect_paper_contamination(live_snapshot, paper_snapshot, runtime_mode=RuntimeMode.LIVE)
    if leaks:
        raise PaperContaminationError("paper state leaked into LIVE runtime: " + ", ".join(leaks))
