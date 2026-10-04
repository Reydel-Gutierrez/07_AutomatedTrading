# Architecture

AI-assisted, deterministic-risk portfolio manager for the dedicated Robinhood **Agentic** account.
One percent-of-NAV policy at any NAV (`PORTFOLIO_POLICY.md` + `config/portfolio_policy.json`).

The system is **one portfolio loop**. It runs identically in PAPER and LIVE. The only differences are the
broker endpoint (the simulated `PaperBroker` or Robinhood) and the state root.

```
                 ┌──────────────────────── AI (advisory, $10/month cap, no broker access) ───────────────────────┐
 broker facts →  DISCOVERY → RESEARCH (Luna screen, Terra report) → THESIS + PORTFOLIO DECISION / CORE COMMITTEE
 (read-only)                                       POSITION MONITOR reassessment ┘                                │
                 └──────────────────────────────────────────────────────────────────────────────────────────────┘
                                   │ ProposedAction (symbol, action, class, sleeve, sector, size, liquidity)
                                   ▼
                        DETERMINISTIC RISK GATE (hard veto, proposal time)
                                   ▼
                        ProposalIntake  ── OBSERVE → journal only, no order
                                   │
                        OrderAuthority (deterministic, no AI)
                          ├─ HUMAN_APPROVAL → PENDING → human APPROVE ─┐
                          └─ AUTONOMOUS → clean PASS within limits ────┤→ APPROVED
                             anything else → ESCALATED to a human ─────┘
                                   ▼
                        OrderExecutor (the ONLY placement surface)
                          kill switch → broker-write permission → fresh account refresh → quote/tradability
                          → send-time revalidation → sizing → FULL RISK GATE RE-RUN on fresh facts
                          → OrderAuthority pre-send checks (limits, window, spread, staleness)
                          → broker review → place → audit/notify
                                   ▼
                        RECONCILE → account refresh → POSITION MONITOR → (loop)
```

## Authority

| Layer | Owner | Authority |
|---|---|---|
| Facts (NAV, cash, positions, quotes) | Broker via read-only adapter | Source of truth |
| Interpretation (research, thesis, action, size) | AI through the AI Gateway | Advisory; structured JSON only |
| Permission | Deterministic Risk Gate | Hard veto, evaluated twice (proposal + send) |
| Authorization | `OrderAuthority` + `config/execution_policy.json` | Human or deterministic policy. **Never AI.** |
| Execution | `OrderExecutor` (`live_execution/executor.py`) | Sole broker-write caller |
| Emergency stop | Kill switch | Stops every submission; human release only |

AI never holds a broker client, never calls `review/place/cancel`, and never calls an approve/authorize
method. `tests/test_autonomy_e2e.py::test_ai_layer_has_no_broker_or_authorization_access` enforces this
statically for `ai/`, `research/`, and `decision/`.

## Three switches (and only three)

| Switch | Where | Values | Meaning |
|---|---|---|---|
| Execution mode | `AGENTIC_EXECUTION_MODE` or `config/execution_policy.json` | `OBSERVE` / `HUMAN_APPROVAL` / `AUTONOMOUS` | Who may authorize an order. Committed default `HUMAN_APPROVAL`. Typos fail safe to `HUMAN_APPROVAL`. |
| Broker writes (LIVE) | `AGENTIC_LIVE_ORDER_PLACEMENT` | `true` / `false` | Whether the executor may bind the Robinhood write transport. Committed default `false`. PAPER always writes to the PaperBroker. |
| Kill switch | `state/runtime/kill_switch.json` (dashboard / `scripts/kill_switch.py`) or `AGENTIC_KILL_SWITCH` | engaged / released | Stops all submissions immediately. Auto-trips on ambiguous submission, repeated broker failures, wrong account. |

The former flags `auto_execution`, `require_human_approval`, and `live_trade_actions_allowed` in
`account_rules.json` / `agent.json` / `live_execution.json` / `runtime.json` were removed. They duplicated these
switches, contradicted the runtime, and several modules crashed if anyone changed them.

## Autonomous authorization (`config/execution_policy.json → autonomous`)

`AUTONOMOUS` authorizes a proposal only when **all** hold; otherwise it is escalated to the human queue with
explicit reasons:

* Risk Gate verdict is exactly `PASS` (not `REQUIRES_ENHANCED_REVIEW`, not `HALTED`);
* action in `allowed_actions`; order sized; full ProposedAction snapshot present (BUY/ADD);
* single BUY/ADD ≤ `max_single_order_pct_nav`; daily BUY notional ≤ `max_buy_notional_pct_nav_per_day`;
  orders today < `max_orders_per_day`; cash after a buy ≥ `min_cash_reserve_pct_nav`;
* kill switch released; broker failure streak < `max_consecutive_execution_failures`.

At send time an autonomous order additionally needs: mode still `AUTONOMOUS`, inside the send window
(`min_minutes_after_open` / `min_minutes_before_close`), spread ≤ `max_spread_bps`, and an authorization from
its first eligible session. An authorization made off-hours (Friday evening) is valid for the next session
(Monday) only; stale ones are consumed and the pipeline re-proposes on fresh facts.

A human APPROVE of an escalated proposal is the review the Risk Gate asked for (enhanced concentration,
sector, speculative liquidity, HALTED exits). Human-approved orders still pass send-time revalidation and the
Risk Gate re-run.

## Send-time protection (every order, every mode)

1. Kill switch.
2. Broker-write permission (`LIVE` + switch + bound write transport, or `PAPER` + PaperBroker; a PaperBroker in
   LIVE or a real broker in PAPER is refused).
3. Fresh account refresh; account number must match the Agentic account (mismatch trips the kill switch).
4. `revalidate_for_send`: expiry, session, tradability, quote move vs proposal, NAV/cash/BP drift, duplicate
   open order, thesis invalidated, risk regime.
5. Deterministic sizing (dollar BUYs; SELL = full exit; REDUCE = to target %).
6. **Full Risk Gate re-run** on the persisted ProposedAction with fresh price, size, spread, and context.
7. OrderAuthority pre-send checks.
8. Broker `review_equity_order`, then `place_equity_order`; ambiguous acknowledgement → UNKNOWN + kill switch.

Deferrable conditions (market closed, outside the send window, transient quote outage, wide spread) leave the
order APPROVED for `APPROVED_EXECUTION_DRAIN`; everything else consumes it.

## PAPER and LIVE

| | LIVE | PAPER |
|---|---|---|
| Account facts | Agentic Robinhood account (read-only MCP) | `PaperBroker` simulated account (`state/paper_broker/account.json`) |
| Market data | Read-only Robinhood quotes | The same read-only Robinhood quotes |
| Orders | `LiveWriteAdapter` (needs `AGENTIC_LIVE_ORDER_PLACEMENT=true`) | `PaperBroker` fills at ask/bid |
| State root | project root (existing Pi state) | `var/paper` (or `AGENTIC_STATE_ROOT`) |
| Pipeline | identical | identical |

The older offline paper toolchain (`execution/` OrderPlan, `paper_fill/`, `approval/` packets, `review/`
review-only, `scripts/run_paper_*.py`) is **legacy**: kept for its tests and historical dashboards, not used by
the runtime.

## Runtime

`scripts/run_service.py` = dashboard thread + `AgentRuntime` (24/7). The orchestrator runs one job per
responsibility (`agent/jobs.py`, 27 jobs; 14 duplicate aliases were removed and remain runnable by name):

| Phase | Jobs |
|---|---|
| Always | HEARTBEAT, BROKER_RECONNECT, APPROVAL_EXPIRY, WATCH_STALE_CLEANUP, LIVE_ACCOUNT_REFRESH |
| Market open | APPROVED_EXECUTION_DRAIN (5m), LIVE_ORDER_RECONCILE (5m), POSITION_MONITOR, WATCH_CONDITION_MONITOR, RISK_MONITOR, AI_REASSESS_IF_WARRANTED (15m), CANDIDATE_DISCOVERY lightweight (15m), RESEARCH_QUEUE_WORKER (30m, 1 item) |
| Pre-market | PREMARKET_EVENT_DISCOVERY, PREMARKET_THESIS_REVALIDATE, PREMARKET_PREPARE_CONDITIONAL |
| After close | CANDIDATE_DISCOVERY broad, POSTMARKET_EARNINGS_DISCOVERY, POSTMARKET_RECONCILE, LUNA_SCREEN, CORE_COMMITTEE_REVIEW, NEXT_SESSION_PLANS |
| Overnight | OVERNIGHT_WATCH_MAINTAIN, OVERNIGHT_RISK, RESEARCH_QUEUE_WORKER |
| Weekend/holiday | CANDIDATE_DISCOVERY broad, RESEARCH_QUEUE_WORKER, LUNA_SCREEN, CORE_COMMITTEE_REVIEW, NEXT_SESSION_PLANS, WEEKEND_PORTFOLIO_REVIEW |

One failed job never stops the process. AI is skipped (not the runtime) when the budget is exhausted.

## Proposal sources (all through `ProposalIntake`)

| Origin | Module | Risk Gate input |
|---|---|---|
| New research → decision / CORE committee | `agent/pipeline.py` | `ProposedAction` from `decision/engine.py` |
| Holding reassessment (ADD/REDUCE/SELL) | `monitoring/live.py` | `ProposedAction` from `monitoring/engine.py` |
| Watch entry conditions met at the open | `agent/handlers.py` | `ProposedAction` rebuilt from persisted research (`autonomy/watch_proposal.py`) |

The watch path previously created approvals with a hard-coded `PASS`; it now runs the real gate.

## AI budget

Unchanged: one combined $10/month cap across providers (`config/ai.json`, persisted ledger). Autonomy adds
**zero** AI calls: authorization, send-time checks, and the kill switch are deterministic.

## Modules

| Package | Role |
|---|---|
| `adapters/` | Read-only Robinhood facts; account identity |
| `live/` | Account snapshot (LIVE and PAPER labeling), HWM, session SOD |
| `discovery/` | Universe → eligibility → scoring → research queue (no BUY) |
| `ai/` | Gateway (sole AI caller), budget ledger, providers, reasoners |
| `research/` | Evidence packet → AI report |
| `decision/` | DRAFT thesis + portfolio decision / CORE committee → ProposedAction |
| `monitoring/` | Holding triggers + AI reassessment → ProposedAction |
| `risk_gate.py` | Deterministic permission (concentration, sector, liquidity, daily halt, HWM states, BP/cash) |
| `autonomy/` | Execution policy, kill switch, OrderAuthority, ProposalIntake, send-time risk, PaperBroker, watch proposals |
| `live_approval/` | Order proposal queue (PENDING / APPROVED / EXECUTED …) |
| `live_execution/` | OrderExecutor, broker adapters, reconcile, audit |
| `watch/` | Persistent watchlist and next-session conditional plans |
| `agent/` | 24/7 runtime, orchestrator, job handlers |
| `dashboard/` | Localhost control room (approve/reject, kill switch, status) |
| `notify/` | Dashboard + Telegram notifications |
