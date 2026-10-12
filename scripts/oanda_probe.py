"""Read-only OANDA integration probe. Places, modifies, and cancels nothing.

Verifies authentication and the account, the instrument list a US account sees,
execution constraints exposed by the API (margin rates, unit precision, minimum
size, hedging flag), live pricing and spreads, candles (bid/ask, 17:00 New York
alignment, the per-request limit), the pricing stream, and the transaction log.

    python scripts/oanda_probe.py            # summary on screen + JSON in state/forex/probe/ (owner-only)

On the Pi:  sudo -u agentic /opt/agentic-portfolio/.venv/bin/python /opt/agentic-portfolio/scripts/oanda_probe.py
    python scripts/oanda_probe.py --quiet    # JSON path only

Credentials: see src/forex/oanda.py (OANDA_ENV, OANDA_API_TOKEN, OANDA_ACCOUNT_ID).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from forex.oanda import MAX_CANDLES, OandaClient, OandaConfigError, OandaError, load_credentials  # noqa: E402

MAJORS = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD", "NZD_USD", "EUR_GBP", "EUR_JPY", "GBP_JPY"]


def _jsonable(x: Any) -> Any:
    if isinstance(x, Decimal):
        return str(x)
    if isinstance(x, datetime):
        return x.isoformat()
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(x).items() if k != "raw"}
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x


class Probe:
    def __init__(self, client: OandaClient) -> None:
        self.client = client
        self.report: dict[str, Any] = {"env": client.env, "started_at": datetime.now(timezone.utc).isoformat(), "checks": {}, "latency_ms": []}

    def check(self, name: str, fn: Callable[[], Any], *, keep: bool = True) -> Any:
        t0 = time.perf_counter()
        try:
            result = fn()
            stored = _jsonable(result) if keep else {"count": len(result)} if hasattr(result, "__len__") else None
            self.report["checks"][name] = {"ok": True, "result": stored}
            return result
        except (OandaError, ValueError, KeyError) as exc:
            self.report["checks"][name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "code": getattr(exc, "code", None)}
            return None
        finally:
            self.report["latency_ms"].append(round((time.perf_counter() - t0) * 1000, 1))

    def run(self) -> dict[str, Any]:
        c = self.client
        accounts = self.check("accounts", lambda: [{"id": a["id"], "tags": a.get("tags", [])} for a in c.accounts()])
        if accounts is None:
            return self.report
        account_id = self.check("account_id", lambda: c.account_id)
        if account_id is None:
            return self.report

        summary = self.check("account_summary", lambda: c.summary(account_id)) or {}
        margin_rate = Decimal(summary.get("marginRate") or "0")
        self.report["account"] = {
            "currency": summary.get("currency"),
            "balance": summary.get("balance"),
            "nav": summary.get("NAV"),
            "hedging_enabled": summary.get("hedgingEnabled"),
            "account_margin_rate": summary.get("marginRate"),
            "account_max_leverage": str(round(1 / margin_rate, 1)) if margin_rate else None,
            "open_trades": summary.get("openTradeCount"),
            "open_positions": summary.get("openPositionCount"),
            "pending_orders": summary.get("pendingOrderCount"),
            "last_transaction_id": summary.get("lastTransactionID"),
            "guaranteed_stop_mode": summary.get("guaranteedStopLossOrderMode"),
            "created": summary.get("createdTime"),
        }

        instruments = self.check("instruments", lambda: c.instruments(account_id), keep=False) or []
        by_name = {i.name: i for i in instruments}
        self.report["instruments"] = {
            "count": len(instruments),
            "by_type": dict(Counter(i.type for i in instruments)),
            "non_currency": sorted(i.name for i in instruments if i.type != "CURRENCY"),
            "names": sorted(by_name),
            "min_trade_sizes": dict(Counter(str(i.minimum_trade_size) for i in instruments)),
            "unit_precisions": dict(Counter(i.trade_units_precision for i in instruments)),
            "max_leverage_by_pair": {n: str(round(by_name[n].max_leverage, 1)) for n in MAJORS if n in by_name},
            "leverage_tiers": {f"{round(lev)}:1": sorted(n for n, i in by_name.items() if round(i.max_leverage) == round(lev)) for lev in sorted({i.max_leverage for i in instruments}, reverse=True)},
            "majors": {
                n: {
                    "pip": str(by_name[n].pip), "min_units": str(by_name[n].minimum_trade_size),
                    "units_precision": by_name[n].trade_units_precision, "margin_rate": str(by_name[n].margin_rate),
                    "max_order_units": str(by_name[n].maximum_order_units),
                    "financing_long": str(by_name[n].long_financing), "financing_short": str(by_name[n].short_financing),
                    "gsl_mode": by_name[n].raw.get("guaranteedStopLossOrderMode"),
                }
                for n in MAJORS if n in by_name
            },
        }

        majors = [n for n in MAJORS if n in by_name] or MAJORS[:3]
        prices = self.check("pricing", lambda: c.pricing(account_id, majors)) or []
        now = datetime.now(timezone.utc)
        self.report["pricing"] = {
            p.instrument: {
                "bid": str(p.bid), "ask": str(p.ask), "tradeable": p.tradeable,
                "spread_pips": str(round(p.spread_pips(by_name[p.instrument].pip_location), 2)) if p.instrument in by_name and p.spread_pips(by_name[p.instrument].pip_location) is not None else None,
                "age_s": round((now - p.time).total_seconds(), 1) if p.time else None,
            }
            for p in prices
        }

        def all_spreads() -> dict[str, Any]:
            names = sorted(by_name)
            ps = [p for k in range(0, len(names), 40) for p in c.pricing(account_id, names[k : k + 40])]
            spreads = {p.instrument: float(round(p.spread_pips(by_name[p.instrument].pip_location), 2)) for p in ps if p.spread_pips(by_name[p.instrument].pip_location) is not None}
            ranked = sorted(spreads.items(), key=lambda kv: kv[1])
            return {
                "priced": len(ps), "tradeable": sum(p.tradeable for p in ps), "not_tradeable": sorted(p.instrument for p in ps if not p.tradeable),
                "median_pips": statistics.median(spreads.values()) if spreads else None,
                "tightest": ranked[:10], "widest": ranked[-8:], "all": dict(ranked),
            }

        self.report["spreads_all"] = self.check("pricing_all_pairs", all_spreads)

        def depth(gran: str) -> dict[str, Any]:
            first = c.candles("EUR_USD", gran, start="2000-01-01T00:00:00Z", count=1)
            return {"earliest": first[0].time.isoformat() if first else None}

        self.report["history_depth"] = {g: self.check(f"history_{g}", lambda g=g: depth(g)) for g in ("D", "H1", "M5", "M1")}

        def candle_check(gran: str, count: int) -> dict[str, Any]:
            cs = c.candles("EUR_USD", gran, count=count)
            return {
                "returned": len(cs), "first": cs[0].time.isoformat() if cs else None, "last": cs[-1].time.isoformat() if cs else None,
                "has_bid_ask": bool(cs and cs[-1].bid and cs[-1].ask), "last_complete": cs[-1].complete if cs else None,
                "utc_hours_seen": sorted({x.time.hour for x in cs[-20:]}) if gran == "D" else None,
            }

        self.report["candles"] = {
            "M5_max": self.check("candles_M5_5000", lambda: candle_check("M5", MAX_CANDLES)),
            "H1": self.check("candles_H1", lambda: candle_check("H1", 500)),
            "D_aligned_17NY": self.check("candles_D", lambda: candle_check("D", 500)),
        }
        # Server-side limit: one over the documented maximum should be refused.
        over = self.check("candles_over_limit", lambda: len(c.get("/v3/instruments/EUR_USD/candles", {"granularity": "M5", "count": MAX_CANDLES + 1}).get("candles") or []))
        self.report["candles"]["over_limit_refused"] = over is None

        def stream() -> dict[str, Any]:
            kinds: Counter[str] = Counter()
            first_price_s = None
            t0 = time.monotonic()
            for msg in c.stream_prices(account_id, majors[:4], max_seconds=8, max_messages=400):
                kinds[msg.get("type", "?")] += 1
                if msg.get("type") == "PRICE" and first_price_s is None:
                    first_price_s = round(time.monotonic() - t0, 2)
            return {"messages": dict(kinds), "first_price_after_s": first_price_s, "seconds": round(time.monotonic() - t0, 1)}

        self.report["stream"] = self.check("pricing_stream", stream)

        last_id = int(summary.get("lastTransactionID") or 0)

        def txns() -> dict[str, Any]:
            items, last = c.transactions_since(account_id, max(0, last_id - 500))
            return {
                "count": len(items), "types": dict(Counter(t.get("type") for t in items)), "last_id": last,
                "funding": [{"id": t.get("id"), "time": t.get("time"), "amount": t.get("amount"), "reason": t.get("fundingReason")} for t in items if t.get("type") == "TRANSFER_FUNDS"],
                "configuration": [{"id": t.get("id"), "type": t.get("type"), "marginRate": t.get("marginRate"), "alias": t.get("alias")} for t in items if t.get("type") in ("CREATE", "CLIENT_CONFIGURE", "CLIENT_CONFIGURE_REJECT")],
            }

        self.report["transactions"] = self.check("transactions_sinceid", txns)
        self.report["open_state"] = {
            "trades": self.check("open_trades", lambda: len(c.open_trades(account_id))),
            "positions": self.check("open_positions", lambda: len(c.open_positions(account_id))),
            "pending_orders": self.check("pending_orders", lambda: len(c.pending_orders(account_id))),
        }
        lat = self.report["latency_ms"]
        self.report["latency_summary_ms"] = {"median": statistics.median(lat), "max": max(lat)} if lat else None
        self.report["finished_at"] = datetime.now(timezone.utc).isoformat()
        return self.report


def print_summary(r: dict[str, Any]) -> None:
    checks = r["checks"]
    ok = sum(1 for v in checks.values() if v["ok"])
    print(f"OANDA probe ({r['env']}): {ok}/{len(checks)} checks passed")
    for name, v in checks.items():
        if not v["ok"]:
            print(f"  FAIL {name}: {v['error']}")
    if a := r.get("account"):
        print(f"  account: {a['currency']} balance {a['balance']} NAV {a['nav']} | hedging {a['hedging_enabled']} | max leverage {a['account_max_leverage']}:1 | open trades {a['open_trades']}, orders {a['pending_orders']}")
    if i := r.get("instruments"):
        print(f"  instruments: {i['count']} {i['by_type']} | non-currency: {i['non_currency'][:8] or 'none'}")
        print(f"  min trade sizes: {i['min_trade_sizes']} | unit precisions: {i['unit_precisions']}")
        print(f"  leverage by pair: {i['max_leverage_by_pair']}")
    for sym, p in (r.get("pricing") or {}).items():
        print(f"  {sym:8} bid {p['bid']:>10} ask {p['ask']:>10} spread {p['spread_pips']} pips  tradeable={p['tradeable']} age={p['age_s']}s")
    if cd := r.get("candles"):
        print(f"  candles: M5 {cd.get('M5_max')} | over-limit refused: {cd.get('over_limit_refused')}")
        print(f"           D  {cd.get('D_aligned_17NY')}")
    if sa := r.get("spreads_all"):
        print(f"  all pairs: {sa['priced']} priced, {sa['tradeable']} tradeable, median spread {sa['median_pips']} pips")
        print(f"    tightest: {sa['tightest']}")
        print(f"    widest:   {sa['widest']}")
    if i := r.get("instruments"):
        print(f"  leverage tiers: { {k: len(v) for k, v in i['leverage_tiers'].items()} }")
    print(f"  history depth (EUR_USD): {r.get('history_depth')}")
    print(f"  stream: {r.get('stream')}")
    print(f"  transactions: {r.get('transactions')} | open: {r.get('open_state')}")
    print(f"  latency: {r.get('latency_summary_ms')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "state" / "forex" / "probe"), help="report directory")
    args = ap.parse_args()
    try:
        client = OandaClient(load_credentials())
    except OandaConfigError as exc:
        print(f"not configured: {exc}. Set OANDA_ENV / OANDA_API_TOKEN (see src/forex/oanda.py).", file=sys.stderr)
        return 2
    report = Probe(client).run()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(out_dir, 0o700)  # reports include the account balance
    path = out_dir / f"probe-{report['env']}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not args.quiet:
        print_summary(report)
    print(f"report: {path}")
    return 0 if all(v["ok"] or k == "candles_over_limit" for k, v in report["checks"].items()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
