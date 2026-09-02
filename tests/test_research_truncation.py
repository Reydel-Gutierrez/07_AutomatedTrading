"""Terra structured-output truncation: concise prompt, one budget-gated retry, fail closed."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from agentic_portfolio.agent.jobs import MARKET_OPEN_DEEP_RESEARCH_PER_CYCLE, research_queue_max_items
from agentic_portfolio.agent.session import MarketPhase
from agentic_portfolio.ai.budget import BudgetManager
from agentic_portfolio.ai.config import load_ai_config
from agentic_portfolio.ai.errors import BudgetDenied, MalformedResponse
from agentic_portfolio.ai.gateway import AIGateway
from agentic_portfolio.ai.ledger import UsageLedger
from agentic_portfolio.ai.providers.base import ProviderResponse
from agentic_portfolio.ai.reasoners import GatewayResearchReasoner
from agentic_portfolio.decision.reasoner import ScriptedDecisionReasoner
from agentic_portfolio.research.reasoner import CONCISE_RETRY_INSTRUCTION, REASONER_INSTRUCTIONS
from agentic_portfolio.research.types import ResearchReasoningRequest
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, RuntimeMode
from agentic_portfolio.schemas import ResearchQueueStatus, ThesisStatus
from tests.test_ai_gateway import SCREEN
from tests.test_decision import _payload as _decision_payload
from tests.test_production_pipeline import _seed, _worker
from tests.test_research import _ai

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


class ResearchTruncationProvider:
    """OpenAI stand-in: screening succeeds; Terra can truncate then complete."""

    name = "openai"

    def __init__(
        self,
        *,
        report: dict,
        screen: dict | None = None,
        fail_times: int = 0,
        other_malformed: str | None = None,
        schema_fail_first: bool = False,
    ) -> None:
        self.report = report
        self.screen = screen or {**SCREEN, "ticker": report.get("ticker") or "CVX"}
        self.fail_times = fail_times
        self.other_malformed = other_malformed
        self.schema_fail_first = schema_fail_first
        self.calls: list = []
        self.research_attempts = 0

    def available(self) -> bool:
        return True

    def complete(self, request) -> ProviderResponse:
        self.calls.append(request)
        if request.schema_name == "screening":
            payload = self.screen
        elif request.schema_name == "research_report":
            self.research_attempts += 1
            if self.other_malformed and self.research_attempts == 1:
                raise MalformedResponse(self.other_malformed)
            if self.schema_fail_first and self.research_attempts == 1:
                payload = {**self.report, "research_conclusion": None}
            elif self.research_attempts <= self.fail_times:
                raise MalformedResponse("OpenAI response incomplete (max_output_tokens)")
            else:
                payload = self.report
        else:
            raise KeyError(f"unexpected schema {request.schema_name}")
        return ProviderResponse(
            payload=copy.deepcopy(payload),
            input_tokens=80,
            output_tokens=40,
            model=request.model,
            provider=self.name,
        )


def _gw(tmp_path, provider, *, spent=None) -> AIGateway:
    cfg = load_ai_config()
    ledger = UsageLedger(tmp_path, config=cfg)
    if spent is not None:
        data = ledger.load_month(now=NOW)
        data["spent"] = str(spent)
        ledger.save_month(data)
    budget = BudgetManager(ledger, cfg, now_fn=lambda: NOW)
    return AIGateway(budget=budget, providers={"openai": provider}, config=cfg, runtime_mode=RuntimeMode.LIVE)


def _request(symbol="CVX") -> ResearchReasoningRequest:
    return ResearchReasoningRequest(
        candidate={"symbol": symbol},
        packet={"facts": [], "derived_metrics": []},
        portfolio_context={},
        policy_context={},
        sleeve_questions=[],
        instructions=REASONER_INSTRUCTIONS,
        comparison_peers=[],
    )


def _research_calls(provider: ResearchTruncationProvider):
    return [c for c in provider.calls if c.schema_name == "research_report"]


def _joined(messages) -> str:
    return "\n".join(m.get("content") or "" for m in messages)


def test_research_reasoner_instructions_require_concise_output():
    text = REASONER_INSTRUCTIONS
    assert "executive_summary: <= 120 words" in text
    assert "individual analysis string fields: <= 80 words each" in text
    assert "bull/base/bear summaries: <= 80 words each" in text
    assert "assessment reasoning: <= 80 words" in text
    assert "recommended_next_step: <= 60 words" in text
    assert "maximum 5 entries each" in text
    assert "ai_interpretations: maximum 5 entries" in text
    assert "evidence_refs: include only directly relevant evidence IDs" in text
    assert "Do not repeat the same fact across multiple sections" in text
    assert "empty strings or empty lists" in text
    for conclusion in ("ADVANCE_TO_THESIS", "KEEP_WATCHING", "REJECT", "NEED_MORE_DATA"):
        assert conclusion in text
    assert "confidence" in text
    assert "TEMPORARY_PRICE_DISLOCATION" in text
    assert "invalidation" in text.lower()
    assert CONCISE_RETRY_INSTRUCTION
    assert "schema validation" in CONCISE_RETRY_INSTRUCTION.lower() or "incomplete" in CONCISE_RETRY_INSTRUCTION.lower()


def test_research_token_ceiling_and_budget_cap_unchanged():
    cfg = load_ai_config()
    assert cfg["roles"]["research"]["default_max_output_tokens"] == 4000
    assert cfg["budget"]["monthly_cap"] == 10.0
    assert cfg["budget"]["hard_stop"] == 10.0
    assert research_queue_max_items(MarketPhase.MARKET_OPEN) == 1
    assert MARKET_OPEN_DEEP_RESEARCH_PER_CYCLE == 1
    assert LIVE_ORDER_PLACEMENT is False


def test_successful_normal_terra_response_does_not_retry(tmp_path):
    provider = ResearchTruncationProvider(report=_ai("CVX", conclusion="KEEP_WATCHING"), fail_times=0)
    reasoner = GatewayResearchReasoner(_gw(tmp_path, provider))
    payload = reasoner.reason(_request())
    calls = _research_calls(provider)
    assert len(calls) == 1
    assert calls[0].max_output_tokens == 4000
    assert CONCISE_RETRY_INSTRUCTION not in _joined(calls[0].messages)
    assert REASONER_INSTRUCTIONS in _joined(calls[0].messages)
    assert payload["research_conclusion"] == "KEEP_WATCHING"
    assert reasoner.truncation_retry_used is False


def test_max_output_tokens_incomplete_retries_once_then_succeeds(tmp_path):
    provider = ResearchTruncationProvider(report=_ai("CVX", conclusion="KEEP_WATCHING"), fail_times=1)
    reasoner = GatewayResearchReasoner(_gw(tmp_path, provider))
    payload = reasoner.reason(_request())
    calls = _research_calls(provider)
    assert provider.research_attempts == 2
    assert len(calls) == 2
    assert CONCISE_RETRY_INSTRUCTION not in _joined(calls[0].messages)
    assert CONCISE_RETRY_INSTRUCTION in _joined(calls[1].messages)
    assert all(c.max_output_tokens == 4000 for c in calls)
    assert payload["research_conclusion"] == "KEEP_WATCHING"
    assert reasoner.truncation_retry_used is True


def test_second_incomplete_fails_closed_without_third_call(tmp_path):
    provider = ResearchTruncationProvider(report=_ai("CVX"), fail_times=2)
    reasoner = GatewayResearchReasoner(_gw(tmp_path, provider))
    with pytest.raises(MalformedResponse, match=r"incomplete \(max_output_tokens\)"):
        reasoner.reason(_request())
    assert provider.research_attempts == 2
    assert reasoner.last_result is None


def test_schema_invalid_keep_watching_retries_once(tmp_path):
    provider = ResearchTruncationProvider(
        report=_ai("NVDA", conclusion="KEEP_WATCHING"),
        schema_fail_first=True,
    )
    reasoner = GatewayResearchReasoner(_gw(tmp_path, provider))
    payload = reasoner.reason(_request())
    assert provider.research_attempts == 2
    assert payload["research_conclusion"] == "KEEP_WATCHING"
    assert payload.get("bull_case")
    assert reasoner.truncation_retry_used is True


def test_other_malformed_response_does_not_retry(tmp_path):
    provider = ResearchTruncationProvider(
        report=_ai("CVX"),
        fail_times=0,
        other_malformed="OpenAI response incomplete (content_filter)",
    )
    reasoner = GatewayResearchReasoner(_gw(tmp_path, provider))
    with pytest.raises(MalformedResponse, match="content_filter"):
        reasoner.reason(_request())
    assert provider.research_attempts == 1
    assert reasoner.truncation_retry_used is False


def test_budget_denial_prevents_truncation_retry(tmp_path):
    provider = ResearchTruncationProvider(report=_ai("CVX"), fail_times=1)
    gw = _gw(tmp_path, provider)
    real = gw.budget.authorize
    research_auths: list[dict] = []

    def authorize(estimated, **kwargs):
        if kwargs.get("purpose") == "deep_research":
            research_auths.append(dict(kwargs))
            if len(research_auths) > 1:
                raise BudgetDenied("retry blocked by budget")
        return real(estimated, **kwargs)

    gw.budget.authorize = authorize  # type: ignore[method-assign]
    reasoner = GatewayResearchReasoner(gw)
    with pytest.raises(BudgetDenied, match="retry blocked by budget"):
        reasoner.reason(_request())
    assert provider.research_attempts == 1
    assert len(research_auths) == 2


def test_truncated_response_is_not_persisted_and_queue_stays_retryable(tmp_path):
    _seed(tmp_path, symbol="CVX")
    provider = ResearchTruncationProvider(report=_ai("CVX", conclusion="KEEP_WATCHING"), fail_times=2)
    gw = _gw(tmp_path, provider)
    worker = _worker(tmp_path, gateway=gw)
    result = worker.run_cycle()
    assert worker.research_store.by_symbol("CVX") == []
    entry = worker.queue.all()[0]
    assert entry.status is ResearchQueueStatus.QUEUED
    assert "MalformedResponse" in (entry.last_error or "")
    assert "max_output_tokens" in (entry.last_error or "")
    assert result.status == "DEGRADED"
    assert result.reports_created == 0
    assert result.proposals_created == 0
    assert LIVE_ORDER_PLACEMENT is False
    assert worker.approvals.store.pending() == []


def test_truncation_retry_success_persists_valid_report_not_truncated_json(tmp_path):
    _seed(tmp_path, symbol="CVX")
    provider = ResearchTruncationProvider(report=_ai("CVX", conclusion="KEEP_WATCHING"), fail_times=1)
    gw = _gw(tmp_path, provider)
    worker = _worker(tmp_path, gateway=gw)
    result = worker.run_cycle()
    reports = worker.research_store.by_symbol("CVX")
    assert len(reports) == 1
    assert reports[0].research_conclusion.value == "KEEP_WATCHING"
    assert reports[0].executive_summary
    assert "truncated" not in (reports[0].executive_summary or "").lower()
    assert worker.queue.all()[0].status is not ResearchQueueStatus.QUEUED
    assert provider.research_attempts == 2
    assert result.reports_created == 1
    assert result.proposals_created == 0
    assert LIVE_ORDER_PLACEMENT is False


def test_truncation_retry_does_not_place_orders_or_skip_human_approval(tmp_path):
    _seed(tmp_path, symbol="CVX")
    provider = ResearchTruncationProvider(report=_ai("CVX", conclusion="ADVANCE_TO_THESIS"), fail_times=1)
    gw = _gw(tmp_path, provider)
    worker = _worker(
        tmp_path,
        gateway=gw,
        decision=ScriptedDecisionReasoner(_decision_payload("CVX", decision="BUY", alloc=5.0)),
    )
    result = worker.run_cycle()
    pending = worker.approvals.store.pending()
    assert result.proposals_created == 1
    assert pending
    assert pending[0].proposed_action == "BUY"
    assert pending[0].placed_order is False
    assert pending[0].broker_submitted is False
    assert pending[0].status.value == "PENDING"
    theses = worker.theses.all_records()
    assert theses
    assert theses[0].status is ThesisStatus.DRAFT
    journal = (tmp_path / "logs").rglob("*.jsonl")
    blob = "".join(p.read_text(encoding="utf-8") for p in journal if p.exists())
    assert "place_equity_order" not in blob
    assert "cancel_equity_order" not in blob
    assert LIVE_ORDER_PLACEMENT is False
    assert gw.budget.status().spent <= Decimal("10")
    assert gw.budget.status().cap == Decimal("10")
