"""Deterministic job handlers. AI is optional and skipped when exhausted or unchanged."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from agentic_portfolio.agent.activity import log_activity
from agentic_portfolio.agent.connection import ConnectionManager
from agentic_portfolio.agent.safety import assert_execution_disabled
from agentic_portfolio.agent.session import MarketPhase, SessionSnapshot
from agentic_portfolio.live_approval import LiveApprovalEngine, LiveApprovalStore
from agentic_portfolio.notify import NotificationEngine, NotificationKind
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, RuntimeMode
from agentic_portfolio.watch import ReassessTrigger, WatchEngine, WatchStatus, WatchStore, context_hash


@dataclass
class AgentServices:
    root: Any
    runtime_mode: RuntimeMode
    watch: WatchEngine
    watch_store: WatchStore
    approvals: LiveApprovalEngine
    approval_store: LiveApprovalStore
    notify: NotificationEngine
    connection: ConnectionManager
    now_fn: Callable[[], datetime]
    snapshots_fn: Callable[[], list[Any]] | None = None
    refresh_fn: Callable[..., Any] | None = None
    context_fn: Callable[[], Any] | None = None
    quotes_fn: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None
    risk_fn: Callable[..., Any] | None = None
    ai_status_fn: Callable[[], dict[str, Any]] | None = None
    ai_call_fn: Callable[..., dict[str, Any]] | None = None
    candidates_fn: Callable[[], list[dict[str, Any]]] | None = None
    budget_exhausted: bool = False
    ai_allowed: bool = True
    last_refresh: Any = None
    last_context: Any = None


def _now(services: AgentServices) -> datetime:
    stamp = services.now_fn()
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp


def _session(ctx: Mapping[str, Any]) -> SessionSnapshot:
    return ctx["session"]


def _ok(job: str, **extra: Any) -> dict[str, Any]:
    payload = {"job": job, "status": "OK", "placement_attempted": False, "LIVE_ORDER_PLACEMENT": LIVE_ORDER_PLACEMENT}
    payload.update(extra)
    return payload


def build_handlers(services: AgentServices) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    def wrap(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def inner(ctx: dict[str, Any]) -> dict[str, Any]:
            assert_execution_disabled()
            return fn(ctx)

        return inner

    return {
        "HEARTBEAT": wrap(lambda ctx: _ok("HEARTBEAT")),
        "BROKER_RECONNECT": wrap(lambda ctx: _broker(services, ctx)),
        "APPROVAL_EXPIRY": wrap(lambda ctx: _expire_approvals(services, ctx)),
        "WATCH_STALE_CLEANUP": wrap(lambda ctx: _watch_cleanup(services, ctx)),
        "LIVE_ACCOUNT_REFRESH": wrap(lambda ctx: _refresh_account(services, ctx)),
        "POSITION_MONITOR": wrap(lambda ctx: _refresh_account(services, ctx, job="POSITION_MONITOR")),
        "QUOTE_REFRESH": wrap(lambda ctx: _quotes(services, ctx)),
        "CANDIDATE_DISCOVERY": wrap(lambda ctx: _discover(services, ctx)),
        "WATCH_CONDITION_MONITOR": wrap(lambda ctx: _watch_conditions(services, ctx)),
        "RISK_MONITOR": wrap(lambda ctx: _risk_monitor(services, ctx)),
        "MARKET_OPEN_CONDITIONAL_VALIDATE": wrap(lambda ctx: _validate_plans(services, ctx)),
        "AI_REASSESS_IF_WARRANTED": wrap(lambda ctx: _ai_reassess(services, ctx)),
        "PREMARKET_NEWS": wrap(lambda ctx: _offhours_maintain(services, ctx, "PREMARKET_NEWS")),
        "PREMARKET_THESIS_REVALIDATE": wrap(lambda ctx: _offhours_maintain(services, ctx, "PREMARKET_THESIS_REVALIDATE")),
        "PREMARKET_PREPARE_CONDITIONAL": wrap(lambda ctx: _prepare_plans(services, ctx)),
        "POSTMARKET_CLOSE_ANALYSIS": wrap(lambda ctx: _session_analysis(services, ctx, "POSTMARKET_CLOSE_ANALYSIS")),
        "POSTMARKET_RECONCILE": wrap(lambda ctx: _refresh_account(services, ctx, job="POSTMARKET_RECONCILE")),
        "POSTMARKET_DAILY_SNAPSHOT": wrap(lambda ctx: _ok("POSTMARKET_DAILY_SNAPSHOT", snapshot=True)),
        "POSTMARKET_CANDIDATE_RANK": wrap(lambda ctx: _session_analysis(services, ctx, "POSTMARKET_CANDIDATE_RANK")),
        "LUNA_SCREEN": wrap(lambda ctx: _ai_screen(services, ctx, role="screening")),
        "TERRA_RESEARCH": wrap(lambda ctx: _ai_screen(services, ctx, role="research")),
        "THESIS_WATCH_CREATE": wrap(lambda ctx: _session_analysis(services, ctx, "THESIS_WATCH_CREATE")),
        "NEXT_SESSION_PLANS": wrap(lambda ctx: _prepare_plans(services, ctx)),
        "OVERNIGHT_NEWS": wrap(lambda ctx: _offhours_maintain(services, ctx, "OVERNIGHT_NEWS")),
        "OVERNIGHT_THESIS": wrap(lambda ctx: _offhours_maintain(services, ctx, "OVERNIGHT_THESIS")),
        "OVERNIGHT_FUNDAMENTALS": wrap(lambda ctx: _offhours_maintain(services, ctx, "OVERNIGHT_FUNDAMENTALS")),
        "OVERNIGHT_RISK": wrap(lambda ctx: _risk_monitor(services, ctx)),
        "OVERNIGHT_WATCH_MAINTAIN": wrap(lambda ctx: _offhours_maintain(services, ctx, "OVERNIGHT_WATCH_MAINTAIN")),
        "WEEKEND_SESSION_ANALYSIS": wrap(lambda ctx: _session_analysis(services, ctx, "WEEKEND_SESSION_ANALYSIS")),
        "WEEKEND_DEEP_RESEARCH": wrap(lambda ctx: _ai_screen(services, ctx, role="research")),
        "WEEKEND_PORTFOLIO_REVIEW": wrap(lambda ctx: _offhours_maintain(services, ctx, "WEEKEND_PORTFOLIO_REVIEW")),
        "WEEKEND_WATCH_CONSTRUCT": wrap(lambda ctx: _session_analysis(services, ctx, "WEEKEND_WATCH_CONSTRUCT")),
        "WEEKEND_STALE_CLEANUP": wrap(lambda ctx: _watch_cleanup(services, ctx)),
        "WEEKEND_NEXT_SESSION_PREP": wrap(lambda ctx: _prepare_plans(services, ctx)),
    }


def _broker(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    try:
        services.connection.ensure(force=not services.connection.connected)
        return _ok("BROKER_RECONNECT", connected=True)
    except Exception as exc:  # noqa: BLE001
        return {"job": "BROKER_RECONNECT", "status": "FAIL_CLOSED", "connected": False, "reason": str(exc), "placement_attempted": False}


def _expire_approvals(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    expired = services.approvals.expire_due()
    for item in expired:
        log_activity(services.root, "APPROVAL_EXPIRED", ticker=item.ticker, approval_id=item.approval_id)
    return _ok("APPROVAL_EXPIRY", expired=len(expired))


def _watch_cleanup(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    expired = services.watch.expire_stale()
    return _ok(ctx.get("job") or "WATCH_STALE_CLEANUP", expired=len(expired))


def _refresh_account(services: AgentServices, ctx: dict[str, Any], job: str | None = None) -> dict[str, Any]:
    name = job or "LIVE_ACCOUNT_REFRESH"
    if services.refresh_fn is None and services.context_fn is None:
        return _ok(name, skipped="no_refresh")
    try:
        if services.refresh_fn is not None:
            services.last_refresh = services.refresh_fn()
            services.last_context = getattr(services.last_refresh, "context", services.last_context)
        elif services.context_fn is not None:
            services.last_context = services.context_fn()
    except Exception as exc:  # noqa: BLE001
        return {"job": name, "status": "FAIL_CLOSED", "reason": str(exc), "placement_attempted": False}
    nav = getattr(services.last_context, "current_nav", None)
    return _ok(name, nav=nav)


def _quotes(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    session = _session(ctx)
    if not session.regular_hours_open:
        return _ok("QUOTE_REFRESH", skipped="off_hours_liquidity_not_executable")
    tickers = [item.ticker for item in services.watch_store.active()]
    if services.quotes_fn is None:
        return _ok("QUOTE_REFRESH", tickers=tickers, skipped="no_quotes_fn")
    quotes = services.quotes_fn(tickers)
    return _ok("QUOTE_REFRESH", count=len(quotes or {}))


def _discover(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    rows = []
    if services.candidates_fn:
        try:
            rows = list(services.candidates_fn() or [])
        except Exception as exc:  # noqa: BLE001 — malformed candidate data must not kill runtime
            log_activity(services.root, "CANDIDATE_REJECTED", reason=f"malformed:{exc}")
            return _ok("CANDIDATE_DISCOVERY", rejected=1, reason=str(exc))
    created = 0
    session = _session(ctx)
    for raw in rows:
        if not isinstance(raw, dict):
            log_activity(services.root, "CANDIDATE_REJECTED", reason="malformed_candidate")
            continue
        ticker = str(raw.get("ticker") or raw.get("symbol") or "").upper()
        if not ticker or ticker in {"NONE", "NULL", "TEST", "FAKE"}:
            log_activity(services.root, "CANDIDATE_REJECTED", ticker=ticker or "UNKNOWN", reason="invalid_synthetic")
            continue
        item = services.watch.upsert_from_candidate(
            ticker=ticker,
            score=raw.get("score"),
            security_identity=raw.get("security_identity") or raw.get("name"),
            security_type=raw.get("security_type") or "equity",
            thesis=raw.get("thesis"),
            confidence=raw.get("confidence"),
            reasons=list(raw.get("reasons") or []),
            risks=list(raw.get("risks") or []),
            screening=dict(raw.get("screening") or {}),
            last_price=raw.get("price") or raw.get("last_price"),
            session_id=session.latest_completed_session,
            next_session_id=session.next_session_id,
            off_hours=session.phase is not MarketPhase.MARKET_OPEN,
            status=WatchStatus.DISCOVERED if not raw.get("thesis") else WatchStatus.WATCH,
            context={"ticker": ticker, "score": raw.get("score"), "thesis": raw.get("thesis")},
        )
        created += 1
        log_activity(services.root, "CANDIDATE_DISCOVERED", ticker=item.ticker, watch_id=item.watch_id)
    return _ok("CANDIDATE_DISCOVERY", created=created, count=len(rows))


def _session_analysis(services: AgentServices, ctx: dict[str, Any], job: str) -> dict[str, Any]:
    """Latest-session analysis → persistent watch items. Never treats off-hours quotes as executable."""
    session = _session(ctx)
    discovered = _discover(services, ctx)
    created = int(discovered.get("created") or 0)
    for item in services.watch_store.active():
        if item.conditional_plan is None:
            services.watch.upsert_from_candidate(
                ticker=item.ticker,
                score=item.source_candidate_score,
                thesis=item.research_thesis,
                last_price=item.last_price,
                session_id=session.latest_completed_session,
                next_session_id=session.next_session_id,
                off_hours=True,
                status=WatchStatus.WAITING_FOR_OPEN,
            )
            log_activity(services.root, "THESIS_UPDATED", ticker=item.ticker, watch_id=item.watch_id)
    return _ok(job, created=created, latest_completed_session=session.latest_completed_session, executable_liquidity=False)


def _prepare_plans(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    session = _session(ctx)
    prepared = 0
    for item in services.watch_store.active():
        services.watch.upsert_from_candidate(
            ticker=item.ticker,
            score=item.source_candidate_score,
            thesis=item.research_thesis,
            last_price=item.last_price,
            session_id=session.latest_completed_session,
            next_session_id=session.next_session_id,
            off_hours=True,
            status=WatchStatus.WAITING_FOR_OPEN,
        )
        prepared += 1
    return _ok(ctx.get("job") or "NEXT_SESSION_PLANS", prepared=prepared, executable_liquidity=False)


def _offhours_maintain(services: AgentServices, ctx: dict[str, Any], job: str) -> dict[str, Any]:
    count = len(services.watch_store.active())
    return _ok(job, watch_items=count, executable_liquidity=False)


def _quotes_for(services: AgentServices, tickers: list[str]) -> dict[str, dict[str, Any]]:
    if services.quotes_fn is None:
        return {}
    return dict(services.quotes_fn(tickers) or {})


def _cash_available(services: AgentServices, dollars: float | None) -> bool:
    ctx = services.last_context
    cash = getattr(ctx, "cash", None) if ctx is not None else None
    if cash is None:
        return True
    if dollars is None:
        return float(cash) > 0
    return float(cash) >= float(dollars)


def _risk_pass(services: AgentServices, item, quote: dict[str, Any]) -> bool | None:
    if services.risk_fn is None:
        return True
    try:
        result = services.risk_fn(item, quote, services.last_context)
    except Exception:  # noqa: BLE001
        return False
    if result is True or result is False:
        return result
    verdict = getattr(result, "verdict", None)
    if verdict is None and isinstance(result, dict):
        verdict = result.get("verdict")
    value = getattr(verdict, "value", verdict)
    return str(value or "").upper() == "PASS"


def _watch_conditions(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    return _validate_plans(services, ctx, job="WATCH_CONDITION_MONITOR")


def _validate_plans(services: AgentServices, ctx: dict[str, Any], job: str = "MARKET_OPEN_CONDITIONAL_VALIDATE") -> dict[str, Any]:
    session = _session(ctx)
    created = 0
    failed = 0
    skipped = 0
    if not session.regular_hours_open:
        return _ok(job, skipped="off_hours_liquidity_not_executable", create_approval=False)
    tickers = [item.ticker for item in services.watch_store.active()]
    quotes = _quotes_for(services, tickers)
    for item in services.watch_store.active():
        if item.conditional_plan is None and item.status not in {WatchStatus.WAITING_FOR_OPEN, WatchStatus.READY_FOR_RISK_GATE, WatchStatus.WATCH}:
            continue
        quote = quotes.get(item.ticker) or {}
        price = quote.get("price") or quote.get("last")
        spread = quote.get("spread_bps")
        volume = quote.get("dollar_volume")
        adverse = bool(quote.get("adverse_catalyst"))
        result = services.watch.evaluate_conditions(
            item,
            regular_hours_open=True,
            price=price,
            spread_bps=spread,
            dollar_volume=volume,
            adverse_catalyst=adverse,
            cash_available=_cash_available(services, item.conditional_plan.max_price if item.conditional_plan else None),
            risk_pass=_risk_pass(services, item, quote),
        )
        if not result.get("create_approval"):
            failed += 1
            if "risk_gate" in str(result.get("reason") or ""):
                log_activity(services.root, "RISK_GATE_REJECTED", ticker=item.ticker, watch_id=item.watch_id)
            log_activity(services.root, "WATCH_CONDITION_TRIGGERED", ticker=item.ticker, ok=False, reason=result.get("reason"))
            continue
        approval = services.approvals.create(
            ticker=item.ticker,
            proposed_action="BUY",
            proposed_dollar_amount=quote.get("proposed_dollar_amount"),
            proposed_allocation_pct=quote.get("proposed_allocation_pct"),
            reason="Conditional next-session plan passed live regular-hours confirmation.",
            ai_rationale=item.research_thesis,
            supporting_thesis=item.research_thesis,
            current_quote=price,
            current_spread_bps=spread,
            risk_gate_result={"verdict": "PASS"},
            portfolio_impact={"cash_check": True},
            watch_id=item.watch_id,
        )
        services.watch.set_status(item, WatchStatus.APPROVAL_REQUIRED, reason="approval_created")
        item.approval_id = approval.approval_id
        services.watch_store.save(item)
        services.notify.emit(
            NotificationKind.APPROVAL_REQUIRED,
            title=f"TRADE APPROVAL REQUIRED — {item.ticker}",
            body=f"{item.ticker} is ready for human approval. Approving does not place an order.",
            payload={"approval_id": approval.approval_id, "ticker": item.ticker},
        )
        log_activity(services.root, "APPROVAL_CREATED", ticker=item.ticker, approval_id=approval.approval_id)
        created += 1
        skipped += 0
    return _ok(job, approvals_created=created, failed=failed, executable_liquidity=True)


def _risk_monitor(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    ctx_obj = services.last_context
    state = getattr(ctx_obj, "risk_state", None)
    if state and str(state).upper() in {"HALTED", "CRITICAL"}:
        services.notify.emit(
            NotificationKind.RISK_ALERT,
            title="Risk alert",
            body=f"Portfolio risk state is {state}.",
            payload={"risk_state": str(state)},
        )
    return _ok(ctx.get("job") or "RISK_MONITOR", risk_state=str(state) if state else None)


def _ai_blocked(services: AgentServices) -> tuple[bool, str]:
    if services.budget_exhausted or (services.ai_status_fn and (services.ai_status_fn() or {}).get("mode") == "EXHAUSTED"):
        return True, "budget_exhausted"
    if not services.ai_allowed:
        return True, "ai_disabled"
    return False, ""


def _ai_reassess(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    blocked, reason = _ai_blocked(services)
    spent = 0
    skipped = 0
    session = _session(ctx)
    quotes = _quotes_for(services, [item.ticker for item in services.watch_store.active()])
    for item in services.watch_store.active():
        quote = quotes.get(item.ticker) or {}
        price = quote.get("price")
        headlines = list(quote.get("headlines") or [])
        triggers = services.watch.detect_triggers(
            item,
            price=price,
            headlines=headlines,
            risk_state=str(getattr(services.last_context, "risk_state", "") or "") or None,
            regular_hours_open=session.regular_hours_open,
            earnings_update=bool(quote.get("earnings_update")),
            fundamental_update=bool(quote.get("fundamental_update")),
        )
        context = {"ticker": item.ticker, "price": price, "headlines": headlines, "thesis": item.research_thesis}
        allow, why = services.watch.should_spend_ai(
            item,
            context=context,
            triggers=triggers,
            ai_allowed=not blocked,
            budget_exhausted=blocked,
        )
        if not allow:
            skipped += 1
            services.watch.mark_reassessed(item, price=price, headlines=headlines)
            if blocked:
                log_activity(services.root, "AI_SKIPPED", ticker=item.ticker, reason=reason or why)
            continue
        if services.ai_call_fn is None:
            services.watch.mark_ai_spent(item, context=context, cost=0.0)
            spent += 1
            continue
        result = services.ai_call_fn(item, context, triggers)
        services.watch.mark_ai_spent(item, context=context, cost=float((result or {}).get("cost") or 0), context_id=(result or {}).get("context_id"))
        spent += 1
        if result and result.get("thesis"):
            item.research_thesis = str(result["thesis"])
            services.watch_store.save(item)
            log_activity(services.root, "THESIS_UPDATED", ticker=item.ticker)
    return _ok("AI_REASSESS_IF_WARRANTED", ai_calls=spent, skipped=skipped, budget_exhausted=blocked)


def _ai_screen(services: AgentServices, ctx: dict[str, Any], *, role: str) -> dict[str, Any]:
    blocked, reason = _ai_blocked(services)
    job = ctx.get("job") or "LUNA_SCREEN"
    if blocked:
        log_activity(services.root, "AI_SKIPPED", job=job, reason=reason)
        kind = NotificationKind.AI_BUDGET_EXHAUSTED if reason == "budget_exhausted" else NotificationKind.AI_BUDGET_CRITICAL
        if reason == "budget_exhausted":
            services.notify.emit(kind, title="AI budget exhausted", body="Runtime continues without external AI calls.", payload={"job": job})
        return _ok(job, ai_calls=0, skipped=reason, runtime_continues=True)
    analysis = _session_analysis(services, ctx, job)
    analysis["role"] = role
    if services.ai_call_fn is None:
        analysis["ai_calls"] = 0
        analysis["scripted_or_skipped"] = True
        return analysis
    calls = 0
    for item in services.watch_store.active():
        context = {"ticker": item.ticker, "thesis": item.research_thesis, "role": role}
        allow, why = services.watch.should_spend_ai(item, context=context, triggers=[], ai_allowed=True, budget_exhausted=False)
        if not allow:
            continue
        result = services.ai_call_fn(item, context, [])
        services.watch.mark_ai_spent(item, context=context, cost=float((result or {}).get("cost") or 0))
        calls += 1
    analysis["ai_calls"] = calls
    return analysis
