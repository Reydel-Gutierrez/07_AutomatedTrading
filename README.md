# Agentic Portfolio Manager

One **percent-of-NAV** investment policy on the dedicated Robinhood Agentic account. Policy does not change with account size.

Canonical: `PORTFOLIO_POLICY.md` + `config/portfolio_policy.json`.

The AI never overrides hard ceilings and **never moves money**.

## Last observed book (fact, not a budget)

As of 2026-08-29, read-only: NAV $500, buying power $500, cash 100%, no positions.

Use live `get_portfolio` going forward. Do not treat $500 as a policy constraint.

## Execution (unchanged)

| Setting | Value |
|---|---|
| State | `HUMAN_APPROVAL` |
| `auto_execution` | `false` |
| `require_human_approval` | `true` |
| `live_trade_actions_allowed` | `false` |
| Stop | After Robinhood review-only |

`review_equity_order` is informational/preflight after a still-valid APPROVED packet. No place/cancel. No deposits/withdrawals/transfers.

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
src/agentic_portfolio/   ← context, session SOD, classification adapter, sleeve/thesis registries, candidate discovery, deep research, thesis+portfolio decision, position monitoring, risk gate, paper execution controller, paper fill/blotter, human approval packet, Robinhood review-only (no place/cancel)
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
11. If prose ≠ JSON, **stop**

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
