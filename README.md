# Agentic Portfolio Manager

One **percent-of-NAV** investment policy on the dedicated Robinhood Agentic account. Policy does not change with account size.

Canonical: `PORTFOLIO_POLICY.md` + `config/portfolio_policy.json`.

The AI never overrides hard ceilings and **never moves money**.

## Last observed book (fact, not a budget)

As of 2026-08-30, read-only LIVE refresh: Agentic account confirmed (`agentic_allowed`); NAV $500; buying power $500; cash 100%; no positions.

Use live `get_portfolio` going forward. Do not treat $500 as a policy constraint. Do not use the paper $10,000 book as LIVE NAV.

## Execution

| Setting | Value |
|---|---|
| State | `HUMAN_APPROVAL` |
| `auto_execution` | `false` |
| `require_human_approval` | `true` |
| `live_trade_actions_allowed` | `false` |
| `LIVE_ORDER_PLACEMENT` | `false` (committed default) |
| Placement surface | `LiveOrderExecutor` only, after human APPROVE + send-time revalidation + broker review |

Human approval is mandatory. With `LIVE_ORDER_PLACEMENT=false`, APPROVE → `APPROVED_EXECUTION_DISABLED` and the broker place tool is not called. Enabling placement is a manual Raspberry Pi step after validation — never a git default. No deposits/withdrawals/transfers.

## Layout

```
PORTFOLIO_POLICY.md
ARCHITECTURE.md
STRATEGY.md
RISK_RULES.md
AGENT_RULES.md
RUNBOOK.md
JOURNAL.md
config/portfolio_policy.json
config/account_rules.json
config/pipeline.json
config/thesis_schema.json
config/discovery.json
config/research.json
config/decision.json
config/monitoring.json
config/execution.json
config/paper_fill.json
config/approval.json
config/review.json
config/runtime.json
config/ai.json
config/dashboard.json
deploy/                  ← Raspberry Pi systemd unit, env example, install procedure
src/agentic_portfolio/   ← context, LIVE Robinhood snapshot, session SOD, classification adapter, sleeve/thesis registries, candidate discovery, deep research, thesis+portfolio decision, position monitoring, risk gate, paper execution controller, paper fill/blotter, human approval packet, Robinhood review-only (no place/cancel)
tests/
logs/  reports/  state/
```

## Source of truth

1. `config/portfolio_policy.json` — active risk/allocation math  
2. `config/account_rules.json` — identity + execution flags  
3. `config/discovery.json` — discovery channel weights (heuristics, not backtests)  
4. `config/research.json` — research freshness and sleeve questions (not buy rules)  
5. `config/decision.json` — thesis + portfolio decision contract (not buy thresholds)  
6. `config/monitoring.json` — position monitoring + thesis reassessment (not an investment-rule engine)  
7. `config/execution.json` — paper Execution Controller / OrderPlan (not live trading)  
8. `config/paper_fill.json` — paper fill + blotter (isolated paper book; not live trading)  
9. `config/approval.json` — human approval packet (APPROVED does not place)  
10. `config/review.json` — Robinhood review-only (`review_equity_order` preflight; does not place)  
11. `config/runtime.json` — PAPER vs LIVE source of truth (placement still off)  
12. `config/ai.json` — AI Gateway roles/providers, $10/month hard cap, conservative LIVE shortlist, Raspberry Pi scheduler  
13. If prose ≠ JSON, **stop**

## Candidate Discovery

Answers: **what is worth researching right now?**  
Does not answer: should we buy?

Four channels: Core quality, opportunistic dislocation, tactical setup, speculative asymmetry. Output is a persisted `Candidate` plus optional `ResearchQueue` entry. `NO_HIGH_QUALITY_CANDIDATES` is valid. `URGENT_RESEARCH` means research quickly, not buy quickly.

Path that is **not** allowed: Candidate → BUY.

Required path: Candidate → ResearchReport → InvestmentThesis → PortfolioDecision → ProposedAction → RiskGate.

Read-only live run: `PYTHONPATH=src python scripts/run_live_readonly_discovery.py` (no order or account-mutation MCP tools).

## Deep Research

Answers: **is the Discovery opportunity actually attractive enough to justify an investment thesis?**  
Does not answer: what to allocate, or whether Risk Gate would permit it.

Python prepares a `ResearchEvidencePacket` (observed facts + deterministic derived metrics). A programmatic `ResearchReasoner` interprets. Output is a persisted `ResearchReport` under `state/research_reports/`. Most ideas may be rejected. `RESEARCH_INCONCLUSIVE` / `NEED_MORE_DATA` are valid.

A favorable report is **not** a trade. No `ProposedAction` is created here.

Read-only live run: `PYTHONPATH=src python scripts/run_live_readonly_research.py` (no order or account-mutation MCP tools).

## Investment Thesis + Portfolio Decision

Answers: **should this position exist, at what % of NAV, versus cash, SPY, and other researched names?**  
Does not answer: whether Risk Gate will permit it, and does not execute.

AI forms a DRAFT thesis (summary, bull/base/bear, catalysts, risks, horizon, invalidation, review triggers, exit policy) and a decision (`BUY` / `ADD` / `HOLD` / `REDUCE` / `SELL` / `WATCH` / `REJECT` / `NO_ACTION`). Python validates, persists DRAFT only, converts a valid decision into `ProposedAction`, and sends it to the existing Risk Gate. Cash and SPY are valid alternatives. `NO_ACTION` is always valid. No broker stop orders. Theses stay DRAFT until a future real execution.

Paper run: `PYTHONPATH=src python scripts/run_paper_thesis_decision.py` (uses existing ResearchReports; no order MCP tools).

## Position Monitoring + Thesis Reassessment

Answers: **does new evidence require research, thesis, or portfolio reassessment of a holding?**  
Does not answer: place a broker stop, or trade.

Python detects facts and triggers (price, earnings, news, filings, freshness, exit-policy conditions, risk state). AI interprets. CORE is not invalidated by price movement alone. Meaningful triggers reuse Research refresh → thesis reassessment → Portfolio Decision → Risk Gate. Actions: HOLD / ADD / REDUCE / SELL / NO_ACTION. An exit condition is not a broker stop order.

Paper run: `PYTHONPATH=src python scripts/run_paper_position_monitor.py` (mocked holdings + existing ResearchReports; no order MCP tools).

## Execution Controller

Answers: **what paper order would carry out this Risk-Gate-approved action?**  
Does not answer: is this a good investment, and does not review/place/cancel at the broker.

Takes a permitted `ProposedAction` and emits a paper `OrderPlan` for BUY / ADD / REDUCE / SELL. HOLD / WATCH / REJECT / NO_ACTION create no plan. Does not invent stop orders. Status remains `PAPER_ONLY` / `BLOCKED_FROM_LIVE`.

Paper run: `PYTHONPATH=src python scripts/run_paper_execution.py` (current monitoring outputs; no order MCP tools).

## Paper Fill + Blotter Reconciliation

Answers: **what would the isolated paper book look like if this OrderPlan filled?**  
Does not answer: is this a good investment, and does not review/place/cancel at the broker.

Takes a `PAPER_ONLY` OrderPlan and simulates a deterministic market fill at the eligible quote/reference price. Updates paper cash, quantity, average cost, P&L, sleeve/sector exposure, and NAV. Writes a blotter line and reconciles. BUY may mark an isolated paper thesis ACTIVE. Live thesis/account state is untouched.

Paper run: `PYTHONPATH=src python scripts/run_paper_fill.py` (existing paper OrderPlans; no order MCP tools).

## Human Approval Packet

Answers: **what should a human read before Robinhood review?**  
Does not answer: place or cancel at the broker.

Takes a Risk-Gate-approved paper OrderPlan plus thesis, research, decision, context, risk result, and optional monitoring state. Emits a persisted `ApprovalPacket`. `APPROVED` still does not place an order. Packets expire or are superseded when quotes, thesis/research, the book, or risk state drift, or when a newer decision replaces them.

Paper run: `PYTHONPATH=src python scripts/run_paper_approval.py` (existing paper OrderPlans; no place/cancel).

## Robinhood Review-Only

Answers: **what does Robinhood say about this still-valid APPROVED order plan?**  
Does not answer: place or cancel the order.

Takes an APPROVED, still-valid `ApprovalPacket`, revalidates quote / portfolio / thesis / risk state, re-checks Risk Gate, and calls `review_equity_order`. Persists a `ReviewResult`. `REVIEW_ACCEPTED` does not execute.

Paper/live-shaped run: `PYTHONPATH=src python scripts/run_paper_review.py` (one approved packet; `review_equity_order` only).

## LIVE runtime (read-only)

`PAPER` (default) uses the isolated paper book for tests/dev. `LIVE` uses the Agentic Robinhood account as the single source of truth for NAV, cash, buying power, positions, allocations, HWM/drawdown, daily P/L, dashboard, family shares, monitoring holdings, and Risk Gate inputs.

Switch: `AGENTIC_RUNTIME_MODE=LIVE` or `DASHBOARD_ENVIRONMENT=LIVE` (see `config/runtime.json`). Dashboard shows **LIVE**. It does not fall back to paper $10,000 NAV.

The overnight `RESEARCH_QUEUE_WORKER` job consumes promoted candidates in `state/research_queue.json` (and `state/live_ai/` when present) through the existing Research Engine and AI Gateway. Successful research can create DRAFT theses, persistent watches, or human approval packets.

Live discovery is dynamic (`LIVE_DISCOVERY_WIRED=true`): positions, watchlists, popular lists, saved scans, and earnings — no AI, no hard-coded 25-name universe.

Committed default: `LIVE_ORDER_PLACEMENT=false` (`AGENTIC_LIVE_ORDER_PLACEMENT=false`). Human APPROVE is mandatory. With the switch off, APPROVE becomes `APPROVED_EXECUTION_DISABLED` and never calls the broker. The live path (`LiveOrderExecutor`) is implemented and tested: send-time revalidation → `review_equity_order` → place (only if the switch is on) → reconcile → holdings update. Do not enable placement in git. Raspberry Pi validation is still required before the first real order.

Read-only pipeline dump: `PYTHONPATH=src python scripts/diagnose_pipeline.py`.

Read-only launch check: `PYTHONPATH=src python scripts/run_live_launch_check.py` (confirms Agentic account, refreshes `state/live_book`, fails if paper state leaks). Does **not** call `place_equity_order`. `review_equity_order` remains behind the existing approval/review gate.

## Production AI (proposal-only)

Cursor is the **development agent**. The Raspberry Pi application (`scripts/run_service.py`) is the **24/7 autonomous production runtime**. The scheduler is an internal orchestrator only. OpenAI/Anthropic are **reasoning services** used only through `src/agentic_portfolio/ai/` (the AI Gateway). Risk Gate remains the deterministic authority. The broker remains the account source of truth.

AI never has unrestricted trading authority. LIVE invariants: `LIVE_AI_ALLOWED=true`, `LIVE_PROPOSALS_ALLOWED=true`, `LIVE_ORDER_PLACEMENT=false`. Combined spend is capped at **$10/month**. The budget ledger is persisted so a restart cannot reset it.

Complete local application:

```
$env:PYTHONPATH = "src"
$env:AGENTIC_RUNTIME_MODE = "LIVE"
$env:DASHBOARD_ENVIRONMENT = "LIVE"
python scripts/run_service.py
```

Dashboard control room: `http://127.0.0.1:3100` (localhost only). Human APPROVE is required. Default `LIVE_ORDER_PLACEMENT=false` does not place.

Raspberry Pi LIVE install: dedicated non-root user, `/opt/agentic-portfolio/.venv`, secrets in `/etc/agentic-portfolio/env`. Procedure: `deploy/README.md`. Unit: `deploy/systemd/agentic-portfolio.service`. Do not run as root. Reach the dashboard with `ssh -L 3100:127.0.0.1:3100 <pi>`.

AI never has unrestricted trading authority. LIVE invariants: `LIVE_AI_ALLOWED=true`, `LIVE_PROPOSALS_ALLOWED=true`, `LIVE_ORDER_PLACEMENT=false`. Combined spend is capped at **$10/month**. The budget ledger is persisted so a restart cannot reset it.

Proposal-only LIVE check: `PYTHONPATH=src python scripts/run_live_ai_check.py --scripted` (confirmed Agentic snapshot; scripted provider; no spend). Real OpenAI check: `PYTHONPATH=src python scripts/run_live_ai_check.py --use-real-ai` (requires `OPENAI_API_KEY`; screening `gpt-5.6-luna`, research `gpt-5.6-terra`, escalation `gpt-5.6-sol` via `POST /v1/responses`). If `OPENAI_API_KEY` is set, the script will not silently substitute the scripted provider. Does **not** call `place_equity_order` or `cancel_equity_order`.

