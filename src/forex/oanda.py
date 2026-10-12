"""Minimal OANDA v20 client: live and practice endpoints, read-only.

The transport only issues GET requests; there is no code path that creates,
modifies, or cancels anything at the broker. OANDA personal access tokens cannot
be scoped to read-only, so this module is the read-only boundary. Order support
will be added separately, behind its own explicit authorization.

Credentials come from the environment, or from a KEY=VALUE file named by
OANDA_ENV_FILE (default ~/.agentic-portfolio/forex/oanda.env, written by
scripts/oanda_credentials.py); the environment wins. It is a separate file from
the equity engine's /etc/agentic-portfolio/env. On POSIX the file must be readable
by its owner only (0600) and owned by the user reading it, or loading refuses:

    OANDA_ENV         live | practice          (required, no default)
    OANDA_API_TOKEN   personal access token     (required; never logged)
    OANDA_ACCOUNT_ID  e.g. 001-001-1234567-001  (optional; checked against the token's accounts)
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

HOSTS = {
    "live": ("https://api-fxtrade.oanda.com", "https://stream-fxtrade.oanda.com"),
    "practice": ("https://api-fxpractice.oanda.com", "https://stream-fxpractice.oanda.com"),
}
MAX_CANDLES = 5000
DEFAULT_ENV_FILE = Path.home() / ".agentic-portfolio" / "forex" / "oanda.env"

# fetch(url, headers, timeout) -> (status, headers, body). GET only: there is no method argument.
Fetch = Callable[[str, Mapping[str, str], float], tuple[int, Mapping[str, str], bytes]]
# open_stream(url, headers, timeout) -> iterator of raw lines
OpenStream = Callable[[str, Mapping[str, str], float], Iterator[bytes]]


class OandaError(RuntimeError):
    """The broker could not be reached or refused the request. Fails closed; never guess."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class OandaConfigError(ValueError):
    pass


class OandaAccountMismatch(OandaError):
    """The configured account is not one the token can see."""


# -- credentials -----------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    env: str
    token: str
    account_id: str | None

    def __repr__(self) -> str:  # never print the token
        return f"Credentials(env={self.env!r}, token='***', account_id={self.account_id!r})"


def check_private(path: Path) -> None:
    """Refuse a credentials file that anyone but its owner could read (POSIX only)."""
    if os.name != "posix":
        return
    st = path.stat()
    if st.st_mode & 0o077:
        raise OandaConfigError(f"{path} is readable by other users (mode {oct(st.st_mode & 0o777)}); run: chmod 600 {path}")
    if st.st_uid != os.getuid():
        raise OandaConfigError(f"{path} is not owned by the user reading it")


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    check_private(path)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def load_credentials(environ: Mapping[str, str] | None = None) -> Credentials:
    environ = os.environ if environ is None else environ
    file_values = _read_env_file(Path(environ.get("OANDA_ENV_FILE") or DEFAULT_ENV_FILE))
    get = lambda k: (environ.get(k) or file_values.get(k) or "").strip()  # noqa: E731
    env, token, account = get("OANDA_ENV").lower(), get("OANDA_API_TOKEN"), get("OANDA_ACCOUNT_ID")
    if env not in HOSTS:
        raise OandaConfigError("OANDA_ENV must be 'live' or 'practice'")
    if not token:
        raise OandaConfigError("OANDA_API_TOKEN is not set")
    return Credentials(env, token, account or None)


# -- typed facts -----------------------------------------------------------------


def parse_time(value: str | None) -> datetime | None:
    """OANDA RFC3339 with nanoseconds → aware UTC datetime (microsecond precision)."""
    if not value:
        return None
    head, _, frac = value.rstrip("Z").partition(".")
    dt = datetime.fromisoformat(head).replace(tzinfo=timezone.utc)
    return dt.replace(microsecond=int((frac + "000000")[:6])) if frac else dt


def _d(value: Any) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


@dataclass(frozen=True)
class Instrument:
    name: str  # EUR_USD
    type: str  # CURRENCY | METAL | CFD
    display_name: str
    pip_location: int  # -4 → 1 pip = 0.0001
    display_precision: int
    trade_units_precision: int  # 0 → whole units
    minimum_trade_size: Decimal
    maximum_order_units: Decimal | None
    maximum_position_size: Decimal | None
    margin_rate: Decimal  # 0.02 → 50:1
    long_financing: Decimal | None
    short_financing: Decimal | None
    raw: Mapping[str, Any]

    @property
    def max_leverage(self) -> Decimal:
        return Decimal(1) / self.margin_rate if self.margin_rate else Decimal(0)

    @property
    def pip(self) -> Decimal:
        return Decimal(10) ** self.pip_location

    @classmethod
    def parse(cls, r: Mapping[str, Any]) -> "Instrument":
        fin = r.get("financing") or {}
        max_pos = _d(r.get("maximumPositionSize"))
        return cls(
            name=r["name"], type=r.get("type", ""), display_name=r.get("displayName", r["name"]),
            pip_location=int(r.get("pipLocation", 0)), display_precision=int(r.get("displayPrecision", 0)),
            trade_units_precision=int(r.get("tradeUnitsPrecision", 0)),
            minimum_trade_size=_d(r.get("minimumTradeSize")) or Decimal(0),
            maximum_order_units=_d(r.get("maximumOrderUnits")), maximum_position_size=max_pos or None,
            margin_rate=_d(r.get("marginRate")) or Decimal(0),
            long_financing=_d(fin.get("longRate")), short_financing=_d(fin.get("shortRate")), raw=dict(r),
        )


@dataclass(frozen=True)
class Price:
    instrument: str
    bid: Decimal | None
    ask: Decimal | None
    time: datetime | None
    tradeable: bool

    @property
    def mid(self) -> Decimal | None:
        return (self.bid + self.ask) / 2 if self.bid and self.ask else None

    def spread_pips(self, pip_location: int) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / (Decimal(10) ** pip_location)

    @classmethod
    def parse(cls, r: Mapping[str, Any]) -> "Price":
        bids, asks = r.get("bids") or [], r.get("asks") or []
        return cls(
            instrument=r["instrument"],
            bid=_d(bids[0]["price"]) if bids else _d(r.get("closeoutBid")),
            ask=_d(asks[0]["price"]) if asks else _d(r.get("closeoutAsk")),
            time=parse_time(r.get("time")),
            tradeable=bool(r.get("tradeable", False)),
        )


@dataclass(frozen=True)
class OHLC:
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741
    c: Decimal

    @classmethod
    def parse(cls, r: Mapping[str, Any] | None) -> "OHLC | None":
        return cls(Decimal(r["o"]), Decimal(r["h"]), Decimal(r["l"]), Decimal(r["c"])) if r else None


@dataclass(frozen=True)
class Candle:
    time: datetime
    volume: int
    complete: bool
    bid: OHLC | None
    ask: OHLC | None
    mid: OHLC | None

    @classmethod
    def parse(cls, r: Mapping[str, Any]) -> "Candle":
        return cls(
            time=parse_time(r["time"]),  # type: ignore[arg-type]
            volume=int(r.get("volume", 0)), complete=bool(r.get("complete", False)),
            bid=OHLC.parse(r.get("bid")), ask=OHLC.parse(r.get("ask")), mid=OHLC.parse(r.get("mid")),
        )


# -- transport -------------------------------------------------------------------


def _urllib_fetch(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, Mapping[str, str], bytes]:
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https hosts
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() or b""


def _urllib_stream(url: str, headers: Mapping[str, str], timeout: float) -> Iterator[bytes]:
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)  # noqa: S310
    except urllib.error.HTTPError as exc:
        raise OandaError(f"stream HTTP {exc.code}: {_error_text(exc.read() or b'')}", status=exc.code) from None
    with resp:
        for line in resp:
            yield line


def _error_text(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        return body[:200].decode("utf-8", errors="replace")
    return str(payload.get("errorMessage") or payload.get("message") or payload)[:300]


class _RateLimiter:
    """At most `per_second` requests per second (OANDA allows 120 per IP; we stay far below)."""

    def __init__(self, per_second: float, *, sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.interval = 1.0 / per_second
        self.sleep, self.monotonic = sleep, monotonic
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self.monotonic()
            if now < self._next:
                self.sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


# -- client ----------------------------------------------------------------------


class OandaClient:
    def __init__(
        self,
        creds: Credentials,
        *,
        timeout: float = 15.0,
        fetch: Fetch | None = None,
        open_stream: OpenStream | None = None,
        per_second: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if creds.env not in HOSTS:
            raise OandaConfigError(f"unknown OANDA environment {creds.env!r}")
        self.env = creds.env
        self.rest_host, self.stream_host = HOSTS[creds.env]
        self._token = creds.token
        self._account_id = creds.account_id
        self.timeout = timeout
        self._fetch = fetch or _urllib_fetch
        self._open_stream = open_stream or _urllib_stream
        self._limiter = _RateLimiter(per_second, sleep=sleep)
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"OandaClient(env={self.env!r}, account_id={self._account_id!r})"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept-Datetime-Format": "RFC3339", "Accept": "application/json"}

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self.rest_host}{path}" + (f"?{query}" if query else "")
        for attempt in range(3):
            self._limiter.wait()
            try:
                status, headers, body = self._fetch(url, self._headers, self.timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise OandaError(f"OANDA unreachable: {type(exc).__name__}: {exc}", code="UNREACHABLE") from None
            if status == 429 and attempt < 2:
                self._sleep(float(headers.get("Retry-After") or 1.0))
                continue
            if status >= 400:
                code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 429: "RATE_LIMITED"}.get(status, f"HTTP_{status}")
                raise OandaError(f"{path}: HTTP {status}: {_error_text(body)}", status=status, code=code)
            try:
                return json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                raise OandaError(f"{path}: response is not JSON", status=status, code="BAD_RESPONSE") from None
        raise OandaError(f"{path}: rate limited", status=429, code="RATE_LIMITED")

    # -- account -------------------------------------------------------------------
    def accounts(self) -> list[dict[str, Any]]:
        return list(self.get("/v3/accounts").get("accounts") or [])

    @property
    def account_id(self) -> str:
        """The configured account, verified against the token; or the token's only account."""
        ids = [a["id"] for a in self.accounts()]
        if self._account_id:
            if self._account_id not in ids:
                raise OandaAccountMismatch(f"account {self._account_id} is not visible to this token ({len(ids)} accounts)", code="ACCOUNT_MISMATCH")
            return self._account_id
        if len(ids) != 1:
            raise OandaConfigError(f"token sees {len(ids)} accounts; set OANDA_ACCOUNT_ID")
        self._account_id = ids[0]
        return self._account_id

    def summary(self, account_id: str) -> dict[str, Any]:
        return dict(self.get(f"/v3/accounts/{account_id}/summary").get("account") or {})

    def instruments(self, account_id: str, names: list[str] | None = None) -> list[Instrument]:
        params = {"instruments": ",".join(names)} if names else None
        return [Instrument.parse(r) for r in self.get(f"/v3/accounts/{account_id}/instruments", params).get("instruments") or []]

    def open_trades(self, account_id: str) -> list[dict[str, Any]]:
        return list(self.get(f"/v3/accounts/{account_id}/openTrades").get("trades") or [])

    def open_positions(self, account_id: str) -> list[dict[str, Any]]:
        return list(self.get(f"/v3/accounts/{account_id}/openPositions").get("positions") or [])

    def pending_orders(self, account_id: str) -> list[dict[str, Any]]:
        return list(self.get(f"/v3/accounts/{account_id}/pendingOrders").get("orders") or [])

    def transactions_since(self, account_id: str, since_id: str | int) -> tuple[list[dict[str, Any]], str]:
        """Transactions with id > since_id, oldest first, and the account's lastTransactionID."""
        r = self.get(f"/v3/accounts/{account_id}/transactions/sinceid", {"id": since_id})
        return list(r.get("transactions") or []), str(r.get("lastTransactionID") or since_id)

    # -- market data ---------------------------------------------------------------
    def pricing(self, account_id: str, instruments: list[str]) -> list[Price]:
        r = self.get(f"/v3/accounts/{account_id}/pricing", {"instruments": ",".join(instruments)})
        return [Price.parse(p) for p in r.get("prices") or []]

    def candles(
        self,
        instrument: str,
        granularity: str,
        *,
        count: int | None = None,
        start: str | None = None,
        end: str | None = None,
        price: str = "BA",
        daily_alignment: int = 17,
        alignment_timezone: str = "America/New_York",
    ) -> list[Candle]:
        if count is not None and not 1 <= count <= MAX_CANDLES:
            raise ValueError(f"count must be 1..{MAX_CANDLES}")
        params = {
            "granularity": granularity, "price": price, "count": count, "from": start, "to": end,
            "dailyAlignment": daily_alignment, "alignmentTimezone": alignment_timezone,
        }
        return [Candle.parse(c) for c in self.get(f"/v3/instruments/{instrument}/candles", params).get("candles") or []]

    def stream_prices(self, account_id: str, instruments: list[str], *, max_seconds: float = 10.0, max_messages: int = 1000) -> Iterator[dict[str, Any]]:
        """Messages from the pricing stream (PRICE and HEARTBEAT) until a limit is reached."""
        query = urllib.parse.urlencode({"instruments": ",".join(instruments)})
        url = f"{self.stream_host}/v3/accounts/{account_id}/pricing/stream?{query}"
        deadline = time.monotonic() + max_seconds
        received = 0
        try:
            for line in self._open_stream(url, self._headers, self.timeout):
                line = line.strip()
                if line:
                    yield json.loads(line.decode("utf-8"))
                    received += 1
                if received >= max_messages or time.monotonic() >= deadline:
                    return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OandaError(f"stream unreachable: {type(exc).__name__}: {exc}", code="UNREACHABLE") from None
