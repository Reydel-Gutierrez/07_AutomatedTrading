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


def test_telegram_is_the_only_channel(monkeypatch):
    sent = []

    def fake_urlopen(req, timeout):
        sent.append((req.full_url, json.loads(req.data)))
        return _Resp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    env = {"TELEGRAM_NOTIFICATIONS": "true", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c", "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/abc"}
    sinks = notify.sinks(env)
    assert len(sinks) == 1 and notify.sinks({}) == [] and not hasattr(notify, "discord_sink")
    sinks[0](_event())
    sinks[0](_event(kind="MANAGER_RUN"))  # routine events are not sent
    assert len(sent) == 1 and "api.telegram.org" in sent[0][0] and "Academy sprint report ready" in sent[0][1]["text"]


def test_morning_report_job_is_morning_only(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from firm.runtime import Service

    sent = []
    monkeypatch.setattr("firm.api.academy.morning_message", lambda day: f"report {day}")

    class FakeEvents:
        def emit(self, kind, summary, **kw):
            sent.append((kind, summary))

    for hour_utc, expected in ((13, 1), (20, 0)):  # 09:00 and 16:00 New York (EDT)
        sent.clear()
        svc = Service.__new__(Service)
        svc.clock = lambda h=hour_utc: datetime(2026, 10, 14, h, 0, tzinfo=timezone.utc)
        svc.firm = type("F", (), {"events": FakeEvents()})()
        svc._mark_daily = lambda key: None
        svc.academy_report()
        assert len(sent) == expected
