from __future__ import annotations

import json

from firm import notify
from firm.events import Event


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b""


def _event(kind="ACADEMY_REPORT", severity="info", summary="📚 Academy sprint report ready for review (2026-10-14)."):
    return Event(id=1, ts="2026-10-14T12:00:00+00:00", actor="academy", type=kind, manager_id=None, symbol=None, severity=severity, summary=summary, payload={})


def test_discord_only_with_a_real_webhook_url():
    assert notify.discord_sink({}) is None
    assert notify.discord_sink({"DISCORD_WEBHOOK_URL": "https://example.com/hook"}) is None
    assert notify.discord_sink({"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/abc"}) is not None


def test_discord_posts_alerts_without_mentions(monkeypatch):
    sent = []

    def fake_urlopen(req, timeout):
        sent.append((req.full_url, json.loads(req.data)))
        return _Resp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    sink = notify.discord_sink({"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/abc", "AGENTIC_PUBLIC_DASHBOARD_URL": "https://hq.example"})
    sink(_event())
    sink(_event(kind="MANAGER_RUN"))  # routine events are not sent
    assert len(sent) == 1
    url, body = sent[0]
    assert "Academy sprint report ready" in body["content"] and body["content"].endswith("https://hq.example")
    assert body["allowed_mentions"] == {"parse": []}


def test_sinks_combines_configured_channels():
    env = {"TELEGRAM_NOTIFICATIONS": "true", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c", "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/abc"}
    assert len(notify.sinks(env)) == 2 and notify.sinks({}) == []
