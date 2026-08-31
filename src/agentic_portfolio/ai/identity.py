"""LIVE instrument identity, fact provenance, and pre-AI candidate validation.

Never invent market facts. Fixture/demo/sample/synthetic/paper/mock values
are TEST/PAPER only. Invalid LIVE candidates must not reach a paid AI call.
"""

from __future__ import annotations

import re
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from agentic_portfolio.ai.config import load_ai_config
from agentic_portfolio.calendar import EASTERN, NyseEquityCalendar, is_regular_hours
from agentic_portfolio.discovery.snapshot import SecuritySnapshot, compute_spread_metrics
from agentic_portfolio.live.engine import market_session_state
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import RuntimeMode
from agentic_portfolio.schemas import (
    CandidateValidationStatus,
    FactOrigin,
    FreshnessStatus,
    ProvenanceFact,
    SecurityClass,
    to_dict,
)

SYNTHETIC_ORIGINS = frozenset(
    {
        FactOrigin.FIXTURE,
        FactOrigin.TEST,
        FactOrigin.DEMO,
        FactOrigin.SAMPLE,
        FactOrigin.SYNTHETIC,
        FactOrigin.PAPER,
        FactOrigin.MOCK,
    }
)

CONTAMINATION_TOKENS = frozenset(
    {"fixture", "test", "demo", "sample", "synthetic", "paper", "mock", "placeholder", "hardcoded"}
)

UNSUPPORTED_KINDS = frozenset({"option", "crypto", "future", "event", "currency_pair"})
FUND_KINDS = frozenset({"etf", "etn", "fund", "mutual_fund", "closed_end_fund"})
_ETF_NAME = re.compile(r"\betf\b|\betn\b|exchange[- ]traded", re.I)
_FUND_INDUSTRY = re.compile(r"investment trusts|mutual funds|exchange traded", re.I)
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

LIVE_ALLOWED_ORIGINS = frozenset({FactOrigin.MCP_OBSERVED, FactOrigin.DERIVED, FactOrigin.MISSING, FactOrigin.UNAVAILABLE})

MATERIAL_FACT_KEYS = (
    "last_price",
    "security_name",
    "security_type",
    "market_cap",
    "net_assets",
    "pe_ratio",
    "sector",
    "average_volume",
    "dollar_volume",
    "spread_pct",
    "absolute_spread_usd",
    "spread_percent",
    "spread_bps",
)

UNUSABLE_SPREAD_FRACTION = 0.08
QUOTE_SESSION_KINDS = frozenset({"quote", "spread"})
SPREAD_PERCENT_NOTES = (
    "unit=fraction",
    "formula=(ask-bid)/midpoint",
    "0.0193_means_1.93_percent_not_1.93_dollars",
)
SPREAD_BPS_NOTES = ("unit=bps", "formula=spread_percent*10000")
ABSOLUTE_SPREAD_NOTES = ("unit=usd", "formula=ask-bid")
OFF_HOURS_NOTES = ("off_hours_not_regular_session", "indicative_context_only")
INDICATIVE_NOTES = ("indicative_off_hours_not_regular_session",)
LIQUIDITY_UNIT_NOTE = (
    "Liquidity spread is not a unitless number. absolute_spread_usd is ask minus bid in dollars. "
    "spread_percent is (ask-bid)/midpoint as a fraction (0.01 = 1 percent). "
    "spread_bps is spread_percent times 10000. Do not treat $0.019 as 1.9%."
)

DEFAULT_FRESHNESS = {
    "quote_seconds": 3600,
    "quote_market_hours_seconds": 900,
    "quote_closed_uses_last_session": True,
    "liquidity_seconds": 21600,
    "fundamentals_seconds": 604800,
    "identity_seconds": 2592000,
    "technicals_seconds": 21600,
    "news_seconds": 86400,
}


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def later_as_of(*stamps: str | None) -> str | None:
    best: str | None = None
    best_dt: datetime | None = None
    for stamp in stamps:
        parsed = parse_iso(stamp)
        if parsed is None:
            continue
        if best_dt is None or parsed > best_dt:
            best = stamp
            best_dt = parsed
    return best


def freshness_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(config or load_ai_config())
    raw = dict((cfg.get("pipeline") or {}).get("fact_freshness") or {})
    out = dict(DEFAULT_FRESHNESS)
    out.update(raw)
    return out


def _off_hours_status(kind: str) -> FreshnessStatus:
    return FreshnessStatus.INDICATIVE if kind == "spread" else FreshnessStatus.OFF_HOURS


def _in_completed_regular_session(stamp: datetime, now: datetime) -> bool:
    cal = NyseEquityCalendar()
    completed = cal.latest_completed_session(now)
    if completed is None or not is_regular_hours(stamp, cal):
        return False
    return stamp.astimezone(EASTERN).date() == completed.session_date


def _after_completed_session_close(stamp: datetime, now: datetime) -> bool:
    cal = NyseEquityCalendar()
    completed = cal.latest_completed_session(now)
    if completed is None:
        return False
    close_dt = datetime.combine(completed.session_date, completed.close_time, tzinfo=EASTERN)
    return stamp.astimezone(EASTERN) >= close_dt


def _in_current_regular_session(stamp: datetime, now: datetime) -> bool:
    cal = NyseEquityCalendar()
    current = cal.session_for(now)
    stamp_session = cal.session_for(stamp)
    if current is None or stamp_session is None:
        return False
    if stamp_session.session_id != current.session_id:
        return False
    return is_regular_hours(stamp, cal)


def evaluate_freshness(
    as_of: str | None,
    *,
    now: datetime,
    max_age: timedelta,
    kind: str = "generic",
    config: Mapping[str, Any] | None = None,
) -> FreshnessStatus:
    stamp = parse_iso(as_of)
    if stamp is None:
        return FreshnessStatus.UNAVAILABLE if as_of is None else FreshnessStatus.UNKNOWN
    age = (now - stamp).total_seconds()
    if age < 0 and age > -60:
        age = 0
    session = market_session_state(now)
    regular_open = bool(session.get("regular_hours_open"))
    if kind in QUOTE_SESSION_KINDS:
        if regular_open:
            if not _in_current_regular_session(stamp, now):
                return _off_hours_status(kind) if not is_regular_hours(stamp) else FreshnessStatus.STALE
            if age > max_age.total_seconds():
                return FreshnessStatus.STALE
            return FreshnessStatus.FRESH
        rules = freshness_config(config)
        if bool(rules.get("quote_closed_uses_last_session")):
            if _in_completed_regular_session(stamp, now):
                return FreshnessStatus.LAST_SESSION
            if _after_completed_session_close(stamp, now):
                return _off_hours_status(kind)
            return FreshnessStatus.STALE
    if kind == "liquidity":
        if regular_open:
            if age > max_age.total_seconds():
                return FreshnessStatus.STALE
            return FreshnessStatus.FRESH
        rules = freshness_config(config)
        if bool(rules.get("quote_closed_uses_last_session")):
            cal = NyseEquityCalendar()
            completed = cal.latest_completed_session(now)
            if completed is not None and stamp.astimezone(EASTERN).date() == completed.session_date:
                return FreshnessStatus.LAST_SESSION
    if age > max_age.total_seconds():
        return FreshnessStatus.STALE
    return FreshnessStatus.FRESH


def ttl_for(kind: str, *, now: datetime, config: Mapping[str, Any] | None = None) -> timedelta:
    rules = freshness_config(config)
    if kind in QUOTE_SESSION_KINDS:
        session = market_session_state(now)
        key = "quote_market_hours_seconds" if session.get("regular_hours_open") else "quote_seconds"
        return timedelta(seconds=int(rules.get(key) or DEFAULT_FRESHNESS[key]))
    mapping = {
        "liquidity": "liquidity_seconds",
        "fundamentals": "fundamentals_seconds",
        "identity": "identity_seconds",
        "technicals": "technicals_seconds",
        "news": "news_seconds",
    }
    key = mapping.get(kind, "fundamentals_seconds")
    return timedelta(seconds=int(rules.get(key) or DEFAULT_FRESHNESS[key]))


def unavailable(source: str | None = None, *, notes: list[str] | None = None) -> ProvenanceFact:
    return ProvenanceFact(
        value=None,
        source=source,
        as_of=None,
        freshness=FreshnessStatus.UNAVAILABLE,
        origin=FactOrigin.UNAVAILABLE,
        unavailable=True,
        notes=list(notes or []),
    )


def freshness_notes(fresh: FreshnessStatus) -> list[str]:
    if fresh is FreshnessStatus.LAST_SESSION:
        return ["last_completed_regular_session"]
    if fresh is FreshnessStatus.OFF_HOURS:
        return list(OFF_HOURS_NOTES)
    if fresh is FreshnessStatus.INDICATIVE:
        return list(INDICATIVE_NOTES)
    return []


def session_for_freshness(fresh: FreshnessStatus, *, kind: str) -> str | None:
    if kind not in QUOTE_SESSION_KINDS:
        return None
    if fresh in {FreshnessStatus.FRESH, FreshnessStatus.LAST_SESSION, FreshnessStatus.OFF_HOURS, FreshnessStatus.INDICATIVE}:
        return fresh.value
    return None


def merge_notes(*groups: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    for group in groups:
        for note in list(group or []):
            if note not in out:
                out.append(note)
    return out


def observed(
    value: Any,
    *,
    source: str,
    as_of: str | None,
    now: datetime,
    kind: str,
    origin: FactOrigin = FactOrigin.MCP_OBSERVED,
    notes: list[str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> ProvenanceFact:
    if value is None or value == "":
        return unavailable(source, notes=notes)
    fresh = evaluate_freshness(as_of, now=now, max_age=ttl_for(kind, now=now, config=config), kind=kind, config=config)
    extra_notes = merge_notes(notes, freshness_notes(fresh))
    return ProvenanceFact(
        value=value,
        source=source,
        as_of=as_of,
        freshness=fresh,
        origin=origin,
        unavailable=False,
        notes=extra_notes,
        session=session_for_freshness(fresh, kind=kind),
    )


def _tokens(value: Any) -> set[str]:
    text = str(value or "").strip().lower()
    if not text:
        return set()
    return {part for part in _TOKEN_SPLIT.split(text) if part}


def _looks_contaminated(value: Any) -> bool:
    return bool(_tokens(value) & CONTAMINATION_TOKENS)


def detect_synthetic_markers(*values: Any) -> list[str]:
    hits: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, FactOrigin) and value in SYNTHETIC_ORIGINS:
            hits.append(value.value)
            continue
        if isinstance(value, (list, tuple, set)):
            for item in value:
                hits.extend(detect_synthetic_markers(item))
            continue
        if isinstance(value, dict):
            for key, item in value.items():
                if _looks_contaminated(key) or _looks_contaminated(item):
                    hits.append(str(key))
                hits.extend(detect_synthetic_markers(item))
            continue
        if isinstance(value, ProvenanceFact):
            if value.origin in SYNTHETIC_ORIGINS:
                hits.append(value.origin.value)
            hits.extend(detect_synthetic_markers(value.source, value.notes))
            continue
        if _looks_contaminated(value):
            hits.append(str(value))
    return list(dict.fromkeys(hits))


def is_fund_instrument(snap: SecuritySnapshot) -> bool:
    kind = (snap.instrument_kind or "").strip().lower()
    if kind in FUND_KINDS:
        return True
    cls = snap.classification
    if cls and cls.security_class in {SecurityClass.BROAD_MARKET_INDEX_ETF, SecurityClass.OTHER_DIVERSIFIED_ETF}:
        return True
    if snap.name and _ETF_NAME.search(snap.name):
        return True
    if snap.industry and _FUND_INDUSTRY.search(snap.industry):
        return True
    return False


def is_individual_equity(snap: SecuritySnapshot) -> bool:
    if is_fund_instrument(snap):
        return False
    kind = (snap.instrument_kind or "").strip().lower()
    if kind == "equity":
        return True
    cls = snap.classification
    return bool(cls and cls.security_class == SecurityClass.INDIVIDUAL_EQUITY and kind != "etf")


def security_type_label(snap: SecuritySnapshot) -> str | None:
    if is_fund_instrument(snap):
        kind = (snap.instrument_kind or "").strip().lower()
        return kind if kind in FUND_KINDS else "etf"
    kind = (snap.instrument_kind or "").strip().lower()
    return kind or None


def _origin_from_snapshot(snap: SecuritySnapshot) -> FactOrigin | None:
    origin_raw = (snap.data_origin or "").strip().lower()
    if origin_raw:
        for item in FactOrigin:
            if item.value.lower() == origin_raw or item.name.lower() == origin_raw:
                return item
        if origin_raw in {"mcp", "robinhood", "live"}:
            return FactOrigin.MCP_OBSERVED
        if origin_raw in CONTAMINATION_TOKENS:
            try:
                return FactOrigin(origin_raw.upper()) if origin_raw.upper() in FactOrigin.__members__ else FactOrigin.SYNTHETIC
            except ValueError:
                return FactOrigin.SYNTHETIC
    hits = detect_synthetic_markers(snap.sources, snap.data_origin, snap.extra)
    if hits:
        return FactOrigin.SYNTHETIC
    return None


def _fact_or_unprovenanced(
    snap: SecuritySnapshot,
    key: str,
    value: Any,
    *,
    source: str | None,
    as_of: str | None,
    now: datetime,
    kind: str,
    origin: FactOrigin | None = None,
    notes: list[str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> ProvenanceFact:
    existing = (snap.fact_provenance or {}).get(key)
    if isinstance(existing, ProvenanceFact):
        if existing.unavailable or existing.value is None:
            return existing
        stamp = as_of or existing.as_of
        fresh = evaluate_freshness(
            stamp,
            now=now,
            max_age=ttl_for(kind, now=now, config=config),
            kind=kind,
            config=config,
        )
        return replace(
            existing,
            as_of=stamp,
            freshness=fresh,
            notes=merge_notes(existing.notes, notes, freshness_notes(fresh)),
            session=session_for_freshness(fresh, kind=kind) or existing.session,
        )
    if value is None or value == "":
        return unavailable(source, notes=notes)
    tagged = origin or _origin_from_snapshot(snap)
    if tagged in SYNTHETIC_ORIGINS:
        return ProvenanceFact(
            value=value,
            source=source or snap.data_origin,
            as_of=as_of,
            freshness=FreshnessStatus.UNKNOWN,
            origin=tagged,
            unavailable=False,
            notes=list(notes or []) + ["synthetic_or_fixture_origin"],
        )
    if tagged is FactOrigin.MCP_OBSERVED:
        return observed(value, source=source or "mcp", as_of=as_of or snap.observed_at, now=now, kind=kind, notes=notes, config=config)
    if not snap.fact_provenance and tagged is None:
        return ProvenanceFact(
            value=value,
            source=source,
            as_of=as_of,
            freshness=FreshnessStatus.UNKNOWN,
            origin=FactOrigin.SYNTHETIC,
            unavailable=False,
            notes=list(notes or []) + ["unprovenanced_live_fact"],
        )
    return observed(
        value,
        source=source or "snapshot",
        as_of=as_of or snap.observed_at,
        now=now,
        kind=kind,
        origin=tagged or FactOrigin.DERIVED,
        notes=notes,
        config=config,
    )


@dataclass
class InstrumentIdentity:
    ticker: str
    security_name: ProvenanceFact
    security_type: ProvenanceFact
    is_etf: bool
    is_equity: bool
    exchange: ProvenanceFact
    broker_instrument_id: ProvenanceFact
    quote: ProvenanceFact
    quote_source: str | None
    quote_as_of: str | None
    fundamentals_source: str | None
    fundamentals_as_of: str | None
    liquidity_source: str | None
    liquidity_as_of: str | None

    def as_report(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "security_name": self.security_name.for_ai(),
            "security_type": self.security_type.for_ai(),
            "is_etf": self.is_etf,
            "is_equity": self.is_equity,
            "exchange": self.exchange.for_ai(),
            "broker_instrument_id": self.broker_instrument_id.for_ai(),
            "quote": self.quote.for_ai(),
            "quote_source": self.quote_source,
            "quote_as_of": self.quote_as_of,
            "fundamentals_source": self.fundamentals_source,
            "fundamentals_as_of": self.fundamentals_as_of,
            "liquidity_source": self.liquidity_source,
            "liquidity_as_of": self.liquidity_as_of,
        }


@dataclass
class LiveCandidateFacts:
    ticker: str
    identity: InstrumentIdentity
    facts: dict[str, ProvenanceFact]
    is_etf: bool
    is_equity: bool
    synthetic_markers: list[str] = field(default_factory=list)

    def get(self, key: str) -> ProvenanceFact:
        return self.facts.get(key) or unavailable(key)

    def context_blobs(self) -> dict[str, Any]:
        identity = {
            "ticker": self.ticker,
            "security_name": self.identity.security_name.for_ai(),
            "security_type": self.identity.security_type.for_ai(),
            "is_etf": self.is_etf,
            "is_equity": self.is_equity,
            "exchange": self.identity.exchange.for_ai(),
            "broker_instrument_id": self.identity.broker_instrument_id.for_ai(),
        }
        market = {
            "last": self.get("last_price").for_ai(),
            "bid": self.get("bid").for_ai(),
            "ask": self.get("ask").for_ai(),
            "previous_close": self.get("previous_close").for_ai(),
            "name": self.identity.security_name.for_ai(),
            "instrument_kind": self.identity.security_type.for_ai(),
            "quote_source": self.identity.quote_source,
            "quote_as_of": self.identity.quote_as_of,
        }
        liquidity = {
            "dollar_volume": self.get("dollar_volume").for_ai(),
            "average_volume": self.get("average_volume").for_ai(),
            "bid_price": self.get("bid").for_ai(),
            "ask_price": self.get("ask").for_ai(),
            "absolute_spread_usd": self.get("absolute_spread_usd").for_ai(),
            "spread_percent": self.get("spread_percent").for_ai(),
            "spread_bps": self.get("spread_bps").for_ai(),
            "source": self.identity.liquidity_source,
            "as_of": self.identity.liquidity_as_of,
        }
        if self.is_etf:
            market["sector"] = unavailable("company_sector", notes=["etf_not_a_single_company_sector"]).for_ai()
            fundamentals = {
                "market_cap": unavailable("corporate_market_cap", notes=["etf_uses_net_assets_not_company_market_cap"]).for_ai(),
                "pe_ratio": unavailable("company_pe_ratio", notes=["etf_not_an_operating_company"]).for_ai(),
                "pb_ratio": unavailable("company_pb_ratio", notes=["etf_not_an_operating_company"]).for_ai(),
                "sector": unavailable("company_sector", notes=["diversified_fund_has_no_single_operating_sector"]).for_ai(),
                "industry": self.get("industry").for_ai(),
                "description": self.get("description").for_ai(),
                "revenue_growth": unavailable("corporate_revenue_growth", notes=["etf_not_an_operating_company"]).for_ai(),
                "earnings_growth": unavailable("corporate_earnings_growth", notes=["etf_not_an_operating_company"]).for_ai(),
                "net_assets": self.get("net_assets").for_ai(),
                "fund_nav": self.get("fund_nav").for_ai(),
                "expense_ratio": self.get("expense_ratio").for_ai(),
                "benchmark": self.get("benchmark").for_ai(),
                "fund_mandate": self.get("fund_mandate").for_ai(),
                "holdings": self.get("holdings").for_ai(),
                "fund_pe_ratio": self.get("fund_pe_ratio").for_ai(),
                "source": self.identity.fundamentals_source,
                "as_of": self.identity.fundamentals_as_of,
            }
        else:
            market["sector"] = self.get("sector").for_ai()
            fundamentals = {
                "market_cap": self.get("market_cap").for_ai(),
                "pe_ratio": self.get("pe_ratio").for_ai(),
                "pb_ratio": self.get("pb_ratio").for_ai(),
                "sector": self.get("sector").for_ai(),
                "industry": self.get("industry").for_ai(),
                "description": self.get("description").for_ai(),
                "revenue_growth": self.get("revenue_growth").for_ai(),
                "earnings_growth": self.get("earnings_growth").for_ai(),
                "source": self.identity.fundamentals_source,
                "as_of": self.identity.fundamentals_as_of,
            }
        return {
            "identity": identity,
            "market": market,
            "liquidity": liquidity,
            "fundamentals": fundamentals,
            "price_history": {
                "return_5d": self.get("return_5d").for_ai(),
                "return_21d": self.get("return_21d").for_ai(),
                "return_63d": self.get("return_63d").for_ai(),
                "return_252d": self.get("return_252d").for_ai(),
                "high_52_week": self.get("high_52_week").for_ai(),
                "low_52_week": self.get("low_52_week").for_ai(),
                "drawdown_from_52w_high": self.get("drawdown_from_52w_high").for_ai(),
            },
            "indicators": {
                "rsi": self.get("rsi").for_ai(),
                "sma_50": self.get("sma_50").for_ai(),
                "sma_200": self.get("sma_200").for_ai(),
                "atr": self.get("atr").for_ai(),
                "volume_vs_avg": self.get("volume_vs_avg").for_ai(),
            },
        }


@dataclass
class CandidateValidationResult:
    ticker: str
    status: CandidateValidationStatus
    reasons: list[str]
    facts: LiveCandidateFacts | None
    identity: InstrumentIdentity | None
    eligible_for_ai: bool
    synthetic_data_detected: bool
    data_freshness: dict[str, str] = field(default_factory=dict)

    def as_report(self) -> dict[str, Any]:
        facts = self.facts
        identity = self.identity
        quote = facts.get("last_price") if facts else unavailable("quote")
        return {
            "ticker": self.ticker,
            "security_name": identity.security_name.for_ai() if identity else unavailable("name").for_ai(),
            "security_type": identity.security_type.for_ai() if identity else unavailable("security_type").for_ai(),
            "quote": quote.for_ai(),
            "quote_source": identity.quote_source if identity else None,
            "quote_as_of": identity.quote_as_of if identity else None,
            "fundamental_or_fund_source": identity.fundamentals_source if identity else None,
            "liquidity": facts.get("dollar_volume").for_ai() if facts else unavailable("liquidity").for_ai(),
            "data_freshness": dict(self.data_freshness),
            "synthetic_data_detected": self.synthetic_data_detected,
            "eligible_for_ai": self.eligible_for_ai,
            "status": self.status.value,
            "rejection_reasons": list(self.reasons),
        }


def collect_candidate_facts(
    snap: SecuritySnapshot,
    *,
    now: datetime,
    config: Mapping[str, Any] | None = None,
) -> LiveCandidateFacts:
    as_of = snap.quote_as_of or snap.observed_at
    fund = is_fund_instrument(snap)
    equity = is_individual_equity(snap)
    type_label = security_type_label(snap)
    name_fact = _fact_or_unprovenanced(
        snap, "security_name", snap.name, source="get_equity_tradability.name", as_of=snap.observed_at, now=now, kind="identity", config=config
    )
    type_fact = _fact_or_unprovenanced(
        snap,
        "security_type",
        type_label,
        source="derived:instrument_kind+name+industry",
        as_of=snap.observed_at,
        now=now,
        kind="identity",
        origin=FactOrigin.DERIVED,
        config=config,
    )
    quote_source = snap.quote_source or "get_equity_quotes.last_trade_price"
    quote = _fact_or_unprovenanced(
        snap, "last_price", snap.current_price, source=quote_source, as_of=as_of, now=now, kind="quote", config=config
    )
    spread_value = snap.spread_pct
    spread_notes: list[str] = []
    if spread_value is not None and spread_value >= UNUSABLE_SPREAD_FRACTION:
        spread_value = None
        spread_notes.append("unusable_wide_spread_not_used")
    metrics = compute_spread_metrics(snap.bid, snap.ask) if spread_value is not None else None
    bid_as_of = snap.bid_as_of or snap.observed_at
    ask_as_of = snap.ask_as_of or snap.observed_at
    spread_as_of = later_as_of(snap.ask_as_of, snap.bid_as_of) or snap.observed_at
    percent_value = metrics["spread_percent"] if metrics is not None else spread_value
    bps_value = metrics["spread_bps"] if metrics is not None else (spread_value * 10000.0 if spread_value is not None else None)
    absolute_value = metrics["absolute_spread_usd"] if metrics is not None else None
    dollar = snap.dollar_volume
    avg_vol = snap.average_volume
    facts: dict[str, ProvenanceFact] = {
        "last_price": quote,
        "bid": _fact_or_unprovenanced(snap, "bid", snap.bid if spread_value is not None else None, source="get_equity_quotes.bid_price", as_of=bid_as_of, now=now, kind="quote", notes=spread_notes, config=config),
        "ask": _fact_or_unprovenanced(snap, "ask", snap.ask if spread_value is not None else None, source="get_equity_quotes.ask_price", as_of=ask_as_of, now=now, kind="quote", notes=spread_notes, config=config),
        "previous_close": _fact_or_unprovenanced(snap, "previous_close", snap.previous_close, source="get_equity_quotes.previous_close", as_of=as_of, now=now, kind="quote", config=config),
        "spread_pct": _fact_or_unprovenanced(snap, "spread_pct", spread_value, source="derived:bid_ask", as_of=spread_as_of, now=now, kind="spread", notes=list(spread_notes) + list(SPREAD_PERCENT_NOTES), origin=FactOrigin.DERIVED, config=config),
        "absolute_spread_usd": _fact_or_unprovenanced(
            snap,
            "absolute_spread_usd",
            absolute_value,
            source="derived:ask-bid",
            as_of=spread_as_of,
            now=now,
            kind="spread",
            origin=FactOrigin.DERIVED,
            notes=list(spread_notes) + list(ABSOLUTE_SPREAD_NOTES),
            config=config,
        ),
        "spread_percent": _fact_or_unprovenanced(
            snap,
            "spread_percent",
            percent_value,
            source="derived:(ask-bid)/midpoint",
            as_of=spread_as_of,
            now=now,
            kind="spread",
            origin=FactOrigin.DERIVED,
            notes=list(spread_notes) + list(SPREAD_PERCENT_NOTES),
            config=config,
        ),
        "spread_bps": _fact_or_unprovenanced(
            snap,
            "spread_bps",
            bps_value,
            source="derived:spread_percent*10000",
            as_of=spread_as_of,
            now=now,
            kind="spread",
            origin=FactOrigin.DERIVED,
            notes=list(spread_notes) + list(SPREAD_BPS_NOTES),
            config=config,
        ),
        "average_volume": _fact_or_unprovenanced(snap, "average_volume", avg_vol, source="get_equity_fundamentals.average_volume", as_of=snap.observed_at, now=now, kind="liquidity", config=config),
        "dollar_volume": _fact_or_unprovenanced(snap, "dollar_volume", dollar, source="derived:average_volume*price", as_of=as_of, now=now, kind="liquidity", config=config),
        "industry": _fact_or_unprovenanced(snap, "industry", snap.industry, source="get_equity_fundamentals.industry", as_of=snap.observed_at, now=now, kind="identity", config=config),
        "description": _fact_or_unprovenanced(snap, "description", snap.description, source="get_equity_fundamentals.description", as_of=snap.observed_at, now=now, kind="fundamentals", config=config),
        "high_52_week": _fact_or_unprovenanced(snap, "high_52_week", snap.high_52_week, source="get_equity_fundamentals.high_52_weeks", as_of=snap.observed_at, now=now, kind="fundamentals", config=config),
        "low_52_week": _fact_or_unprovenanced(snap, "low_52_week", snap.low_52_week, source="get_equity_fundamentals.low_52_weeks", as_of=snap.observed_at, now=now, kind="fundamentals", config=config),
        "return_5d": _fact_or_unprovenanced(snap, "return_5d", snap.return_5d, source="derived:get_equity_historicals", as_of=snap.observed_at, now=now, kind="technicals", config=config),
        "return_21d": _fact_or_unprovenanced(snap, "return_21d", snap.return_21d, source="derived:get_equity_historicals", as_of=snap.observed_at, now=now, kind="technicals", config=config),
        "return_63d": _fact_or_unprovenanced(snap, "return_63d", snap.return_63d, source="derived:get_equity_historicals", as_of=snap.observed_at, now=now, kind="technicals", config=config),
        "return_252d": _fact_or_unprovenanced(snap, "return_252d", snap.return_252d, source="derived:get_equity_historicals", as_of=snap.observed_at, now=now, kind="technicals", config=config),
        "drawdown_from_52w_high": _fact_or_unprovenanced(snap, "drawdown_from_52w_high", snap.drawdown_from_52w_high, source="derived:price/52w_high", as_of=snap.observed_at, now=now, kind="technicals", config=config),
        "rsi": _fact_or_unprovenanced(snap, "rsi", snap.rsi, source="get_equity_technical_indicators", as_of=snap.observed_at, now=now, kind="technicals", config=config),
        "sma_50": _fact_or_unprovenanced(snap, "sma_50", snap.sma_50, source="get_equity_technical_indicators", as_of=snap.observed_at, now=now, kind="technicals", config=config),
        "sma_200": _fact_or_unprovenanced(snap, "sma_200", snap.sma_200, source="get_equity_technical_indicators", as_of=snap.observed_at, now=now, kind="technicals", config=config),
        "atr": _fact_or_unprovenanced(snap, "atr", snap.atr, source="get_equity_technical_indicators", as_of=snap.observed_at, now=now, kind="technicals", config=config),
        "volume_vs_avg": _fact_or_unprovenanced(snap, "volume_vs_avg", snap.volume_vs_avg, source="derived:volume/average_volume", as_of=snap.observed_at, now=now, kind="technicals", config=config),
        "expense_ratio": unavailable("expense_ratio"),
        "fund_nav": unavailable("fund_nav"),
        "benchmark": _fact_or_unprovenanced(
            snap,
            "benchmark",
            getattr(snap.classification.evidence, "underlying_index", None) if snap.classification and snap.classification.evidence else None,
            source="derived:name+description",
            as_of=snap.observed_at,
            now=now,
            kind="identity",
            config=config,
        ),
        "fund_mandate": _fact_or_unprovenanced(snap, "fund_mandate", snap.description if fund else None, source="get_equity_fundamentals.description", as_of=snap.observed_at, now=now, kind="fundamentals", config=config),
        "holdings": unavailable("holdings", notes=["constituent_weights_not_in_mcp"]),
        "revenue_growth": unavailable("revenue_growth") if fund else _fact_or_unprovenanced(snap, "revenue_growth", _growth(snap.revenue_periods), source="derived:get_financials.revenue", as_of=snap.observed_at, now=now, kind="fundamentals", config=config),
        "earnings_growth": unavailable("earnings_growth") if fund else _fact_or_unprovenanced(snap, "earnings_growth", _growth(snap.net_income_periods), source="derived:get_financials.net_income", as_of=snap.observed_at, now=now, kind="fundamentals", config=config),
    }
    if fund:
        facts["net_assets"] = _fact_or_unprovenanced(
            snap,
            "net_assets",
            snap.market_cap,
            source="get_equity_fundamentals.market_cap",
            as_of=snap.observed_at,
            now=now,
            kind="fundamentals",
            notes=["fund_assets_not_corporate_market_cap"],
            config=config,
        )
        facts["market_cap"] = unavailable("corporate_market_cap", notes=["etf_uses_net_assets_not_company_market_cap"])
        facts["pe_ratio"] = unavailable("company_pe_ratio", notes=["etf_not_an_operating_company"])
        facts["pb_ratio"] = unavailable("company_pb_ratio", notes=["etf_not_an_operating_company"])
        facts["sector"] = unavailable("company_sector", notes=["diversified_fund_has_no_single_operating_sector"])
        facts["fund_pe_ratio"] = _fact_or_unprovenanced(
            snap, "fund_pe_ratio", snap.pe_ratio, source="get_equity_fundamentals.pe_ratio", as_of=snap.observed_at, now=now, kind="fundamentals", notes=["holdings_weighted_fund_multiple_not_company_pe"], config=config
        )
    else:
        facts["market_cap"] = _fact_or_unprovenanced(snap, "market_cap", snap.market_cap, source="get_equity_fundamentals.market_cap", as_of=snap.observed_at, now=now, kind="fundamentals", config=config)
        facts["pe_ratio"] = _fact_or_unprovenanced(snap, "pe_ratio", snap.pe_ratio, source="get_equity_fundamentals.pe_ratio", as_of=snap.observed_at, now=now, kind="fundamentals", config=config)
        facts["pb_ratio"] = _fact_or_unprovenanced(snap, "pb_ratio", snap.pb_ratio, source="get_equity_fundamentals.pb_ratio", as_of=snap.observed_at, now=now, kind="fundamentals", config=config)
        facts["sector"] = _fact_or_unprovenanced(snap, "sector", snap.sector, source="get_equity_fundamentals.sector", as_of=snap.observed_at, now=now, kind="fundamentals", config=config)
        facts["net_assets"] = unavailable("net_assets")
        facts["fund_pe_ratio"] = unavailable("fund_pe_ratio")

    identity = InstrumentIdentity(
        ticker=str(snap.symbol or "").upper(),
        security_name=name_fact,
        security_type=type_fact,
        is_etf=fund,
        is_equity=equity,
        exchange=_fact_or_unprovenanced(snap, "exchange", snap.exchange, source="tradability.exchange", as_of=snap.observed_at, now=now, kind="identity", config=config),
        broker_instrument_id=_fact_or_unprovenanced(
            snap, "broker_instrument_id", snap.broker_instrument_id, source="search.instrument_id", as_of=snap.observed_at, now=now, kind="identity", config=config
        ),
        quote=quote,
        quote_source=quote.source,
        quote_as_of=quote.as_of,
        fundamentals_source="get_equity_fundamentals" if (snap.market_cap is not None or snap.description or snap.industry) else None,
        fundamentals_as_of=snap.observed_at if (snap.market_cap is not None or snap.description or snap.industry) else None,
        liquidity_source="get_equity_fundamentals.average_volume+get_equity_quotes" if (dollar or avg_vol) else None,
        liquidity_as_of=later_as_of(as_of, spread_as_of) if (dollar or avg_vol or spread_value is not None) else None,
    )
    markers = detect_synthetic_markers(
        snap.data_origin,
        snap.sources,
        snap.extra,
        [fact.origin for fact in facts.values()],
        [fact.source for fact in facts.values()],
        name_fact.origin,
        type_fact.origin,
        quote.origin,
    )
    return LiveCandidateFacts(
        ticker=identity.ticker,
        identity=identity,
        facts=facts,
        is_etf=fund,
        is_equity=equity,
        synthetic_markers=markers,
    )


def _growth(series: list[float] | None, lag: int = 4) -> float | None:
    rows = [x for x in (series or []) if x is not None]
    if len(rows) > lag and rows[lag] not in (None, 0):
        return (rows[0] - rows[lag]) / abs(rows[lag])
    if len(rows) >= 2 and rows[1] not in (None, 0):
        return (rows[0] - rows[1]) / abs(rows[1])
    return None


def _identity_conflict(snap: SecuritySnapshot) -> list[str]:
    reasons: list[str] = []
    name = snap.name or ""
    kind = (snap.instrument_kind or "").strip().lower()
    fund_name = bool(_ETF_NAME.search(name))
    fund_industry = bool(snap.industry and _FUND_INDUSTRY.search(snap.industry))
    if kind == "equity" and (fund_name or fund_industry):
        reasons.append("equity_kind_conflicts_with_fund_name_or_industry")
    if kind in FUND_KINDS and name and re.search(r"common stock|ordinary shares", name, re.I) and not fund_name:
        reasons.append("etf_kind_conflicts_with_common_stock_name")
    cls = snap.classification
    if cls and cls.security_class == SecurityClass.INDIVIDUAL_EQUITY and fund_name and kind != "etf":
        reasons.append("individual_equity_class_conflicts_with_etf_name")
    return reasons


def validate_live_candidate(
    snap: SecuritySnapshot,
    *,
    now: datetime | None = None,
    runtime_mode: RuntimeMode | str = RuntimeMode.LIVE,
    config: Mapping[str, Any] | None = None,
) -> CandidateValidationResult:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    mode = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode).upper()
    ticker = str(snap.symbol or "").upper()
    facts = collect_candidate_facts(snap, now=stamp, config=config)
    freshness = {
        "quote": facts.get("last_price").freshness.value,
        "liquidity": facts.get("dollar_volume").freshness.value
        if not facts.get("dollar_volume").unavailable
        else facts.get("average_volume").freshness.value,
        "identity": facts.identity.security_name.freshness.value,
        "fundamentals": (facts.get("net_assets") if facts.is_etf else facts.get("market_cap")).freshness.value,
    }
    if mode != RuntimeMode.LIVE.value:
        return CandidateValidationResult(
            ticker=ticker,
            status=CandidateValidationStatus.VALID,
            reasons=[],
            facts=facts,
            identity=facts.identity,
            eligible_for_ai=True,
            synthetic_data_detected=bool(facts.synthetic_markers),
            data_freshness=freshness,
        )

    reasons: list[str] = []
    status: CandidateValidationStatus | None = None
    synthetic = bool(facts.synthetic_markers)
    if not ticker:
        status = CandidateValidationStatus.MISSING_IDENTITY
        reasons.append("ticker_missing")
    kind = (snap.instrument_kind or "").strip().lower()
    if kind in UNSUPPORTED_KINDS:
        status = CandidateValidationStatus.UNSUPPORTED_SECURITY_TYPE
        reasons.append(f"unsupported_instrument_kind={kind}")
    conflicts = _identity_conflict(snap)
    if conflicts:
        status = CandidateValidationStatus.IDENTITY_CONFLICT
        reasons.extend(conflicts)
    if not snap.name and not type_label_present(snap):
        status = status or CandidateValidationStatus.MISSING_IDENTITY
        reasons.append("security_name_and_type_missing")
    if synthetic:
        status = CandidateValidationStatus.SYNTHETIC_DATA_DETECTED
        reasons.append("synthetic_or_fixture_market_fact")
        reasons.extend(facts.synthetic_markers[:8])
    quote = facts.get("last_price")
    if quote.unavailable or quote.value is None:
        status = status or CandidateValidationStatus.MISSING_QUOTE
        reasons.append("quote_unavailable")
    elif quote.freshness is FreshnessStatus.STALE:
        status = status or CandidateValidationStatus.STALE_QUOTE
        reasons.append("quote_stale")
    elif quote.origin in SYNTHETIC_ORIGINS:
        status = CandidateValidationStatus.SYNTHETIC_DATA_DETECTED
        reasons.append("quote_origin_not_live")
    dollar = facts.get("dollar_volume")
    avg = facts.get("average_volume")
    if (dollar.unavailable or dollar.value is None) and (avg.unavailable or avg.value is None):
        status = status or CandidateValidationStatus.MISSING_LIQUIDITY
        reasons.append("liquidity_unavailable")
    fund_or_mcap = facts.get("net_assets") if facts.is_etf else facts.get("market_cap")
    if not fund_or_mcap.unavailable and fund_or_mcap.freshness is FreshnessStatus.STALE:
        status = status or CandidateValidationStatus.STALE_FUNDAMENTALS
        reasons.append("fundamentals_stale")
    if not facts.identity.security_name.value and not facts.identity.security_type.value:
        status = status or CandidateValidationStatus.INVALID_IDENTITY
        reasons.append("identity_unresolved")

    if status is None:
        status = CandidateValidationStatus.VALID
        reasons = []
    eligible = status is CandidateValidationStatus.VALID and not synthetic
    return CandidateValidationResult(
        ticker=ticker,
        status=status,
        reasons=list(dict.fromkeys(reasons)),
        facts=facts,
        identity=facts.identity,
        eligible_for_ai=eligible,
        synthetic_data_detected=synthetic,
        data_freshness=freshness,
    )


def type_label_present(snap: SecuritySnapshot) -> bool:
    return bool(security_type_label(snap) or snap.instrument_kind or (snap.classification and snap.classification.instrument_type))


def persist_identity(
    result: CandidateValidationResult,
    *,
    root: Path | None = None,
    runtime_mode: RuntimeMode | str = RuntimeMode.LIVE,
    now: datetime | None = None,
) -> Path | None:
    if not result.ticker:
        return None
    stamp = now or datetime.now(timezone.utc)
    mode = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode).upper()
    folder = (root or project_root()) / ("state/live_ai/identities" if mode == RuntimeMode.LIVE.value else "state/paper_ai/identities")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{result.ticker}.json"
    payload = {
        "ticker": result.ticker,
        "created_at": stamp.isoformat(),
        "runtime_mode": mode,
        "status": result.status.value,
        "eligible_for_ai": result.eligible_for_ai,
        "synthetic_data_detected": result.synthetic_data_detected,
        "rejection_reasons": list(result.reasons),
        "identity": result.identity.as_report() if result.identity else None,
        "facts": {key: fact.for_ai() for key, fact in (result.facts.facts if result.facts else {}).items()},
        "data_freshness": dict(result.data_freshness),
        "live_order_placement": False,
    }
    path.write_text(json.dumps(to_dict(payload), indent=2, default=str), encoding="utf-8")
    return path


def facts_from_payloads(
    ticker: str,
    payloads: Mapping[str, Any],
    *,
    now: datetime | None = None,
    source_id: str = "robinhood",
    config: Mapping[str, Any] | None = None,
) -> tuple[SecuritySnapshot, CandidateValidationResult]:
    """Assemble a snapshot from read-only MCP payloads and validate it. No AI."""
    from agentic_portfolio.adapters.discovery_source import SymbolPayloads, assemble_snapshot

    stamp = now or datetime.now(timezone.utc)
    payload = SymbolPayloads(
        symbol=str(ticker).upper(),
        sources=[source_id],
        tradability=payloads.get("tradability"),
        fundamentals=payloads.get("fundamentals"),
        search=payloads.get("search"),
        quotes=payloads.get("quotes"),
        financials=payloads.get("financials"),
        historicals=payloads.get("historicals"),
        rsi=payloads.get("rsi"),
        sma_50=payloads.get("sma_50"),
        sma_200=payloads.get("sma_200"),
        earnings_results=payloads.get("earnings_results"),
        news=payloads.get("news"),
        sec_index=payloads.get("sec_index"),
        observed_at=stamp.isoformat(),
    )
    snap = assemble_snapshot(payload, source_id=source_id)
    return snap, validate_live_candidate(snap, now=stamp, runtime_mode=RuntimeMode.LIVE, config=config)


def observe_live_candidate(
    ticker: str,
    fetcher: Any,
    *,
    now: datetime | None = None,
    runtime_mode: RuntimeMode | str = RuntimeMode.LIVE,
    config: Mapping[str, Any] | None = None,
) -> tuple[SecuritySnapshot | None, CandidateValidationResult, list[str]]:
    """Fetch LIVE facts through the authorized read-only adapter, then validate.

    Reuses facts_from_payloads + validate_live_candidate. Never calls an AI provider.
    """
    from agentic_portfolio.adapters.robinhood_read import fetch_instrument_payloads

    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    mode = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode).upper()
    symbol = str(ticker).upper()
    try:
        payloads, calls = fetch_instrument_payloads(symbol, fetcher)
    except Exception as exc:
        snap = SecuritySnapshot(symbol=symbol, observed_at=stamp.isoformat(), data_origin="mcp")
        validation = validate_live_candidate(snap, now=stamp, runtime_mode=mode, config=config)
        validation.reasons = [str(exc)] + [r for r in validation.reasons if r != "quote_unavailable"]
        validation.eligible_for_ai = False
        if validation.status is CandidateValidationStatus.VALID:
            validation.status = CandidateValidationStatus.MISSING_QUOTE
        return snap, validation, list(getattr(fetcher, "calls", []) or [])
    snap, validation = facts_from_payloads(symbol, payloads, now=stamp, config=config)
    return snap, validation, calls
