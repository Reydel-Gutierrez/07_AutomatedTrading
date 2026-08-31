"""Deterministic live universe construction. No AI. One source failure is not fatal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from agentic_portfolio.adapters.discovery_source import (
    assemble_snapshot,
    symbols_from_earnings_calendar,
    symbols_from_scan,
    symbols_from_watchlist_items,
)
from agentic_portfolio.discovery.snapshot import SecuritySnapshot
from agentic_portfolio.policy import load_discovery_config


UNSUPPORTED = {"option", "crypto", "future", "event", "currency_pair"}
GARBAGE = {"NONE", "NULL", "TEST", "FAKE", "N/A", ""}


@dataclass
class UniverseMember:
    symbol: str
    sources: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str | None = None


@dataclass
class SourceAttempt:
    name: str
    attempted: bool = False
    successful: bool = False
    symbols: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class UniverseResult:
    members: list[UniverseMember]
    sources: list[SourceAttempt]
    unique_symbols: list[str]
    skipped: list[UniverseMember]
    errors: list[str]
    unique_universe_size: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "sources_attempted": [s.name for s in self.sources if s.attempted],
            "sources_successful": [s.name for s in self.sources if s.successful],
            "symbols_discovered_by_source": {s.name: list(s.symbols) for s in self.sources},
            "unique_universe_size": self.unique_universe_size,
            "unique_symbols": list(self.unique_symbols),
            "skipped": [m.symbol for m in self.skipped],
            "errors": list(self.errors),
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def construct_universe(
    fetcher: Any,
    *,
    held_symbols: list[str] | None = None,
    config: dict | None = None,
    now: datetime | None = None,
    source_filter: list[str] | None = None,
) -> UniverseResult:
    """Build a bounded, de-duplicated symbol universe from live read sources.

    AI is never called. Missing sources are recorded and skipped.
    """
    cfg = (config or load_discovery_config()).get("universe_construction") or {}
    wanted = list(source_filter or cfg.get("sources") or [])
    max_size = int(cfg.get("max_universe_size") or 40)
    max_per = int(cfg.get("max_per_source") or 25)
    min_price = float(cfg.get("min_last_price") or 5.0)
    held = {str(s).upper() for s in (held_symbols or []) if s}
    attempts: list[SourceAttempt] = []
    by_symbol: dict[str, UniverseMember] = {}
    errors: list[str] = []

    def add(symbols: list[str], source: str, reason: str) -> list[str]:
        kept: list[str] = []
        for raw in symbols:
            sym = _normalize(raw)
            if not sym:
                continue
            if len(kept) >= max_per:
                break
            member = by_symbol.get(sym)
            if member is None:
                member = UniverseMember(symbol=sym, sources=[source], reasons=[reason])
                by_symbol[sym] = member
            else:
                if source not in member.sources:
                    member.sources.append(source)
                if reason not in member.reasons:
                    member.reasons.append(reason)
            kept.append(sym)
        return kept

    runners: dict[str, Callable[[], list[str]]] = {
        "account_positions": lambda: _run_positions(fetcher, held),
        "account_watchlists": lambda: _run_watchlists(fetcher),
        "popular_watchlists": lambda: _run_popular(fetcher, cfg),
        "saved_scans": lambda: _run_scans(fetcher),
        "earnings_calendar": lambda: _run_earnings(fetcher, cfg, now or _now()),
        "portfolio_adjacent": lambda: _run_adjacent(held),
        "core_liquid": lambda: _run_configured(cfg, "core_liquid_symbols"),
        "liquid_etfs": lambda: _run_configured(cfg, "liquid_etf_symbols"),
    }
    if not wanted:
        wanted = list(runners)

    for name in wanted:
        runner = runners.get(name)
        attempt = SourceAttempt(name=name, attempted=True)
        if runner is None:
            attempt.error = "unknown_source"
            attempts.append(attempt)
            continue
        try:
            symbols = runner()
            attempt.symbols = add(symbols, name, _reason_for(name))
            attempt.successful = True
        except Exception as exc:  # noqa: BLE001 — one source must not abort discovery
            attempt.error = f"{type(exc).__name__}: {exc}"
            errors.append(f"{name}: {attempt.error}")
        attempts.append(attempt)

    members = list(by_symbol.values())
    skipped: list[UniverseMember] = []
    unique: list[str] = []
    for member in members:
        if member.symbol in GARBAGE or not member.symbol.isascii() or not member.symbol.replace(".", "").isalnum():
            member.skipped = True
            member.skip_reason = "invalid_symbol"
            skipped.append(member)
            continue
        unique.append(member.symbol)

    unique = _bound(unique, max_size, held)
    kept_members = [by_symbol[s] for s in unique]

    quotes = _safe_quotes(fetcher, unique)
    trad = _safe_tradability(fetcher, unique)
    for member in kept_members:
        row = quotes.get(member.symbol) or {}
        trad_row = trad.get(member.symbol) or {}
        kind = str(trad_row.get("instrument_kind") or trad_row.get("type") or "").lower()
        if kind in UNSUPPORTED or str(row.get("asset_type") or "").lower() in UNSUPPORTED:
            member.skipped = True
            member.skip_reason = "unsupported_instrument"
            skipped.append(member)
            continue
        tradable = trad_row.get("tradeable")
        if tradable is False:
            member.skipped = True
            member.skip_reason = "not_tradable"
            skipped.append(member)
            continue
        price = _f(row.get("last_trade_price") or row.get("previous_close"))
        if (
            price is not None
            and price < min_price
            and not (member.symbol in held and cfg.get("existing_position_exempt_from_min_price", True))
        ):
            member.skipped = True
            member.skip_reason = "penny_or_low_price"
            skipped.append(member)

    accepted = [m for m in kept_members if not m.skipped]
    accepted_symbols = [m.symbol for m in accepted]
    if not accepted_symbols and cfg.get("baseline_only_if_empty") and held:
        for sym in list(held)[:max_size]:
            member = by_symbol.get(sym) or UniverseMember(symbol=sym, sources=["account_positions"], reasons=["fallback_existing_holding"])
            member.skipped = False
            member.skip_reason = None
            accepted.append(member)
            accepted_symbols.append(sym)

    return UniverseResult(
        members=accepted,
        sources=attempts,
        unique_symbols=accepted_symbols,
        skipped=skipped,
        errors=errors,
        unique_universe_size=len(accepted_symbols),
    )


def snapshots_for_universe(
    fetcher: Any,
    universe: UniverseResult,
    *,
    config: dict | None = None,
    now: datetime | None = None,
) -> list[SecuritySnapshot]:
    """Fetch bounded read-only snapshots. No AI. Missing payload fields stay missing."""
    cfg = (config or load_discovery_config()).get("universe_construction") or {}
    cap = int(cfg.get("max_snapshots_to_score") or 30)
    stamp = (now or _now()).isoformat()
    symbols = list(universe.unique_symbols)[:cap]
    if not symbols:
        return []
    quotes = _safe_quotes(fetcher, symbols)
    trad = _safe_tradability(fetcher, symbols)
    funds = _safe_fundamentals(fetcher, symbols)
    out: list[SecuritySnapshot] = []
    by_symbol = {m.symbol: m for m in universe.members}
    for symbol in symbols:
        member = by_symbol.get(symbol)
        from agentic_portfolio.adapters.discovery_source import SymbolPayloads

        payload = SymbolPayloads(
            symbol=symbol,
            sources=list(member.sources if member else ["live_universe"]),
            tradability=_wrap_row(trad.get(symbol), symbol),
            fundamentals=_wrap_row(funds.get(symbol), symbol),
            quotes=_wrap_row(quotes.get(symbol), symbol),
            observed_at=stamp,
        )
        snap = assemble_snapshot(payload, source_id="robinhood")
        if member:
            snap.sources = list(member.sources)
            snap.evidence_refs = list(dict.fromkeys(list(snap.evidence_refs) + [f"universe:{r}" for r in member.reasons]))
        out.append(snap)
    return out


def _normalize(raw: Any) -> str:
    text = str(raw or "").strip().upper()
    if not text or text in GARBAGE:
        return ""
    if " " in text or len(text) > 8:
        return ""
    return text


def _reason_for(source: str) -> str:
    return {
        "account_positions": "existing_live_holding",
        "account_watchlists": "account_watchlist",
        "popular_watchlists": "robinhood_popular_watchlist",
        "saved_scans": "saved_market_scan",
        "earnings_calendar": "upcoming_earnings",
        "portfolio_adjacent": "portfolio_adjacent_symbol",
        "core_liquid": "core_liquid_universe",
        "liquid_etfs": "liquid_etf_universe",
    }.get(source, source)


def _bound(symbols: list[str], max_size: int, held: set[str]) -> list[str]:
    ordered: list[str] = []
    for sym in list(held) + [s for s in symbols if s not in held]:
        if sym not in ordered:
            ordered.append(sym)
        if len(ordered) >= max_size:
            break
    return ordered


def _run_positions(fetcher: Any, held: set[str]) -> list[str]:
    if held:
        return list(held)
    payload = _call(fetcher, "positions") or _call(fetcher, "get_equity_positions")
    return _symbols_from_positions(payload)


def _run_watchlists(fetcher: Any) -> list[str]:
    lists = _call(fetcher, "watchlists") or _call(fetcher, "get_watchlists") or {}
    out: list[str] = []
    for item in _rows(lists):
        list_id = item.get("list_id") or item.get("id") or item.get("id")
        if not list_id:
            continue
        items = _call(fetcher, "watchlist_items", list_id=str(list_id)) or _call(fetcher, "get_watchlist_items", list_id=str(list_id))
        out.extend(symbols_from_watchlist_items(items))
    return out


def _run_popular(fetcher: Any, cfg: Mapping[str, Any]) -> list[str]:
    payload = _call(fetcher, "popular_watchlists") or _call(fetcher, "get_popular_watchlists") or {}
    titles = {str(t).lower() for t in (cfg.get("popular_watchlist_titles") or [])}
    out: list[str] = []
    for item in _rows(payload):
        title = str(item.get("title") or item.get("name") or "").lower()
        if titles and title not in titles and not any(t in title for t in titles):
            continue
        list_id = item.get("list_id") or item.get("id")
        if not list_id:
            continue
        items = _call(fetcher, "watchlist_items", list_id=str(list_id)) or _call(fetcher, "get_watchlist_items", list_id=str(list_id))
        out.extend(symbols_from_watchlist_items(items))
    return out


def _run_scans(fetcher: Any) -> list[str]:
    scans = _call(fetcher, "scans") or _call(fetcher, "get_scans") or {}
    out: list[str] = []
    for item in _rows(scans):
        scan_id = item.get("scan_id") or item.get("id")
        if not scan_id:
            continue
        result = _call(fetcher, "run_scan", scan_id=str(scan_id))
        out.extend(symbols_from_scan(result))
    return out


def _run_earnings(fetcher: Any, cfg: Mapping[str, Any], now: datetime) -> list[str]:
    days = int(cfg.get("earnings_days") or 7)
    filt = cfg.get("earnings_filter")
    kwargs: dict[str, Any] = {"days": days}
    if filt:
        kwargs["filter"] = filt
    payload = _call(fetcher, "earnings_calendar", **kwargs) or _call(fetcher, "get_earnings_calendar", **kwargs)
    return [sym for sym, _days in symbols_from_earnings_calendar(payload)]


def _run_adjacent(held: set[str]) -> list[str]:
    return list(held)


def _run_configured(cfg: Mapping[str, Any], key: str) -> list[str]:
    return [str(s).strip().upper() for s in (cfg.get(key) or []) if str(s).strip()]


def _symbols_from_positions(payload: Mapping[str, Any] | None) -> list[str]:
    out: list[str] = []
    data = payload.get("data", payload) if isinstance(payload, Mapping) else {}
    rows = []
    if isinstance(data, dict):
        rows = data.get("positions") or data.get("results") or []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                sym = _normalize(row.get("symbol") or row.get("ticker"))
                if sym:
                    out.append(sym)
    return out


def _safe_quotes(fetcher: Any, symbols: list[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    try:
        payload = _call(fetcher, "quotes", symbols) or _call(fetcher, "get_equity_quotes", symbols)
    except Exception:  # noqa: BLE001
        payload = None
    return _index_by_symbol(payload, extra_keys=("quote",))


def _safe_tradability(fetcher: Any, symbols: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(symbols, 10):
        try:
            payload = _call(fetcher, "tradability", chunk) or _call(fetcher, "get_equity_tradability", chunk)
        except Exception:  # noqa: BLE001
            continue
        out.update(_index_by_symbol(payload))
    return out


def _safe_fundamentals(fetcher: Any, symbols: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(symbols, 10):
        try:
            payload = _call(fetcher, "fundamentals", chunk) or _call(fetcher, "get_equity_fundamentals", chunk)
        except Exception:  # noqa: BLE001
            continue
        out.update(_index_by_symbol(payload))
    return out


def _call(fetcher: Any, name: str, *args: Any, **kwargs: Any) -> Mapping[str, Any] | None:
    method = getattr(fetcher, name, None)
    if not callable(method):
        return None
    if args and not kwargs:
        try:
            return method(*args)
        except TypeError:
            if len(args) == 1:
                return method(args[0])
            raise
    return method(*args, **kwargs) if kwargs else method(*args) if args else method()


def _rows(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return []
    rows = data.get("results") or data.get("watchlists") or data.get("scans") or data.get("items") or data.get("events") or []
    if isinstance(rows, list):
        return [r for r in rows if isinstance(r, dict)]
    return []


def _index_by_symbol(payload: Mapping[str, Any] | None, *, extra_keys: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not payload:
        return out
    data = payload.get("data", payload) if isinstance(payload, Mapping) else {}
    rows = []
    if isinstance(data, dict):
        rows = data.get("results") or data.get("quotes") or []
        if not rows and ("symbol" in data or "last_trade_price" in data):
            rows = [data]
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            inner = dict(row)
            for key in extra_keys:
                nested = row.get(key)
                if isinstance(nested, dict):
                    inner = {**inner, **nested}
            sym = _normalize(inner.get("symbol") or row.get("symbol"))
            if sym:
                out[sym] = inner
    return out


def _wrap_row(row: dict[str, Any] | None, symbol: str) -> dict[str, Any] | None:
    if not row:
        return None
    return {"data": {"results": [{**row, "symbol": symbol}]}}


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _f(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
