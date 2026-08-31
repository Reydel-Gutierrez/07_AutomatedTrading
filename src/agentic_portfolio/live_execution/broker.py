"""Broker client protocol, fake broker, and gated live write adapter.

Only LiveOrderExecutor may call place_equity_order through this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from uuid import uuid4

from agentic_portfolio.adapters.portfolio_facts import LiveDataUnavailable
from agentic_portfolio.adapters.robinhood_read import FORBIDDEN_MCP_TOOLS
from agentic_portfolio.runtime import live_placement_enabled


class BrokerClient(Protocol):
    """Injected broker used by LiveOrderExecutor. FakeBroker implements the same surface."""

    def review_equity_order(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def place_equity_order(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def get_equity_orders(self, account_number: str, **kwargs: Any) -> dict[str, Any]: ...

    def get_equity_quotes(self, symbols: list[str] | str) -> dict[str, Any]: ...

    def get_accounts(self) -> dict[str, Any]: ...

    def get_portfolio(self, account_number: str) -> dict[str, Any]: ...

    def get_equity_positions(self, account_number: str) -> dict[str, Any]: ...

    def get_equity_tradability(self, symbols: str | list[str]) -> dict[str, Any]: ...


WRITE_TOOLS = frozenset({"review_equity_order", "place_equity_order", "cancel_equity_order"})
READ_TOOLS = frozenset(
    {
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_equity_quotes",
        "get_equity_orders",
        "get_equity_tradability",
    }
)


class LiveWriteAdapter:
    """Allowlisted write+read transport used exclusively by LiveOrderExecutor."""

    def __init__(self, transport: Any, *, account_number: str) -> None:
        self._transport = transport
        self.account_number = str(account_number)
        self.calls: list[str] = []

    def _invoke(self, tool: str, **kwargs: Any) -> dict[str, Any]:
        if tool not in WRITE_TOOLS | READ_TOOLS:
            raise LiveDataUnavailable(f"live write adapter refused tool: {tool}")
        if tool in FORBIDDEN_MCP_TOOLS - WRITE_TOOLS:
            raise LiveDataUnavailable(f"refused forbidden MCP tool: {tool}")
        self.calls.append(tool)
        result = self._transport(tool, **kwargs)
        return dict(result) if isinstance(result, Mapping) else {"data": result}

    def review_equity_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._invoke("review_equity_order", **payload)

    def place_equity_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._invoke("place_equity_order", **payload)

    def get_equity_orders(self, account_number: str, **kwargs: Any) -> dict[str, Any]:
        return self._invoke("get_equity_orders", account_number=str(account_number), **kwargs)

    def get_equity_quotes(self, symbols: list[str] | str) -> dict[str, Any]:
        tickers = [symbols] if isinstance(symbols, str) else list(symbols)
        return self._invoke("get_equity_quotes", symbols=tickers)

    def get_accounts(self) -> dict[str, Any]:
        return self._invoke("get_accounts")

    def get_portfolio(self, account_number: str) -> dict[str, Any]:
        return self._invoke("get_portfolio", account_number=str(account_number))

    def get_equity_positions(self, account_number: str) -> dict[str, Any]:
        return self._invoke("get_equity_positions", account_number=str(account_number))

    def get_equity_tradability(self, symbols: str | list[str]) -> dict[str, Any]:
        tickers = [symbols] if isinstance(symbols, str) else list(symbols)
        return self._invoke("get_equity_tradability", account_number=self.account_number, symbols=tickers)


def bind_live_write_broker(
    *,
    account_number: str,
    environ: Mapping[str, str] | None = None,
    transport: Any | None = None,
) -> LiveWriteAdapter | None:
    """Return a write adapter only when LIVE_ORDER_PLACEMENT is explicitly on.

    The readonly observation transport stays read-only. This adapter is a
    separate allowlisted client. Missing credentials fail closed (None).
    """
    env = environ if environ is not None else os.environ
    if not live_placement_enabled(environ=env):
        return None
    if transport is not None:
        return LiveWriteAdapter(transport, account_number=str(account_number))
    try:
        from agentic_portfolio.adapters.readonly_runtime import (
            _make_http_transport,
            _read_token,
            resolve_readonly_mcp_url,
        )
    except Exception:  # noqa: BLE001
        return None
    token, error = _read_token(environ=env)
    if not token:
        del error
        return None
    url = resolve_readonly_mcp_url(environ=env)
    try:
        http = _make_http_transport(url, token, environ=env)
    except Exception:  # noqa: BLE001
        return None
    return LiveWriteAdapter(http, account_number=str(account_number))


@dataclass
class FakeBroker:
    """Deterministic broker for release-candidate tests. Same protocol as production."""

    account_number: str = "549688554"
    nav: float = 500.0
    cash: float = 500.0
    buying_power: float = 500.0
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    quotes: dict[str, float] = field(default_factory=lambda: {"SPY": 500.0})
    spreads: dict[str, float] = field(default_factory=dict)
    tradable: dict[str, bool] = field(default_factory=dict)
    connected: bool = True
    review_ok: bool = True
    review_errors: list[str] = field(default_factory=list)
    review_warnings: list[str] = field(default_factory=list)
    place_ok: bool = True
    place_timeout: bool = False
    ambiguous_submit: bool = False
    reject_reason: str | None = None
    next_status: str = "filled"
    fill_ratio: float = 1.0
    orders: list[dict[str, Any]] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    place_calls: list[dict[str, Any]] = field(default_factory=list)
    cancel_calls: list[dict[str, Any]] = field(default_factory=list)
    disconnected_once: bool = False

    def disconnect(self) -> None:
        self.connected = False

    def reconnect(self) -> None:
        self.connected = True

    def move_quote(self, symbol: str, price: float) -> None:
        self.quotes[str(symbol).upper()] = float(price)

    def set_buying_power(self, value: float) -> None:
        self.buying_power = float(value)

    def _require(self) -> None:
        if not self.connected:
            raise LiveDataUnavailable("broker disconnected")

    def review_equity_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require()
        self.reviews.append(dict(payload))
        symbol = str(payload.get("symbol") or "").upper()
        quote = self.quotes.get(symbol)
        if not self.review_ok:
            return {
                "data": {
                    "ok": False,
                    "errors": list(self.review_errors or [self.reject_reason or "REVIEW_REJECTED"]),
                    "warnings": list(self.review_warnings),
                    "quote": {"last_trade_price": quote},
                }
            }
        return {
            "data": {
                "ok": True,
                "estimated_cost": payload.get("dollar_amount") or payload.get("quantity"),
                "warnings": list(self.review_warnings),
                "errors": [],
                "quote": {"last_trade_price": quote, "symbol": symbol},
            }
        }

    def place_equity_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require()
        self.place_calls.append(dict(payload))
        if self.place_timeout:
            if self.ambiguous_submit:
                order = self._record_order(payload, status="queued")
                raise TimeoutError("ambiguous broker submission")
            raise TimeoutError("broker place timeout")
        if not self.place_ok:
            return {
                "data": {
                    "ok": False,
                    "state": "rejected",
                    "reject_reason": self.reject_reason or "broker_rejected",
                    "orders": [],
                }
            }
        status = self.next_status
        order = self._record_order(payload, status=status)
        if self.ambiguous_submit:
            raise TimeoutError("ambiguous broker submission")
        return {"data": {"ok": True, "orders": [order], "order_id": order["id"], "state": status}}

    def get_equity_orders(self, account_number: str, **kwargs: Any) -> dict[str, Any]:
        self._require()
        order_id = kwargs.get("order_id")
        rows = list(self.orders)
        if order_id:
            rows = [o for o in rows if o.get("id") == order_id]
        symbol = kwargs.get("symbol")
        if symbol:
            rows = [o for o in rows if str(o.get("symbol") or "").upper() == str(symbol).upper()]
        return {"data": {"orders": rows}}

    def get_equity_quotes(self, symbols: list[str] | str) -> dict[str, Any]:
        self._require()
        tickers = [symbols] if isinstance(symbols, str) else list(symbols)
        results = []
        for symbol in tickers:
            price = self.quotes.get(str(symbol).upper())
            if price is None:
                continue
            spread = self.spreads.get(str(symbol).upper(), 0.01)
            results.append(
                {
                    "quote": {
                        "symbol": str(symbol).upper(),
                        "last_trade_price": str(price),
                        "previous_close": str(price),
                        "bid_price": str(price - spread / 2),
                        "ask_price": str(price + spread / 2),
                    }
                }
            )
        return {"data": {"results": results}}

    def get_accounts(self) -> dict[str, Any]:
        self._require()
        return {
            "data": {
                "accounts": [
                    {
                        "account_number": self.account_number,
                        "nickname": "Agentic",
                        "agentic_allowed": True,
                        "state": "active",
                    }
                ]
            }
        }

    def get_portfolio(self, account_number: str) -> dict[str, Any]:
        self._require()
        del account_number
        return {
            "data": {
                "total_value": str(self.nav),
                "cash": str(self.cash),
                "buying_power": {"buying_power": f"{self.buying_power:.4f}", "display_currency": "USD"},
            }
        }

    def get_equity_positions(self, account_number: str) -> dict[str, Any]:
        self._require()
        del account_number
        rows = []
        for symbol, pos in self.positions.items():
            rows.append({"symbol": symbol, "quantity": str(pos.get("quantity") or 0), "average_buy_price": str(pos.get("avg") or 0)})
        return {"data": {"positions": rows}}

    def get_equity_tradability(self, symbols: str | list[str]) -> dict[str, Any]:
        self._require()
        tickers = [symbols] if isinstance(symbols, str) else list(symbols)
        results = []
        for symbol in tickers:
            ok = self.tradable.get(str(symbol).upper(), True)
            results.append(
                {
                    "symbol": str(symbol).upper(),
                    "name": str(symbol).upper(),
                    "state": "active" if ok else "untradable",
                    "tradeable": bool(ok),
                }
            )
        return {"data": {"results": results}}

    def apply_fill(self, broker_order_id: str, *, ratio: float | None = None, status: str | None = None) -> dict[str, Any]:
        ratio = self.fill_ratio if ratio is None else ratio
        status = status or "filled"
        for order in self.orders:
            if order.get("id") == broker_order_id:
                qty = float(order.get("quantity") or 0)
                filled = qty * float(ratio)
                order["filled_quantity"] = str(filled)
                order["average_fill_price"] = str(self.quotes.get(str(order.get("symbol") or "").upper()) or 0)
                order["state"] = "partially_filled" if ratio < 1 and status != "filled" else status
                if status == "filled" or ratio >= 1:
                    order["state"] = "filled"
                    self._apply_position(order)
                return order
        return {}

    def reject_order(self, broker_order_id: str, reason: str = "broker_rejected") -> None:
        for order in self.orders:
            if order.get("id") == broker_order_id:
                order["state"] = "rejected"
                order["reject_reason"] = reason

    def cancel_order(self, broker_order_id: str) -> None:
        for order in self.orders:
            if order.get("id") == broker_order_id:
                order["state"] = "cancelled"

    def _record_order(self, payload: dict[str, Any], *, status: str) -> dict[str, Any]:
        symbol = str(payload.get("symbol") or "").upper()
        qty = payload.get("quantity")
        dollars = payload.get("dollar_amount")
        price = self.quotes.get(symbol) or 0.0
        quantity = float(qty) if qty not in (None, "") else (float(dollars) / price if dollars and price else 0.0)
        order = {
            "id": str(uuid4()),
            "ref_id": payload.get("ref_id"),
            "symbol": symbol,
            "side": payload.get("side"),
            "type": payload.get("type"),
            "quantity": str(quantity),
            "dollar_amount": dollars,
            "state": status,
            "filled_quantity": "0",
            "average_fill_price": None,
            "account_number": payload.get("account_number") or self.account_number,
        }
        self.orders.append(order)
        if status == "filled":
            order["filled_quantity"] = str(quantity)
            order["average_fill_price"] = str(price)
            self._apply_position(order)
        elif status == "partially_filled":
            filled = quantity * float(self.fill_ratio)
            order["filled_quantity"] = str(filled)
            order["average_fill_price"] = str(price)
        return order

    def _apply_position(self, order: dict[str, Any]) -> None:
        symbol = str(order.get("symbol") or "").upper()
        qty = float(order.get("filled_quantity") or order.get("quantity") or 0)
        price = float(order.get("average_fill_price") or self.quotes.get(symbol) or 0)
        side = str(order.get("side") or "buy").lower()
        notional = qty * price
        if side == "buy":
            current = self.positions.get(symbol) or {"quantity": 0.0, "avg": price}
            new_qty = float(current.get("quantity") or 0) + qty
            self.positions[symbol] = {"quantity": new_qty, "avg": price}
            self.cash = max(0.0, self.cash - notional)
            self.buying_power = max(0.0, self.buying_power - notional)
            self.nav = self.cash + sum(
                float(p.get("quantity") or 0) * float(self.quotes.get(sym) or price) for sym, p in self.positions.items()
            )
        else:
            current = self.positions.get(symbol) or {"quantity": 0.0, "avg": price}
            new_qty = max(0.0, float(current.get("quantity") or 0) - qty)
            if new_qty <= 1e-9:
                self.positions.pop(symbol, None)
            else:
                self.positions[symbol] = {"quantity": new_qty, "avg": current.get("avg") or price}
            self.cash += notional
            self.buying_power += notional
            self.nav = self.cash + sum(
                float(p.get("quantity") or 0) * float(self.quotes.get(sym) or price) for sym, p in self.positions.items()
            )
