"""Human-readable Robinhood ReviewResult. Informational only; does not place."""

from __future__ import annotations

from agentic_portfolio.review.types import ReviewResult, ReviewRun
from agentic_portfolio.schemas import to_dict


def _num(value: float | None) -> str:
    if value is None:
        return "—"
    return str(value)


def render_result(result: ReviewResult) -> str:
    warnings = "; ".join(result.warnings) if result.warnings else "none"
    errors = "; ".join(result.errors) if result.errors else "none"
    reasons = "; ".join(result.fail_reasons) if result.fail_reasons else "none"
    lines = [
        f"# Robinhood Review {result.review_id}",
        "",
        f"Status: **{result.status.value}**. Review is preflight only and does not place an order.",
        f"Reviewed: {result.reviewed_at}",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Symbol | {result.symbol} |",
        f"| Side | {result.side or '—'} |",
        f"| Quantity | {_num(result.quantity)} |",
        f"| Notional | {_num(result.notional)} |",
        f"| Order type | {result.requested_order_type or '—'} |",
        f"| Time in force | {result.time_in_force or '—'} |",
        f"| Estimated cost | {_num(result.estimated_cost)} |",
        f"| Estimated proceeds | {_num(result.estimated_proceeds)} |",
        f"| Risk-gate verdict | {result.risk_gate_verdict or '—'} |",
        f"| Approval | {result.approval_id} |",
        f"| Order plan | {result.order_plan_id} |",
        "",
        f"**Warnings:** {warnings}",
        f"**Errors:** {errors}",
        f"**Fail reasons:** {reasons}",
        "",
        "REVIEW_ACCEPTED does not execute. No cancel. No transfers.",
        "",
    ]
    return "\n".join(lines)


def render_run(run: ReviewRun) -> str:
    lines = [
        "# Robinhood Review-Only",
        "",
        f"Run {run.run_id}. Preflight only. Does not place.",
        "",
        "| Symbol | Side | Status | Qty | Notional | Cost | Proceeds |",
        "|---|---|---|---|---|---|---|",
    ]
    for result in run.results:
        lines.append(
            f"| {result.symbol} | {result.side or '—'} | {result.status.value} | "
            f"{_num(result.quantity)} | {_num(result.notional)} | "
            f"{_num(result.estimated_cost)} | {_num(result.estimated_proceeds)} |"
        )
    lines += [
        "",
        f"Orders placed: {int(run.order_placed)}. Live execution attempted: {run.live_execution_attempted}.",
        "REVIEW_ACCEPTED does not execute. No cancel. No transfers.",
        "",
    ]
    return "\n".join(lines)


def result_payload(result: ReviewResult) -> dict:
    return to_dict(result)
