"""Telegram and Discord delivery for alert-worthy events. Best effort; never blocks the firm.

Telegram: enabled when TELEGRAM_NOTIFICATIONS is truthy and TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID are set in the Pi env file. Discord: enabled when DISCORD_WEBHOOK_URL is
set (a channel webhook URL). Tokens and webhook URLs are never logged.
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
        text = f"{EMOJI.get(event.severity, '')} The 537 Firm · {event.summary}".strip()
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


def _alert_text(event: Event, public: str) -> str:
    text = f"{EMOJI.get(event.severity, '')} The 537 Firm · {event.summary}".strip()
    return f"{text}\n{public}" if public else text


def discord_sink(environ: Mapping[str, str] | None = None):
    env = environ if environ is not None else os.environ
    url = str(env.get("DISCORD_WEBHOOK_URL") or "").strip()
    if not url.startswith("https://discord.com/api/webhooks/") and not url.startswith("https://discordapp.com/api/webhooks/"):
        return None
    public = str(env.get("AGENTIC_PUBLIC_DASHBOARD_URL") or "").strip()

    def sink(event: Event) -> None:
        if event.type not in ALERT_TYPES and event.severity != "critical":
            return
        body = json.dumps({"content": _alert_text(event, public)[:1900], "allowed_mentions": {"parse": []}}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "the-537-firm"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception as exc:  # noqa: BLE001
            log.warning("discord delivery failed for %s: %s", event.type, type(exc).__name__)

    return sink


def sinks(environ: Mapping[str, str] | None = None) -> list:
    return [s for s in (telegram_sink(environ), discord_sink(environ)) if s]
