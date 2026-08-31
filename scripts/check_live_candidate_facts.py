"""Print LIVE candidate fact provenance. Never calls an AI provider. Cost is $0."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))

from agentic_portfolio.adapters.robinhood_read import FORBIDDEN_MCP_TOOLS
from agentic_portfolio.ai.config import load_ai_config
from agentic_portfolio.ai.identity import CandidateValidationResult, facts_from_payloads, observe_live_candidate, persist_identity
from agentic_portfolio.ai.safety import LIVE_ORDER_PLACEMENT
from agentic_portfolio.live.engine import market_session_state
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.runtime import RuntimeMode, bootstrap_readonly_broker_runtime
from agentic_portfolio.schemas import CandidateValidationStatus

WRITE_TOOLS = frozenset(FORBIDDEN_MCP_TOOLS) | {"review_equity_order"}


def _fact(facts, key: str) -> dict[str, Any] | None:
    if facts is None:
        return None
    return facts.get(key).for_ai()


def _report(
    ticker: str,
    validation,
    *,
    runtime: str = RuntimeMode.LIVE.value,
    source_calls: list[str] | None = None,
    now: datetime | None = None,
    fetch_error: str | None = None,
    readonly_broker_transport: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stamp = now or datetime.now(timezone.utc)
    market = market_session_state(stamp)
    row = validation.as_report()
    identity = validation.identity
    facts = validation.facts
    reasons = list(row.get("rejection_reasons") or [])
    if fetch_error and fetch_error not in reasons:
        reasons = [fetch_error] + reasons
    return {
        "runtime": runtime,
        "ticker": ticker.upper(),
        "security_name": (identity.security_name.for_ai() if identity else None),
        "security_type": (identity.security_type.for_ai() if identity else None),
        "broker_instrument_id": (identity.broker_instrument_id.for_ai() if identity else None),
        "quote": row.get("quote"),
        "quote_source": row.get("quote_source"),
        "quote_as_of": row.get("quote_as_of"),
        "market_session_state": market,
        "latest_completed_session": market.get("latest_completed_session"),
        "fundamental_or_fund_source": row.get("fundamental_or_fund_source"),
        "net_assets": _fact(facts, "net_assets") if (identity and identity.is_etf) else None,
        "company_market_cap": _fact(facts, "market_cap") if (identity and identity.is_equity) else _fact(facts, "market_cap"),
        "company_pe_ratio": _fact(facts, "pe_ratio") if (identity and identity.is_equity) else _fact(facts, "pe_ratio"),
        "average_volume": _fact(facts, "average_volume"),
        "bid_price": _fact(facts, "bid"),
        "ask_price": _fact(facts, "ask"),
        "absolute_spread_usd": _fact(facts, "absolute_spread_usd"),
        "spread_percent": _fact(facts, "spread_percent"),
        "spread_bps": _fact(facts, "spread_bps"),
        "derived_dollar_liquidity": _fact(facts, "dollar_volume"),
        "liquidity": row.get("liquidity"),
        "provenance": {
            "security_name": identity.security_name.for_ai() if identity else None,
            "security_type": identity.security_type.for_ai() if identity else None,
            "broker_instrument_id": identity.broker_instrument_id.for_ai() if identity else None,
            "quote": _fact(facts, "last_price"),
            "average_volume": _fact(facts, "average_volume"),
            "bid_price": _fact(facts, "bid"),
            "ask_price": _fact(facts, "ask"),
            "absolute_spread_usd": _fact(facts, "absolute_spread_usd"),
            "spread_percent": _fact(facts, "spread_percent"),
            "spread_bps": _fact(facts, "spread_bps"),
            "dollar_liquidity": _fact(facts, "dollar_volume"),
            "net_assets": _fact(facts, "net_assets"),
            "company_market_cap": _fact(facts, "market_cap"),
            "company_pe_ratio": _fact(facts, "pe_ratio"),
        },
        "freshness": dict(row.get("data_freshness") or {}),
        "data_freshness": row.get("data_freshness"),
        "synthetic_data_detected": row.get("synthetic_data_detected"),
        "eligible_for_ai": False if fetch_error else row.get("eligible_for_ai"),
        "validation_status": row.get("status"),
        "status": row.get("status"),
        "rejection_reasons": reasons,
        "read_only_source_calls": list(source_calls or []),
        "is_etf": bool(identity.is_etf) if identity else False,
        "is_equity": bool(identity.is_equity) if identity else False,
        "ai_provider_called": False,
        "ai_cost": 0,
        "LIVE_ORDER_PLACEMENT": LIVE_ORDER_PLACEMENT,
        "auto_execution": bool((load_account_rules().get("execution") or {}).get("auto_execution")),
        "live_trade_actions_allowed": bool((load_account_rules().get("execution") or {}).get("live_trade_actions_allowed")),
        "readonly_broker_transport": dict(readonly_broker_transport or {}),
    }


def _script_calls_ai_or_writes() -> list[str]:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    hits: list[str] = []
    forbidden = {
        "place_equity_order",
        "cancel_equity_order",
        "review_equity_order",
        "place_option_order",
        "place_crypto_order",
        "build_gateway",
        "run_candidate_pipeline",
        "OpenAIProvider",
        "AnthropicProvider",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden:
            hits.append(node.func.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in forbidden:
            hits.append(node.func.attr)
    return hits


def main(argv: list[str] | None = None, *, fetcher: Any = None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description="LIVE candidate fact diagnostic. Does not call AI. Cost $0.")
    parser.add_argument("ticker", help="Ticker to inspect, for example QUAL")
    parser.add_argument("--payloads", type=Path, default=None, help="Optional JSON override; LIVE default fetches via the read-only MCP adapter")
    parser.add_argument("--persist", action="store_true", help="Write identity JSON under state/live_ai/identities")
    args = parser.parse_args(argv)
    ticker = str(args.ticker).upper()
    stamp = now or datetime.now(timezone.utc)
    write_hits = _script_calls_ai_or_writes()
    if write_hits:
        raise RuntimeError(f"diagnostic refused forbidden calls: {write_hits}")
    source_calls: list[str] = []
    fetch_error: str | None = None
    cfg = load_ai_config()
    runtime = bootstrap_readonly_broker_runtime()
    transport_report = runtime.as_report()
    if args.payloads:
        payloads = json.loads(args.payloads.read_text(encoding="utf-8"))
        snap, validation = facts_from_payloads(ticker, payloads, now=stamp, config=cfg)
        del snap
        source_calls = ["payloads_file"]
    elif fetcher is None and not runtime.bound:
        fetch_error = runtime.initialization_error or "readonly_mcp_unreachable: authorized Robinhood MCP transport is not bound"
        validation = CandidateValidationResult(
            ticker=ticker,
            status=CandidateValidationStatus.MISSING_QUOTE,
            reasons=[fetch_error],
            facts=None,
            identity=None,
            eligible_for_ai=False,
            synthetic_data_detected=False,
        )
        source_calls = []
    else:
        live_fetcher = fetcher or runtime.fetcher
        if live_fetcher is None:
            fetch_error = runtime.initialization_error or "readonly_mcp_unreachable: authorized Robinhood MCP transport is not bound"
            validation = CandidateValidationResult(
                ticker=ticker,
                status=CandidateValidationStatus.MISSING_QUOTE,
                reasons=[fetch_error],
                facts=None,
                identity=None,
                eligible_for_ai=False,
                synthetic_data_detected=False,
            )
        else:
            snap, validation, source_calls = observe_live_candidate(
                ticker,
                live_fetcher,
                now=stamp,
                runtime_mode=RuntimeMode.LIVE,
                config=cfg,
            )
            del snap
            bad = set(source_calls) & WRITE_TOOLS
            if bad:
                raise RuntimeError(f"diagnostic refused forbidden MCP tools: {sorted(bad)}")
            if validation.reasons and str(validation.reasons[0]).startswith("readonly_mcp_unreachable"):
                fetch_error = validation.reasons[0]
                validation.eligible_for_ai = False
    if args.persist and fetch_error is None:
        persist_identity(validation, root=project_root(), runtime_mode=RuntimeMode.LIVE, now=stamp)
    report = _report(
        ticker,
        validation,
        source_calls=source_calls,
        now=stamp,
        fetch_error=fetch_error,
        readonly_broker_transport=transport_report,
    )
    print(json.dumps(report, indent=2, default=str))
    return 1 if fetch_error else 0


if __name__ == "__main__":
    sys.exit(main())
