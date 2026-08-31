"""Assemble AI context from application facts. Models interpret; they do not invent the book."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import uuid4

from agentic_portfolio.ai.identity import LIQUIDITY_UNIT_NOTE
from agentic_portfolio.discovery.snapshot import compute_spread_metrics
from agentic_portfolio.research.packet import freeze_portfolio, freeze_risk_limits
from agentic_portfolio.runtime import RuntimeMode, source_of_truth
from agentic_portfolio.schemas import PortfolioContext, to_dict


AUTHORITATIVE_KEYS = (
    "current_nav",
    "cash",
    "buying_power",
    "positions",
    "holdings_count",
    "cash_allocation_pct",
    "sleeve_allocation_pct",
    "sector_allocation_pct",
    "risk_state",
    "high_water_mark",
    "current_drawdown",
    "daily_risk_halt",
    "open_orders",
)


@dataclass
class AIContext:
    context_id: str
    ticker: str
    runtime_mode: str
    source_of_truth: str
    assembled_at: str
    market: dict[str, Any] = field(default_factory=dict)
    price_history: dict[str, Any] = field(default_factory=dict)
    indicators: dict[str, Any] = field(default_factory=dict)
    liquidity: dict[str, Any] = field(default_factory=dict)
    fundamentals: dict[str, Any] = field(default_factory=dict)
    news: list[Any] = field(default_factory=list)
    catalysts: list[Any] = field(default_factory=list)
    account: dict[str, Any] = field(default_factory=dict)
    positions: list[dict[str, Any]] = field(default_factory=list)
    concentration: dict[str, Any] = field(default_factory=dict)
    risk_state: dict[str, Any] = field(default_factory=dict)
    prior_research: dict[str, Any] | None = None
    policy_constraints: dict[str, Any] = field(default_factory=dict)
    discovery: dict[str, Any] = field(default_factory=dict)
    identity: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_prompt_dict(self) -> dict[str, Any]:
        return to_dict(self)


def overlay_broker_facts(payload: Mapping[str, Any], context: PortfolioContext) -> dict[str, Any]:
    """AI-generated numbers never replace authoritative portfolio/broker facts."""
    out = dict(payload)
    for key in ("current_nav", "nav", "cash", "buying_power"):
        out.pop(key, None)
    out["authoritative_facts"] = {
        "current_nav": context.current_nav,
        "cash": context.cash,
        "buying_power": context.buying_power,
        "holdings_count": context.holdings_count,
        "risk_state": context.risk_state.value if hasattr(context.risk_state, "value") else context.risk_state,
        "positions": [to_dict(p) for p in context.positions],
        "open_orders": [to_dict(o) for o in (context.open_orders or [])],
        "note": "These values are observed from the broker. Do not rewrite them.",
    }
    return out


def assemble_context(
    ticker: str,
    context: PortfolioContext,
    *,
    now_iso: str,
    runtime_mode: RuntimeMode | str = RuntimeMode.PAPER,
    market: Mapping[str, Any] | None = None,
    snapshot: Mapping[str, Any] | None = None,
    prior_research: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    discovery: Mapping[str, Any] | None = None,
    instrument_facts: Any | None = None,
) -> AIContext:
    mode = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode)
    facts = freeze_portfolio(context)
    snap = dict(snapshot or {})
    market_blob = dict(market or {})
    blobs: dict[str, Any] = {}
    notes = [
        "Python assembled these facts. Interpret them. Do not invent missing observed facts.",
        "Do not rewrite NAV, cash, buying power, positions, or risk limits.",
        "A null/unavailable field means the application does not have that fact. Do not guess it.",
        "Output must match the provided JSON schema. Advisory only. You cannot place an order.",
    ]
    if instrument_facts is not None:
        blobs = instrument_facts.context_blobs()
        if getattr(instrument_facts, "is_etf", False):
            notes.append("This instrument is an ETF/fund. Do not treat it as an individual operating company.")
            notes.append("Do not use corporate market cap, company P/E, revenue growth, or a single-company sector for a diversified fund.")
        elif getattr(instrument_facts, "is_equity", False):
            notes.append("This instrument is an individual equity. Company fundamentals apply when present.")
    notes.append(LIQUIDITY_UNIT_NOTE)
    metrics = None
    if isinstance(snap, dict):
        metrics = compute_spread_metrics(snap.get("bid"), snap.get("ask"))
        if metrics is None and snap.get("spread_percent") is not None:
            metrics = {
                "absolute_spread_usd": snap.get("absolute_spread_usd"),
                "spread_percent": snap.get("spread_percent"),
                "spread_bps": snap.get("spread_bps"),
            }
    fallback_liquidity = {
        "dollar_volume": snap.get("dollar_volume") if isinstance(snap, dict) else None,
        "average_volume": snap.get("average_volume") if isinstance(snap, dict) else None,
        "bid_price": snap.get("bid") if isinstance(snap, dict) else None,
        "ask_price": snap.get("ask") if isinstance(snap, dict) else None,
        "absolute_spread_usd": (metrics or {}).get("absolute_spread_usd") if metrics else (snap.get("absolute_spread_usd") if isinstance(snap, dict) else None),
        "spread_percent": (metrics or {}).get("spread_percent") if metrics else None,
        "spread_bps": (metrics or {}).get("spread_bps") if metrics else None,
    }
    return AIContext(
        context_id=str(uuid4()),
        ticker=str(ticker).upper(),
        runtime_mode=mode,
        source_of_truth=source_of_truth(RuntimeMode(mode) if mode in RuntimeMode.__members__ else RuntimeMode.PAPER),
        assembled_at=now_iso,
        identity=dict(blobs.get("identity") or {}),
        market=dict(blobs.get("market") or {
            "last": market_blob.get("last") or market_blob.get("current_price") or snap.get("current_price"),
            "bid": market_blob.get("bid") or snap.get("bid"),
            "ask": market_blob.get("ask") or snap.get("ask"),
            "previous_close": market_blob.get("previous_close") or snap.get("previous_close"),
            "name": market_blob.get("name") or snap.get("name"),
            "sector": market_blob.get("sector") or snap.get("sector"),
            "instrument_kind": snap.get("instrument_kind"),
        }),
        price_history=dict(blobs.get("price_history") or {
            "return_5d": snap.get("return_5d"),
            "return_21d": snap.get("return_21d"),
            "return_63d": snap.get("return_63d"),
            "return_252d": snap.get("return_252d"),
            "high_52_week": snap.get("high_52_week"),
            "low_52_week": snap.get("low_52_week"),
            "drawdown_from_52w_high": snap.get("drawdown_from_52w_high"),
        }),
        indicators=dict(blobs.get("indicators") or {
            "rsi": snap.get("rsi"),
            "sma_50": snap.get("sma_50"),
            "sma_200": snap.get("sma_200"),
            "atr": snap.get("atr"),
            "volume_vs_avg": snap.get("volume_vs_avg"),
        }),
        liquidity=dict(blobs.get("liquidity") or fallback_liquidity),
        fundamentals=dict(blobs.get("fundamentals") or {
            "market_cap": snap.get("market_cap") if isinstance(snap, dict) else None,
            "pe_ratio": snap.get("pe_ratio") if isinstance(snap, dict) else None,
            "pb_ratio": snap.get("pb_ratio") if isinstance(snap, dict) else None,
            "sector": snap.get("sector") if isinstance(snap, dict) else None,
            "industry": snap.get("industry") if isinstance(snap, dict) else None,
            "description": snap.get("description") if isinstance(snap, dict) else None,
        }),
        news=list((snap.get("news_headlines") if isinstance(snap, dict) else None) or market_blob.get("news") or []),
        catalysts=list(market_blob.get("catalysts") or []),
        account={
            "current_nav": facts.current_nav,
            "cash": facts.cash,
            "buying_power": facts.buying_power,
            "cash_allocation_pct": facts.cash_allocation_pct,
            "holdings_count": facts.holdings_count,
        },
        positions=list(facts.positions),
        concentration={
            "sleeve_allocation_pct": dict(facts.sleeve_allocation_pct),
            "sector_allocation_pct": dict(facts.sector_allocation_pct),
        },
        risk_state={
            "risk_state": facts.risk_state,
            "high_water_mark": facts.high_water_mark,
            "current_drawdown": facts.current_drawdown,
            "daily_risk_halt": facts.daily_risk_halt,
            "limits": to_dict(freeze_risk_limits(dict(policy) if policy else None)),
        },
        prior_research=dict(prior_research) if prior_research else None,
        policy_constraints=dict(policy or {}),
        discovery=dict(discovery or {}),
        notes=notes,
    )
