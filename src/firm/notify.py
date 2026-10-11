"""Telegram delivery for alert-worthy events. Best effort; never blocks the firm.

Enabled when TELEGRAM_NOTIFICATIONS is truthy and TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID are set in the Pi env file. Tokens are never logged.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Mapping

from firm.events import ALERT_TYPES, Event

log = logging.getLogger(__name__)

EMOJI = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}


def telegram_sink(environ: Mapping[str, str] | None = None):
    env = environ if environ is not None else os.environ
    enabled = str(env.get("TELEGRAM_NOTIFICATIONS", "")).strip().lower() in {"1", "true", "yes", "on"}
    token = str(env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = str(env.get("TELEGRAM_CHAT_ID") or "").strip()
    if not (enabled and token and chat):
        return None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    public = str(env.get("AGENTIC_PUBLIC_DASHBOARD_URL") or "").strip()

    def sink(event: Event) -> None:
        if event.type not in ALERT_TYPES and event.severity != "critical":
            return
        text = f"{EMOJI.get(event.severity, '')} {event.summary}".strip()
        if public:
            text += f"\n{public}"
        body = json.dumps({"chat_id": chat, "text": text[:3500], "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram delivery failed for %s: %s", event.type, type(exc).__name__)

    return sink
