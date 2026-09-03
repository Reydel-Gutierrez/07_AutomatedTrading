"""Telegram notification sink. Never calls the live Telegram API."""

from __future__ import annotations

import io
import logging
import urllib.error
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agentic_portfolio.agent.runtime import AgentRuntime
from agentic_portfolio.notify import NotificationEngine, NotificationKind, NotificationStore
from agentic_portfolio.notify.telegram import (
    TELEGRAM_KINDS,
    TelegramNotificationSink,
    format_telegram_message,
    telegram_configured,
    telegram_sink_from_env,
)
from agentic_portfolio.notify.types import Notification
from agentic_portfolio.runtime import AUTO_EXECUTION, RuntimeMode

NOW = datetime(2026, 9, 3, 16, 0, tzinfo=timezone.utc)
TOKEN = "tg-test-token-DO-NOT-LEAK"
CHAT = "111222333"
DASHBOARD = "https://dashboard.example.test/approvals"

ALLOWED = (
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
)


class FakeTransport:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    def __call__(self, payload: dict) -> None:
        self.calls.append(dict(payload))
        if self.error is not None:
            raise self.error


def _note(kind: NotificationKind, *, title="title", body="body", payload=None) -> Notification:
    return Notification(
        notification_id=str(uuid4()),
        kind=kind,
        title=title,
        body=body,
        created_at=NOW.isoformat(),
        payload=dict(payload or {}),
    )


def _sink(*, transport=None, dashboard_url=None) -> TelegramNotificationSink:
    return TelegramNotificationSink(
        bot_token=TOKEN,
        chat_id=CHAT,
        dashboard_url=dashboard_url,
        transport=transport,
    )


def test_telegram_disabled_no_http(tmp_path, monkeypatch):
    calls = []

    def boom(*_a, **_k):
        calls.append(1)
        raise AssertionError("urlopen must not run")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    monkeypatch.setenv("TELEGRAM_NOTIFICATIONS", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    assert telegram_sink_from_env() is None
    engine = NotificationEngine(NotificationStore(tmp_path), now_fn=lambda: NOW)
    engine.emit(NotificationKind.APPROVAL_REQUIRED, title="need", body="approve MSFT")
    assert calls == []
    assert engine.store.all()


def test_missing_token_or_chat_id_no_http(tmp_path, monkeypatch):
    calls = []

    def boom(*_a, **_k):
        calls.append(1)
        raise AssertionError("urlopen must not run")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    monkeypatch.setenv("TELEGRAM_NOTIFICATIONS", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert telegram_sink_from_env() is None
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    assert telegram_sink_from_env() is None
    engine = NotificationEngine(NotificationStore(tmp_path), now_fn=lambda: NOW)
    engine.emit(NotificationKind.RISK_ALERT, title="risk", body="HALTED")
    assert calls == []
    assert len(engine.store.all()) == 1


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on", "On"])
def test_truthy_flags_enable_sink(flag):
    env = {"TELEGRAM_NOTIFICATIONS": flag, "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}
    assert telegram_configured(env) is True
    assert telegram_sink_from_env(env) is not None


def test_approval_required_sends_once():
    transport = FakeTransport()
    sink = _sink(transport=transport, dashboard_url=DASHBOARD)
    sink.emit(
        _note(
            NotificationKind.APPROVAL_REQUIRED,
            title="TRADE APPROVAL REQUIRED — MSFT",
            body="MSFT is ready for human approval. Approving does not place an order.",
            payload={
                "ticker": "MSFT",
                "action": "BUY",
                "proposed_dollar_amount": 50.0,
                "proposed_allocation_pct": 10.0,
                "sleeve": "CORE_GROWTH",
                "reason": "Quality compounder at a fair entry.",
            },
        )
    )
    assert len(transport.calls) == 1
    text = transport.calls[0]["text"]
    assert transport.calls[0]["chat_id"] == CHAT
    assert TOKEN not in text
    assert "APPROVAL REQUIRED" in text
    assert "MSFT — BUY" in text
    assert "Proposed: $50.00" in text
    assert "Allocation: 10.0%" in text
    assert "Sleeve: CORE_GROWTH" in text
    assert "Quality compounder" in text
    assert DASHBOARD in text
    assert "Agentic Portfolio" in text


def test_watch_created_is_not_sent():
    transport = FakeTransport()
    _sink(transport=transport).emit(_note(NotificationKind.WATCH_CREATED, title="watch", body="QUAL added"))
    assert transport.calls == []


def test_all_selected_kinds_are_accepted():
    assert set(ALLOWED) == set(TELEGRAM_KINDS)
    transport = FakeTransport()
    sink = _sink(transport=transport)
    for kind in ALLOWED:
        sink.emit(_note(kind, title=kind.value, body=f"body-{kind.value}", payload={"ticker": "MSFT", "action": "BUY"}))
    assert len(transport.calls) == 10


def test_disallowed_kinds_are_not_sent():
    transport = FakeTransport()
    sink = _sink(transport=transport)
    blocked = (
        NotificationKind.WATCH_CREATED,
        NotificationKind.TRADE_PROPOSAL,
        NotificationKind.RESEARCH_COMPLETED,
        NotificationKind.RESEARCH_REJECTED,
        NotificationKind.CANDIDATE_PROMOTED,
        NotificationKind.THESIS_CHANGED,
        NotificationKind.AI_BUDGET_WARNING,
        NotificationKind.RISK_GATE_BLOCKED,
        NotificationKind.BROKER_CONNECTION_RESTORED,
        NotificationKind.ORDER_CANCELED,
        NotificationKind.APPROVAL_SUPERSEDED,
    )
    for kind in blocked:
        sink.emit(_note(kind, title=kind.value, body=kind.value))
    assert transport.calls == []


def test_telegram_network_failure_does_not_raise():
    sink = _sink(transport=FakeTransport(error=OSError("dns failed")))
    sink.emit(_note(NotificationKind.SERVICE_ERROR, title="down", body="cycle failed"))


def test_telegram_http_error_does_not_raise(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            400,
            "Bad Request",
            hdrs={},
            fp=io.BytesIO(b'{"ok":false}'),
        )

    monkeypatch.setattr("agentic_portfolio.notify.telegram.urllib.request.urlopen", boom)
    sink = TelegramNotificationSink(bot_token=TOKEN, chat_id=CHAT)
    sink.emit(_note(NotificationKind.ORDER_REJECTED, title="rejected", body="broker_rejected"))


def test_engine_persists_when_telegram_sink_fails(tmp_path):
    class Boom:
        def emit(self, notification):
            raise RuntimeError("telegram down")

    store = NotificationStore(tmp_path)
    engine = NotificationEngine(store, sinks=[Boom()], now_fn=lambda: NOW)
    item = engine.emit(NotificationKind.APPROVAL_REQUIRED, title="need", body="approve MSFT")
    assert item.notification_id
    saved = store.all()
    assert len(saved) == 1
    assert saved[0].kind is NotificationKind.APPROVAL_REQUIRED
    assert saved[0].body == "approve MSFT"


def test_token_and_chat_id_not_leaked_on_http_error(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def boom(req, timeout=None):
        url = getattr(req, "full_url", "") or getattr(req, "get_full_url", lambda: "")()
        assert TOKEN in url
        raise urllib.error.HTTPError(url, 401, "Unauthorized", hdrs={}, fp=io.BytesIO(b"nope"))

    monkeypatch.setattr("agentic_portfolio.notify.telegram.urllib.request.urlopen", boom)
    sink = TelegramNotificationSink(bot_token=TOKEN, chat_id=CHAT)
    sink.emit(_note(NotificationKind.BROKER_CONNECTION_LOST, title="lost", body="fail closed"))
    blob = "\n".join(rec.getMessage() for rec in caplog.records)
    blob += "".join(str(rec.args) for rec in caplog.records)
    assert TOKEN not in blob
    assert CHAT not in blob
    assert TOKEN not in caplog.text
    assert CHAT not in caplog.text


def test_token_not_leaked_on_urlerror(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def boom(req, timeout=None):
        raise urllib.error.URLError(f"timed out contacting https://api.telegram.org/bot{TOKEN}/sendMessage")

    monkeypatch.setattr("agentic_portfolio.notify.telegram.urllib.request.urlopen", boom)
    TelegramNotificationSink(bot_token=TOKEN, chat_id=CHAT).emit(
        _note(NotificationKind.AI_BUDGET_EXHAUSTED, title="budget", body="no ai")
    )
    assert TOKEN not in caplog.text


def test_dashboard_url_included_when_configured():
    text = format_telegram_message(
        _note(NotificationKind.APPROVAL_REQUIRED, title="need", body="please approve", payload={"ticker": "MSFT"}),
        dashboard_url=DASHBOARD,
    )
    assert "Open dashboard:" in text
    assert DASHBOARD in text


def test_dashboard_url_absence_still_sends():
    transport = FakeTransport()
    _sink(transport=transport).emit(
        _note(
            NotificationKind.ORDER_FILLED,
            title="ORDER FILLED — MSFT",
            body="MSFT filled.",
            payload={"ticker": "MSFT", "action": "BUY"},
        )
    )
    assert len(transport.calls) == 1
    text = transport.calls[0]["text"]
    assert "ORDER FILLED" in text
    assert "MSFT BUY" in text
    assert "MSFT filled." in text
    assert "Open dashboard:" not in text
    assert "cloudflareaccess.com" not in text


def test_deduped_engine_event_does_not_resend_telegram(tmp_path):
    transport = FakeTransport()
    sink = _sink(transport=transport)
    engine = NotificationEngine(NotificationStore(tmp_path), sinks=[sink], now_fn=lambda: NOW)
    first = engine.emit(NotificationKind.RISK_ALERT, title="Risk alert", body="Portfolio risk state is HALTED.")
    second = engine.emit(NotificationKind.RISK_ALERT, title="Risk alert", body="Portfolio risk state is HALTED.")
    assert first.notification_id == second.notification_id
    assert len(transport.calls) == 1
    assert len(engine.store.all()) == 1


def test_runtime_attaches_sink_from_env_without_changing_safety(tmp_path, monkeypatch):
    transport = FakeTransport()
    monkeypatch.setenv("TELEGRAM_NOTIFICATIONS", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setenv("AGENTIC_PUBLIC_DASHBOARD_URL", DASHBOARD)
    monkeypatch.setattr(
        "agentic_portfolio.agent.runtime.telegram_sink_from_env",
        lambda: TelegramNotificationSink(bot_token=TOKEN, chat_id=CHAT, dashboard_url=DASHBOARD, transport=transport),
    )
    runtime = AgentRuntime(tmp_path, runtime_mode=RuntimeMode.LIVE, max_cycles=0, sleep_fn=lambda _s: None)
    assert any(isinstance(sink, TelegramNotificationSink) for sink in runtime.notify.sinks)
    assert runtime.services is not None
    assert runtime.notify.store is not None
    assert AUTO_EXECUTION is False
    runtime.notify.emit(
        NotificationKind.APPROVAL_REQUIRED,
        title="TRADE APPROVAL REQUIRED — MSFT",
        body="approve",
        payload={"ticker": "MSFT", "action": "BUY"},
    )
    assert len(transport.calls) == 1
    assert DASHBOARD in transport.calls[0]["text"]
    assert TOKEN not in transport.calls[0]["text"]


def test_runtime_without_telegram_env_has_no_sink(tmp_path):
    runtime = AgentRuntime(tmp_path, runtime_mode=RuntimeMode.LIVE, max_cycles=0, sleep_fn=lambda _s: None)
    assert runtime.notify.sinks == []
    item = runtime.notify.emit(NotificationKind.SERVICE_ERROR, title="Service error", body="boom")
    assert item.kind is NotificationKind.SERVICE_ERROR
    assert runtime.notify.store.all()
