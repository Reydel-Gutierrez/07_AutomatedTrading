"""Parse Robinhood account/portfolio/position facts. No investment logic. No orders."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.schemas import (
    ClassificationStatus,
    OpenOrder,
    Position,
    PositionRegistryStatus,
    SpyBenchmark,
)
from agentic_portfolio.sleeve_registry import SleeveRegistry
from agentic_portfolio.thesis_registry import ThesisRegistry


class LiveErrorCode:
    """Stable production diagnostic codes. Safe to show on the dashboard."""

    MCP_INITIALIZE_FAILED = "MCP_INITIALIZE_FAILED"
    MCP_GET_ACCOUNTS_FAILED = "MCP_GET_ACCOUNTS_FAILED"
    MCP_GET_PORTFOLIO_FAILED = "MCP_GET_PORTFOLIO_FAILED"
    MCP_GET_POSITIONS_FAILED = "MCP_GET_POSITIONS_FAILED"
    MCP_QUOTES_FAILED = "MCP_QUOTES_FAILED"
    MCP_ORDERS_FAILED = "MCP_ORDERS_FAILED"
    MCP_TOOLS_CALL_FAILED = "MCP_TOOLS_CALL_FAILED"
    MCP_HTTP_TIMEOUT = "MCP_HTTP_TIMEOUT"
    MCP_HTTP_401 = "MCP_HTTP_401"
    MCP_HTTP_ERROR = "MCP_HTTP_ERROR"
    OAUTH_TOKEN_UNAVAILABLE = "OAUTH_TOKEN_UNAVAILABLE"
    OAUTH_REFRESH_FAILED = "OAUTH_REFRESH_FAILED"
    ACCOUNT_IDENTITY_MISMATCH = "ACCOUNT_IDENTITY_MISMATCH"
    LIVE_SNAPSHOT_PERSIST_FAILED = "LIVE_SNAPSHOT_PERSIST_FAILED"
    LIVE_DATA_UNAVAILABLE = "LIVE_DATA_UNAVAILABLE"


_SECRET_RE = re.compile(r"Bearer\s+\S+", re.I)
_SECRET_KV_RE = re.compile(
    r"(access_token|refresh_token|client_secret|password)([\"']?\s*[:=]\s*[\"']?)[^,\s\"']+",
    re.I,
)


def redact_live_error(message: str) -> str:
    """Strip tokens/credentials from diagnostic strings. Never log secrets."""
    text = _SECRET_RE.sub("Bearer [redacted]", str(message or ""))
    return _SECRET_KV_RE.sub(r"\1\2[redacted]", text)


def live_error_code_of(exc: BaseException | None, *, default: str = LiveErrorCode.LIVE_DATA_UNAVAILABLE) -> str:
    code = getattr(exc, "code", None)
    if code:
        return str(code)
    if isinstance(exc, LiveAccountError):
        return LiveErrorCode.ACCOUNT_IDENTITY_MISMATCH
    return default


class LiveDataUnavailable(RuntimeError):
    """Required LIVE Robinhood data is missing or unusable."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        cleaned = redact_live_error(message)
        super().__init__(cleaned)
        self.code = code or LiveErrorCode.LIVE_DATA_UNAVAILABLE


class LiveAccountError(RuntimeError):
    """Configured Agentic account was not confirmed."""

    def __init__(self, message: str, *, code: str = LiveErrorCode.ACCOUNT_IDENTITY_MISMATCH) -> None:
        super().__init__(redact_live_error(message))
        self.code = code


class PortfolioFetcher(Protocol):
    """Injected Robinhood read surface. Implementations must not wrap execution tools."""

    def get_accounts(self) -> Mapping[str, Any] | None: ...

    def get_portfolio(self, account_number: str) -> Mapping[str, Any] | None: ...

    def get_equity_positions(self, account_number: str) -> Mapping[str, Any] | None: ...

    def get_equity_quotes(self, symbols: str | list[str]) -> Mapping[str, Any] | None: ...

    def get_equity_orders(self, account_number: str, *, state: str | None = None) -> Mapping[str, Any] | None: ...


def as_symbol_list(symbols: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize quote/tradability symbol arguments. MCP always wants symbols: list[str]."""
    if symbols is None:
        return []
    if isinstance(symbols, str):
        token = symbols.strip().upper()
        return [token] if token else []
    out: list[str] = []
    for item in symbols:
        token = str(item or "").strip().upper()
        if token:
            out.append(token)
    return list(dict.fromkeys(out))


@dataclass
class StaticPortfolioFetcher:
    """Deterministic client for tests and scripted MCP responses."""

    accounts: Mapping[str, Any] | None = None
    portfolio: Mapping[str, Any] | None = None
    positions: Mapping[str, Any] | None = None
    quotes: Mapping[str, Any] | None = None
    orders: Mapping[str, Any] | None = None
    error: BaseException | None = None
    calls: list[str] = field(default_factory=list)

    def _check(self, name: str, payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
        self.calls.append(name)
        if self.error is not None:
            raise self.error
        if payload is None:
            raise LiveDataUnavailable(f"{name} unavailable")
        return dict(payload)

    def get_accounts(self) -> Mapping[str, Any]:
        return self._check("get_accounts", self.accounts)

    def get_portfolio(self, account_number: str) -> Mapping[str, Any]:
        del account_number
        return self._check("get_portfolio", self.portfolio)

    def get_equity_positions(self, account_number: str) -> Mapping[str, Any]:
        del account_number
        return self._check("get_equity_positions", self.positions)

    def get_equity_quotes(self, symbols: str | list[str]) -> Mapping[str, Any]:
        del symbols
        return self._check("get_equity_quotes", self.quotes)

    def get_equity_orders(self, account_number: str, *, state: str | None = None) -> Mapping[str, Any]:
        del account_number, state
        if self.orders is None:
            self.calls.append("get_equity_orders")
            return {"data": {"orders": []}}
        return self._check("get_equity_orders", self.orders)


def _payload_data(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    data = payload.get("data", payload)
    return dict(data) if isinstance(data, dict) else {}


def _float(value: Any, *, field: str) -> float:
    if value in (None, ""):
        raise LiveDataUnavailable(f"missing {field}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise LiveDataUnavailable(f"invalid {field}: {value!r}") from exc


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_accounts(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    data = _payload_data(payload)
    rows = data.get("accounts")
    if rows is None and isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
        rows = payload.get("accounts")
    if not isinstance(rows, list):
        raise LiveDataUnavailable("get_accounts did not return an account list")
    out: list[dict[str, Any]] = []
    for item in rows:
        if isinstance(item, dict):
            out.append(dict(item))
    if not out:
        raise LiveDataUnavailable("get_accounts returned no accounts")
    return out


def confirm_agentic_account(
    payload: Mapping[str, Any] | None,
    *,
    expected_number: str | None = None,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed unless the configured Agentic account is present and agentic-allowed."""
    account_cfg = dict((rules or load_account_rules()).get("account") or {})
    expected = str(expected_number or account_cfg.get("account_number") or "").strip()
    if not expected:
        raise LiveAccountError("configured Agentic account_number is missing")
    accounts = extract_accounts(payload)
    match = next((row for row in accounts if str(row.get("account_number") or "") == expected), None)
    if match is None:
        raise LiveAccountError("configured Agentic account was not found")
    if match.get("agentic_allowed") is not True:
        raise LiveAccountError("configured account is not accessible to this agent")
    if match.get("deactivated") or match.get("permanently_deactivated"):
        raise LiveAccountError("configured Agentic account is deactivated")
    state = str(match.get("state") or "active").strip().lower()
    if state not in {"", "active"}:
        raise LiveAccountError(f"configured Agentic account is not active ({state})")
    expected_nick = str(account_cfg.get("nickname") or "").strip()
    observed_nick = str(match.get("nickname") or "").strip()
    if expected_nick and observed_nick and observed_nick != expected_nick:
        raise LiveAccountError("configured Agentic nickname does not match the observed account")
    return match


def parse_portfolio(payload: Mapping[str, Any] | None) -> dict[str, float]:
    data = _payload_data(payload)
    if not data:
        raise LiveDataUnavailable("get_portfolio returned no data")
    nav = _float(data.get("total_value"), field="total_value")
    cash = _float(data.get("cash"), field="cash")
    bp_raw = data.get("buying_power")
    if isinstance(bp_raw, Mapping):
        buying_power = _float(bp_raw.get("buying_power"), field="buying_power")
    else:
        buying_power = _float(bp_raw, field="buying_power")
    if nav <= 0:
        raise LiveDataUnavailable("LIVE NAV must be positive")
    equity_value = _optional_float(data.get("equity_value")) or 0.0
    return {
        "current_nav": nav,
        "cash": cash,
        "buying_power": buying_power,
        "equity_value": equity_value,
    }


def quote_map(payload: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not payload:
        return {}
    data = payload.get("data", payload)
    results = data.get("results") if isinstance(data, dict) else None
    out: dict[str, dict[str, Any]] = {}
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            quote = item.get("quote") if isinstance(item.get("quote"), dict) else item
            symbol = str((quote or {}).get("symbol") or "").upper()
            if symbol:
                out[symbol] = dict(quote)
    return out


def quote_price(quote: Mapping[str, Any] | None) -> float | None:
    if not quote:
        return None
    for key in ("last_trade_price", "last_non_reg_trade_price", "previous_close", "adjusted_previous_close"):
        value = _optional_float(quote.get(key))
        if value is not None:
            return value
    return None


# Share-count fields for dollar volume. Same order as adapt_liquidity_evidence
# (ADV proxy) then SecuritySnapshot.dollar_volume (session volume fallback).
_SHARE_VOLUME_FIELDS = (
    "average_volume_2_weeks",
    "average_volume_30_days",
    "average_volume",
    "volume",
)
_PRECOMPUTED_DOLLAR_VOLUME_FIELDS = (
    "dollar_volume",
    "volume_usd",
    "recent_dollar_volume",
)


def quote_share_volume(quote: Mapping[str, Any] | None) -> float | None:
    """Usable share count for dollar volume. Fail closed when none is present."""
    if not quote:
        return None
    for key in _SHARE_VOLUME_FIELDS:
        value = _optional_float(quote.get(key))
        if value is not None and value > 0:
            return value
    return None


def quote_dollar_volume(quote: Mapping[str, Any] | None, *, price: float | None = None) -> float | None:
    """price × share volume, or an already-dollar field when the source provides one."""
    if not quote:
        return None
    for key in _PRECOMPUTED_DOLLAR_VOLUME_FIELDS:
        value = _optional_float(quote.get(key))
        if value is not None and value > 0:
            return value
    px = price if price is not None else quote_price(quote)
    if px is None or px <= 0:
        return None
    shares = quote_share_volume(quote)
    if shares is None:
        return None
    return shares * px


def _overlay_volume_fields(quote: dict[str, Any], extra: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(quote)
    if not extra:
        return merged
    for key in _SHARE_VOLUME_FIELDS + _PRECOMPUTED_DOLLAR_VOLUME_FIELDS:
        if _optional_float(merged.get(key)) is None and extra.get(key) not in (None, ""):
            merged[key] = extra[key]
    return merged


def parse_spy(payload: Mapping[str, Any] | None) -> SpyBenchmark | None:
    quote = quote_map(payload).get("SPY")
    if not quote:
        return None
    price = quote_price(quote)
    prev = _optional_float(quote.get("adjusted_previous_close") or quote.get("previous_close"))
    period_return = None
    if price is not None and prev:
        period_return = (price / prev) - 1.0
    return SpyBenchmark(price=price, period_return=period_return)


def parse_open_orders(payload: Mapping[str, Any] | None) -> list[OpenOrder]:
    data = _payload_data(payload)
    rows = data.get("orders") if isinstance(data.get("orders"), list) else []
    open_states = {"new", "queued", "confirmed", "unconfirmed", "partially_filled"}
    out: list[OpenOrder] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "").strip().lower()
        if state not in open_states:
            continue
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        qty = _optional_float(item.get("quantity") or item.get("dollar_based_amount"))
        out.append(
            OpenOrder(
                order_id=str(item.get("id") or item.get("order_id") or symbol),
                symbol=symbol,
                side=str(item.get("side") or ""),
                state=state,
                notional=qty,
            )
        )
    return out


def parse_filled_orders(
    payload: Mapping[str, Any] | None,
    *,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Filled/partial fills for cash-flow reconciliation. Not an execution path."""
    data = _payload_data(payload)
    rows = data.get("orders") if isinstance(data.get("orders"), list) else []
    filled_states = {"filled", "partially_filled"}
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or item.get("status") or "").strip().lower()
        if state not in filled_states:
            continue
        stamp = str(item.get("updated_at") or item.get("last_transaction_at") or item.get("created_at") or "")
        if since and stamp and stamp < since:
            continue
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        qty = _optional_float(item.get("filled_quantity") or item.get("quantity"))
        price = _optional_float(item.get("average_fill_price") or item.get("price") or item.get("average_price"))
        if not qty or not price:
            continue
        out.append(
            {
                "symbol": symbol,
                "side": str(item.get("side") or ""),
                "quantity": qty,
                "price": price,
                "state": state,
                "updated_at": stamp or None,
            }
        )
    return out


def parse_positions(
    payload: Mapping[str, Any] | None,
    *,
    quotes: Mapping[str, Any] | None = None,
    sleeves: SleeveRegistry | None = None,
    theses: ThesisRegistry | None = None,
) -> list[Position]:
    data = _payload_data(payload)
    if "positions" not in data and payload:
        raise LiveDataUnavailable("get_equity_positions returned no positions field")
    rows = data.get("positions")
    if rows is None:
        raise LiveDataUnavailable("get_equity_positions returned no positions field")
    if not isinstance(rows, list):
        raise LiveDataUnavailable("get_equity_positions positions is not a list")
    quote_by_symbol = quote_map(quotes)
    positions: list[Position] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            raise LiveDataUnavailable("position is missing symbol")
        quantity = _float(item.get("quantity"), field=f"{symbol}.quantity")
        avg = _optional_float(item.get("average_buy_price") or item.get("average_cost"))
        price = quote_price(quote_by_symbol.get(symbol))
        if price is None:
            price = _optional_float(item.get("current_price") or item.get("last_trade_price"))
        if quantity and price is None:
            raise LiveDataUnavailable(f"LIVE market value unavailable for {symbol}")
        market_value = quantity * (price or 0.0)
        unreal = None
        if avg is not None and price is not None:
            unreal = (price - avg) * quantity
        sleeve_rec = sleeves.get(symbol) if sleeves is not None else None
        thesis = theses.current_for_symbol(symbol) if theses is not None else None
        positions.append(
            Position(
                symbol=symbol,
                market_value=market_value,
                quantity=quantity,
                average_cost=avg,
                current_price=price,
                sleeve=sleeve_rec.sleeve if sleeve_rec else None,
                security_class=None,
                classification_status=ClassificationStatus.PARTIAL,
                unrealized_pnl=unreal,
                registry_status=PositionRegistryStatus.REGISTERED if sleeve_rec else PositionRegistryStatus.UNREGISTERED_POSITION,
                thesis_id=thesis.thesis_id if thesis else (sleeve_rec.thesis_id if sleeve_rec else None),
            )
        )
    return positions


def watch_quotes_from_payload(
    payload: Mapping[str, Any] | None,
    *,
    fundamentals: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Normalize MCP quote payloads into the watch/condition {symbol: {price, ...}} map.

    Live get_equity_quotes has bid/ask/last but no volume. Overlay get_equity_fundamentals
    when provided so dollar_volume can use the same share-count fields as discovery liquidity.
    Missing volume stays None (fail closed).
    """
    funds = quote_map(fundamentals) if fundamentals else {}
    out: dict[str, dict[str, Any]] = {}
    for symbol, quote in quote_map(payload).items():
        merged = _overlay_volume_fields(dict(quote), funds.get(symbol))
        price = quote_price(merged)
        bid = _optional_float(merged.get("bid_price"))
        ask = _optional_float(merged.get("ask_price"))
        spread_bps = None
        if bid is not None and ask is not None and (bid + ask) > 0:
            spread_bps = ((ask - bid) / ((ask + bid) / 2.0)) * 10_000.0
        out[symbol] = {
            "price": price,
            "last": price,
            "previous_close": _optional_float(merged.get("previous_close") or merged.get("adjusted_previous_close")),
            "spread_bps": spread_bps,
            "bid": bid,
            "ask": ask,
            "dollar_volume": quote_dollar_volume(merged, price=price),
        }
    return out
