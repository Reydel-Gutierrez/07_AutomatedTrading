"""Parse Robinhood account/portfolio/position facts. No investment logic. No orders."""

from __future__ import annotations

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


class LiveDataUnavailable(RuntimeError):
    """Required LIVE Robinhood data is missing or unusable."""


class LiveAccountError(RuntimeError):
    """Configured Agentic account was not confirmed."""


class PortfolioFetcher(Protocol):
    """Injected Robinhood read surface. Implementations must not wrap execution tools."""

    def get_accounts(self) -> Mapping[str, Any] | None: ...

    def get_portfolio(self, account_number: str) -> Mapping[str, Any] | None: ...

    def get_equity_positions(self, account_number: str) -> Mapping[str, Any] | None: ...

    def get_equity_quotes(self, symbols: list[str]) -> Mapping[str, Any] | None: ...

    def get_equity_orders(self, account_number: str, *, state: str | None = None) -> Mapping[str, Any] | None: ...


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

    def get_equity_quotes(self, symbols: list[str]) -> Mapping[str, Any]:
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


def _quote_map(payload: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
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


def parse_spy(payload: Mapping[str, Any] | None) -> SpyBenchmark | None:
    quote = _quote_map(payload).get("SPY")
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
    quote_by_symbol = _quote_map(quotes)
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
