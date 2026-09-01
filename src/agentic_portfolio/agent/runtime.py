"""Long-running 24/7 Agent Runtime. Jobs are internal; the process stays alive."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from agentic_portfolio.adapters.portfolio_facts import LiveDataUnavailable, live_error_code_of, redact_live_error, watch_quotes_from_payload
from agentic_portfolio.agent.activity import log_activity
from agentic_portfolio.agent.connection import ConnectionManager
from agentic_portfolio.agent.handlers import AgentServices, build_handlers
from agentic_portfolio.agent.heartbeat import load_health, mark_offline, write_health, write_pid
from agentic_portfolio.agent.orchestrator import JobOrchestrator
from agentic_portfolio.agent.safety import assert_execution_disabled
from agentic_portfolio.agent.session import classify_market_phase
from agentic_portfolio.live.engine import refresh_live_portfolio
from agentic_portfolio.live.store import LivePortfolioStore
from agentic_portfolio.live_approval import LiveApprovalEngine, LiveApprovalStore
from agentic_portfolio.notify import NotificationEngine, NotificationKind, NotificationStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules, load_agent_config
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, RuntimeMode, get_active_runtime
from agentic_portfolio.watch import WatchEngine, WatchStore


def refresh_live_from_connection(
    connection: ConnectionManager,
    *,
    root: Path,
    now: datetime | None = None,
) -> Any:
    """Ensure the bound READ_ONLY runtime, then persist a LIVE snapshot. Never places."""
    store = LivePortfolioStore(root)
    try:
        bound = connection.ensure()
        fetcher = bound.fetcher
        if fetcher is None:
            raise LiveDataUnavailable("bound Robinhood runtime has no fetcher")
        result = refresh_live_portfolio(fetcher, now=now, root=root)
        store.clear_error()
        return result
    except Exception as exc:  # noqa: BLE001 — persist the failing layer, then fail closed
        store.save_error(live_error_code_of(exc), redact_live_error(str(exc)), observed_at=(now or datetime.now(timezone.utc)).isoformat())
        raise


class AgentRuntime:
    """while service_running: determine phase → due jobs → sleep → repeat."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        config: dict[str, Any] | None = None,
        runtime_mode: RuntimeMode | str | None = None,
        now_fn: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        stop: Callable[[], bool] | None = None,
        max_cycles: int | None = None,
        services: AgentServices | None = None,
        handlers: Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
        connection: ConnectionManager | None = None,
        budget_exhausted: bool = False,
        ai_allowed: bool = True,
    ) -> None:
        self.base = root or project_root()
        self.config = config or load_agent_config()
        self.runtime_mode = runtime_mode or get_active_runtime()
        if isinstance(self.runtime_mode, str):
            self.runtime_mode = RuntimeMode(self.runtime_mode)
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep_fn or time.sleep
        self._stop = stop
        self.max_cycles = max_cycles
        self.running = False
        self.cycles = 0
        self.started_at: str | None = None
        self.last_results: list[dict[str, Any]] = []
        self.fatal = False
        rules = load_account_rules()
        exec_cfg = dict(rules.get("execution") or {})
        assert_execution_disabled(
            live_trade_actions_allowed=bool(exec_cfg.get("live_trade_actions_allowed")),
            auto_execution=bool(exec_cfg.get("auto_execution")),
        )
        self.notify = NotificationEngine(NotificationStore(self.base), now_fn=self._now)
        self.connection = connection or ConnectionManager(notify=self.notify, root=self.base, now_fn=self._now)
        if services is None:
            watch_store = WatchStore(self.base, runtime_mode=self.runtime_mode)
            approval_store = LiveApprovalStore(self.base, runtime_mode=self.runtime_mode)
            journal = self.base / "logs" / "agent.jsonl"

            def refresh_live() -> Any:
                return refresh_live_from_connection(self.connection, root=self.base, now=self.now())

            def quotes_live(tickers: list[str]) -> dict[str, dict[str, Any]]:
                if not tickers:
                    return {}
                bound = self.connection.ensure()
                fetcher = bound.fetcher
                if fetcher is None or not hasattr(fetcher, "get_equity_quotes"):
                    raise LiveDataUnavailable("bound Robinhood runtime cannot fetch quotes")
                payload = fetcher.get_equity_quotes(list(tickers))
                return watch_quotes_from_payload(payload)

            from agentic_portfolio.ai.gateway import build_gateway
            from agentic_portfolio.live_execution import ExecutionStore, LiveOrderExecutor, bind_live_write_broker
            from agentic_portfolio.runtime import live_placement_enabled

            gateway = build_gateway(self.base, runtime_mode=self.runtime_mode, now_fn=self._now)
            exec_store = ExecutionStore(self.base, runtime_mode=self.runtime_mode)
            broker = None
            if live_placement_enabled() and self.runtime_mode is RuntimeMode.LIVE:
                broker = bind_live_write_broker(account_number=str(rules["account"]["account_number"]))
            executor = LiveOrderExecutor(
                exec_store,
                broker,
                root=self.base,
                runtime_mode=self.runtime_mode,
                context_fn=lambda: getattr(self.services, "last_context", None),
                regular_hours_fn=lambda: classify_market_phase(self.now()).regular_hours_open,
                notify=self.notify,
                now_fn=self._now,
                refresh_fn=refresh_live,
            )

            def ai_status() -> dict[str, Any]:
                status = gateway.budget.status()
                if status.mode.value == "EXHAUSTED":
                    self.services.budget_exhausted = True
                return {
                    "mode": status.mode.value,
                    "cap": float(status.cap),
                    "spent": float(status.spent),
                    "reserved": float(status.reserved),
                    "remaining": float(status.remaining),
                    "pct_used": status.pct_used,
                    "calls_month": status.calls_month,
                }

            def live_discover(sources=None, lightweight=False):
                from agentic_portfolio.discovery.live import run_live_discovery
                from agentic_portfolio.agent.pipeline import load_live_context

                bound = self.connection.ensure()
                fetcher = getattr(bound, "fetcher", None)
                if fetcher is None:
                    raise LiveDataUnavailable("bound Robinhood runtime has no fetcher")
                ctx_obj = getattr(self.services, "last_context", None)
                if ctx_obj is None:
                    ctx_obj = load_live_context(self.base, runtime_mode=self.runtime_mode)
                if ctx_obj is None:
                    raise LiveDataUnavailable("missing_live_context")
                return run_live_discovery(
                    fetcher,
                    ctx_obj,
                    root=self.base,
                    runtime_mode=self.runtime_mode,
                    source_filter=sources,
                    lightweight=lightweight,
                    now=self.now(),
                )

            services = AgentServices(
                root=self.base,
                runtime_mode=self.runtime_mode,
                watch=WatchEngine(watch_store, config=self.config, journal=journal, now_fn=self._now),
                watch_store=watch_store,
                approvals=LiveApprovalEngine(
                    approval_store,
                    config=self.config,
                    journal=journal,
                    now_fn=self._now,
                    executor=executor,
                ),
                approval_store=approval_store,
                notify=self.notify,
                connection=self.connection,
                now_fn=self._now,
                refresh_fn=refresh_live,
                quotes_fn=quotes_live,
                gateway=gateway,
                ai_status_fn=ai_status,
                budget_exhausted=budget_exhausted,
                ai_allowed=ai_allowed,
                executor=executor,
                discovery_fn=live_discover,
            )
        else:
            services.budget_exhausted = budget_exhausted or services.budget_exhausted
            services.ai_allowed = ai_allowed and services.ai_allowed
        self.services = services
        self.services.watch.reconcile_waiting_for_open_schedules()
        self.orchestrator = JobOrchestrator(
            self.base,
            config=self.config,
            handlers=dict(handlers or build_handlers(services)),
            now_fn=self._now,
        )

    def now(self) -> datetime:
        stamp = self._now()
        if stamp.tzinfo is None:
            return stamp.replace(tzinfo=timezone.utc)
        return stamp

    def request_stop(self) -> None:
        self.running = False

    def _should_stop(self) -> bool:
        if not self.running:
            return True
        if self._stop and self._stop():
            return True
        if self.max_cycles is not None and self.cycles >= self.max_cycles:
            return True
        return False

    def _budget(self) -> dict[str, Any]:
        if self.services.ai_status_fn:
            return dict(self.services.ai_status_fn())
        from agentic_portfolio.ai.budget import BudgetManager
        from agentic_portfolio.ai.config import load_ai_config
        from agentic_portfolio.ai.ledger import UsageLedger

        try:
            status = BudgetManager(UsageLedger(self.base), load_ai_config()).status()
        except Exception:  # noqa: BLE001
            return {"mode": "UNKNOWN", "cap": 10, "spent": 0, "reserved": 0, "remaining": 10, "calls_month": 0}
        if status.mode.value == "EXHAUSTED":
            self.services.budget_exhausted = True
            self.notify.emit(
                NotificationKind.AI_BUDGET_EXHAUSTED,
                title="AI budget exhausted",
                body="Runtime continues. Broker, risk, watch, and dashboard stay up. No external AI calls.",
            )
        elif status.mode.value == "CRITICAL":
            self.notify.emit(
                NotificationKind.AI_BUDGET_CRITICAL,
                title="AI budget critical",
                body=f"Spent ${float(status.spent):.2f} of ${float(status.cap):.2f}.",
            )
        return {
            "mode": status.mode.value,
            "cap": float(status.cap),
            "spent": float(status.spent),
            "reserved": float(status.reserved),
            "remaining": float(status.remaining),
            "pct_used": status.pct_used,
            "calls_month": status.calls_month,
        }

    def _openai_state(self) -> dict[str, Any]:
        if self.services.budget_exhausted:
            return {"state": "BUDGET_EXHAUSTED", "calls_allowed": False}
        gateway = getattr(self.services, "gateway", None)
        adapter = None
        if gateway is not None:
            adapter = (gateway.providers or {}).get("openai")
        ready = bool(adapter.available()) if adapter is not None else bool(os.environ.get("OPENAI_API_KEY"))
        return {"state": "READY" if ready else "UNCONFIGURED", "calls_allowed": ready}

    def cycle(self) -> list[dict[str, Any]]:
        stamp = self.now()
        session = classify_market_phase(stamp)
        try:
            results = self.orchestrator.tick(stamp)
        except Exception as exc:  # noqa: BLE001 — runtime must survive cycle failures
            log_activity(self.base, "JOB_ERROR", reason=str(exc))
            self.notify.emit(NotificationKind.SERVICE_ERROR, title="Service error", body=str(exc), payload={"fatal": False})
            results = [{"job": "CYCLE", "status": "ERROR", "reason": str(exc), "placement_attempted": False}]
        self.last_results = results
        self.cycles += 1
        budget = self._budget()
        live_error = LivePortfolioStore(self.base).last_error()
        job_skips = [
            {"job": row.get("job"), "skipped": row.get("skipped"), "status": row.get("status"), "reason": row.get("reason")}
            for row in results
            if row.get("status") == "SKIPPED" or row.get("skipped")
        ]
        write_health(
            self.base,
            started_at=self.started_at or stamp.isoformat(),
            session=session,
            last_cycle=self.orchestrator.state.get("last_cycle"),
            next_jobs=self.orchestrator.scheduled_preview(stamp),
            broker=self.connection.snapshot(),
            openai=self._openai_state(),
            budget=budget,
            cycles=self.cycles,
            runtime_mode=self.runtime_mode.value,
            config=self.config,
            live_error=live_error,
            job_skips=job_skips,
        )
        return results

    def run(self) -> None:
        assert_execution_disabled()
        self.running = True
        self.fatal = False
        self.started_at = self.now().isoformat()
        write_pid(self.base, config=self.config)
        try:
            self.connection.ensure()
        except Exception:  # noqa: BLE001 — first bind is recorded in health; runtime continues
            pass
        write_health(
            self.base,
            started_at=self.started_at,
            session=classify_market_phase(self.now()),
            last_cycle=None,
            next_jobs=self.orchestrator.scheduled_preview(self.now()),
            broker=self.connection.snapshot(),
            openai=self._openai_state(),
            budget=self._budget(),
            cycles=0,
            runtime_mode=self.runtime_mode.value,
            config=self.config,
        )
        try:
            while not self._should_stop():
                self.cycle()
                if self._should_stop():
                    break
                delay = self.orchestrator.next_job_delay_seconds(self.now())
                self._sleep(delay)
        finally:
            self.running = False
            mark_offline(self.base, config=self.config)


def run_forever(root: Path | None = None, **kwargs: Any) -> None:
    AgentRuntime(root, **kwargs).run()
