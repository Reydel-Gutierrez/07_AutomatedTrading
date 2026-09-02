"""Robinhood read-only connection manager. Reconnects; fails closed on auth loss."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from agentic_portfolio.adapters.portfolio_facts import (
    LiveDataUnavailable,
    live_error_code_of,
    redact_live_error,
)
from agentic_portfolio.adapters.readonly_runtime import (
    ReadonlyBrokerRuntime,
    bootstrap_readonly_broker_runtime,
)
from agentic_portfolio.agent.activity import log_activity
from agentic_portfolio.notify import NotificationEngine, NotificationKind


class ConnectionManager:
    def __init__(
        self,
        *,
        bootstrap: Callable[..., ReadonlyBrokerRuntime] | None = None,
        notify: NotificationEngine | None = None,
        root=None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._bootstrap = bootstrap or bootstrap_readonly_broker_runtime
        self.notify = notify
        self.root = root
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.runtime: ReadonlyBrokerRuntime | None = None
        self.last_error: str | None = None
        self.connected = False
        self.last_change_at: str | None = None
        self._notified_lost = False

    def now(self) -> datetime:
        stamp = self._now()
        if stamp.tzinfo is None:
            return stamp.replace(tzinfo=timezone.utc)
        return stamp

    def snapshot(self) -> dict[str, Any]:
        error = redact_live_error(self.last_error) if self.last_error else None
        code = None
        if error:
            token = error.split(":", 1)[0].strip()
            if token.isupper() and "_" in token:
                code = token
        return {
            "connected": self.connected,
            "bound": bool(self.runtime and self.runtime.bound),
            "error": error,
            "error_code": code,
            "mode": "READ_ONLY",
            "observation_mode": "READ_ONLY",
            "last_change_at": self.last_change_at,
        }

    def ensure(self, *, environ: Mapping[str, str] | None = None, force: bool = False) -> ReadonlyBrokerRuntime:
        prior = self.connected
        try:
            self.runtime = self._bootstrap(environ=environ, force=force or self.runtime is None or not self.connected)
        except Exception as exc:  # noqa: BLE001 — fail closed, stay alive
            self.runtime = None
            self.connected = False
            self.last_error = f"{live_error_code_of(exc)}: {redact_live_error(str(exc))}"
            self.last_change_at = self.now().isoformat()
            self._lost(prior)
            raise LiveDataUnavailable(self.last_error) from exc
        bound = bool(self.runtime and self.runtime.bound)
        self.last_error = None if bound else (self.runtime.initialization_error if self.runtime else "not bound")
        if self.last_error:
            self.last_error = redact_live_error(self.last_error)
        self.connected = bound
        if bound and not prior:
            self.last_change_at = self.now().isoformat()
            self._recovered()
        elif not bound:
            self.last_change_at = self.now().isoformat()
            self._lost(prior)
        if not bound:
            raise LiveDataUnavailable(self.last_error or "authorized Robinhood MCP transport is not bound")
        return self.runtime

    def probe(self) -> bool:
        """Cheap health check. Does not re-initialize a working MCP session."""
        if not self.runtime or not self.runtime.bound:
            return False
        fetcher = self.runtime.fetcher
        if fetcher is None:
            return True
        method = getattr(fetcher, "get_accounts", None)
        if not callable(method):
            return True
        try:
            method()
            return True
        except Exception:  # noqa: BLE001 — probe failure is not itself a reconnect
            return False

    def _lost(self, prior: bool) -> None:
        log_activity(self.root, "CONNECTION_FAILURE", error=self.last_error)
        if self.notify is not None and not self._notified_lost:
            self._notified_lost = True
            self.notify.emit(
                NotificationKind.BROKER_CONNECTION_LOST,
                title="Robinhood connection lost",
                body=str(self.last_error or "fail closed"),
                payload={"fail_closed": True},
            )

    def _recovered(self) -> None:
        self._notified_lost = False
        log_activity(self.root, "CONNECTION_RECOVERY")
        if self.notify is not None:
            self.notify.emit(
                NotificationKind.BROKER_CONNECTION_RESTORED,
                title="Robinhood connection restored",
                body="Read-only transport is bound again.",
                payload={"fail_closed": False},
            )
