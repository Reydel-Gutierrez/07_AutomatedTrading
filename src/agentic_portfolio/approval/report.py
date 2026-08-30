"""Human-readable Approval Packet rendering. No broker calls."""

from __future__ import annotations

from agentic_portfolio.approval.types import ApprovalPacket, ApprovalResult
from agentic_portfolio.schemas import to_dict


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}%"


def _num(value: float | None) -> str:
    if value is None:
        return "—"
    return str(value)


def render_packet(packet: ApprovalPacket) -> str:
    """Compact human-readable package. APPROVED still does not place an order."""
    plan = packet.order_plan_summary
    refs = packet.evidence_refs
    risks = "; ".join(packet.key_risks) if packet.key_risks else "—"
    reviews = ", ".join(packet.enhanced_review_requirements) if packet.enhanced_review_requirements else "none"
    lines = [
        f"# Approval Packet {packet.approval_id}",
        "",
        f"Status: **{packet.status.value}**. APPROVED does not place a live order.",
        f"Created: {packet.created_at}",
        "",
        f"| Field | Value |",
        f"|---|---|",
        f"| Symbol | {packet.symbol} |",
        f"| Action | {packet.action.value} |",
        f"| Sleeve | {packet.sleeve.value if packet.sleeve else '—'} |",
        f"| Current allocation | {_pct(packet.current_allocation_pct)} |",
        f"| Desired allocation | {_pct(packet.desired_allocation_pct)} |",
        f"| Order notional | {_num(packet.order_notional)} |",
        f"| Order quantity | {_num(packet.order_quantity)} |",
        f"| Current price | {_num(packet.current_price)} |",
        f"| Risk-gate verdict | {packet.risk_gate_verdict or '—'} |",
        f"| Enhanced review | {reviews} |",
        f"| Monitoring | {packet.monitoring_state or '—'} |",
        "",
        "## Thesis",
        packet.thesis_summary or "—",
        "",
        f"**Why now:** {packet.why_now or '—'}",
        f"**Why not cash:** {packet.why_not_cash or '—'}",
        f"**Why not SPY:** {packet.why_not_spy or '—'}",
        f"**Horizon:** {packet.expected_horizon or '—'}",
        "",
        "## Bull / base / bear",
        f"- Bull: {packet.bull_case or '—'}",
        f"- Base: {packet.base_case or '—'}",
        f"- Bear: {packet.bear_case or '—'}",
        "",
        f"**Key risks:** {risks}",
        f"**Invalidation / exit:** {packet.invalidation_exit_policy or '—'}",
        "",
        "## Portfolio effect",
        packet.portfolio_effect or "—",
        "",
        "## Sector / concentration",
        packet.sector_concentration_effect or "—",
        "",
        "## Order plan",
        f"{plan.side or '—'} {plan.order_type or '—'} {plan.time_in_force or '—'}; "
        f"qty {_num(plan.quantity)}; notional {_num(plan.notional)}; "
        f"status {plan.execution_status}; live blocked={plan.live_execution_blocked}; "
        f"stops={plan.stop_orders_created}; broker_submitted={plan.broker_submitted}.",
        "",
        "## References",
        f"order_plan={refs.order_plan_id or '—'}; decision={refs.source_decision_id or '—'}; "
        f"thesis={refs.thesis_id or '—'}; research={refs.research_id or '—'}; "
        f"risk={refs.risk_evaluation_id or '—'}; monitoring={refs.monitoring_run_id or '—'}.",
        "",
        "No review/place/cancel. No transfers.",
        "",
    ]
    return "\n".join(lines)


def render_run(result: ApprovalResult) -> str:
    lines = [
        "# Human Approval Packets",
        "",
        f"Run {result.run_id}. Paper packaging only. APPROVED does not place a live order.",
        "",
        "| Symbol | Action | Status | Current % | Desired % | Notional | Qty |",
        "|---|---|---|---|---|---|---|",
    ]
    for packet in result.packets:
        lines.append(
            f"| {packet.symbol} | {packet.action.value} | {packet.status.value} | "
            f"{_pct(packet.current_allocation_pct)} | {_pct(packet.desired_allocation_pct)} | "
            f"{_num(packet.order_notional)} | {_num(packet.order_quantity)} |"
        )
    for skipped in result.skipped:
        lines.append(
            f"| {skipped.symbol} | {skipped.action.value} | skipped:{skipped.reason} | — | — | — | — |"
        )
    lines += [
        "",
        f"Broker submitted: {result.broker_orders_submitted}. Live execution attempted: {result.live_execution_attempted}.",
        "No review/place/cancel. No transfers.",
        "",
    ]
    return "\n".join(lines)


def packet_payload(packet: ApprovalPacket) -> dict:
    return to_dict(packet)
