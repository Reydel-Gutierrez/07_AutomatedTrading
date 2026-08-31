"""LIVE AI pipeline check. Uses the confirmed Agentic snapshot. Never places.

Default is the scripted provider (no spend). An explicit real-provider test
requires OPENAI_API_KEY and --use-real-ai. If OPENAI_API_KEY is set, this
script will not silently substitute the scripted provider.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from agentic_portfolio.adapters.portfolio_facts import LiveDataUnavailable, StaticPortfolioFetcher
from agentic_portfolio.adapters.robinhood_read import MappingReadOnlyFetcher
from agentic_portfolio.ai.config import load_ai_config, money
from agentic_portfolio.ai.config import load_ai_config, money
from agentic_portfolio.ai.gateway import build_gateway, default_providers
from agentic_portfolio.ai.identity import observe_live_candidate
from agentic_portfolio.ai.pipeline import run_candidate_pipeline
from agentic_portfolio.ai.pricing import quantize
from agentic_portfolio.ai.providers.scripted import ScriptedProvider
from agentic_portfolio.ai.safety import (
    LIVE_AI_ALLOWED,
    LIVE_ORDER_PLACEMENT,
    LIVE_PROPOSALS_ALLOWED,
    inspect_ai_module_for_forbidden_calls,
    redact_secrets,
)
from agentic_portfolio.discovery.snapshot import SecuritySnapshot
from agentic_portfolio.live.engine import refresh_live_portfolio
from agentic_portfolio.live.isolation import detect_paper_contamination
from agentic_portfolio.live.store import LivePortfolioStore
from agentic_portfolio.paper_fill.store import PaperFillStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.runtime import RuntimeMode, bootstrap_readonly_broker_runtime

FORBIDDEN = ("place_equity_order", "cancel_equity_order")

SCRIPTED = {
    "screening": {
        "ticker": "QUAL",
        "score": 70.0,
        "classification": "QUALITY_GROWTH",
        "catalyst_summary": "Supplied facts show durable growth.",
        "risk_flags": [],
        "worth_deep_research": True,
        "confidence": "MEDIUM",
    },
    "deep_research": {
        "ticker": "QUAL",
        "thesis": "Quality compounder versus cash on this $500 book, subject to Risk Gate.",
        "bull_case": "Growth continues.",
        "bear_case": "Multiple compresses.",
        "catalysts": ["earnings"],
        "risks": ["valuation"],
        "valuation_observations": "PE interpreted in supplied growth context.",
        "technical_observations": "Technicals are supporting context only.",
        "confidence": "MEDIUM",
        "recommended_action": "WATCH",
    },
    "portfolio_decision": {
        "ticker": "QUAL",
        "action": "WATCH",
        "confidence": "MEDIUM",
        "rationale": "Starter size is optional; cash is valid. Proposal-only.",
        "suggested_allocation_pct": 0.0,
        "suggested_max_dollars": 0.0,
        "reassessment_conditions": ["material news"],
        "risk_notes": ["tiny account", "placement disabled"],
    },
}


def resolve_live_ai_provider_mode(
    *,
    use_real_ai: bool = False,
    scripted: bool = False,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return True for a real-provider run. Never silently substitute scripted when a key exists."""
    env = environ if environ is not None else os.environ
    has_key = bool(env.get("OPENAI_API_KEY"))
    if use_real_ai and scripted:
        raise ValueError("pass either --use-real-ai or --scripted, not both")
    if scripted:
        return False
    if use_real_ai:
        return True
    if has_key:
        raise ValueError(
            "OPENAI_API_KEY is set. Pass --use-real-ai for a real OpenAI test, "
            "or --scripted to use the scripted provider. Refusing to silently substitute."
        )
    return False


def _payloads_from_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fetcher(payloads: dict) -> StaticPortfolioFetcher:
    return StaticPortfolioFetcher(
        accounts=payloads.get("accounts"),
        portfolio=payloads.get("portfolio"),
        positions=payloads.get("positions"),
        quotes=payloads.get("quotes"),
        orders=payloads.get("orders"),
    )


def _check_universe() -> list[SecuritySnapshot]:
    """LIVE checks must not invent a fixture universe. Callers pass resolved snapshots."""
    return []


def _observe_diagnostic_ticker(
    ticker: str,
    *,
    fetcher: Any | None,
    now: datetime,
    config: Mapping[str, Any] | None,
):
    """Fetch one ticker through the authorized read-only MCP path. Diagnostic only."""
    live_fetcher = fetcher
    runtime = bootstrap_readonly_broker_runtime()
    if live_fetcher is None:
        if not runtime.bound or runtime.fetcher is None:
            live_fetcher = MappingReadOnlyFetcher.from_payloads(
                str(ticker).upper(),
                {},
                error=LiveDataUnavailable(
                    runtime.initialization_error
                    or "readonly_mcp_unreachable: authorized Robinhood MCP transport is not bound"
                ),
            )
        else:
            live_fetcher = runtime.fetcher
    return observe_live_candidate(
        ticker,
        live_fetcher,
        now=now,
        runtime_mode=RuntimeMode.LIVE,
        config=config,
    )


def _explicit_ticker_validation(validation: Any) -> dict[str, Any] | None:
    """Surface the exact production-validator result for an explicit --ticker."""
    if validation is None:
        return None
    row = validation.as_report() if hasattr(validation, "as_report") else dict(validation)
    quote = row.get("quote") if isinstance(row.get("quote"), dict) else {}
    liquidity = row.get("liquidity") if isinstance(row.get("liquidity"), dict) else {}
    freshness = row.get("data_freshness") if isinstance(row.get("data_freshness"), dict) else {}
    name = row.get("security_name") if isinstance(row.get("security_name"), dict) else {}
    stype = row.get("security_type") if isinstance(row.get("security_type"), dict) else {}
    return {
        "status": row.get("status"),
        "eligible_for_ai": row.get("eligible_for_ai"),
        "rejection_reasons": list(row.get("rejection_reasons") or []),
        "security_name": name.get("value"),
        "security_type": stype.get("value"),
        "quote_value": quote.get("value"),
        "quote_freshness": freshness.get("quote", quote.get("freshness")),
        "liquidity_value": liquidity.get("value"),
        "liquidity_freshness": freshness.get("liquidity", liquidity.get("freshness")),
        "synthetic_data_detected": row.get("synthetic_data_detected"),
    }


def _row(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    if hasattr(obj, "__dict__"):
        data = {k: getattr(obj, k) for k in obj.__dataclass_fields__} if hasattr(obj, "__dataclass_fields__") else dict(vars(obj))
        out = {}
        for key, value in data.items():
            if hasattr(value, "value"):
                out[key] = value.value
            elif isinstance(value, Decimal):
                out[key] = float(value)
            else:
                out[key] = value
        return out
    if isinstance(obj, dict):
        return dict(obj)
    return {"value": str(obj)}


def run_live_ai_check(
    fetcher: StaticPortfolioFetcher | None = None,
    *,
    payloads: dict | None = None,
    root: Path | None = None,
    persist: bool = True,
    now: datetime | None = None,
    use_real_ai: bool = False,
    providers: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    snapshots: list[SecuritySnapshot] | None = None,
    ticker: str | None = None,
    instrument_fetcher: Any | None = None,
) -> dict:
    base = root or project_root()
    stamp = now or datetime.now(timezone.utc)
    env = environ if environ is not None else os.environ
    broker_runtime = bootstrap_readonly_broker_runtime()
    transport_report = broker_runtime.as_report()
    rules = load_account_rules()
    exe = dict(rules.get("execution") or {})
    expected = str(rules["account"]["account_number"])
    if payloads is None and fetcher is None:
        mcp_path = LivePortfolioStore(base).mcp_path()
        if not mcp_path.exists():
            return _fail("LIVE MCP payloads are unavailable", expected=expected, stamp=stamp, readonly_broker_transport=transport_report)
        payloads = _payloads_from_file(mcp_path)
    client = fetcher or _fetcher(payloads or {})
    try:
        refresh = refresh_live_portfolio(client, now=stamp, root=base, persist=persist)
    except Exception as exc:
        return _fail(str(exc), expected=expected, stamp=stamp, tools=getattr(client, "calls", []), readonly_broker_transport=transport_report)

    hits = inspect_ai_module_for_forbidden_calls(base)
    cfg = json.loads(json.dumps(load_ai_config()))
    ticker_symbol = str(ticker).strip().upper() if ticker else ""
    observed_snap: SecuritySnapshot | None = None
    ticker_validation = None
    instrument_calls: list[str] = []
    ticker_eligible = False
    if ticker_symbol:
        observed_snap, ticker_validation, instrument_calls = _observe_diagnostic_ticker(
            ticker_symbol,
            fetcher=instrument_fetcher,
            now=stamp,
            config=cfg,
        )
        ticker_eligible = bool(ticker_validation and ticker_validation.eligible_for_ai)
    explicit_ticker_validation = _explicit_ticker_validation(ticker_validation) if ticker_symbol else None
    if ticker_symbol:
        explicit_ticker_status = "VALID" if ticker_eligible else "INVALID"
    else:
        explicit_ticker_status = "NOT_REQUESTED"
    resolved_snapshots: list[SecuritySnapshot]
    if ticker_symbol:
        # Diagnostic ticker only. Never fall back to ordinary universe discovery.
        resolved_snapshots = [observed_snap] if observed_snap is not None else []
    elif snapshots is not None:
        resolved_snapshots = list(snapshots)
    else:
        resolved_snapshots = _check_universe()
    scripted = ScriptedProvider(SCRIPTED, name="scripted")
    openai_key_present = bool(env.get("OPENAI_API_KEY"))
    require_provider_key = use_real_ai and (not ticker_symbol or ticker_eligible)
    if use_real_ai:
        if require_provider_key and providers is None and not openai_key_present:
            return _fail(
                "OPENAI_API_KEY is not set; refusing to substitute the scripted provider",
                expected=expected,
                stamp=stamp,
                tools=getattr(client, "calls", []),
                ticker=ticker_symbol or None,
                explicit_ticker_status=explicit_ticker_status,
                readonly_broker_transport=transport_report,
            )
        cfg["providers"] = dict(cfg.get("providers") or {})
        cfg["providers"]["scripted"] = dict(cfg["providers"].get("scripted") or {})
        cfg["providers"]["scripted"]["enabled"] = False
        if not env.get("ANTHROPIC_API_KEY"):
            cfg["providers"]["anthropic"] = dict(cfg["providers"].get("anthropic") or {})
            cfg["providers"]["anthropic"]["enabled"] = False
        adapters = dict(providers) if providers is not None else default_providers(cfg, environ=env, scripted=None)
        adapters.pop("scripted", None)
        gateway_providers = adapters
        scripted_for_gateway = None
    else:
        cfg["budget"] = dict(cfg.get("budget") or {})
        cfg["budget"]["ledger_dir"] = "state/live_ai/check_budget"
        for spec in (cfg.get("roles") or {}).values():
            spec["provider"] = "scripted"
            spec["model"] = "scripted"
        gateway_providers = {
            "scripted": scripted,
            "openai": ScriptedProvider(SCRIPTED, name="openai"),
            "anthropic": ScriptedProvider(SCRIPTED, name="anthropic"),
        }
        scripted_for_gateway = scripted
    gateway = build_gateway(
        base,
        config=cfg,
        runtime_mode=RuntimeMode.LIVE,
        now_fn=lambda: stamp,
        providers=gateway_providers,
        scripted=scripted_for_gateway,
        environ=env,
    )
    availability = gateway.provider_availability()
    if not use_real_ai:
        real = default_providers(load_ai_config(), environ=env)
        availability = {name: bool(adapter.available()) for name, adapter in real.items()}
        availability["check_provider"] = "scripted"
    budget_before = gateway.budget.status()
    leaks = detect_paper_contamination(refresh.snapshot, PaperFillStore(base).current_book(), runtime_mode=RuntimeMode.LIVE)
    try:
        pipeline = run_candidate_pipeline(
            resolved_snapshots,
            refresh.context,
            gateway,
            runtime_mode=RuntimeMode.LIVE,
            root=base,
            now=stamp,
            persist=persist,
            snapshot=refresh.snapshot,
            snapshot_id=refresh.snapshot_id,
            config=cfg,
            skip_ai=bool(ticker_symbol and not ticker_eligible),
            skip_universe_discovery=bool(ticker_symbol),
        )
        pipeline_error = None
    except Exception as exc:
        pipeline = None
        pipeline_error = f"{type(exc).__name__}: {exc}"

    budget_after = gateway.budget.status()
    calls = list(gateway.calls)
    call_providers = [c.provider for c in calls]
    call_models = [c.model for c in calls]
    scripted_adapters_called = [
        adapter
        for adapter in (gateway.providers or {}).values()
        if isinstance(adapter, ScriptedProvider) and adapter.calls
    ]
    scripted_used = bool(scripted_adapters_called) or any(name == "scripted" for name in call_providers)
    if not use_real_ai:
        scripted_used = True
    real_used = bool(use_real_ai) and (not scripted_used) and any(name == "openai" for name in call_providers)
    input_tokens = sum(int(c.input_tokens) for c in calls)
    output_tokens = sum(int(c.output_tokens) for c in calls)
    estimated = sum((c.estimated_cost for c in calls), Decimal("0"))
    actual = sum((c.actual_cost for c in calls), Decimal("0"))
    if pipeline is not None:
        estimated = pipeline.estimated_cost
        actual = pipeline.actual_cost

    placement_attempted = False if pipeline is None else pipeline.placement_attempted
    paper_contamination = bool(leaks) or (pipeline.paper_contamination if pipeline else False)
    write_tools = set(FORBIDDEN) | {"review_equity_order"}
    placement_tools = [t for t in list(refresh.tools_used) + list(instrument_calls) if t in write_tools]
    structured_invalid = any(
        (getattr(row, "classification", None) == "AI_UNAVAILABLE") or (getattr(row, "rejection_reason", None) or "").lower().find("schema") >= 0
        for row in ((pipeline.screened if pipeline else []) + (pipeline.researched if pipeline else []) + (pipeline.decisions if pipeline else []))
    )
    provider_call_attempted = bool(calls)
    if use_real_ai and pipeline_error:
        err = pipeline_error
        if any(
            token in err
            for token in ("ProviderOutage", "ProviderTimeout", "MalformedResponse", "SchemaViolation")
        ):
            provider_call_attempted = True
    provider_failed = bool(pipeline_error) or structured_invalid
    if pipeline is not None and pipeline.ai_blocked and provider_call_attempted:
        provider_failed = True
    if use_real_ai and provider_call_attempted:
        if any(name != "openai" for name in call_providers):
            provider_failed = True
        if scripted_used:
            provider_failed = True
        if structured_invalid:
            provider_failed = True

    spent_delta = quantize(budget_after.spent - budget_before.spent)
    expected_remaining = max(Decimal("0"), budget_after.cap - budget_after.spent - budget_after.reserved)
    budget_ok = (
        budget_after.cap == money(10)
        and budget_after.spent >= budget_before.spent
        and budget_after.remaining == expected_remaining
        and (not use_real_ai or spent_delta == quantize(actual) or (quantize(actual) == Decimal("0") and spent_delta == Decimal("0")))
    )
    if use_real_ai and calls and (input_tokens + output_tokens) > 0 and quantize(actual) <= Decimal("0"):
        budget_ok = False
    live_facts_ok = (
        refresh.context.current_nav is not None
        and refresh.context.cash is not None
        and refresh.context.buying_power is not None
        and float(refresh.context.current_nav) > 0
        and pipeline_error != "StaleSnapshotError"
    )
    if pipeline is None and pipeline_error and "StaleSnapshot" in (pipeline_error or ""):
        live_facts_ok = False
    if pipeline is None and pipeline_error and "MissingBrokerFacts" in (pipeline_error or ""):
        live_facts_ok = False

    fail_reasons: list[str] = []
    if ticker_symbol and not ticker_eligible:
        fail_reasons.append("CANDIDATE_INVALID")
    if ticker_symbol and ticker_eligible:
        ranked = list(pipeline.ranked) if pipeline is not None else []
        if not ranked:
            fail_reasons.append("EXPLICIT_TICKER_DROPPED")
        if use_real_ai and not provider_call_attempted:
            fail_reasons.append("EXPLICIT_TICKER_DID_NOT_REACH_AI")
    if use_real_ai and scripted_used:
        fail_reasons.append("scripted_provider_used")
    if use_real_ai and provider_failed and provider_call_attempted:
        fail_reasons.append("provider_call_failed")
    if use_real_ai and structured_invalid:
        fail_reasons.append("structured_response_invalid")
    if not budget_ok:
        fail_reasons.append("budget_accounting_failed")
    if not live_facts_ok:
        fail_reasons.append("live_account_facts_missing_or_stale")
    if paper_contamination:
        fail_reasons.append("paper_contamination")
    if placement_attempted or placement_tools:
        fail_reasons.append("broker_placement_or_cancel_attempted")
    if hits:
        fail_reasons.append("forbidden_source_hits")
    if bool(exe.get("live_trade_actions_allowed")) or bool(exe.get("auto_execution")) or LIVE_ORDER_PLACEMENT:
        fail_reasons.append("live_placement_enabled")
    if pipeline_error and "pipeline_error" not in fail_reasons:
        fail_reasons.append("pipeline_error")
    if pipeline is None and not fail_reasons:
        fail_reasons.append("pipeline_error")

    ok = not fail_reasons
    roles = dict(load_ai_config().get("roles") or {})
    models_configured = {
        "screening": (roles.get("screening") or {}).get("model"),
        "research": (roles.get("research") or {}).get("model"),
        "escalation": (roles.get("escalation") or {}).get("model"),
    }
    primary_provider = call_providers[0] if call_providers else ("openai" if use_real_ai else "scripted")
    report = {
        "ok": ok,
        "run": "live_ai_check",
        "observed_at": stamp.isoformat(),
        "provider": primary_provider,
        "models": call_models or list({m for m in models_configured.values() if m}),
        "models_configured": models_configured,
        "real_provider": real_used,
        "scripted_provider": scripted_used,
        "runtime": RuntimeMode.LIVE.value,
        "account": refresh.account.get("account_number") or expected,
        "account_used": {
            "account_number": refresh.account.get("account_number"),
            "nickname": refresh.account.get("nickname"),
            "agentic_allowed": refresh.account.get("agentic_allowed"),
        },
        "NAV": refresh.context.current_nav,
        "nav": refresh.context.current_nav,
        "cash": refresh.context.cash,
        "buying_power": refresh.context.buying_power,
        "LIVE_AI_ALLOWED": LIVE_AI_ALLOWED,
        "LIVE_PROPOSALS_ALLOWED": LIVE_PROPOSALS_ALLOWED,
        "LIVE_ORDER_PLACEMENT": LIVE_ORDER_PLACEMENT,
        "openai_endpoint": "/v1/responses",
        "ai_provider_availability": availability,
        "used_real_ai": use_real_ai,
        "provider_call_attempted": provider_call_attempted,
        "ticker": ticker_symbol or None,
        "explicit_ticker_status": explicit_ticker_status,
        "explicit_ticker_validation": explicit_ticker_validation,
        "readonly_broker_transport": transport_report,
        "budget_before": {
            "mode": budget_before.mode.value,
            "cap": float(budget_before.cap),
            "spent": float(budget_before.spent),
            "remaining": float(budget_before.remaining),
            "calls_month": budget_before.calls_month,
        },
        "estimated_cost": float(estimated),
        "actual_input_tokens": input_tokens,
        "actual_output_tokens": output_tokens,
        "actual_cost": float(actual),
        "budget_after": {
            "mode": budget_after.mode.value,
            "cap": float(budget_after.cap),
            "spent": float(budget_after.spent),
            "remaining": float(budget_after.remaining),
            "calls_month": budget_after.calls_month,
        },
        "budget_remaining": float(budget_after.remaining),
        "budget": {
            "mode": budget_after.mode.value,
            "cap": float(budget_after.cap),
            "spent": float(budget_after.spent),
            "remaining": float(budget_after.remaining),
            "calls_month": budget_after.calls_month,
        },
        "candidate": (pipeline.ranked[0] if pipeline and pipeline.ranked else None),
        "screening_result": [_row(s) for s in (pipeline.screened if pipeline else [])],
        "research_result": [_row(r) for r in (pipeline.researched if pipeline else [])],
        "portfolio_decision": [
            {"ticker": d.ticker, "action": d.action.value, "confidence": d.confidence.value, "rationale": d.rationale}
            for d in (pipeline.decisions if pipeline else [])
        ],
        "risk_gate_result": [
            {"ticker": p.ticker, "verdict": getattr(p.risk_verdict, "value", p.risk_verdict), "status": p.status.value, "reasons": list(p.risk_reasons or [])}
            for p in (pipeline.proposals if pipeline else [])
        ],
        "proposal_result": [
            {
                "proposal_id": p.proposal_id,
                "ticker": p.ticker,
                "action": p.action.value,
                "status": p.status.value,
                "placement_attempted": p.placement_attempted,
                "live_order_placement": p.live_order_placement,
            }
            for p in (pipeline.proposals if pipeline else [])
        ],
        "candidate_discovery": {
            "eligibility_count": pipeline.eligibility_count if pipeline else 0,
            "ranked": pipeline.ranked if pipeline else [],
            "rejected": pipeline.rejected if pipeline else [],
            "validations": pipeline.validations if pipeline else [],
            "diagnostic_ticker": ticker_symbol or None,
            "explicit_ticker_status": explicit_ticker_status,
            "ticker_eligible_for_ai": ticker_eligible if ticker_symbol else None,
            "observe_validation": ticker_validation.as_report() if ticker_validation is not None else None,
            "explicit_ticker_validation": explicit_ticker_validation,
            "read_only_source_calls": list(instrument_calls),
        },
        "ai_calls_made": pipeline.ai_calls if pipeline else 0,
        "recommendations": [
            {"ticker": d.ticker, "action": d.action.value, "confidence": d.confidence.value}
            for d in (pipeline.decisions if pipeline else [])
        ],
        "risk_gate": [
            {"ticker": p.ticker, "verdict": getattr(p.risk_verdict, "value", p.risk_verdict), "status": p.status.value}
            for p in (pipeline.proposals if pipeline else [])
        ],
        "live_proposals_generated": [p.proposal_id for p in (pipeline.proposals if pipeline else [])],
        "placement_attempted": placement_attempted,
        "paper_contamination": paper_contamination,
        "mcp_tools_used": list(dict.fromkeys(list(refresh.tools_used) + list(instrument_calls))),
        "mcp_not_called": list(FORBIDDEN) + ["review_equity_order"],
        "forbidden_call_hits": hits,
        "pipeline_error": pipeline_error,
        "fail_reasons": fail_reasons,
        "note": "LIVE AI check. Proposal-only. Did not call place_equity_order or cancel_equity_order.",
    }
    return redact_secrets(report)


def _fail(
    message: str,
    *,
    expected: str,
    stamp: datetime,
    tools: list | None = None,
    ticker: str | None = None,
    explicit_ticker_status: str = "NOT_REQUESTED",
    readonly_broker_transport: dict[str, Any] | None = None,
) -> dict:
    return redact_secrets(
        {
            "ok": False,
            "run": "live_ai_check",
            "observed_at": stamp.isoformat(),
            "provider": None,
            "models": [],
            "real_provider": False,
            "scripted_provider": False,
            "runtime": RuntimeMode.LIVE.value,
            "account": expected,
            "account_used": {"account_number": expected, "found": False},
            "NAV": None,
            "nav": None,
            "cash": None,
            "buying_power": None,
            "budget_before": None,
            "estimated_cost": 0,
            "actual_input_tokens": 0,
            "actual_output_tokens": 0,
            "actual_cost": 0,
            "budget_after": None,
            "budget_remaining": None,
            "provider_call_attempted": False,
            "ticker": ticker,
            "explicit_ticker_status": explicit_ticker_status,
            "explicit_ticker_validation": None,
            "readonly_broker_transport": dict(readonly_broker_transport or bootstrap_readonly_broker_runtime().as_report()),
            "candidate": None,
            "screening_result": [],
            "research_result": [],
            "portfolio_decision": [],
            "risk_gate_result": [],
            "proposal_result": [],
            "placement_attempted": False,
            "paper_contamination": "paper" in message.lower(),
            "fail_reasons": [message],
            "mcp_tools_used": list(tools or []),
            "mcp_not_called": list(FORBIDDEN),
            "note": "LIVE AI check failed closed. Did not place.",
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="LIVE AI proposal-only check. Never places.")
    parser.add_argument("--payloads", type=Path, default=None)
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--use-real-ai", action="store_true", help="Call configured OpenAI models. Requires OPENAI_API_KEY.")
    parser.add_argument("--scripted", action="store_true", help="Force the scripted provider even if OPENAI_API_KEY is set.")
    parser.add_argument(
        "--ticker",
        default=None,
        help="Diagnostic/testing only. Fetch this ticker via the live read-only MCP path and run production validation. Does not enable live order placement.",
    )
    args = parser.parse_args()
    try:
        use_real_ai = resolve_live_ai_provider_mode(use_real_ai=args.use_real_ai, scripted=args.scripted)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    root = project_root()
    payloads = _payloads_from_file(args.payloads) if args.payloads else None
    report = run_live_ai_check(
        payloads=payloads,
        root=root,
        persist=not args.no_persist,
        use_real_ai=use_real_ai,
        ticker=args.ticker,
    )
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    out_json = reports / "2026-08-30_live_ai_check.json"
    out_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md = [
        "# LIVE AI check",
        "",
        f"Observed at {report.get('observed_at')}. Proposal-only. Did not place.",
        "",
        f"**Result:** `{'PASS' if report.get('ok') else 'FAIL'}`",
        "",
        f"- provider: {report.get('provider')}",
        f"- model(s): {report.get('models')}",
        f"- real_provider: {report.get('real_provider')}",
        f"- scripted_provider: {report.get('scripted_provider')}",
        f"- runtime: {report.get('runtime')}",
        f"- account: {report.get('account')}",
        f"- NAV: {report.get('NAV')}",
        f"- cash: {report.get('cash')}",
        f"- buying_power: {report.get('buying_power')}",
        f"- used_real_ai: {report.get('used_real_ai')}",
        f"- provider_call_attempted: {report.get('provider_call_attempted')}",
        f"- ticker: {report.get('ticker')}",
        f"- explicit_ticker_status: {report.get('explicit_ticker_status')}",
        f"- explicit_ticker_validation: {report.get('explicit_ticker_validation')}",
        f"- readonly_broker_transport: {report.get('readonly_broker_transport')}",
        f"- budget_before: {report.get('budget_before')}",
        f"- estimated_cost: {report.get('estimated_cost')}",
        f"- actual_input_tokens: {report.get('actual_input_tokens')}",
        f"- actual_output_tokens: {report.get('actual_output_tokens')}",
        f"- actual_cost: {report.get('actual_cost')}",
        f"- budget_after: {report.get('budget_after')}",
        f"- budget_remaining: {report.get('budget_remaining')}",
        f"- candidate: {report.get('candidate')}",
        f"- screening result: {report.get('screening_result')}",
        f"- research result: {report.get('research_result')}",
        f"- portfolio decision: {report.get('portfolio_decision')}",
        f"- Risk Gate result: {report.get('risk_gate_result')}",
        f"- proposal result: {report.get('proposal_result')}",
        f"- placement_attempted: {report.get('placement_attempted')}",
        f"- paper_contamination: {report.get('paper_contamination')}",
        "",
        f"**MCP NOT called:** {', '.join(report.get('mcp_not_called') or [])}",
        "",
    ]
    if report.get("fail_reasons"):
        md.append("**Fail reasons:** " + "; ".join(str(x) for x in report["fail_reasons"]))
        md.append("")
    (reports / "2026-08-30_live_ai_check.md").write_text("\n".join(md), encoding="utf-8")
    print(
        json.dumps(
            {
                k: report.get(k)
                for k in (
                    "ok",
                    "provider",
                    "models",
                    "real_provider",
                    "scripted_provider",
                    "runtime",
                    "account",
                    "NAV",
                    "cash",
                    "buying_power",
                    "used_real_ai",
                    "provider_call_attempted",
                    "ticker",
                    "explicit_ticker_status",
                    "explicit_ticker_validation",
                    "readonly_broker_transport",
                    "budget_before",
                    "estimated_cost",
                    "actual_input_tokens",
                    "actual_output_tokens",
                    "actual_cost",
                    "budget_after",
                    "budget_remaining",
                    "candidate",
                    "screening_result",
                    "research_result",
                    "portfolio_decision",
                    "risk_gate_result",
                    "proposal_result",
                    "placement_attempted",
                    "paper_contamination",
                    "fail_reasons",
                )
            },
            indent=2,
            default=str,
        )
    )
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
