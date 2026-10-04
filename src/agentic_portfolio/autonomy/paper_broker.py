"""PaperBroker — a persisted simulated brokerage account.

PAPER mode runs the *identical* pipeline as LIVE (refresh → discover → research →
decide → Risk Gate → intake → executor → reconcile → monitor). Only two things
differ: this class replaces the Robinhood account/order endpoints, and runtime
state lives under a separate state root (``var/paper`` by default).

Market data is real when a read-only quote source is supplied (the same
read-only Robinhood MCP used in LIVE); tests inject a deterministic source.
Fills are immediate during the call at the ask (buys) / bid (sells), falling
back to the last trade. No money moves anywhere. ``is_paper`` lets the executor
refuse a PaperBroker in LIVE mode and refuse a real broker in PAPER mode.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from agentic_portfolio.agent.persist import atomic_write_json, read_json
from agentic_portfolio.adapters.portfolio_facts import LiveDataUnavailable

QuoteSource = Callable[[list[str]], Mapping[str, Any]]


def _f(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return None if value != value else value


class PaperBroker:
    is_paper = True

    def __init__(
        self,
        root: Path,
        *,
        account_number: str,
        nickname: str = "Agentic",
        quote_source: QuoteSource | None = None,
        starting_cash: float = 10_000.0,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root)
        self.account_number = str(account_number)
        self.nickname = nickname
        self.quote_source = quote_source
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.path = self.root / "state" / "paper_broker" / "account.json"
        if not self.path.exists():
            atomic_write_json(
                self.path,
                {
                    "account_number": self.account_number,
                    "cash": float(starting_cash),
                    "positions": {},
                    "orders": [],
                    "created_at": self._stamp(),
                    "paper": True,
                },
            )

    # ------------------------------------------------------------ state
    def _stamp(self) -> str:
        stamp = self._now()
        return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).isoformat()

    def _load(self) -> dict[str, Any]:
        data = read_json(self.path, None)
        if not isinstance(data, dict):
            raise LiveDataUnavailable("paper broker state unreadable")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        atomic_write_json(self.path, data)

    def deposit(self, amount: float) -> None:
        """Test/ops helper for the simulated account only."""
        data = self._load()
        data["cash"] = float(data.get("cash") or 0) + float(amount)
        self._save(data)

    # ------------------------------------------------------------ quotes
    def _quote_rows(self, symbols: list[str]) -> list[dict[str, Any]]:
        if self.quote_source is None or not symbols:
            return []
        payload = self.quote_source(list(symbols)) or {}
        data = payload.get("data") if isinstance(payload, Mapping) and isinstance(payload.get("data"), Mapping) else payload
        rows = data.get("results") if isinstance(data, Mapping) else None
        out = []
        for row in rows or []:
            if isinstance(row, Mapping):
                out.append(dict(row.get("quote") if isinstance(row.get("quote"), Mapping) else row))
        return out

    def _quote(self, symbol: str) -> dict[str, float | None]:
        for row in self._quote_rows([symbol]):
            if str(row.get("symbol") or "").upper() == symbol.upper():
                return {
                    "last": _f(row.get("last_trade_price")),
                    "bid": _f(row.get("bid_price")),
                    "ask": _f(row.get("ask_price")),
                }
        return {"last": None, "bid": None, "ask": None}

    def get_equity_quotes(self, symbols: list[str] | str) -> dict[str, Any]:
        tickers = [symbols] if isinstance(symbols, str) else list(symbols)
        return {"data": {"results": [{"quote": row} for row in self._quote_rows(tickers)]}}

    # ------------------------------------------------------------ account reads
    def get_accounts(self) -> dict[str, Any]:
        return {
            "data": {
                "accounts": [
                    {
                        "account_number": self.account_number,
                        "nickname": self.nickname,
                        "agentic_allowed": True,
                        "state": "active",
                        "type": "cash",
                        "paper": True,
                    }
                ]
            }
        }

    def _marks(self, data: dict[str, Any]) -> dict[str, float]:
        symbols = list((data.get("positions") or {}).keys())
        marks: dict[str, float] = {}
        for row in self._quote_rows(symbols):
            price = _f(row.get("last_trade_price"))
            if price is not None:
                marks[str(row.get("symbol") or "").upper()] = price
        return marks

    def get_portfolio(self, account_number: str) -> dict[str, Any]:
        self._require_account(account_number)
        data = self._load()
        cash = float(data.get("cash") or 0)
        marks = self._marks(data)
        equity = 0.0
        for symbol, pos in (data.get("positions") or {}).items():
            price = marks.get(symbol, _f(pos.get("last_price")) or _f(pos.get("avg")) or 0.0)
            equity += float(pos.get("quantity") or 0) * price
        return {
            "data": {
                "total_value": f"{cash + equity:.4f}",
                "cash": f"{cash:.4f}",
                "equity_value": f"{equity:.4f}",
                "buying_power": {"buying_power": f"{cash:.4f}", "display_currency": "USD"},
                "paper": True,
            }
        }

    def get_equity_positions(self, account_number: str) -> dict[str, Any]:
        self._require_account(account_number)
        data = self._load()
        rows = [
            {"symbol": symbol, "quantity": str(pos.get("quantity") or 0), "average_buy_price": str(pos.get("avg") or 0)}
            for symbol, pos in sorted((data.get("positions") or {}).items())
            if float(pos.get("quantity") or 0) > 0
        ]
        return {"data": {"positions": rows}}

    def get_equity_orders(self, account_number: str, **kwargs: Any) -> dict[str, Any]:
        self._require_account(account_number)
        rows = list(self._load().get("orders") or [])
        if kwargs.get("order_id"):
            rows = [o for o in rows if o.get("id") == kwargs["order_id"]]
        if kwargs.get("symbol"):
            rows = [o for o in rows if str(o.get("symbol") or "").upper() == str(kwargs["symbol"]).upper()]
        return {"data": {"orders": rows}}

    def get_equity_tradability(self, symbols: str | list[str]) -> dict[str, Any]:
        tickers = [symbols] if isinstance(symbols, str) else list(symbols)
        quoted = {str(r.get("symbol") or "").upper() for r in self._quote_rows(tickers)}
        return {
            "data": {
                "results": [
                    {"symbol": s.upper(), "tradeable": s.upper() in quoted, "state": "active" if s.upper() in quoted else "unknown"}
                    for s in tickers
                ]
            }
        }

    def _require_account(self, account_number: str | None) -> None:
        if account_number is not None and str(account_number) != self.account_number:
            raise LiveDataUnavailable("paper broker refused a different account number")

    # ------------------------------------------------------------ orders
    def _fill_price(self, symbol: str, side: str) -> float | None:
        q = self._quote(symbol)
        if side == "buy":
            return q["ask"] or q["last"]
        return q["bid"] or q["last"]

    def review_equity_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        errors = self._order_errors(payload)
        symbol = str(payload.get("symbol") or "").upper()
        return {
            "data": {
                "ok": not errors,
                "errors": errors,
                "warnings": ["PAPER_ACCOUNT"],
                "quote": {"symbol": symbol, "last_trade_price": self._quote(symbol)["last"]},
            }
        }

    def _order_errors(self, payload: Mapping[str, Any]) -> list[str]:
        errors: list[str] = []
        self._require_account(payload.get("account_number"))
        symbol = str(payload.get("symbol") or "").upper()
        side = str(payload.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            errors.append("UNSUPPORTED_SIDE")
            return errors
        price = self._fill_price(symbol, side)
        if price is None or price <= 0:
            errors.append("NO_QUOTE")
            return errors
        data = self._load()
        qty = _f(payload.get("quantity"))
        dollars = _f(payload.get("dollar_amount"))
        if qty is None and dollars is not None:
            qty = dollars / price
        if qty is None or qty <= 0:
            errors.append("UNSIZED")
            return errors
        if side == "buy" and qty * price > float(data.get("cash") or 0) + 1e-6:
            errors.append("INSUFFICIENT_CASH")
        if side == "sell":
            held = float(((data.get("positions") or {}).get(symbol) or {}).get("quantity") or 0)
            if qty > held + 1e-9:
                errors.append("OVERSELL")
        return errors

    def place_equity_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        errors = self._order_errors(payload)
        symbol = str(payload.get("symbol") or "").upper()
        side = str(payload.get("side") or "").lower()
        stamp = self._stamp()
        data = self._load()
        if errors:
            order = {"id": str(uuid4()), "ref_id": payload.get("ref_id"), "symbol": symbol, "side": side, "state": "rejected", "reject_reason": ",".join(errors), "created_at": stamp, "updated_at": stamp}
            data.setdefault("orders", []).append(order)
            self._save(data)
            return {"data": {"ok": False, "state": "rejected", "reject_reason": order["reject_reason"], "orders": [order]}}
        price = float(self._fill_price(symbol, side) or 0)
        qty = _f(payload.get("quantity"))
        if qty is None:
            qty = float(payload.get("dollar_amount")) / price
        qty = int(qty * 1_000_000) / 1_000_000  # 6-decimal fractional share precision, like the broker
        notional = qty * price
        positions = data.setdefault("positions", {})
        pos = positions.get(symbol) or {"quantity": 0.0, "avg": price}
        if side == "buy":
            new_qty = float(pos.get("quantity") or 0) + qty
            avg = ((float(pos.get("quantity") or 0) * float(pos.get("avg") or price)) + notional) / new_qty
            positions[symbol] = {"quantity": new_qty, "avg": avg, "last_price": price}
            data["cash"] = float(data.get("cash") or 0) - notional
        else:
            new_qty = max(0.0, float(pos.get("quantity") or 0) - qty)
            if new_qty < 1e-6:  # sub-micro-share dust from 6-decimal rounding closes the position
                positions.pop(symbol, None)
            else:
                positions[symbol] = {**pos, "quantity": new_qty, "last_price": price}
            data["cash"] = float(data.get("cash") or 0) + notional
        order = {
            "id": str(uuid4()),
            "ref_id": payload.get("ref_id"),
            "symbol": symbol,
            "side": side,
            "type": payload.get("type") or "market",
            "quantity": f"{qty:.8f}",
            "dollar_amount": payload.get("dollar_amount"),
            "state": "filled",
            "filled_quantity": f"{qty:.8f}",
            "average_fill_price": f"{price:.6f}",
            "account_number": self.account_number,
            "created_at": stamp,
            "updated_at": stamp,
            "paper": True,
        }
        data.setdefault("orders", []).append(order)
        self._save(data)
        return {"data": {"ok": True, "orders": [order], "order_id": order["id"], "state": "filled"}}
