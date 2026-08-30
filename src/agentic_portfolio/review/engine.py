"""Robinhood review-only engine.

APPROVED ApprovalPacket → revalidate → Risk Gate → review_equity_order → persist → STOP.
Never places. Never cancels. Never moves money.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentic_portfolio.execution.types import OrderPlan
from agentic_portfolio.journal import append_jsonl
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules, load_review_config
from agentic_portfolio.review.safety import (
    assert_flags_remain_gated,
    assert_no_forbidden_tools,
    assert_review_does_not_place,
)
from agentic_portfolio.review.store import ReviewStore
from agentic_portfolio.review.types import ReviewClient, ReviewRequest, ReviewResult, ReviewRun, ReviewStatus
from agentic_portfolio.review.validate import (
    build_review_payload,
    local_fail_codes,
    parse_review_response,
    plan_vs_review_codes,
    robinhood_side,
    status_from_parsed,
)
from agentic_portfolio.risk_gate import evaluate
from agentic_portfolio.schemas import to_dict


def journal_path(root: Path | None = None) -> Path:
    return (root or project_root()) / "logs" / "robinhood_review.jsonl"


def run_review(
    req: ReviewRequest,
    client: ReviewClient,
    *,
    now: datetime | None = None,
    store: ReviewStore | None = None,
    persist: bool = True,
    config: dict | None = None,
    account_rules: dict | None = None,
    journal: Path | None = None,
    sources_observed: list[str] | None = None,
) -> ReviewResult:
    """Submit one approved packet to broker review. Does not place the order."""
    run = run_reviews(
        [req],
        client,
        now=now,
        store=store,
        persist=persist,
        config=config,
        account_rules=account_rules,
        journal=journal,
        sources_observed=sources_observed,
    )
    return run.results[0]


def run_reviews(
    items: list[ReviewRequest],
    client: ReviewClient,
    *,
    now: datetime | None = None,
    store: ReviewStore | None = None,
    persist: bool = True,
    config: dict | None = None,
    account_rules: dict | None = None,
    journal: Path | None = None,
    sources_observed: list[str] | None = None,
) -> ReviewRun:
    cfg = config or load_review_config()
    rules = account_rules or load_account_rules()
    now = now or datetime.now(timezone.utc)
    run_id = str(uuid4())
    assert_no_forbidden_tools(sources_observed or [])
    live = bool(rules.get("execution", {}).get("live_trade_actions_allowed")) or bool(cfg.get("live_trade_actions_allowed"))
    auto = bool(rules.get("execution", {}).get("auto_execution")) or bool(cfg.get("auto_execution"))
    assert_flags_remain_gated(live_trade_actions_allowed=live, auto_execution=auto)
    assert_review_does_not_place(broker_submitted=False, execution_attempted=False, order_placed=False)
    if persist and store is None:
        store = ReviewStore()

    account_number = str(rules["account"]["account_number"])
    _journal(
        {
            "type": "REVIEW_RUN_STARTED",
            "run_id": run_id,
            "live_trade_actions_allowed": False,
            "auto_execution": False,
            "order_placed": False,
        },
        journal,
        persist=persist,
    )

    results: list[ReviewResult] = []
    for req in items:
        result = _review_one(
            req,
            client,
            now=now,
            config=cfg,
            account_number=account_number,
            store=store if persist else None,
            journal=journal,
            persist=persist,
            run_id=run_id,
        )
        results.append(result)

    run = ReviewRun(run_id=run_id, results=results)
    if persist and store is not None:
        store.save_run(run_id, _run_record(run, now))
    _journal(
        {
            "type": "REVIEW_RUN_COMPLETED",
            "run_id": run_id,
            "symbols": [r.symbol for r in results],
            "statuses": [r.status.value for r in results],
            "broker_orders_submitted": 0,
            "order_placed": False,
            "execution_attempted": False,
            "live_execution_attempted": False,
        },
        journal,
        persist=persist,
    )
    return run


def _review_one(
    req: ReviewRequest,
    client: ReviewClient,
    *,
    now: datetime,
    config: dict,
    account_number: str,
    store: ReviewStore | None,
    journal: Path | None,
    persist: bool,
    run_id: str,
) -> ReviewResult:
    ts = now.isoformat()
    plan = req.plan
    risk = evaluate(req.context, req.action)
    codes, local_status = local_fail_codes(req, now=now, config=config, risk=risk)
    payload = build_review_payload(
        plan,
        account_number,
        market_hours=str(config.get("market_hours") or "regular_hours"),
        decimals=int(config.get("quantity_decimal_places") or 6),
    )
    if codes:
        result = _result(
            plan,
            req,
            payload=payload,
            raw={},
            parsed={},
            status=local_status or ReviewStatus.REVIEW_FAILED,
            reasons=codes,
            at=ts,
            risk_verdict=risk.verdict.value,
            account_number=account_number,
        )
        return _finish(result, store=store, journal=journal, persist=persist, run_id=run_id, called=False)

    raw: dict[str, Any] = {}
    parsed: dict[str, Any] = {}
    try:
        raw = dict(client.review_equity_order(payload) or {})
        parsed = parse_review_response(raw)
    except Exception as exc:  # noqa: BLE001 — fail closed on any broker error
        result = _result(
            plan,
            req,
            payload=payload,
            raw={"error": str(exc)},
            parsed={"errors": [str(exc)]},
            status=ReviewStatus.REVIEW_FAILED,
            reasons=["REVIEW_CALL_FAILED"],
            at=ts,
            risk_verdict=risk.verdict.value,
            account_number=account_number,
        )
        return _finish(result, store=store, journal=journal, persist=persist, run_id=run_id, called=True)

    mismatch = plan_vs_review_codes(plan, payload, parsed, config=config)
    status = status_from_parsed(parsed, mismatch)
    if status == ReviewStatus.REVIEW_REJECTED:
        reasons = ["ROBINHOOD_REJECTED"]
    else:
        reasons = list(mismatch)
    result = _result(
        plan,
        req,
        payload=payload,
        raw=raw,
        parsed=parsed,
        status=status,
        reasons=reasons,
        at=ts,
        risk_verdict=risk.verdict.value,
        account_number=account_number,
    )
    return _finish(result, store=store, journal=journal, persist=persist, run_id=run_id, called=True)


def _result(
    plan: OrderPlan,
    req: ReviewRequest,
    *,
    payload: dict[str, Any],
    raw: dict[str, Any],
    parsed: dict[str, Any],
    status: ReviewStatus,
    reasons: list[str],
    at: str,
    risk_verdict: str | None,
    account_number: str,
) -> ReviewResult:
    assert_review_does_not_place(broker_submitted=False, execution_attempted=False, order_placed=False)
    return ReviewResult(
        review_id=str(uuid4()),
        approval_id=req.packet.approval_id,
        order_plan_id=plan.order_plan_id,
        symbol=plan.symbol.upper(),
        side=robinhood_side(plan),
        quantity=plan.quantity,
        notional=plan.notional,
        requested_order_type=plan.order_type.value if plan.order_type else payload.get("type"),
        robinhood_response=raw,
        estimated_cost=parsed.get("estimated_cost"),
        estimated_proceeds=parsed.get("estimated_proceeds"),
        warnings=list(parsed.get("warnings") or []),
        errors=list(parsed.get("errors") or []),
        reviewed_at=at,
        status=status,
        fail_reasons=list(reasons),
        account_number=account_number,
        time_in_force=plan.time_in_force.value if plan.time_in_force else payload.get("time_in_force"),
        market_hours=str(payload.get("market_hours") or "regular_hours"),
        review_payload=payload,
        risk_gate_verdict=risk_verdict,
        broker_submitted=False,
        order_placed=False,
        execution_attempted=False,
        live_execution_blocked=True,
        live_trade_actions_allowed=False,
        auto_execution=False,
        review_accepted_does_not_execute=True,
    )


def _finish(
    result: ReviewResult,
    *,
    store: ReviewStore | None,
    journal: Path | None,
    persist: bool,
    run_id: str,
    called: bool,
) -> ReviewResult:
    if persist and store is not None:
        store.save(result)
    _journal(
        {
            "type": "REVIEW_RECORDED",
            "run_id": run_id,
            "review_id": result.review_id,
            "approval_id": result.approval_id,
            "order_plan_id": result.order_plan_id,
            "symbol": result.symbol,
            "status": result.status.value,
            "fail_reasons": list(result.fail_reasons),
            "broker_called": called,
            "broker_submitted": False,
            "order_placed": False,
            "execution_attempted": False,
        },
        journal,
        persist=persist,
    )
    return result


def _run_record(run: ReviewRun, now: datetime) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "created_at": now.isoformat(),
        "symbols": [r.symbol for r in run.results],
        "results": [to_dict(r) for r in run.results],
        "broker_orders_submitted": 0,
        "order_placed": False,
        "execution_attempted": False,
        "live_execution_attempted": False,
        "live_trade_actions_allowed": False,
        "auto_execution": False,
        "review_accepted_does_not_execute": True,
    }


def _journal(row: dict, path: Path | None, *, persist: bool = True) -> None:
    if path is None and not persist:
        return
    append_jsonl(row, path or journal_path())
