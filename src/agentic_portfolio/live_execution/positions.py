"""Associate LIVE holdings with thesis / approval / broker order provenance."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentic_portfolio.agent.persist import atomic_write_json, read_json
from agentic_portfolio.live_execution.store import ExecutionStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import RuntimeMode
from agentic_portfolio.schemas import Sleeve, SleeveAssignmentStatus, ThesisStatus, to_dict


@dataclass
class PositionLink:
    symbol: str
    thesis_id: str | None = None
    approval_id: str | None = None
    intent_id: str | None = None
    broker_order_id: str | None = None
    sleeve: str | None = None
    entry_rationale: str | None = None
    invalidation: list[str] = field(default_factory=list)
    target_review_date: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)


def links_path(root, *, mode: RuntimeMode | str = RuntimeMode.LIVE):
    current = mode.value if isinstance(mode, RuntimeMode) else str(mode)
    folder = "live_execution" if current == RuntimeMode.LIVE.value else "paper_execution"
    return (root or project_root()) / "state" / folder / "position_links.json"


def load_links(root, *, mode: RuntimeMode | str = RuntimeMode.LIVE) -> dict[str, PositionLink]:
    data = read_json(links_path(root, mode=mode), {"records": {}})
    rows = {}
    for key, raw in dict(data.get("records") or {}).items():
        rows[str(key).upper()] = PositionLink(
            symbol=str(raw.get("symbol") or key).upper(),
            thesis_id=raw.get("thesis_id"),
            approval_id=raw.get("approval_id"),
            intent_id=raw.get("intent_id"),
            broker_order_id=raw.get("broker_order_id"),
            sleeve=raw.get("sleeve"),
            entry_rationale=raw.get("entry_rationale"),
            invalidation=list(raw.get("invalidation") or []),
            target_review_date=raw.get("target_review_date"),
        )
    return rows


def save_links(root, links: dict[str, PositionLink], *, mode: RuntimeMode | str = RuntimeMode.LIVE) -> None:
    payload = {"records": {k: v.to_dict() for k, v in links.items()}}
    atomic_write_json(links_path(root, mode=mode), payload)


def upsert_from_fill(
    root,
    *,
    symbol: str,
    store: ExecutionStore,
    sleeve: str | None = None,
    rationale: str | None = None,
    invalidation: list[str] | None = None,
    mode: RuntimeMode | str = RuntimeMode.LIVE,
) -> PositionLink:
    links = load_links(root, mode=mode)
    order = next((o for o in store.orders() if o.symbol.upper() == symbol.upper()), None)
    link = links.get(symbol.upper()) or PositionLink(symbol=symbol.upper())
    if order is not None:
        link.approval_id = order.approval_id
        link.intent_id = order.intent_id
        link.broker_order_id = order.broker_order_id
        link.thesis_id = order.thesis_id
    if sleeve:
        link.sleeve = sleeve
    if rationale:
        link.entry_rationale = rationale
    if invalidation:
        link.invalidation = list(invalidation)
    links[symbol.upper()] = link
    save_links(root, links, mode=mode)
    _sync_registries_from_fill(root, link, store=store)
    return link


def _sync_registries_from_fill(root, link: PositionLink, *, store: ExecutionStore) -> None:
    """BUY/ADD fills become actively managed holdings. SELL closes local assignment."""
    from pathlib import Path

    from agentic_portfolio.sleeve_registry import SleeveRegistry
    from agentic_portfolio.thesis_registry import ThesisRegistry

    base = Path(root)
    action = ""
    if store is not None and link.intent_id:
        intent = store.get_intent(link.intent_id)
        if intent is not None:
            action = str(getattr(intent, "action", "") or "").upper()
    if not action:
        order = next((o for o in store.orders() if o.symbol.upper() == link.symbol.upper()), None) if store is not None else None
        side = str(getattr(order, "side", "") or "").lower() if order is not None else ""
        if side in {"buy", "b"}:
            action = "BUY"
        elif side in {"sell", "s"}:
            action = ""
    theses = ThesisRegistry(base / "state" / "thesis_registry.json", runtime_mode="LIVE")
    sleeves = SleeveRegistry(base / "state" / "sleeve_registry.json")
    sleeve_enum = None
    raw_sleeve = link.sleeve
    if raw_sleeve:
        try:
            sleeve_enum = raw_sleeve if isinstance(raw_sleeve, Sleeve) else Sleeve(str(raw_sleeve).upper())
        except ValueError:
            sleeve_enum = None
    if action in {"BUY", "ADD"}:
        if link.thesis_id:
            rec = theses.get(link.thesis_id)
            if rec is not None and rec.status == ThesisStatus.DRAFT:
                theses.set_status(link.thesis_id, ThesisStatus.ACTIVE)
        existing = sleeves.get(link.symbol)
        if existing is not None:
            if existing.status == SleeveAssignmentStatus.PROPOSED:
                sleeves.set_status(link.symbol, SleeveAssignmentStatus.ACTIVE)
        elif sleeve_enum is not None:
            sleeves.assign(
                symbol=link.symbol,
                sleeve=sleeve_enum,
                thesis_id=link.thesis_id,
                status=SleeveAssignmentStatus.ACTIVE,
            )
    elif action == "SELL":
        if sleeves.get(link.symbol) is not None:
            sleeves.set_status(link.symbol, SleeveAssignmentStatus.CLOSED)
        if link.thesis_id:
            rec = theses.get(link.thesis_id)
            if rec is not None and rec.status not in {ThesisStatus.CLOSED, ThesisStatus.REJECTED}:
                theses.set_status(link.thesis_id, ThesisStatus.CLOSED)
    elif action == "REDUCE":
        if sleeves.get(link.symbol) is not None:
            sleeves.set_status(link.symbol, SleeveAssignmentStatus.REDUCING)
