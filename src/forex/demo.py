"""Floor 3: the OANDA Demo Desk. Real orders on an OANDA *practice* account (fake money).

This is the only module that sends orders, and it can only reach OANDA's practice server:
  * PracticeTrader refuses to construct unless the credentials say OANDA_ENV=practice, and
    re-checks the host before every request; the live host is never reachable from here;
  * credentials live in their own file, ~/.agentic-portfolio/forex/oanda-practice.env
    (0600), separate from the live read-only token; it also names the seat sub-accounts:
        OANDA_ENV=practice
        OANDA_API_TOKEN=<practice token>
        DEMO_ACCOUNT_1=101-001-xxxxxxx-001
        DEMO_ACCOUNT_2=101-001-xxxxxxx-002
  * each Demo Desk agent trades its own practice sub-account (US FIFO and no-hedging rules
    apply per account, so two agents never share one).

How a demo agent trades (the broker is the source of truth):
  1. mirror the seat's account (balance, open trades with their stop/target) as a Book;
  2. run the agent's strategy and time rules on that mirror through the same Executor
     (same risk envelope, sizing, gates) used on Floor 2;
  3. send the difference to OANDA: new positions become market orders with stop-loss and
     take-profit attached, removed positions are closed, moved stops are replaced;
  4. closed trades (stops/targets filled by OANDA, or our closes) are recorded from OANDA's
     own trade records.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from forex.oanda import HOSTS, Credentials, OandaClient, OandaConfigError, OandaError, _read_env_file
from forex.sim import Book, Executor, Position, Quote, Trade

PRACTICE_REST = HOSTS["practice"][0]
DEFAULT_PRACTICE_FILE = Path.home() / ".agentic-portfolio" / "forex" / "oanda-practice.env"


class LiveHostRefused(RuntimeError):
    """Anything that would reach the live trading host from the Demo Desk."""


def load_practice(environ: Mapping[str, str] | None = None) -> tuple[Credentials, list[str]] | None:
    """(credentials, seat account IDs), or None when the Demo Desk is not configured."""
    environ = os.environ if environ is None else environ
    path = Path(environ.get("OANDA_PRACTICE_ENV_FILE") or DEFAULT_PRACTICE_FILE)
    if not path.is_file():
        return None
    v = _read_env_file(path)  # also refuses a file other users can read
    if (v.get("OANDA_ENV") or "").strip().lower() != "practice":
        raise OandaConfigError(f"{path} must say OANDA_ENV=practice")
    token = (v.get("OANDA_API_TOKEN") or "").strip()
    if not token:
        raise OandaConfigError(f"{path} has no OANDA_API_TOKEN")
    seats = [v[k].strip() for k in ("DEMO_ACCOUNT_1", "DEMO_ACCOUNT_2") if (v.get(k) or "").strip()]
    return Credentials("practice", token, seats[0] if seats else None), seats


class PracticeTrader(OandaClient):
    """The read-only client plus order methods, pinned to the practice host."""

    def __init__(self, creds: Credentials, **kw: Any) -> None:
        if creds.env != "practice":
            raise LiveHostRefused("the Demo Desk only trades OANDA practice accounts")
        send = kw.pop("send", None)
        super().__init__(creds, **kw)
        self._send = send or _urllib_send
        if self.rest_host != PRACTICE_REST:
            raise LiveHostRefused(self.rest_host)

    def _write(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if self.rest_host != PRACTICE_REST or self.env != "practice":
            raise LiveHostRefused(self.rest_host)
        url = f"{PRACTICE_REST}{path}"
        self._limiter.wait()
        status, raw = self._send(method, url, {**self._headers, "Content-Type": "application/json"}, json.dumps(body or {}).encode(), self.timeout)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        if status >= 400:
            raise OandaError(f"{method} {path}: HTTP {status}: {payload.get('errorMessage') or payload}", status=status, code=payload.get("errorCode") or f"HTTP_{status}")
        return payload

    def market_order(self, account_id: str, instrument: str, units: int, *, stop: float, target: float | None, client_id: str, tag: str, digits: int) -> dict[str, Any]:
        order: dict[str, Any] = {
            "type": "MARKET", "instrument": instrument, "units": str(int(units)), "timeInForce": "FOK", "positionFill": "OPEN_ONLY",
            "stopLossOnFill": {"price": f"{stop:.{digits}f}", "timeInForce": "GTC"},
            "clientExtensions": {"id": client_id[:120], "tag": tag[:120]},
            "tradeClientExtensions": {"id": client_id[:120], "tag": tag[:120]},
        }
        if target is not None:
            order["takeProfitOnFill"] = {"price": f"{target:.{digits}f}", "timeInForce": "GTC"}
        return self._write("POST", f"/v3/accounts/{account_id}/orders", {"order": order})

    def set_stop(self, account_id: str, trade_id: str, price: float, digits: int) -> dict[str, Any]:
        return self._write("PUT", f"/v3/accounts/{account_id}/trades/{trade_id}/orders", {"stopLoss": {"price": f"{price:.{digits}f}", "timeInForce": "GTC"}})

    def close_trade(self, account_id: str, trade_id: str) -> dict[str, Any]:
        return self._write("PUT", f"/v3/accounts/{account_id}/trades/{trade_id}/close", {"units": "ALL"})

    def trade(self, account_id: str, trade_id: str) -> dict[str, Any]:
        return dict(self.get(f"/v3/accounts/{account_id}/trades/{trade_id}").get("trade") or {})


def _urllib_send(method: str, url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    if not url.startswith(PRACTICE_REST + "/"):
        raise LiveHostRefused(url)
    req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (practice host only)
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


def _digits(instrument: str) -> int:
    return 3 if instrument.endswith("JPY") else 5


def mirror(trader: PracticeTrader, account_id: str, agent_id: str, version: int) -> Book:
    """The seat's account as a Book: balance as cash, each open trade as a position."""
    summary = trader.summary(account_id)
    book = Book(agent_id, float(summary.get("balance") or 0), start_capital=float(summary.get("balance") or 0))
    for t in trader.open_trades(account_id):
        units = int(float(t.get("currentUnits") or t.get("initialUnits") or 0))
        if units == 0:
            continue
        entry = float(t["price"])
        stop = float(((t.get("stopLossOrder") or {}).get("price")) or 0) or None
        target = float(((t.get("takeProfitOrder") or {}).get("price")) or 0) or None
        side = 1 if units > 0 else -1
        risk = abs(entry - stop) if stop else 0.0
        book.positions[str(t["id"])] = Position(
            str(t["id"]), t["instrument"], side, abs(units), entry, stop or entry, target, int(_ts(t.get("openTime"))), risk, 0.0,
            ((t.get("clientExtensions") or {}).get("tag") or "")[:80], version,
        )
    return book


def _ts(value: str | None) -> float:
    from forex.oanda import parse_time

    dt = parse_time(value)
    return dt.timestamp() if dt else 0.0


def execute(trader: PracticeTrader, account_id: str, before: Book, after: Book, *, agent_id: str, version: int, now: int) -> list[dict[str, Any]]:
    """Send the difference between the mirrored book and the decided book to OANDA practice."""
    events: list[dict[str, Any]] = []
    for pid, p in before.positions.items():
        if pid not in after.positions:
            trader.close_trade(account_id, pid)
            events.append({"type": "DEMO_CLOSE", "summary": f"Closed demo trade {p.instrument} (#{pid})", "payload": {"trade_id": pid}})
        elif abs(after.positions[pid].stop - p.stop) > 1e-9:
            trader.set_stop(account_id, pid, after.positions[pid].stop, _digits(p.instrument))
            events.append({"type": "DEMO_STOP", "summary": f"Moved demo stop on {p.instrument} to {after.positions[pid].stop:.5g}", "payload": {"trade_id": pid}})
    for pid, p in after.positions.items():
        if pid in before.positions:
            continue
        client_id = f"firm:demo:{agent_id}:v{version}:{now}:{p.instrument}"
        res = trader.market_order(account_id, p.instrument, p.units * p.side, stop=p.stop, target=p.target, client_id=client_id, tag=f"{agent_id} v{version}", digits=_digits(p.instrument))
        fill = res.get("orderFillTransaction")
        if fill:
            events.append({"type": "DEMO_FILL", "summary": f"Demo {'BUY' if p.side > 0 else 'SELL'} {p.units:,} {p.instrument} filled @ {fill.get('price')}", "payload": {"fill": fill.get("id"), "client_id": client_id}})
        else:
            reason = (res.get("orderCancelTransaction") or {}).get("reason") or "not filled"
            events.append({"type": "DEMO_REJECTED", "summary": f"Demo order {p.instrument} not filled: {reason}", "payload": {"client_id": client_id, "reason": reason}})
    return events


def closed_since(trader: PracticeTrader, account_id: str, before_ids: set[str], now_ids: set[str], version: int) -> list[Trade]:
    """Trades that disappeared since the last sync, as recorded by OANDA (fill price, P&L, financing)."""
    out = []
    for tid in sorted(before_ids - now_ids):
        t = trader.trade(account_id, tid)
        if t.get("state") != "CLOSED":
            continue
        units = int(float(t.get("initialUnits") or 0))
        pnl = float(t.get("realizedPL") or 0)
        fin = float(t.get("financing") or 0)
        entry, exit_ = float(t.get("price") or 0), float(t.get("averageClosePrice") or 0)
        stop = float(((t.get("stopLossOrder") or {}).get("price")) or 0)
        risk = abs(entry - stop) * abs(units) if stop else 0.0
        out.append(Trade(tid, version, t.get("instrument", ""), 1 if units > 0 else -1, abs(units), entry, exit_, int(_ts(t.get("openTime"))), int(_ts(t.get("closeTime"))),
                         pnl, fin, (pnl + fin) / risk if risk else 0.0, "closed at OANDA", ((t.get("clientExtensions") or {}).get("tag") or "")[:80]))
    return out


def decide_on_mirror(executor: Executor, book: Book, actions, quotes: Mapping[str, Quote], now: int, conv) -> tuple[Book, list[dict[str, Any]]]:
    """Apply actions and time rules to a copy of the mirrored book; returns (decided book, events)."""
    import copy

    after = copy.deepcopy(book)
    after.closed = []
    events = executor.apply(after, actions, quotes, now, conv) + executor.time_rules(after, now, quotes, conv)
    return after, events
