"""Typed Robinhood client over the Trading MCP.

Two instances exist at runtime: a read-only one shared by everything, and a
writer owned by the Execution Desk. The writer is the only object that can
send ``review_equity_order`` / ``place_equity_order``; every other tool
(cancel, options, crypto, transfers, watchlists...) is refused outright.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Mapping

from firm.broker.errors import (
    AmbiguousSubmission,
    BrokerAccountMismatch,
    BrokerUnavailable,
    BrokerWriteRefused,
)
from firm.broker.types import Bar, BrokerOrder, Execution, PortfolioFacts, Quote, Tradability
from firm.clock import et_date, parse
from firm.money import D, opt_D

READ_TOOLS = frozenset(
    {
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_equity_quotes",
        "get_equity_orders",
        "get_equity_tradability",
        "get_equity_historicals",
        "get_trade_approval_setting",
    }
)
WRITE_TOOLS = frozenset({"review_equity_order", "place_equity_order"})
MAX_PAGES = 20
BATCH = 10

Transport = Callable[..., Any]


def _data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    inner = payload.get("data", payload)
    return dict(inner) if isinstance(inner, Mapping) else {}


def _ts(*values: Any) -> datetime | None:
    stamps = []
    for v in values:
        if v:
            try:
                stamps.append(parse(str(v)))
            except ValueError:
                continue
    return max(stamps) if stamps else None


def parse_quote(item: Mapping[str, Any]) -> Quote | None:
    quote = item.get("quote") if isinstance(item.get("quote"), Mapping) else item
    if not isinstance(quote, Mapping) or not quote.get("symbol"):
        return None
    close = item.get("close") if isinstance(item.get("close"), Mapping) else {}
    bid, ask = opt_D(quote.get("bid_price")), opt_D(quote.get("ask_price"))
    return Quote(
        symbol=str(quote["symbol"]).upper(),
        bid=bid if bid and bid > 0 else None,
        ask=ask if ask and ask > 0 else None,
        last=opt_D(quote.get("last_trade_price")),
        observed_at=_ts(quote.get("venue_bid_time"), quote.get("venue_ask_time"), quote.get("venue_last_trade_time")),
        close=opt_D(close.get("price")),
        close_date=close.get("date"),
        tradable_state=str(quote.get("state") or "active"),
    )


def parse_order(raw: Mapping[str, Any]) -> BrokerOrder:
    executions = tuple(
        Execution(
            id=str(e["id"]),
            price=D(e.get("price")),
            quantity=D(e.get("quantity")),
            fees=D(e.get("fees") or 0),
            timestamp=str(e.get("timestamp") or ""),
        )
        for e in (raw.get("executions") or [])
        if isinstance(e, Mapping) and e.get("id")
    )
    dollars = raw.get("dollar_based_amount")
    return BrokerOrder(
        id=str(raw.get("id") or ""),
        ref_id=str(raw["ref_id"]) if raw.get("ref_id") else None,
        symbol=str(raw.get("symbol") or "").upper(),
        side=str(raw.get("side") or "").lower(),
        state=str(raw.get("state") or "").lower(),
        quantity=opt_D(raw.get("quantity")),
        cumulative_quantity=D(raw.get("cumulative_quantity") or 0),
        average_price=opt_D(raw.get("average_price")),
        fees=D(raw.get("fees") or 0),
        dollar_amount=opt_D(dollars.get("amount")) if isinstance(dollars, Mapping) else None,
        placed_agent=raw.get("placed_agent"),
        created_at=str(raw.get("created_at") or ""),
        reject_reason=raw.get("reject_reason"),
        executions=executions,
    )


def bar_date(begins_at: str) -> str:
    """Daily bars are labeled at UTC midnight of the session date; intraday labels convert to ET."""
    stamp = parse(begins_at)
    if (stamp.hour, stamp.minute, stamp.second) == (0, 0, 0):
        return stamp.date().isoformat()
    return et_date(stamp).isoformat()


class RobinhoodBroker:
    def __init__(self, transport: Transport, *, account_number: str, writer: bool = False) -> None:
        self._transport = transport
        self.account_number = str(account_number)
        self.writer = writer
        self.calls: list[str] = []

    # -- transport ---------------------------------------------------------
    def _call(self, tool: str, **kwargs: Any) -> dict[str, Any]:
        if tool in WRITE_TOOLS:
            if not self.writer:
                raise BrokerWriteRefused(f"{tool} refused on a read-only broker client")
        elif tool not in READ_TOOLS:
            raise BrokerWriteRefused(f"tool not allowlisted: {tool}")
        self.calls.append(tool)
        try:
            return _data(self._transport(tool, **kwargs))
        except TimeoutError as exc:
            raise BrokerUnavailable(f"{tool} timed out", code="TIMEOUT") from exc

    def _pages(self, tool: str, key: str, **kwargs: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = None
        for _ in range(MAX_PAGES):
            data = self._call(tool, cursor=cursor, **kwargs)
            rows.extend(r for r in (data.get(key) or []) if isinstance(r, Mapping))
            cursor = data.get("next") or None
            if not cursor:
                return rows
        raise BrokerUnavailable(f"{tool}: more than {MAX_PAGES} pages")

    # -- reads -------------------------------------------------------------
    def accounts(self) -> list[dict[str, Any]]:
        rows = self._call("get_accounts").get("accounts")
        if not isinstance(rows, list):
            raise BrokerUnavailable("get_accounts returned no account list")
        return [dict(r) for r in rows if isinstance(r, Mapping)]

    def confirm_account(self) -> dict[str, Any]:
        """Fail closed unless the configured account is present, agentic-allowed, and active."""
        match = next((a for a in self.accounts() if str(a.get("account_number")) == self.account_number), None)
        if match is None:
            raise BrokerAccountMismatch("configured Agentic account not found")
        if match.get("agentic_allowed") is not True:
            raise BrokerAccountMismatch("configured account is not agentic_allowed")
        if match.get("deactivated") or match.get("permanently_deactivated"):
            raise BrokerAccountMismatch("configured account is deactivated")
        if str(match.get("state") or "active").lower() != "active":
            raise BrokerAccountMismatch(f"configured account state is {match.get('state')}")
        return match

    def portfolio(self) -> PortfolioFacts:
        data = self._call("get_portfolio", account_number=self.account_number)
        if not data or data.get("total_value") in (None, "") or data.get("cash") in (None, ""):
            raise BrokerUnavailable("get_portfolio returned no values")
        bp = data.get("buying_power") if isinstance(data.get("buying_power"), Mapping) else {}
        return PortfolioFacts(
            nav=D(data["total_value"]),
            cash=D(data["cash"]),
            buying_power=opt_D(bp.get("buying_power")),
            unleveraged_buying_power=opt_D(bp.get("unleveraged_buying_power")),
            pending_deposits=D(data.get("pending_deposits") or 0),
            equity_value=D(data.get("equity_value") or 0),
        )

    def positions(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for row in self._pages("get_equity_positions", "positions", account_number=self.account_number):
            symbol = str(row.get("symbol") or "").upper()
            qty = D(row.get("quantity") or 0)
            if not symbol:
                raise BrokerUnavailable("position without symbol")
            if qty != 0:
                out[symbol] = out.get(symbol, Decimal(0)) + qty
        return out

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        wanted = sorted({s.upper() for s in symbols})
        for i in range(0, len(wanted), BATCH):
            data = self._call("get_equity_quotes", symbols=wanted[i : i + BATCH])
            for item in data.get("results") or []:
                if isinstance(item, Mapping):
                    q = parse_quote(item)
                    if q:
                        out[q.symbol] = q
        return out

    def orders(self, *, order_id: str | None = None, symbol: str | None = None, since: str | None = None) -> list[BrokerOrder]:
        rows = self._pages(
            "get_equity_orders",
            "orders",
            account_number=self.account_number,
            order_id=order_id,
            symbol=symbol,
            created_at_gte=since,
        )
        return [parse_order(r) for r in rows]

    def tradability(self, symbols: list[str]) -> dict[str, Tradability]:
        out: dict[str, Tradability] = {}
        wanted = sorted({s.upper() for s in symbols})
        for i in range(0, len(wanted), BATCH):
            data = self._call("get_equity_tradability", account_number=self.account_number, symbols=wanted[i : i + BATCH])
            for row in data.get("results") or []:
                if not isinstance(row, Mapping) or not row.get("symbol"):
                    continue
                per_type = {
                    str(t.get("account_type")): str(t.get("account_type_tradability"))
                    for t in (row.get("account_type_tradabilities") or [])
                    if isinstance(t, Mapping)
                }
                account_ok = per_type.get("individual", "tradable") == "tradable"
                halted = "regular_hours" in (row.get("internal_halt_sessions") or [])
                out[str(row["symbol"]).upper()] = Tradability(
                    symbol=str(row["symbol"]).upper(),
                    tradable=bool(row.get("tradeable")) and str(row.get("state") or "active") == "active" and account_ok,
                    fractional=str(row.get("fractional_tradability") or "") == "tradable",
                    halted=halted,
                )
        return out

    def trade_approval_required(self) -> bool:
        data = self._call("get_trade_approval_setting", account_number=self.account_number)
        setting = data.get("setting") if isinstance(data.get("setting"), Mapping) else {}
        if "human_must_approve_trades" not in setting:
            raise BrokerUnavailable("trade approval setting unavailable")
        return bool(setting["human_must_approve_trades"])

    def daily_bars(self, symbols: list[str], *, start: str) -> dict[str, list[Bar]]:
        out: dict[str, list[Bar]] = {}
        wanted = sorted({s.upper() for s in symbols})
        for i in range(0, len(wanted), BATCH):
            data = self._call(
                "get_equity_historicals",
                symbols=wanted[i : i + BATCH],
                start_time=start,
                interval="day",
                bounds="regular",
                adjustment_type="split",
            )
            for row in data.get("results") or []:
                if not isinstance(row, Mapping) or not row.get("symbol"):
                    continue
                bars = [
                    Bar(
                        date=bar_date(b["begins_at"]),
                        open=D(b.get("open_price")),
                        high=D(b.get("high_price")),
                        low=D(b.get("low_price")),
                        close=D(b.get("close_price")),
                        volume=int(b.get("volume") or 0),
                    )
                    for b in (row.get("bars") or [])
                    if isinstance(b, Mapping) and b.get("begins_at") and b.get("close_price") and not b.get("interpolated")
                ]
                out[str(row["symbol"]).upper()] = bars
        return out

    # -- writes (Execution Desk only) ------------------------------------------
    def review(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("review_equity_order", **payload)

    def place(self, payload: dict[str, Any]) -> BrokerOrder | None:
        """Send an order. Raises AmbiguousSubmission when the outcome is unknown.

        A 4xx response or a tool error means the server answered and nothing was
        placed (BrokerUnavailable). A timeout, 5xx, or transport failure after
        sending is ambiguous: the order may exist under its ref_id.
        """
        try:
            data = self._call("place_equity_order", **payload)
        except BrokerUnavailable as exc:
            if exc.code == "TOOL_ERROR" or exc.code.startswith("HTTP_4"):
                raise
            raise AmbiguousSubmission(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise AmbiguousSubmission(f"{type(exc).__name__}: {exc}") from exc
        order = data.get("order")
        if not isinstance(order, Mapping) or not order.get("id"):
            raise AmbiguousSubmission("place_equity_order returned no order id")
        return parse_order(order)
