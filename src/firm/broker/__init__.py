"""Broker access. Build the read client for the firm and the writer for the desk."""

from __future__ import annotations

import os
from typing import Mapping

from firm.broker.robinhood import RobinhoodBroker

READ_CLIENT = "agentic-firm-readonly"
WRITE_CLIENT = "agentic-firm-executor"


def live_writes_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """The owner's Pi-level master switch. Anything but an explicit true is off."""
    env = environ if environ is not None else os.environ
    return str(env.get("AGENTIC_LIVE_ORDER_PLACEMENT", "")).strip().lower() in {"1", "true", "yes", "on"}


def connect_reader(account_number: str, environ: Mapping[str, str] | None = None) -> RobinhoodBroker:
    from firm.broker.mcp_transport import connect

    return RobinhoodBroker(connect(client_name=READ_CLIENT, environ=environ), account_number=account_number)


def connect_writer(account_number: str, environ: Mapping[str, str] | None = None) -> RobinhoodBroker | None:
    """A write-capable client only when the master switch is on; otherwise None."""
    if not live_writes_enabled(environ):
        return None
    from firm.broker.mcp_transport import connect

    return RobinhoodBroker(connect(client_name=WRITE_CLIENT, environ=environ), account_number=account_number, writer=True)
