"""Broker-layer errors. Every one of them fails closed."""

from __future__ import annotations

UNREACHABLE = "ROBINHOOD_MCP_UNREACHABLE"


class BrokerUnavailable(RuntimeError):
    """The broker could not be reached or returned unusable data. Retry later; never guess."""

    def __init__(self, message: str, *, code: str = UNREACHABLE) -> None:
        super().__init__(message)
        self.code = code


class BrokerAccountMismatch(RuntimeError):
    """The connected account is not the configured Agentic account. Trips the kill switch."""


class BrokerWriteRefused(RuntimeError):
    """A write tool was requested on a read-only client, or a tool is not allowlisted."""


class AmbiguousSubmission(RuntimeError):
    """place_equity_order outcome unknown (timeout / transport error after send). Reconcile before trading."""
