"""AI provider billing/auth failures alert once per day instead of failing silently."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_portfolio.agent.pipeline import ResearchQueueWorker
from agentic_portfolio.ai.errors import provider_account_problem
from agentic_portfolio.notify import NotificationEngine, NotificationKind, NotificationStore
from agentic_portfolio.notify.telegram import TELEGRAM_KINDS

OUT_OF_CREDITS = (
    'OpenAI HTTP 429: {"error": {"message": "You have no credits remaining.", '
    '"type": "insufficient_quota", "code": "credit_balance_exhausted"}}'
)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (OUT_OF_CREDITS, "billing"),
        ("OpenAI HTTP 401: invalid_api_key", "auth"),
        ("OpenAI API key is not configured", "auth"),
        ('OpenAI HTTP 429: {"error": {"type": "rate_limit_exceeded"}}', None),
        ("OpenAI HTTP 503: upstream unavailable", None),
        ("OpenAI request timed out", None),
        (None, None),
    ],
)
def test_only_non_retryable_account_failures_are_classified(reason, expected):
    assert provider_account_problem(reason) == expected


def _worker(tmp_path, clock):
    engine = NotificationEngine(NotificationStore(tmp_path), now_fn=lambda: clock[0])
    worker = ResearchQueueWorker(tmp_path, runtime_mode="LIVE", notify=engine, now_fn=lambda: clock[0])
    return worker, engine


def _alerts(engine):
    return [n for n in engine.store.all() if n.kind is NotificationKind.AI_PROVIDER_UNAVAILABLE]


def test_out_of_credits_alerts_once_per_day_across_restarts(tmp_path):
    clock = [datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)]
    details = [{"symbol": "APP", "status": "FAILED", "reason": OUT_OF_CREDITS}]
    worker, engine = _worker(tmp_path, clock)

    worker._alert_provider_account_problem(details, job="RESEARCH_QUEUE_WORKER")
    alerts = _alerts(engine)
    assert len(alerts) == 1
    assert alerts[0].title == "AI provider out of credits"
    assert alerts[0].payload["problem"] == "billing"
    assert alerts[0].payload["symbol"] == "APP"

    # Next cycle 30 minutes later, after a restart and after the alert was read.
    engine.store.mark_read()
    clock[0] += timedelta(minutes=30)
    worker, engine = _worker(tmp_path, clock)
    worker._alert_provider_account_problem(details, job="RESEARCH_QUEUE_WORKER")
    assert len(_alerts(engine)) == 1

    clock[0] += timedelta(days=1)
    worker._alert_provider_account_problem(details, job="RESEARCH_QUEUE_WORKER")
    assert len(_alerts(engine)) == 2


def test_transient_failures_do_not_alert(tmp_path):
    clock = [datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)]
    worker, engine = _worker(tmp_path, clock)
    details = [
        {"symbol": "APP", "status": "FAILED", "reason": "OpenAI request timed out"},
        {"symbol": "NVDA", "status": "OK"},
    ]
    worker._alert_provider_account_problem(details, job="RESEARCH_QUEUE_WORKER")
    assert _alerts(engine) == []


def test_provider_alert_goes_to_telegram():
    assert NotificationKind.AI_PROVIDER_UNAVAILABLE in TELEGRAM_KINDS
