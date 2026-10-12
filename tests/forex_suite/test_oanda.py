from __future__ import annotations

import importlib.util
import inspect
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from forex import oanda
from forex.oanda import Credentials, OandaAccountMismatch, OandaClient, OandaConfigError, OandaError, load_credentials

TOKEN = "tok-SECRET-123"
ROOT = Path(__file__).resolve().parents[2]


class FakeFetch:
    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        path = url.split(".com", 1)[1].split("?", 1)[0]
        resp = self.routes.get(path)
        if isinstance(resp, list):  # sequence of responses
            resp = resp.pop(0)
        if resp is None:
            return 404, {}, b'{"errorMessage": "not found"}'
        if isinstance(resp, tuple):
            return resp
        return 200, {}, json.dumps(resp).encode()


def client(routes, *, env="live", account_id=None, **kw):
    fetch = FakeFetch(routes)
    c = OandaClient(Credentials(env, TOKEN, account_id), fetch=fetch, sleep=lambda s: None, per_second=1e6, **kw)
    return c, fetch


ACCOUNTS = {"/v3/accounts": {"accounts": [{"id": "001-001-1-001", "tags": []}]}}


def test_hosts_follow_environment():
    live, f1 = client(ACCOUNTS)
    practice, f2 = client(ACCOUNTS, env="practice")
    live.accounts()
    practice.accounts()
    assert f1.calls[0][0].startswith("https://api-fxtrade.oanda.com/v3/accounts")
    assert f2.calls[0][0].startswith("https://api-fxpractice.oanda.com/v3/accounts")
    assert f1.calls[0][1]["Authorization"] == f"Bearer {TOKEN}"


def test_client_is_read_only_by_construction():
    # No method that writes exists, and the transport takes no HTTP method.
    names = {n for n in dir(OandaClient) if not n.startswith("_")}
    assert not {n for n in names if any(w in n.lower() for w in ("order_create", "place", "close", "cancel", "replace", "post", "put", "patch", "delete"))}
    assert list(inspect.signature(oanda._urllib_fetch).parameters) == ["url", "headers", "timeout"]
    src = inspect.getsource(oanda)
    assert 'method="GET"' in src and "POST" not in src and "PUT" not in src and "PATCH" not in src and "DELETE" not in src


def test_token_never_in_repr_or_errors():
    c, _ = client({"/v3/accounts": (401, {}, b'{"errorMessage": "Insufficient authorization"}')})
    assert TOKEN not in repr(c) and TOKEN not in repr(Credentials("live", TOKEN, None))
    with pytest.raises(OandaError) as exc:
        c.accounts()
    assert exc.value.code == "UNAUTHORIZED" and TOKEN not in str(exc.value)


def test_rate_limit_is_retried_then_fails_closed():
    ok, f = client({"/v3/accounts": [(429, {"Retry-After": "0"}, b"{}"), ACCOUNTS["/v3/accounts"]]})
    assert ok.accounts()[0]["id"] == "001-001-1-001" and len(f.calls) == 2
    bad, _ = client({"/v3/accounts": [(429, {}, b"{}")] * 3})
    with pytest.raises(OandaError) as exc:
        bad.accounts()
    assert exc.value.code == "RATE_LIMITED"


def test_account_must_be_visible_to_token():
    c, _ = client(ACCOUNTS, account_id="001-001-9-999")
    with pytest.raises(OandaAccountMismatch):
        c.account_id
    c2, _ = client(ACCOUNTS)
    assert c2.account_id == "001-001-1-001"
    multi, _ = client({"/v3/accounts": {"accounts": [{"id": "a"}, {"id": "b"}]}})
    with pytest.raises(OandaConfigError):
        multi.account_id


def test_parses_instruments_pricing_and_candles():
    aid = "001-001-1-001"
    c, f = client({
        f"/v3/accounts/{aid}/instruments": {"instruments": [{
            "name": "EUR_USD", "type": "CURRENCY", "displayName": "EUR/USD", "pipLocation": -4, "displayPrecision": 5,
            "tradeUnitsPrecision": 0, "minimumTradeSize": "1", "maximumOrderUnits": "100000000", "marginRate": "0.02",
            "financing": {"longRate": "-0.0123", "shortRate": "0.0045"},
        }]},
        f"/v3/accounts/{aid}/pricing": {"prices": [{
            "instrument": "EUR_USD", "time": "2026-10-12T20:00:01.123456789Z", "tradeable": True,
            "bids": [{"price": "1.08001", "liquidity": 1000000}], "asks": [{"price": "1.08013", "liquidity": 1000000}],
        }]},
        "/v3/instruments/EUR_USD/candles": {"candles": [{
            "time": "2026-10-11T21:00:00.000000000Z", "volume": 1234, "complete": True,
            "bid": {"o": "1.08", "h": "1.09", "l": "1.07", "c": "1.085"}, "ask": {"o": "1.0801", "h": "1.0901", "l": "1.0701", "c": "1.0851"},
        }]},
    })
    (inst,) = c.instruments(aid)
    assert inst.max_leverage == Decimal(50) and inst.pip == Decimal("0.0001") and inst.minimum_trade_size == Decimal(1)
    (px,) = c.pricing(aid, ["EUR_USD"])
    assert px.spread_pips(inst.pip_location) == Decimal("1.2") and px.tradeable and px.time.microsecond == 123456
    (candle,) = c.candles("EUR_USD", "D", count=1)
    assert candle.complete and candle.bid.c == Decimal("1.085") and candle.mid is None
    url = f.calls[-1][0]
    assert "price=BA" in url and "dailyAlignment=17" in url and "alignmentTimezone=America%2FNew_York" in url
    with pytest.raises(ValueError):
        c.candles("EUR_USD", "M5", count=5001)


def test_transactions_since_returns_cursor():
    aid = "x"
    c, f = client({f"/v3/accounts/{aid}/transactions/sinceid": {"transactions": [{"id": "5", "type": "TRANSFER_FUNDS"}], "lastTransactionID": "5"}})
    items, last = c.transactions_since(aid, 4)
    assert last == "5" and items[0]["type"] == "TRANSFER_FUNDS" and "id=4" in f.calls[0][0]


def test_stream_stops_at_limits():
    lines = [b'{"type":"HEARTBEAT"}\n', b"\n", b'{"type":"PRICE","instrument":"EUR_USD"}\n'] * 10
    c = OandaClient(Credentials("practice", TOKEN, None), open_stream=lambda url, h, t: iter(lines))
    msgs = list(c.stream_prices("x", ["EUR_USD"], max_messages=3))
    assert [m["type"] for m in msgs] == ["HEARTBEAT", "PRICE", "HEARTBEAT"]


def test_credentials_from_file_env_wins(tmp_path):
    f = tmp_path / "oanda.env"
    f.write_text("# comment\nOANDA_ENV=practice\nOANDA_API_TOKEN='from-file'\nOANDA_ACCOUNT_ID=101-1\n", encoding="utf-8")
    os.chmod(f, 0o600)
    creds = load_credentials({"OANDA_ENV_FILE": str(f), "OANDA_ENV": "live"})
    assert (creds.env, creds.token, creds.account_id) == ("live", "from-file", "101-1")
    with pytest.raises(OandaConfigError):
        load_credentials({"OANDA_ENV_FILE": str(tmp_path / "missing"), "OANDA_API_TOKEN": "t"})  # no env given
    with pytest.raises(OandaConfigError):
        load_credentials({"OANDA_ENV_FILE": str(tmp_path / "missing"), "OANDA_ENV": "live"})  # no token


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_credentials_file_must_be_private(tmp_path):
    f = tmp_path / "oanda.env"
    f.write_text("OANDA_ENV=live\nOANDA_API_TOKEN=t\n", encoding="utf-8")
    os.chmod(f, 0o644)
    with pytest.raises(OandaConfigError, match="readable by other users"):
        load_credentials({"OANDA_ENV_FILE": str(f)})
    os.chmod(f, 0o600)
    assert load_credentials({"OANDA_ENV_FILE": str(f)}).token == "t"


def test_credentials_writer_is_atomic_and_private(tmp_path):
    spec = importlib.util.spec_from_file_location("oanda_credentials", ROOT / "scripts" / "oanda_credentials.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    path = tmp_path / "forex" / "oanda.env"
    mod.write_env(path, env="live", token="a" * 32 + "-" + "b" * 32, account_id="001-001-1234567-001")
    mod.write_env(path, env="live", token="c" * 32 + "-" + "d" * 32, account_id="001-001-1234567-001")  # overwrite
    creds = load_credentials({"OANDA_ENV_FILE": str(path)})
    assert creds.token.startswith("c") and creds.account_id == "001-001-1234567-001"
    assert not list(path.parent.glob(".*.tmp"))
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700


def test_probe_runs_end_to_end_and_report_is_json(tmp_path):
    spec = importlib.util.spec_from_file_location("oanda_probe", ROOT / "scripts" / "oanda_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    aid = "001-001-1-001"
    inst = {"name": "EUR_USD", "type": "CURRENCY", "pipLocation": -4, "tradeUnitsPrecision": 0, "minimumTradeSize": "1", "marginRate": "0.02"}
    price = {"instrument": "EUR_USD", "time": "2026-10-12T20:00:01.000000000Z", "tradeable": True, "bids": [{"price": "1.08001"}], "asks": [{"price": "1.08013"}]}
    candle = {"time": "2026-10-11T21:00:00.000000000Z", "volume": 1, "complete": True, "bid": {"o": "1", "h": "1", "l": "1", "c": "1"}, "ask": {"o": "1", "h": "1", "l": "1", "c": "1"}}
    c, _ = client({
        **ACCOUNTS,
        f"/v3/accounts/{aid}/summary": {"account": {"currency": "USD", "balance": "50", "NAV": "50", "marginRate": "0.02", "hedgingEnabled": False, "lastTransactionID": "3"}},
        f"/v3/accounts/{aid}/instruments": {"instruments": [inst]},
        f"/v3/accounts/{aid}/pricing": {"prices": [price]},
        "/v3/instruments/EUR_USD/candles": {"candles": [candle]},
        f"/v3/accounts/{aid}/transactions/sinceid": {"transactions": [{"id": "2", "type": "TRANSFER_FUNDS", "amount": "50"}], "lastTransactionID": "3"},
        f"/v3/accounts/{aid}/openTrades": {"trades": []},
        f"/v3/accounts/{aid}/openPositions": {"positions": []},
        f"/v3/accounts/{aid}/pendingOrders": {"orders": []},
    }, open_stream=lambda url, h, t: iter([b'{"type":"PRICE"}\n', b'{"type":"HEARTBEAT"}\n']))
    report = mod.Probe(c).run()
    json.dumps(report)  # must serialize
    assert report["account"]["account_max_leverage"] == "50.0"
    assert report["spreads_all"]["median_pips"] == 1.2 and report["transactions"]["funding"][0]["amount"] == "50"
    assert report["instruments"]["leverage_tiers"] == {"50:1": ["EUR_USD"]}


def test_firm_never_imports_forex():
    """Tim's engine stays isolated: nothing under src/firm references the forex package."""
    offenders = [str(p) for p in (ROOT / "src" / "firm").rglob("*.py") if "import forex" in p.read_text(encoding="utf-8") or "from forex" in p.read_text(encoding="utf-8")]
    assert offenders == []
