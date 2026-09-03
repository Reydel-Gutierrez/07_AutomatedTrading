"""Best-effort Telegram sink for critical live notifications.

A Telegram failure must never affect trading, approvals, or runtime control flow.
Secrets stay in env; they are never written to logs or exception messages.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from agentic_portfolio.notify.types import Notification, NotificationKind

log = logging.getLogger(__name__)

TELEGRAM_TIMEOUT_SECONDS = 5.0
TELEGRAM_MAX_TEXT = 3500
TELEGRAM_API_HOST = "api.telegram.org"

TELEGRAM_KINDS = frozenset(
    {
        NotificationKind.APPROVAL_REQUIRED,
        NotificationKind.ORDER_SUBMITTED,
        NotificationKind.ORDER_FILLED,
        NotificationKind.ORDER_REJECTED,
        NotificationKind.APPROVAL_EXPIRED,
        NotificationKind.RISK_ALERT,
        NotificationKind.SERVICE_ERROR,
        NotificationKind.BROKER_CONNECTION_LOST,
        NotificationKind.AI_BUDGET_CRITICAL,
        NotificationKind.AI_BUDGET_EXHAUSTED,
    }
)

_KIND_EMOJI = {
    NotificationKind.APPROVAL_REQUIRED: "🔔",
    NotificationKind.ORDER_SUBMITTED: "📤",
    NotificationKind.ORDER_FILLED: "✅",
    NotificationKind.ORDER_REJECTED: "❌",
    NotificationKind.APPROVAL_EXPIRED: "⌛",
    NotificationKind.RISK_ALERT: "🚨",
    NotificationKind.SERVICE_ERROR: "⚠️",
    NotificationKind.BROKER_CONNECTION_LOST: "🔌",
    NotificationKind.AI_BUDGET_CRITICAL: "💸",
    NotificationKind.AI_BUDGET_EXHAUSTED: "🚫",
}

Transport = Callable[[dict[str, Any]], Any]


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def telegram_configured(environ: Mapping[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    if not _truthy(env.get("TELEGRAM_NOTIFICATIONS")):
        return False
    token = str(env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = str(env.get("TELEGRAM_CHAT_ID") or "").strip()
    return bool(token and chat_id)


def _kind_label(kind: NotificationKind) -> str:
    return kind.value.replace("_", " ")


def _payload_text(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"none", "null"}:
            return text
    return None


def _payload_number(payload: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _short(text: str, limit: int = 280) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


def _dashboard_url(raw: str | None) -> str | None:
    url = str(raw or "").strip()
    if url.startswith("https://") or url.startswith("http://"):
        return url
    return None


def format_telegram_message(notification: Notification, *, dashboard_url: str | None = None) -> str:
    kind = notification.kind
    emoji = _KIND_EMOJI.get(kind, "•")
    payload = dict(notification.payload or {})
    ticker = _payload_text(payload, "ticker", "symbol")
    action = _payload_text(payload, "action", "proposed_action")
    lines = [f"{emoji} Agentic Portfolio", _kind_label(kind), ""]

    if kind is NotificationKind.APPROVAL_REQUIRED:
        if ticker and action:
            lines.append(f"{ticker} — {action}")
        elif ticker:
            lines.append(ticker)
        elif notification.title:
            lines.append(_short(notification.title, 120))
        dollars = _payload_number(payload, "proposed_dollar_amount", "proposed_notional")
        if dollars is not None:
            lines.append(f"Proposed: ${dollars:.2f}")
        alloc = _payload_number(payload, "proposed_allocation_pct", "allocation_pct")
        if alloc is not None:
            lines.append(f"Allocation: {alloc:.1f}%")
        sleeve = _payload_text(payload, "sleeve")
        if sleeve:
            lines.append(f"Sleeve: {sleeve}")
        reason = _payload_text(payload, "reason") or (notification.body or "").strip()
        if reason:
            lines.append("")
            lines.append("Reason:")
            lines.append(_short(reason, 280))
    else:
        headline = " ".join(part for part in (ticker, action) if part)
        if headline:
            lines.append(headline)
        body = (notification.body or "").strip() or (notification.title or "").strip()
        if body:
            lines.append(_short(body, 400))

    url = _dashboard_url(dashboard_url)
    if url:
        lines.append("")
        lines.append("Open dashboard:")
        lines.append(url)
    text = "\n".join(line for line in lines).strip()
    if len(text) > TELEGRAM_MAX_TEXT:
        text = text[: TELEGRAM_MAX_TEXT - 3].rstrip() + "..."
    return text


class TelegramDeliveryError(Exception):
    """Sanitized delivery failure. Message must never contain secrets."""


def _deliver_send_message(*, bot_token: str, chat_id: str, text: str, timeout: float) -> None:
    url = f"https://{TELEGRAM_API_HOST}/bot{bot_token}/sendMessage"
    body = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        separators=(",", ":"),
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        except Exception:
            pass
        raise TelegramDeliveryError(f"HTTP {getattr(exc, 'code', '?')}") from None
    except TimeoutError:
        raise TelegramDeliveryError("timeout") from None
    except urllib.error.URLError:
        raise TelegramDeliveryError("network error") from None
    except OSError:
        raise TelegramDeliveryError("network error") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        raise TelegramDeliveryError("malformed response") from None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise TelegramDeliveryError("telegram rejected")


class TelegramNotificationSink:
    """NotificationSink that posts an allow-listed subset of events to Telegram."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        dashboard_url: str | None = None,
        timeout: float = TELEGRAM_TIMEOUT_SECONDS,
        transport: Transport | None = None,
    ) -> None:
        self._bot_token = str(bot_token or "").strip()
        self._chat_id = str(chat_id or "").strip()
        self._dashboard_url = _dashboard_url(dashboard_url)
        self._timeout = float(timeout) if timeout else TELEGRAM_TIMEOUT_SECONDS
        self._transport = transport

    def __repr__(self) -> str:
        return f"TelegramNotificationSink(timeout={self._timeout})"

    def emit(self, notification: Notification) -> None:
        try:
            kind = notification.kind
            if not isinstance(kind, NotificationKind):
                kind = NotificationKind(str(kind))
            if kind not in TELEGRAM_KINDS:
                return
            if not self._bot_token or not self._chat_id:
                return
            text = format_telegram_message(notification, dashboard_url=self._dashboard_url)
            if not text:
                return
            self._send(text)
        except Exception:
            log.warning(
                "telegram notification failed kind=%s",
                getattr(getattr(notification, "kind", None), "value", "unknown"),
            )

    def _send(self, text: str) -> None:
        payload = {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}
        if self._transport is not None:
            self._transport(payload)
            return
        _deliver_send_message(
            bot_token=self._bot_token,
            chat_id=self._chat_id,
            text=text,
            timeout=self._timeout,
        )


def telegram_sink_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    transport: Transport | None = None,
    timeout: float = TELEGRAM_TIMEOUT_SECONDS,
) -> TelegramNotificationSink | None:
    """Return a sink when Telegram is fully configured; otherwise None."""
    try:
        env = environ if environ is not None else os.environ
        if not telegram_configured(env):
            return None
        dashboard = str(env.get("AGENTIC_PUBLIC_DASHBOARD_URL") or "").strip() or None
        return TelegramNotificationSink(
            bot_token=str(env.get("TELEGRAM_BOT_TOKEN") or "").strip(),
            chat_id=str(env.get("TELEGRAM_CHAT_ID") or "").strip(),
            dashboard_url=dashboard,
            timeout=timeout,
            transport=transport,
        )
    except Exception:
        log.warning("telegram sink not attached")
        return None
