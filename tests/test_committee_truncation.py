"""CORE committee output truncation: compact schema, stage-specific tokens, one retry."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from agentic_portfolio.ai.budget import BudgetManager
from agentic_portfolio.ai.config import committee_output_token_limits, load_ai_config
from agentic_portfolio.ai.errors import BudgetDenied, MalformedResponse
from agentic_portfolio.ai.gateway import AIGateway
from agentic_portfolio.ai.ledger import UsageLedger
from agentic_portfolio.ai.pricing import estimate_tokens
from agentic_portfolio.ai.providers.base import ProviderResponse
from agentic_portfolio.ai.reasoners import GatewayDecisionReasoner
from agentic_portfolio.ai.schemas import COMMITTEE_DECISION_SCHEMA, THESIS_DECISION_SCHEMA, validate_against_schema
from agentic_portfolio.decision.committee import reevaluate_live_core_committee
from agentic_portfolio.decision.engine import run_portfolio_decision
from agentic_portfolio.decision.reasoner import (
    COMMITTEE_CONCISE_RETRY_INSTRUCTION,
    COMMITTEE_REASONER_INSTRUCTIONS,
    REASONER_INSTRUCTIONS,
    ScriptedDecisionReasoner,
)
from agentic_portfolio.decision.types import DecisionReasoningRequest
from agentic_portfolio.decision.validate import DecisionValidationError, validate_payload
from agentic_portfolio.journal import read_jsonl
from agentic_portfolio.runtime import LIVE_ORDER_PLACEMENT, RuntimeMode
from agentic_portfolio.schemas import Decision
from tests.conftest import ctx
from tests.test_ai_gateway import _gw as _paper_gw
from tests.test_core_committee import _seed_core_universe
from tests.test_decision import _core_exit, _payload, _report, _thesis
from tests.test_openai_strict_nulls import _decision_request
from tests.test_production_pipeline import _services

NOW = datetime(2026, 9, 2, 18, 9, tzinfo=timezone.utc)
PRODUCTION_CORE9 = ["ANET", "BAC", "CRM", "LLY", "MA", "MSFT", "SOFI", "SPGI", "SYK"]
SCALE20 = [f"T{i:02d}" for i in range(1, 19)]


def _compact_payload(symbols: list[str], *, buy: str | None = None, alloc: float = 5.0) -> dict:
    names = [str(s).upper() for s in symbols]
    rankings: list[dict] = []
    rank = 1
    if buy:
        rankings.append(
            {
                "symbol": buy,
                "rank": rank,
                "action": "BUY",
                "score": 0.8,
                "confidence": "MEDIUM",
                "concise_reason": f"{buy} is the best residual versus cash.",
            }
        )
        rank += 1
    rankings.append(
        {
            "symbol": "CASH",
            "rank": rank,
            "action": "HOLD",
            "score": 0.7 if buy else 0.9,
            "confidence": "MEDIUM",
            "concise_reason": "Cash remains a valid residual.",
        }
    )
    rank += 1
    rankings.append(
        {
            "symbol": "SPY",
            "rank": rank,
            "action": "NO_ACTION",
            "score": 0.5,
            "confidence": "MEDIUM",
            "concise_reason": "Broad-market residual considered, not selected.",
        }
    )
    rank += 1
    lost_to = [buy, "CASH"] if buy else ["CASH"]
    for symbol in names:
        if symbol == buy:
            continue
        rankings.append(
            {
                "symbol": symbol,
                "rank": rank,
                "action": "WATCH",
                "score": 0.4,
                "confidence": "MEDIUM",
                "concise_reason": f"{symbol} lost the residual comparison.",
                "why_lost": "Another residual or cash ranked higher.",
                "lost_to": lost_to,
                "valuation_condition": "Reconsider if valuation improves versus alternatives.",
                "thesis_condition": "Reconsider if durability evidence strengthens.",
                "required_evidence_improvement": "Updated quality/valuation packet.",
                "next_review_reason": "committee_residual",
                "next_review_at": None,
            }
        )
        rank += 1
    selected = []
    if buy:
        selected.append(
            {
                "symbol": buy,
                "action": "BUY",
                "target_weight": alloc,
                "starter_position": True,
                "rationale": f"Starter CORE allocation to {buy}; residual cash retained.",
                "why_vs_cash": "Expected compounding exceeds cash opportunity cost at a starter size.",
                "why_vs_spy": "" if buy == "SPY" else "Concentrated quality versus generic beta at starter size.",
                "why_vs_alternatives": f"{buy} is the best residual among the qualified set.",
                "research_id": f"res-{buy}",
                "sleeve": "CORE_GROWTH",
                "thesis_summary": f"{buy} should exist as a researched core compounder.",
                "bull_case": "Quality and growth persist.",
                "base_case": "Growth decelerates but remains profitable.",
                "bear_case": "Demand rolls over.",
                "catalysts": [],
                "thesis_drivers": ["quality", "durability", "valuation"],
                "risks": ["competition"],
                "horizon": "12-24 months",
                "invalidation_conditions": ["sustained earnings deterioration"],
                "review_triggers": ["earnings", "material filing"],
                "why_position_should_exist": "Improves expected long-term growth vs idle cash.",
                "confidence": "MEDIUM",
                "exit_policy": _core_exit(),
            }
        )
    cash_pct = 100.0 - (alloc if buy else 0.0)
    return {
        "portfolio_action": "ALLOCATE" if buy else "HOLD_CASH",
        "rankings": rankings,
        "selected_allocations": selected,
        "cash": {
            "target_weight": cash_pct,
            "rationale": "Cash remains a valid residual.",
            "action": "HOLD",
        },
        "comparison": {
            "vs_cash": "Deploy only if a residual improves the book versus retaining cash.",
            "vs_spy": "Selected residual versus generic beta, or cash if no residual is justified.",
            "notes": "One coherent committee allocation.",
        },
    }


class CommitteeTruncationProvider:
    """OpenAI stand-in: committee_decision can truncate then complete. No research/Terra."""

    name = "openai"

    def __init__(self, *, payload: dict, fail_times: int = 0, other_malformed: str | None = None) -> None:
        self.payload = payload
        self.fail_times = fail_times
        self.other_malformed = other_malformed
        self.calls: list = []
        self.committee_attempts = 0
        self.research_attempts = 0

    def available(self) -> bool:
        return True

    def complete(self, request) -> ProviderResponse:
        self.calls.append(request)
        if request.schema_name == "research_report":
            self.research_attempts += 1
            raise AssertionError("committee path must not call Terra research")
        if request.schema_name != "committee_decision":
            raise KeyError(f"unexpected schema {request.schema_name}")
        self.committee_attempts += 1
        if self.other_malformed and self.committee_attempts == 1:
            raise MalformedResponse(self.other_malformed)
        if self.committee_attempts <= self.fail_times:
            raise MalformedResponse("OpenAI response incomplete (max_output_tokens)")
        return ProviderResponse(
            payload=copy.deepcopy(self.payload),
            input_tokens=120,
            output_tokens=80,
            model=request.model,
            provider=self.name,
        )


def _live_gw(tmp_path, provider, *, spent=None) -> AIGateway:
    cfg = load_ai_config()
    ledger = UsageLedger(tmp_path, config=cfg)
    if spent is not None:
        data = ledger.load_month(now=NOW)
        data["spent"] = str(spent)
        ledger.save_month(data)
    budget = BudgetManager(ledger, cfg, now_fn=lambda: NOW)
    return AIGateway(budget=budget, providers={"openai": provider}, config=cfg, runtime_mode=RuntimeMode.LIVE)


def _committee_request(symbols: list[str]) -> DecisionReasoningRequest:
    return DecisionReasoningRequest(
        packet={"committee": True, "reports": [{"symbol": s} for s in symbols]},
        reports=[{"symbol": s} for s in symbols],
        portfolio_context={},
        existing_theses=[],
        policy_context={"committee": True, "residual_allocation_question": True},
        alternatives=["CASH", "SPY", *symbols],
        instructions=COMMITTEE_REASONER_INSTRUCTIONS,
    )


def _committee_calls(provider: CommitteeTruncationProvider):
    return [c for c in provider.calls if c.schema_name == "committee_decision"]


def _joined(messages) -> str:
    return "\n".join(m.get("content") or "" for m in messages)


def test_committee_uses_terra_with_stage_specific_output_limit_not_global_research_ceiling():
    cfg = load_ai_config()
    first, retry = committee_output_token_limits(cfg)
    assert cfg["roles"]["research"]["model"] == "gpt-5.6-terra"
    assert cfg["roles"]["research"]["default_max_output_tokens"] == 4000
    assert cfg["roles"]["screening"]["default_max_output_tokens"] == 1500
    assert first == 8000
    assert retry == 12000
    assert first > cfg["roles"]["research"]["default_max_output_tokens"]
    assert cfg["budget"]["monthly_cap"] == 10.0
    assert cfg["budget"]["hard_stop"] == 10.0
    assert LIVE_ORDER_PLACEMENT is False


def test_committee_prompt_is_compact_and_does_not_require_nine_theses():
    text = COMMITTEE_REASONER_INSTRUCTIONS
    assert "CORE Portfolio Investment Committee" in text
    assert "Do NOT emit a full thesis" in text or "do NOT emit a full thesis" in text.lower() or "ONLY for" in text
    assert "selected_allocations" in text
    assert "rankings" in text
    assert "HOLD_CASH" in text
    assert '"theses"' not in text
    assert "decisions[]" not in text
    assert REASONER_INSTRUCTIONS != text
    assert "Every researched ADVANCE_TO_THESIS symbol MUST appear exactly once in decisions[]" not in text
    assert COMMITTEE_CONCISE_RETRY_INSTRUCTION
    assert "max_output_tokens" in COMMITTEE_CONCISE_RETRY_INSTRUCTION


def test_compact_schema_validates_nine_and_twenty_candidate_packets():
    nine = _compact_payload(PRODUCTION_CORE9, buy=None)
    validate_against_schema(nine, COMMITTEE_DECISION_SCHEMA, name="committee_decision")
    twenty = _compact_payload(SCALE20, buy="T01")
    validate_against_schema(twenty, COMMITTEE_DECISION_SCHEMA, name="committee_decision")
    assert len(twenty["rankings"]) == len(SCALE20) + 2
    assert COMMITTEE_DECISION_SCHEMA["properties"]["rankings"]["maxItems"] == 24
    assert COMMITTEE_DECISION_SCHEMA["properties"]["selected_allocations"]["maxItems"] == 4


def test_compact_response_is_much_smaller_than_verbose_theses_for_every_name():
    compact = _compact_payload(SCALE20, buy="T01")
    verbose = {
        "theses": [_thesis(sym) for sym in SCALE20],
        "comparison": {
            "ranking": ["T01", "CASH", "SPY", *SCALE20[1:]],
            "vs_cash": "x" * 400,
            "vs_spy": "y" * 400,
            "notes": "z" * 400,
        },
        "decisions": [
            {
                "symbol": sym,
                "decision": "BUY" if sym == "T01" else "WATCH",
                "desired_allocation_pct": 5.0 if sym == "T01" else 0,
                "rationale": "verbose " * 40,
                "why_preferable_to_cash": "cash " * 40,
                "why_preferable_to_spy": "spy " * 40,
                "why_preferable_to_alternatives": "alts " * 40,
            }
            for sym in SCALE20
        ]
        + [{"symbol": "CASH", "decision": "HOLD", "desired_allocation_pct": 95.0, "rationale": "cash " * 40}],
    }
    compact_tokens = estimate_tokens(json.dumps(compact))
    verbose_tokens = estimate_tokens(json.dumps(verbose))
    assert compact_tokens < 3500
    assert verbose_tokens > compact_tokens * 2
    assert verbose_tokens > 4000


def test_compact_hold_cash_expands_without_buy_theses():
    reports = [_report(sym) for sym in PRODUCTION_CORE9]
    payload = _compact_payload(PRODUCTION_CORE9, buy=None)
    normalized, _, errors = validate_payload(payload, reports, current_nav=500)
    assert errors == []
    assert normalized["theses"] == []
    by = {d["symbol"]: d["decision"] for d in normalized["decisions"]}
    for sym in PRODUCTION_CORE9:
        assert by[sym] == "WATCH"
    assert by["CASH"] == "HOLD"
    assert "CASH" in normalized["comparison"]["ranking"]
    assert "SPY" in normalized["comparison"]["ranking"]


def test_compact_allocate_requires_thesis_only_for_selected_buy():
    reports = [_report(sym) for sym in PRODUCTION_CORE9]
    payload = _compact_payload(PRODUCTION_CORE9, buy="MSFT")
    out = run_portfolio_decision(
        reports,
        ctx(500),
        ScriptedDecisionReasoner(payload),
        persist=False,
        now=NOW,
        journal=None,
        committee=True,
    )
    assert out.validation_errors == []
    assert [t.symbol for t in out.theses] == ["MSFT"]
    by = {d.symbol: d.decision for d in out.decisions}
    assert by["MSFT"] == Decision.BUY
    assert by["CASH"] == Decision.HOLD
    assert by["ANET"] == Decision.WATCH
    anet = next(d for d in out.decisions if d.symbol == "ANET")
    assert anet.reconsideration is not None
    assert "MSFT" in anet.reconsideration["lost_to"] or "CASH" in anet.reconsideration["lost_to"]


def test_eighteen_name_committee_packet_expands_and_stays_multi_name():
    reports = [_report(sym) for sym in SCALE20]
    payload = _compact_payload(SCALE20, buy="T01", alloc=4.0)
    out = run_portfolio_decision(
        reports,
        ctx(10_000),
        ScriptedDecisionReasoner(payload),
        persist=False,
        now=NOW,
        journal=None,
        committee=True,
    )
    assert out.validation_errors == []
    named = [d.symbol for d in out.decisions if d.symbol not in {"CASH", "SPY"}]
    assert set(named) == set(SCALE20)
    assert len(out.packet.reports) == 18
    assert out.packet.committee is True
    assert [d.symbol for d in out.decisions if d.decision in {Decision.BUY, Decision.ADD}] == ["T01"]


def test_successful_committee_call_uses_8000_tokens_and_does_not_retry(tmp_path):
    provider = CommitteeTruncationProvider(payload=_compact_payload(PRODUCTION_CORE9), fail_times=0)
    reasoner = GatewayDecisionReasoner(_live_gw(tmp_path, provider))
    payload = reasoner.reason(_committee_request(PRODUCTION_CORE9))
    calls = _committee_calls(provider)
    assert len(calls) == 1
    assert calls[0].max_output_tokens == 8000
    assert calls[0].model == "gpt-5.6-terra"
    assert calls[0].purpose == "portfolio_decision"
    assert COMMITTEE_CONCISE_RETRY_INSTRUCTION not in _joined(calls[0].messages)
    assert payload["portfolio_action"] == "HOLD_CASH"
    assert reasoner.truncation_retry_used is False
    assert reasoner.call_count == 1
    assert provider.research_attempts == 0


def test_singleton_decision_keeps_4000_token_thesis_schema(tmp_path):
    from agentic_portfolio.ai.providers.scripted import ScriptedProvider

    provider = ScriptedProvider({"thesis_decision": _payload("QUAL", decision="WATCH", alloc=0)}, name="openai")
    gw = _paper_gw(tmp_path, {"openai": provider})
    reasoner = GatewayDecisionReasoner(gw)
    payload = reasoner.reason(_decision_request("QUAL"))
    assert payload["decisions"][0]["symbol"] == "QUAL"
    assert provider.calls[0].schema_name == "thesis_decision"
    assert provider.calls[0].max_output_tokens == 4000
    assert provider.calls[0].schema == THESIS_DECISION_SCHEMA


def test_max_output_tokens_incomplete_retries_once_then_succeeds(tmp_path):
    provider = CommitteeTruncationProvider(payload=_compact_payload(PRODUCTION_CORE9, buy="MSFT"), fail_times=1)
    reasoner = GatewayDecisionReasoner(_live_gw(tmp_path, provider))
    payload = reasoner.reason(_committee_request(PRODUCTION_CORE9))
    calls = _committee_calls(provider)
    assert provider.committee_attempts == 2
    assert len(calls) == 2
    assert calls[0].max_output_tokens == 8000
    assert calls[1].max_output_tokens == 12000
    assert COMMITTEE_CONCISE_RETRY_INSTRUCTION not in _joined(calls[0].messages)
    assert COMMITTEE_CONCISE_RETRY_INSTRUCTION in _joined(calls[1].messages)
    assert payload["portfolio_action"] == "ALLOCATE"
    assert reasoner.truncation_retry_used is True
    assert reasoner.call_count == 2
    assert provider.research_attempts == 0


def test_second_incomplete_fails_closed_without_third_call(tmp_path):
    provider = CommitteeTruncationProvider(payload=_compact_payload(PRODUCTION_CORE9), fail_times=2)
    reasoner = GatewayDecisionReasoner(_live_gw(tmp_path, provider))
    with pytest.raises(DecisionValidationError, match=r"incomplete \(max_output_tokens\)"):
        reasoner.reason(_committee_request(PRODUCTION_CORE9))
    assert provider.committee_attempts == 2
    assert reasoner.last_result is None
    assert reasoner.truncation_retry_used is True
    assert reasoner.call_count == 2


def test_other_malformed_response_does_not_retry(tmp_path):
    provider = CommitteeTruncationProvider(
        payload=_compact_payload(PRODUCTION_CORE9),
        fail_times=0,
        other_malformed="OpenAI response incomplete (content_filter)",
    )
    reasoner = GatewayDecisionReasoner(_live_gw(tmp_path, provider))
    with pytest.raises(DecisionValidationError, match="content_filter"):
        reasoner.reason(_committee_request(PRODUCTION_CORE9))
    assert provider.committee_attempts == 1
    assert reasoner.truncation_retry_used is False


def test_budget_denial_prevents_committee_truncation_retry(tmp_path):
    provider = CommitteeTruncationProvider(payload=_compact_payload(PRODUCTION_CORE9), fail_times=1)
    gw = _live_gw(tmp_path, provider)
    real = gw.budget.authorize
    decision_auths: list[dict] = []

    def authorize(estimated, **kwargs):
        if kwargs.get("purpose") == "portfolio_decision":
            decision_auths.append(dict(kwargs))
            if len(decision_auths) > 1:
                raise BudgetDenied("retry blocked by budget")
        return real(estimated, **kwargs)

    gw.budget.authorize = authorize  # type: ignore[method-assign]
    reasoner = GatewayDecisionReasoner(gw)
    with pytest.raises(DecisionValidationError, match="retry blocked by budget"):
        reasoner.reason(_committee_request(PRODUCTION_CORE9))
    assert provider.committee_attempts == 1
    assert len(decision_auths) == 2


def _run_live_committee(tmp_path, provider, *, symbols=None, buy=None):
    names = list(symbols or PRODUCTION_CORE9)
    _seed_core_universe(tmp_path, names)
    watch, approvals, notify = _services(tmp_path, now=NOW)
    gw = _live_gw(tmp_path, provider)
    result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=GatewayDecisionReasoner(gw),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
    )
    return result, watch, approvals, gw, provider


def test_production_nine_name_truncation_retry_succeeds_without_research_or_placement(tmp_path):
    provider = CommitteeTruncationProvider(payload=_compact_payload(PRODUCTION_CORE9, buy="MSFT"), fail_times=1)
    result, watch, approvals, gw, provider = _run_live_committee(tmp_path, provider)
    assert set(result.eligible_symbols) >= set(PRODUCTION_CORE9)
    assert "CASH" in result.alternatives_considered
    assert "SPY" in result.alternatives_considered
    assert result.reports_in_packet == 9
    assert result.ai_calls == 2
    assert result.ai_stages_called == ["portfolio_decision"]
    assert result.research_called is False
    assert result.terra_called is False
    assert provider.research_attempts == 0
    assert result.selected_symbols == ["MSFT"]
    assert result.proposals_created == 1
    assert result.approvals_created == 1
    pending = approvals.store.pending()
    assert pending and pending[0].ticker == "MSFT"
    assert pending[0].placed_order is False
    assert result.as_dict()["placement_attempted"] is False
    assert LIVE_ORDER_PLACEMENT is False
    assert gw.budget.status().spent <= Decimal("10")
    assert gw.budget.status().cap == Decimal("10")
    kinds = {row.get("type") for row in read_jsonl(tmp_path / "logs" / "core_committee.jsonl")}
    assert "CORE_COMMITTEE_OUTPUT_TRUNCATED_RETRY" in kinds
    assert watch.store.by_ticker("ANET").status.value == "WATCH"


def test_second_incomplete_live_committee_creates_no_proposal(tmp_path):
    provider = CommitteeTruncationProvider(payload=_compact_payload(PRODUCTION_CORE9, buy="MSFT"), fail_times=2)
    result, _watch, approvals, gw, provider = _run_live_committee(tmp_path, provider)
    assert result.status == "DEGRADED"
    assert "max_output_tokens" in (result.reason or "")
    assert result.proposals_created == 0
    assert result.approvals_created == 0
    assert result.selected_symbols == []
    assert approvals.store.pending() == []
    assert result.research_called is False
    assert result.terra_called is False
    assert provider.committee_attempts == 2
    assert provider.research_attempts == 0
    assert result.ai_calls == 2
    assert LIVE_ORDER_PLACEMENT is False
    assert gw.budget.status().cap == Decimal("10")
    store_dir = tmp_path / "state" / "portfolio_decisions"
    if store_dir.exists():
        for path in store_dir.glob("*.json"):
            if path.name == "index.json":
                continue
            blob = json.loads(path.read_text(encoding="utf-8"))
            assert not blob.get("gated_actions")
            assert not blob.get("decisions")


def test_eighteen_name_live_committee_uses_compact_schema_and_one_call(tmp_path):
    provider = CommitteeTruncationProvider(payload=_compact_payload(SCALE20, buy=None), fail_times=0)
    result, _watch, approvals, gw, provider = _run_live_committee(tmp_path, provider, symbols=SCALE20)
    assert len(result.eligible_symbols) == 18
    assert result.reports_in_packet == 18
    assert result.ai_calls == 1
    assert provider.calls[0].max_output_tokens == 8000
    assert provider.calls[0].schema_name == "committee_decision"
    assert result.status in {"NO_ACTION", "OK"}
    assert result.proposals_created == 0
    assert approvals.store.pending() == []
    assert result.research_called is False
    assert gw.budget.status().cap == Decimal("10")


def test_monthly_cap_still_blocks_committee_before_any_retry(tmp_path):
    _seed_core_universe(tmp_path, PRODUCTION_CORE9)
    watch, approvals, notify = _services(tmp_path, now=NOW)
    provider = CommitteeTruncationProvider(payload=_compact_payload(PRODUCTION_CORE9), fail_times=1)
    gw = _live_gw(tmp_path, provider, spent="10.00")
    result = reevaluate_live_core_committee(
        root=tmp_path,
        runtime_mode=RuntimeMode.LIVE,
        decision_reasoner=GatewayDecisionReasoner(gw),
        context_fn=lambda: ctx(500),
        now=NOW,
        watch=watch,
        approvals=approvals,
        notify=notify,
        force=True,
    )
    assert result.status in {"DEGRADED", "BLOCKED"}
    assert result.proposals_created == 0
    assert approvals.store.pending() == []
    assert provider.committee_attempts == 0
    assert provider.research_attempts == 0
    assert gw.budget.status().cap == Decimal("10")
    assert gw.budget.status().spent == Decimal("10")
