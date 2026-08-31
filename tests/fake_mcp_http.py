"""Fake Robinhood MCP HTTP boundary. Records JSON-RPC bodies; never places."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from agentic_portfolio.policy import load_account_rules

ACCOUNT = str(load_account_rules()["account"]["account_number"])

WRITE_TOOLS = (
    "place_equity_order",
    "review_equity_order",
    "cancel_equity_order",
    "place_option_order",
    "review_option_order",
    "cancel_option_order",
    "place_crypto_order",
    "preview_crypto_order",
    "cancel_crypto_order",
    "initiate_withdrawals",
    "initiate_deposits",
)


def accounts_payload(*, number: str = ACCOUNT, nickname: str = "Agentic", allowed: bool = True) -> dict[str, Any]:
    return {
        "data": {
            "accounts": [
                {
                    "account_number": "5UW82786",
                    "nickname": "Default",
                    "agentic_allowed": False,
                    "state": "active",
                    "brokerage_account_type": "individual",
                    "type": "margin",
                    "is_default": True,
                },
                {
                    "account_number": number,
                    "rhs_account_number": number,
                    "nickname": nickname,
                    "agentic_allowed": allowed,
                    "state": "active",
                    "brokerage_account_type": "individual",
                    "type": "limited_margin",
                    "is_default": False,
                },
            ]
        }
    }


def portfolio_payload(*, nav: float = 1513.67, cash: float = 1000.0, bp: float = 1000.0) -> dict[str, Any]:
    return {
        "data": {
            "total_value": str(nav),
            "equity_value": str(max(0.0, nav - cash)),
            "cash": str(cash),
            "buying_power": {"buying_power": f"{bp:.4f}", "display_currency": "USD"},
        }
    }


def positions_payload(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"data": {"positions": list(rows or [])}}


def quotes_payload(*symbols_prices: tuple[str, float]) -> dict[str, Any]:
    results = []
    for symbol, price in symbols_prices:
        results.append(
            {
                "quote": {
                    "symbol": symbol,
                    "last_trade_price": str(price),
                    "previous_close": str(price),
                    "bid_price": str(price - 0.01),
                    "ask_price": str(price + 0.01),
                }
            }
        )
    return {"data": {"results": results}}


def orders_payload(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"data": {"orders": list(rows or [])}}


def tradability_payload(symbol: str = "MSFT") -> dict[str, Any]:
    return {
        "data": {
            "results": [
                {
                    "symbol": symbol,
                    "name": "Microsoft Corporation",
                    "simple_name": "Microsoft",
                    "state": "active",
                    "tradeable": True,
                }
            ]
        }
    }


def fundamentals_payload(symbol: str = "MSFT") -> dict[str, Any]:
    return {
        "data": {
            "results": [
                {
                    "symbol": symbol,
                    "description": "Microsoft common stock",
                    "sector": "Technology",
                    "industry": "Software",
                    "average_volume": "20000000",
                }
            ]
        }
    }


def search_payload(symbol: str = "MSFT") -> dict[str, Any]:
    return {
        "data": {
            "results": [
                {
                    "symbol": symbol,
                    "name": "Microsoft Corporation",
                    "simple_name": "Microsoft",
                    "instrument_id": "msft-id",
                    "asset_type": "instrument",
                }
            ]
        }
    }


def wrap_tool_result(payload: Mapping[str, Any], *, mode: str = "structured") -> bytes:
    if mode == "structured":
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "structuredContent": dict(payload),
                "content": [{"type": "text", "text": json.dumps(dict(payload))}],
            },
        }
        return json.dumps(body).encode("utf-8")
    if mode == "text":
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps(dict(payload))}]},
        }
        return json.dumps(body).encode("utf-8")
    if mode == "sse":
        inner = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"structuredContent": dict(payload)},
        }
        return f"event: message\ndata: {json.dumps(inner)}\n\n".encode("utf-8")
    raise ValueError(mode)


class FakeMcpHttp:
    """In-process MCP HTTP server. The only mocked boundary for production-stack tests."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.session_id = "mcp-session-test-1"
        self.initialize_ok = True
        self.tools_call_ok = True
        self.status_override: dict[str, int] = {}
        self.fail_tools: set[str] = set()
        self.timeout_tools: set[str] = set()
        self.result_mode = "structured"
        self.token_accepted = "access-live"
        self.account_number = ACCOUNT
        self.nav = 1513.67
        self.cash = 1000.0
        self.bp = 1000.0
        self.positions: list[dict[str, Any]] = [
            {"symbol": "MSFT", "quantity": "1", "average_buy_price": "500"}
        ]
        self.quotes: dict[str, float] = {"MSFT": 513.67, "SPY": 769.39}
        self.orders: list[dict[str, Any]] = []
        self.malformed_portfolio = False
        self.missing_cash = False
        self.missing_buying_power = False
        self.wrong_account = False
        self.advertise_write_tools = True
        self._initialize_count = 0

    def set_zero_positions(self) -> None:
        self.positions = []
        self.quotes = {"SPY": 769.39}

    def set_fractional(self) -> None:
        self.positions = [{"symbol": "MSFT", "quantity": "0.15", "average_buy_price": "500"}]
        self.quotes = {"MSFT": 513.67, "SPY": 769.39}

    def drop_quote(self, symbol: str) -> None:
        self.quotes.pop(str(symbol).upper(), None)

    def post(self, body: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        self.calls.append(dict(body))
        method = str(body.get("method") or "")
        if method in self.timeout_tools or (method == "tools/call" and self._tool_name(body) in self.timeout_tools):
            raise TimeoutError("MCP HTTP timeout")
        if method == "initialize":
            self._initialize_count += 1
            status = self.status_override.get("initialize", 401 if not self.initialize_ok else 200)
            if status >= 400:
                return status, {"Content-Type": "application/json", "Mcp-Session-Id": self.session_id}, b"{}"
            payload = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "robinhood-trading", "version": "fake"},
                },
            }
            tools = ["get_accounts", "get_portfolio", "get_equity_positions", "get_equity_quotes"]
            if self.advertise_write_tools:
                tools.extend(WRITE_TOOLS)
            payload["result"]["capabilities"]["tools"]["advertised"] = tools
            return (
                200,
                {"Content-Type": "application/json", "Mcp-Session-Id": self.session_id},
                json.dumps(payload).encode("utf-8"),
            )
        if method == "notifications/initialized":
            return 200, {"Content-Type": "application/json", "Mcp-Session-Id": self.session_id}, b""
        if method == "tools/call":
            if not self.tools_call_ok:
                return 500, {"Content-Type": "application/json"}, json.dumps({"error": {"message": "tools/call failed"}}).encode("utf-8")
            tool = self._tool_name(body)
            status = self.status_override.get(tool) or self.status_override.get("tools/call")
            if status:
                return status, {"Content-Type": "application/json", "Mcp-Session-Id": self.session_id}, b"{}"
            if tool in self.fail_tools:
                err = {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32000, "message": f"{tool} failed"}}
                return 200, {"Content-Type": "application/json"}, json.dumps(err).encode("utf-8")
            payload = self._tool_payload(tool, dict((body.get("params") or {}).get("arguments") or {}))
            raw = wrap_tool_result(payload, mode=self.result_mode)
            headers = {"Mcp-Session-Id": self.session_id}
            if self.result_mode == "sse":
                headers["Content-Type"] = "text/event-stream"
            else:
                headers["Content-Type"] = "application/json"
            return 200, headers, raw
        return 400, {"Content-Type": "application/json"}, b"{}"

    def _tool_name(self, body: Mapping[str, Any]) -> str:
        params = dict(body.get("params") or {})
        return str(params.get("name") or "")

    def _tool_payload(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if tool == "get_accounts":
            number = "000000000" if self.wrong_account else self.account_number
            return accounts_payload(number=number)
        if tool == "get_portfolio":
            if self.malformed_portfolio:
                return {"data": {"total_value": "not-a-number", "cash": "x", "buying_power": {}}}
            data = portfolio_payload(nav=self.nav, cash=self.cash, bp=self.bp)
            if self.missing_cash:
                data["data"].pop("cash", None)
            if self.missing_buying_power:
                data["data"].pop("buying_power", None)
            return data
        if tool == "get_equity_positions":
            return positions_payload(self.positions)
        if tool == "get_equity_quotes":
            symbols = [str(s).upper() for s in (arguments.get("symbols") or [])]
            pairs = [(sym, self.quotes[sym]) for sym in symbols if sym in self.quotes]
            return quotes_payload(*pairs) if pairs else quotes_payload()
        if tool == "get_equity_orders":
            return orders_payload(self.orders)
        if tool == "get_equity_tradability":
            symbols = [str(s).upper() for s in (arguments.get("symbols") or ["MSFT"])]
            return tradability_payload(symbols[0] if symbols else "MSFT")
        if tool == "get_equity_fundamentals":
            symbols = [str(s).upper() for s in (arguments.get("symbols") or ["MSFT"])]
            return fundamentals_payload(symbols[0] if symbols else "MSFT")
        if tool == "search":
            return search_payload(str(arguments.get("query") or "MSFT").upper())
        return {"data": {}}

    def tool_calls(self) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        for body in self.calls:
            if body.get("method") != "tools/call":
                continue
            params = dict(body.get("params") or {})
            out.append((str(params.get("name") or ""), dict(params.get("arguments") or {})))
        return out

    def as_post(self) -> Callable[[dict[str, Any]], tuple[int, dict[str, str], bytes]]:
        return self.post
