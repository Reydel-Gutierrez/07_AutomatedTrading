"""Deterministic job handlers. AI is optional and skipped when exhausted or unchanged."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from agentic_portfolio.discovery.live import LIVE_DISCOVERY_SKIP_REASON, LIVE_DISCOVERY_WIRED, run_live_discovery
from agentic_portfolio.adapters.portfolio_facts import live_error_code_of, redact_live_error
from agentic_portfolio.agent.activity import log_activity
from agentic_portfolio.agent.jobs import research_queue_max_items
from agentic_portfolio.agent.connection import ConnectionManager
from agentic_portfolio.agent.safety import assert_auto_execution_disabled
from agentic_portfolio.agent.session import MarketPhase, SessionSnapshot
from agentic_portfolio.live_approval import LiveApprovalEngine, LiveApprovalStore
from agentic_portfolio.live_approval.types import LiveApprovalStatus
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
    payload_fn: Callable[..., Any] | None = None
    gateway: Any = None
    research_reasoner: Any = None
    decision_reasoner: Any = None
    budget_exhausted: bool = False
    ai_allowed: bool = True
    last_refresh: Any = None
    last_context: Any = None
    executor: Any = None
    discovery_fn: Callable[..., Any] | None = None


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


def _skipped(services: AgentServices, job: str, reason: str, **extra: Any) -> dict[str, Any]:
    """Intentional skip — visible in activity/health. Never a silent OK."""
    log_activity(services.root, "JOB_SKIPPED", job=job, reason=reason)
    payload = {
        "job": job,
        "status": "SKIPPED",
        "skipped": reason,
        "placement_attempted": False,
        "LIVE_ORDER_PLACEMENT": LIVE_ORDER_PLACEMENT,
    }
    payload.update(extra)
    return payload


def _skipped_no_work(job: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "job": job,
        "status": "SKIPPED_NO_WORK",
        "skipped": extra.get("skipped") or "no_work",
        "placement_attempted": False,
        "LIVE_ORDER_PLACEMENT": LIVE_ORDER_PLACEMENT,
    }
    payload.update(extra)
    return payload


def _blocked(job: str, reason: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "job": job,
        "status": "BLOCKED",
        "skipped": reason,
        "placement_attempted": False,
        "LIVE_ORDER_PLACEMENT": LIVE_ORDER_PLACEMENT,
        "ai_calls": 0,
    }
    payload.update(extra)
    return payload


def _pipeline_worker(services: AgentServices):
    existing = getattr(services, "_pipeline_worker", None)
    if existing is not None:
        return existing

    from agentic_portfolio.agent.pipeline import ResearchQueueWorker, load_live_context

    def context_fn():
        if services.last_context is not None:
            return services.last_context
        if services.context_fn is not None:
            return services.context_fn()
        return load_live_context(Path(services.root), runtime_mode=services.runtime_mode)

    def payload_fn(candidate):
        if services.payload_fn is not None:
            return services.payload_fn(candidate)
        from agentic_portfolio.research.collect import collect_research_payload

        bound = services.connection.ensure()
        fetcher = getattr(bound, "fetcher", None)
        if fetcher is None:
            raise RuntimeError("bound Robinhood runtime has no fetcher")
        return collect_research_payload(candidate.symbol, fetcher, now=_now(services))

    worker = ResearchQueueWorker(
        Path(services.root),
        runtime_mode=services.runtime_mode,
        gateway=services.gateway,
        research_reasoner=services.research_reasoner,
        decision_reasoner=services.decision_reasoner,
        payload_fn=payload_fn,
        context_fn=context_fn,
        watch=services.watch,
        approvals=services.approvals,
        notify=services.notify,
        now_fn=services.now_fn,
    )
    services._pipeline_worker = worker
    return worker


def _research_queue(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    job = str(ctx.get("job") or "RESEARCH_QUEUE_WORKER")
    blocked, reason = _ai_blocked(services)
    if blocked:
        log_activity(services.root, "AI_SKIPPED", job=job, reason=reason)
        return _blocked(job, reason, runtime_continues=True)
    if services.gateway is None and services.research_reasoner is None:
        return {
            "job": job,
            "status": "DEGRADED",
            "skipped": "no_ai_gateway",
            "ai_calls": 0,
            "placement_attempted": False,
            "LIVE_ORDER_PLACEMENT": LIVE_ORDER_PLACEMENT,
        }
    session = ctx.get("session")
    phase = getattr(session, "phase", None)
    max_items = research_queue_max_items(phase)
    result = _pipeline_worker(services).run_cycle(job=job, max_items=max_items)
    row = result.as_dict()
    row["job"] = job
    row["max_items"] = max_items
    row["phase"] = phase.value if phase is not None else None
    return row


def _overnight_thesis(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    return _pipeline_worker(services).revalidate_watches(job="OVERNIGHT_THESIS", allow_ai=False)


def _premarket_revalidate(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    worker = _pipeline_worker(services)
    watches = worker.revalidate_watches(job="PREMARKET_THESIS_REVALIDATE", allow_ai=False)
    quotes = _quotes_for(services, [item.ticker for item in services.watch_store.active()])
    approvals = worker.revalidate_approvals(quotes=quotes, context=services.last_context)
    watches.update({"approvals_expired": approvals.get("expired", 0), "approvals_superseded": approvals.get("superseded", 0)})
    return watches


def build_handlers(services: AgentServices) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    def wrap(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def inner(ctx: dict[str, Any]) -> dict[str, Any]:
            assert_auto_execution_disabled()
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
        "POSTMARKET_EARNINGS_DISCOVERY": wrap(lambda ctx: _discover(services, ctx, job="POSTMARKET_EARNINGS_DISCOVERY", sources=["earnings_calendar", "account_positions"])),
        "PREMARKET_EVENT_DISCOVERY": wrap(lambda ctx: _discover(services, ctx, job="PREMARKET_EVENT_DISCOVERY", sources=["earnings_calendar", "account_positions", "account_watchlists"])),
        "LIVE_ORDER_RECONCILE": wrap(lambda ctx: _reconcile_orders(services, ctx)),
        "APPROVED_EXECUTION_DRAIN": wrap(lambda ctx: _drain_execution(services, ctx)),
        "WATCH_CONDITION_MONITOR": wrap(lambda ctx: _watch_conditions(services, ctx)),
        "RISK_MONITOR": wrap(lambda ctx: _risk_monitor(services, ctx)),
        "MARKET_OPEN_CONDITIONAL_VALIDATE": wrap(lambda ctx: _validate_plans(services, ctx)),
        "AI_REASSESS_IF_WARRANTED": wrap(lambda ctx: _ai_reassess(services, ctx)),
        "PREMARKET_NEWS": wrap(lambda ctx: _offhours_maintain(services, ctx, "PREMARKET_NEWS")),
        "PREMARKET_THESIS_REVALIDATE": wrap(lambda ctx: _premarket_revalidate(services, ctx)),
        "PREMARKET_PREPARE_CONDITIONAL": wrap(lambda ctx: _prepare_plans(services, ctx)),
        "POSTMARKET_CLOSE_ANALYSIS": wrap(lambda ctx: _session_analysis(services, ctx, "POSTMARKET_CLOSE_ANALYSIS")),
        "POSTMARKET_RECONCILE": wrap(lambda ctx: _refresh_account(services, ctx, job="POSTMARKET_RECONCILE")),
        "POSTMARKET_DAILY_SNAPSHOT": wrap(lambda ctx: _ok("POSTMARKET_DAILY_SNAPSHOT", snapshot=True)),
        "POSTMARKET_CANDIDATE_RANK": wrap(lambda ctx: _session_analysis(services, ctx, "POSTMARKET_CANDIDATE_RANK")),
        "LUNA_SCREEN": wrap(lambda ctx: _ai_screen(services, ctx, role="screening")),
        "TERRA_RESEARCH": wrap(lambda ctx: _research_queue(services, ctx)),
        "RESEARCH_QUEUE_WORKER": wrap(lambda ctx: _research_queue(services, ctx)),
        "THESIS_WATCH_CREATE": wrap(lambda ctx: _session_analysis(services, ctx, "THESIS_WATCH_CREATE")),
        "NEXT_SESSION_PLANS": wrap(lambda ctx: _prepare_plans(services, ctx)),
        "OVERNIGHT_NEWS": wrap(lambda ctx: _offhours_maintain(services, ctx, "OVERNIGHT_NEWS")),
        "OVERNIGHT_THESIS": wrap(lambda ctx: _overnight_thesis(services, ctx)),
        "OVERNIGHT_FUNDAMENTALS": wrap(lambda ctx: _offhours_maintain(services, ctx, "OVERNIGHT_FUNDAMENTALS")),
        "OVERNIGHT_RISK": wrap(lambda ctx: _risk_monitor(services, ctx)),
        "OVERNIGHT_WATCH_MAINTAIN": wrap(lambda ctx: _offhours_maintain(services, ctx, "OVERNIGHT_WATCH_MAINTAIN")),
        "WEEKEND_SESSION_ANALYSIS": wrap(lambda ctx: _session_analysis(services, ctx, "WEEKEND_SESSION_ANALYSIS")),
        "WEEKEND_DEEP_RESEARCH": wrap(lambda ctx: _research_queue(services, ctx)),
        "WEEKEND_PORTFOLIO_REVIEW": wrap(lambda ctx: _portfolio_review(services, ctx, "WEEKEND_PORTFOLIO_REVIEW")),
        "WEEKEND_WATCH_CONSTRUCT": wrap(lambda ctx: _session_analysis(services, ctx, "WEEKEND_WATCH_CONSTRUCT")),
        "WEEKEND_STALE_CLEANUP": wrap(lambda ctx: _watch_cleanup(services, ctx)),
        "WEEKEND_NEXT_SESSION_PREP": wrap(lambda ctx: _prepare_plans(services, ctx)),
    }


def _broker(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    try:
        if services.connection.connected and services.connection.probe():
            return _ok("BROKER_RECONNECT", connected=True, reconnected=False)
        services.connection.ensure(force=True)
        return _ok("BROKER_RECONNECT", connected=True, reconnected=True)
    except Exception as exc:  # noqa: BLE001
        return {
            "job": "BROKER_RECONNECT",
            "status": "FAIL_CLOSED",
            "connected": False,
            "reason": redact_live_error(str(exc)),
            "error_code": live_error_code_of(exc),
            "placement_attempted": False,
        }


def _expire_approvals(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    try:
        tickers = [item.ticker for item in services.watch_store.active()] + [a.ticker for a in services.approval_store.pending()]
        quotes = _quotes_for(services, tickers)
    except Exception:  # noqa: BLE001 — expiry must not die on a quote outage
        quotes = {}
    extra = _pipeline_worker(services).revalidate_approvals(quotes=quotes, context=services.last_context)
    expired = int(extra.get("expired") or 0)
    return _ok("APPROVAL_EXPIRY", expired=expired, superseded=int(extra.get("superseded") or 0))


def _watch_cleanup(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    expired = services.watch.expire_stale()
    return _ok(ctx.get("job") or "WATCH_STALE_CLEANUP", expired=len(expired))


def _refresh_account(services: AgentServices, ctx: dict[str, Any], job: str | None = None) -> dict[str, Any]:
    name = job or "LIVE_ACCOUNT_REFRESH"
    if services.refresh_fn is None and services.context_fn is None:
        return _skipped(services, name, "no_refresh")
    try:
        if services.refresh_fn is not None:
            services.last_refresh = services.refresh_fn()
            services.last_context = getattr(services.last_refresh, "context", services.last_context)
        elif services.context_fn is not None:
            services.last_context = services.context_fn()
    except Exception as exc:  # noqa: BLE001 — fail closed; keep the runtime alive
        reason = redact_live_error(str(exc))
        log_activity(services.root, "LIVE_REFRESH_FAILED", job=name, reason=reason, error_code=live_error_code_of(exc))
        return {
            "job": name,
            "status": "FAIL_CLOSED",
            "reason": reason,
            "error_code": live_error_code_of(exc),
            "placement_attempted": False,
        }
    nav = getattr(services.last_context, "current_nav", None)
    cash = getattr(services.last_context, "cash", None)
    buying_power = getattr(services.last_context, "buying_power", None)
    holdings = getattr(services.last_context, "holdings_count", None)
    return _ok(name, nav=nav, cash=cash, buying_power=buying_power, holdings_count=holdings)


def _portfolio_review(services: AgentServices, ctx: dict[str, Any], job: str) -> dict[str, Any]:
    """Closed-session observation refresh. Markets being closed does not skip MCP reads."""
    result = _refresh_account(services, ctx, job=job)
    result["watch_items"] = len(services.watch_store.active())
    result["executable_liquidity"] = False
    return result


def _quotes(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    session = _session(ctx)
    if not session.regular_hours_open:
        return _skipped(services, "QUOTE_REFRESH", "off_hours_liquidity_not_executable")
    tickers = [item.ticker for item in services.watch_store.active()]
    if services.quotes_fn is None:
        return _skipped(services, "QUOTE_REFRESH", "no_quotes_fn", tickers=tickers)
    quotes = services.quotes_fn(tickers)
    return _ok("QUOTE_REFRESH", count=len(quotes or {}))


def _discover(services: AgentServices, ctx: dict[str, Any], job: str | None = None, sources: list[str] | None = None) -> dict[str, Any]:
    name = job or ctx.get("job") or "CANDIDATE_DISCOVERY"
    session = _session(ctx) if "session" in ctx else None
    lightweight = bool(session is not None and session.phase is MarketPhase.MARKET_OPEN and name == "CANDIDATE_DISCOVERY")
    if services.discovery_fn is None and services.candidates_fn is None:
        if not LIVE_DISCOVERY_WIRED:
            return _skipped(services, name, LIVE_DISCOVERY_SKIP_REASON)
        return _skipped(services, name, "no_discovery_fn")
    if services.discovery_fn is not None:
        try:
            result = _invoke_discovery(services.discovery_fn, sources=sources, lightweight=lightweight)
        except Exception as exc:  # noqa: BLE001
            log_activity(services.root, "DISCOVERY_FAILED", job=name, reason=str(exc))
            return {
                "job": name,
                "status": "FAIL_CLOSED",
                "reason": str(exc),
                "placement_attempted": False,
                "LIVE_DISCOVERY_WIRED": LIVE_DISCOVERY_WIRED,
                "mode": "lightweight" if lightweight else "broad",
            }
        run = getattr(result, "run", None)
        extra = {}
        if run is not None:
            extra = {
                "universe_size": (run.market_session_context or {}).get("unique_universe_size"),
                "sources": list(run.sources_queried or []),
                "evaluated": len(run.symbols_evaluated or []),
                "created": len(run.candidates_created or []),
                "promoted": len(run.candidates_promoted or []),
                "rejected": len(run.candidates_rejected or []),
                "conclusion": run.conclusion,
                "errors": list(run.errors or []),
            }
        return _ok(name, LIVE_DISCOVERY_WIRED=True, mode="lightweight" if lightweight else "broad", **extra)
    return _legacy_watch_discover(services, ctx, name)


def _invoke_discovery(fn: Callable[..., Any], *, sources: list[str] | None, lightweight: bool) -> Any:
    try:
        return fn(sources=sources, lightweight=lightweight)
    except TypeError:
        return fn(sources=sources)


def _legacy_watch_discover(services: AgentServices, ctx: dict[str, Any], job: str) -> dict[str, Any]:
    rows = []
    if services.candidates_fn:
        try:
            rows = list(services.candidates_fn() or [])
        except Exception as exc:  # noqa: BLE001
            log_activity(services.root, "CANDIDATE_REJECTED", reason=f"malformed:{exc}")
            return _ok(job, rejected=1, reason=str(exc))
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
    return _ok(job, created=created, count=len(rows))


def _reconcile_orders(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    executor = services.executor
    if executor is None or getattr(executor, "broker", None) is None:
        return _skipped(services, "LIVE_ORDER_RECONCILE", "no_executor")
    from agentic_portfolio.live_execution.reconcile import reconcile_orders
    from agentic_portfolio.policy import load_account_rules

    account = str(load_account_rules()["account"]["account_number"])
    result = reconcile_orders(
        executor.store,
        executor.broker,
        account_number=account,
        root=services.root,
        now=_now(services),
        notify=services.notify,
        refresh_fn=services.refresh_fn,
        approval_store=services.approval_store,
    )
    return _ok("LIVE_ORDER_RECONCILE", **result)


def _drain_execution(services: AgentServices, ctx: dict[str, Any]) -> dict[str, Any]:
    session = _session(ctx)
    if not session.regular_hours_open:
        return _skipped(services, "APPROVED_EXECUTION_DRAIN", "off_hours_liquidity_not_executable")
    if services.executor is None:
        return _skipped(services, "APPROVED_EXECUTION_DRAIN", "no_executor")
    ran = 0
    for item in services.approval_store.all():
        if item.status not in {LiveApprovalStatus.APPROVED, LiveApprovalStatus.APPROVED_EXECUTION_DISABLED}:
            continue
        if item.placed_order:
            continue
        services.executor.execute_approved(item)
        services.approval_store.save(item)
        ran += 1
    return _ok("APPROVED_EXECUTION_DRAIN", drained=ran)


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
    items = list(services.watch_store.active())
    if not items:
        return _skipped_no_work(ctx.get("job") or "NEXT_SESSION_PLANS", prepared=0, watch_items=0, skipped="no_watch_items")
    prepared = 0
    for item in items:
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
    return _ok(ctx.get("job") or "NEXT_SESSION_PLANS", prepared=prepared, watch_items=len(items), executable_liquidity=False)


def _offhours_maintain(services: AgentServices, ctx: dict[str, Any], job: str) -> dict[str, Any]:
    extra = _pipeline_worker(services).revalidate_watches(job=job, allow_ai=False)
    extra.setdefault("executable_liquidity", False)
    extra.setdefault("placement_attempted", False)
    extra.setdefault("LIVE_ORDER_PLACEMENT", LIVE_ORDER_PLACEMENT)
    return extra


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
