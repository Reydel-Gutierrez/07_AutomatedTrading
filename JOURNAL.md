# Journal

Decisions, not only fills. Score later vs **holding SPY**.

Policy: `PORTFOLIO_POLICY.md`. Newest at the bottom. No secrets.

Live trading off. Never log an agent-initiated transfer (forbidden).

Entries that mention dollar/count testing caps are **`SUPERSEDED_HISTORICAL_RULE`** unless this file later records they were re-adopted by a human. They are **not** current policy.

---

## Decision fields

BUY ADD SELL REDUCE HOLD WATCH REJECT NO_ACTION

Symbol, sleeve, **security_class** (evidence, not assertion), timestamp, **current NAV**, allocation before/after proposed, price, thesis, bull/bear, catalysts, risks, fundamental/technical/news, horizon, invalidation, **concentration review** if &gt;10% NAV, justification, confidence, facts vs interpretation, daily-halt and HWM state, outcome.

---

## Thesis block

```
**Symbol / sleeve / security_class (evidence):**
**Decision:**
**Timestamp / current NAV / SOD NAV / HWM / drawdown / risk state / DAILY_RISK_HALT:**
**Allocation before / after proposed (% NAV):**
**Price:**
**Bull / bear / catalysts / risks:**
**Fundamental / technical / news:**
**Horizon / invalidation:**
**ENHANCED_CONCENTRATION_REVIEW:** n/a | attached (if name > 10% NAV)
**CONCENTRATION_LEVEL:** n/a | HIGH (individual equity > 15% NAV)
**Justification vs cash/alternatives:**
**If add to existing:** INVESTMENT_THESIS_REVIEW + RISK_REVIEW
**If decline (Opp):** TEMPORARY_PRICE_DISLOCATION | FUNDAMENTAL_BUSINESS_DETERIORATION
**Facts (MCP) / Interpretation (agent):**
**Risk:** matrix + daily halt + HWM
**Order plan:** none | paper only
**MCP NOT called:** review/place/cancel, option/crypto trading, transfers
```

---

## 2026-08-29 — Project initialized

**Type:** `config_change` / `portfolio_snapshot`  
**SUPERSEDED_HISTORICAL_RULE:** early docs introduced dollar/count testing gates later removed.

Observed (read-only): Agentic account; NAV $500; BP $500; cash $500; no positions. No broker review/place/cancel.

Those dollar figures are an **observed snapshot**, not a budget.

---

## 2026-08-29 — Architecture direction change

**Type:** `config_change`

Rejected Liquid Large-Cap Momentum V1. Three-layer pipeline. Execution disabled. Cash 100%. `NO_ACTION`.

---

## 2026-08-29 — Policy V1 first write-up

**Type:** `config_change`  
**SUPERSEDED_HISTORICAL_RULE (this entry’s “testing caps still active” and pending 10%/2% Speculative):**

That write-up still listed $500/$50/$250/5 names/3 trades/$15 daily/$5 floor as active testing gates, Speculative **2%** per name, and a **proposed 10%** universal individual-equity cap. **Those are not current policy.** See the amendment below.

Execution flags were already: `auto_execution=false`, `live_trade_actions_allowed=false`, `require_human_approval=true`.

---

## 2026-08-29 — Architectural correction: one scalable NAV-% policy

**Type:** `config_change`

**Active now:** percent-of-current-NAV policy only. No dollar/count investment rules. No NAV-tier strategies. No “migration” or “retire later.”

**Concentration matrix (hard % NAV):** Broad-market ETF 40 · Other diversified ETF 25 · Core stock 20 · Opp stock 15 · Tactical stock 10 · Speculative name 3 · Speculative sleeve 5.

**Daily halt:** −2% vs start-of-day NAV. **Adds:** thesis+risk review (no blanket averaging-down ban). **Class:** verifiable, fail closed.

**Execution unchanged:** `HUMAN_APPROVAL`, auto_exec false, live false, human approval true. No trades, no transfers this session.

**Book:** cash 100%. `NO_ACTION`. Observed NAV $500 is **not** a constraint.

---

## 2026-08-29 — Policy resolutions + context/risk kernel

**Type:** `config_change` / `risk_gate`

Resolved: broad-market **criteria** (seed list supporting only); sector 30% review / **45%** hard; liquidity vs ADV$ not price; HALTED = recommend-only; cash-flow-adjusted HWM; ETF fail-closed on insufficient evidence.

Implemented permanent `src/agentic_portfolio/` (context + risk gate). Tests: scale-invariant % rules. **No** review/place/cancel/transfer.

Execution flags unchanged.

---

## 2026-08-29 — Classification adapter + sleeve/thesis registries

**Type:** `config_change` / `classification` / `registry`

GICS-style 11-sector taxonomy with deterministic Robinhood/FactSet mapping. Session SOD uses NYSE calendar in America/New_York (not midnight). Correlation remains informational (no numeric cap). ETF holdings/weights not fabricated.

Permanent: Robinhood read adapter → ClassificationEvidence + provenance; evidence cache; sleeve registry; thesis registry; read-only reconciliation; paper research workflow. Candidate Discovery implemented in a later same-day entry.

**MCP called (read-only samples):** `get_equity_fundamentals`, `get_equity_tradability`, `search`, `get_equity_quotes`.

**MCP NOT called:** review/place/cancel, option/crypto trading, transfers.

Execution flags unchanged.

---

## 2026-08-29 — Candidate Discovery engine (read-only)

**Type:** `config_change` / `discovery`

Permanent component after market/security adapters and before deep Research / Thesis. Answers “what is worth researching?” — not “should we buy?”

Implemented: `Candidate` + structured `DiscoverySignal`s; four channels (Core / Opp / Tactical / Spec); sleeve-specific heuristic weights in `config/discovery.json` (not backtested); rejection + overlap-priority (not max-N diversity reject); portfolio-aware priority; sleeve-specific TTLs; persisted research queue and discovery runs. Discovery cannot emit BUY or ACTIVE theses.

**Live read-only run:** conclusion `CANDIDATES_READY_FOR_RESEARCH`. Same-sector crowding uses overlap priority / `DEFERRED_DUE_TO_RESEARCH_QUEUE_OVERLAP`, not a max-3 reject. See the 2026-08-30 overlap correction for the refreshed queue (AAPL/PLTR kept).

**MCP called (read-only):** `get_accounts`, `get_portfolio`, `get_equity_positions`, `get_scans`, `run_scan` (existing scan only), `get_watchlists`, `get_watchlist_items`, `get_popular_watchlists`, `get_earnings_calendar`, `search`, `get_equity_fundamentals`, `get_equity_quotes`, `get_equity_tradability`, `get_financials`, `get_equity_historicals`, `get_equity_news`.

**MCP NOT called:** review/place/cancel, `create_scan`, watchlist add/follow/update, option/crypto trading, deposits/withdrawals/transfers.

Execution flags unchanged: `auto_execution=false`, `live_trade_actions_allowed=false`, `require_human_approval=true`.

**Next permanent component:** Deep Research (`ResearchReport`).

---

## 2026-08-30 — Deep Research engine (read-only)

**Type:** `config_change` / `research`

Permanent component after Candidate Discovery and before Investment Thesis. Answers “is this attractive enough to justify a thesis?” — not “should we buy?”

Python collects observed facts and deterministic derived metrics into a `ResearchEvidencePacket`. A provider-agnostic `ResearchReasoner` interprets. Output is a persisted `ResearchReport` (`state/research_reports/`). History is never overwritten. Discovery no longer hard-rejects extra same-sector/sleeve names (`max_per_sector_per_sleeve` removed); they receive `OVERLAP_PRIORITY_PENALTY` / `DEFERRED_DUE_TO_RESEARCH_QUEUE_OVERLAP` and a comparison group.

**Live read-only pilot (4 of ~19 queued names, not a buy list):**

| Symbol | Sleeve | Conclusion | Confidence |
|---|---|---|---|
| NVDA | CORE_GROWTH | ADVANCE_TO_THESIS | MEDIUM |
| NKE | OPPORTUNISTIC | KEEP_WATCHING | MEDIUM |
| ESTC | TACTICAL | KEEP_WATCHING | LOW |
| SPY | CORE_GROWTH | ADVANCE_TO_THESIS | LOW (incomplete ETF packet) |

No ProposedAction. No ACTIVE theses. No orders. Observed NAV $500 is a snapshot, not a constraint.

**MCP called (read-only):** `get_accounts`, `get_portfolio`, `get_equity_quotes`, `get_equity_fundamentals`, `get_financials`, `get_equity_tradability`, `get_equity_news`, `get_earnings_results`, `get_sec_filing_index`, `get_sec_filing_facts`, `get_equity_historicals`, `get_equity_technical_indicators`.

**MCP NOT called:** review/place/cancel, `create_scan`, watchlist writes, option/crypto trading, deposits/withdrawals/transfers.

Execution flags unchanged: `auto_execution=false`, `live_trade_actions_allowed=false`, `require_human_approval=true`.

**Next permanent component:** Investment Thesis engine (`InvestmentThesis` from a completed `ResearchReport`).

---

## 2026-08-30 — Discovery overlap is a deferral, not a count reject

**Type:** `config_change` / `discovery`

Removed the remaining live-run effect of `max_per_sector_per_sleeve`. A research-worthy name in a crowded sector/sleeve stays a candidate. Rank-1 keeps full research priority; peers get `OVERLAP_PRIORITY_PENALTY` and `DEFERRED_DUE_TO_RESEARCH_QUEUE_OVERLAP`, stay on the queue, and share a comparison group so Research can compare AAPL vs MSFT vs NVDA vs AVGO. Portfolio Decision later chooses capital.

This is not a new holdings-count cap. Large-universe triage (hundreds of names → not 70% deep-researched) is a future AI Research step.

**MCP NOT called:** review/place/cancel, transfers.

Execution flags unchanged.

---

## 2026-08-30 — Investment Thesis + Portfolio Decision (paper)

**Type:** `config_change` / `thesis` / `portfolio_decision`

Permanent component after Deep Research and before Execution. Answers “should this position exist, at what % of NAV, versus cash / SPY / other researched names?” — not “is Risk Gate allowing it?” and not “place the order.”

Python validates, persists DRAFT theses, converts a valid AI decision into `ProposedAction`, and sends it to the existing Risk Gate. No PE/growth/RSI cutoffs. No broker stop orders. Theses stay DRAFT until a future real execution.

**Paper run (existing ResearchReports, 100% cash book):** NVDA BUY 5% DRAFT Core; NKE WATCH; ESTC WATCH; SPY NO_ACTION; CASH HOLD 95%. See `reports/2026-08-30_thesis_decision.md`.

**MCP NOT called:** review/place/cancel, `create_scan`, watchlist writes, option/crypto trading, deposits/withdrawals/transfers.

Execution flags unchanged: `auto_execution=false`, `live_trade_actions_allowed=false`, `require_human_approval=true`.

**Next permanent component:** Execution Controller (paper order plan only; still gated off).

---

## 2026-08-30 — Position Monitoring + Thesis Reassessment (paper)

**Type:** `config_change` / `monitoring` / `thesis_reassessment`

Permanent component after Portfolio Decision. Evaluates existing positions when new evidence arrives. Python owns facts, triggers, state, persistence, and hard safety. AI interprets. Reuses Research freshness/refresh, Thesis registry, Portfolio Decision, and Risk Gate. No separate investment-rule engine.

**Paper run (mocked holdings; live book still 100% cash):** NVDA CORE price decline → RESEARCH_REFRESH_REQUIRED / HOLD (not invalidated). NKE OPP → THESIS_WEAKENED / REDUCE. ESTC TACTICAL predefined SMA break → EXIT_CONDITION_TRIGGERED / SELL. IONQ SPEC catalyst fail → EXIT_CONDITION_TRIGGERED / SELL. Exit conditions are not broker stops. See `reports/2026-08-30_position_monitor.md`.

**MCP NOT called:** review/place/cancel, `create_scan`, watchlist writes, option/crypto trading, deposits/withdrawals/transfers.

Execution flags unchanged: `auto_execution=false`, `live_trade_actions_allowed=false`, `require_human_approval=true`.

**Next permanent component:** Execution Controller (paper order plan only; still gated off).

---

## 2026-08-30 — Execution Controller (paper OrderPlan)

**Type:** `config_change` / `execution`

Permanent mechanical component after Risk Gate. Converts a Risk-Gate-approved `ProposedAction` into a paper `OrderPlan`. No investment logic. BUY/ADD/REDUCE/SELL only. HOLD/WATCH/REJECT/NO_ACTION create no plan. Does not invent stop orders. Live review/place/cancel remain off. Status remains `PAPER_ONLY` / `BLOCKED_FROM_LIVE`.

**Paper run (current monitoring outputs):** NKE REDUCE paper plan; ESTC SELL paper plan; IONQ SELL paper plan; NVDA HOLD no order. See `reports/2026-08-30_order_plan.md`.

**MCP NOT called:** review/place/cancel, `create_scan`, watchlist writes, option/crypto trading, deposits/withdrawals/transfers.

Execution flags unchanged: `auto_execution=false`, `live_trade_actions_allowed=false`, `require_human_approval=true`.

**Next permanent component:** Paper fill / blotter reconciliation (still no `review_equity_order`).

---

## 2026-08-30 — Paper fill + blotter reconciliation

**Type:** `config_change` / `paper_fill`

Permanent mechanical component after Execution Controller. Simulates fills for `PAPER_ONLY` OrderPlans on an isolated paper book. Deterministic market fill at the eligible quote/reference price. Updates paper cash, quantity, average cost / FIFO lots, realized/unrealized P&L, sleeve/sector exposure, and NAV. Writes blotter + reconciliation. BUY may mark an isolated paper thesis ACTIVE. Live thesis/account state is untouched.

**Paper run:** NKE REDUCE filled; ESTC SELL filled (closed); IONQ SELL filled (closed); NVDA HOLD no fill. Monitoring re-run sees remaining NVDA + NKE only. See `reports/2026-08-30_paper_fill.md`.

**MCP NOT called:** review/place/cancel, `create_scan`, watchlist writes, option/crypto trading, deposits/withdrawals/transfers.

Execution flags unchanged: `auto_execution=false`, `live_trade_actions_allowed=false`, `require_human_approval=true`.

**Next permanent component:** Human-approval packet for a future live order (still no `review_equity_order`).

---

## 2026-08-30 — Human Approval Packet

**Type:** `config_change` / `human_approval`

Permanent packaging component after paper OrderPlan / paper fill. Takes a Risk-Gate-approved `ProposedAction` / `OrderPlan` plus thesis, research, decision, context, risk result, and optional monitoring state. Emits a human-readable `ApprovalPacket`. `APPROVED` does not place, review, or cancel a live order. Packets expire or are superseded when the quote, thesis/research, book, or risk state is no longer the frozen snapshot, or when a newer decision replaces them.

**Paper run (existing paper OrderPlans):** NKE REDUCE pending; ESTC SELL pending; IONQ SELL pending; NVDA HOLD no packet. See `reports/2026-08-30_approval.md`.

**MCP NOT called:** review/place/cancel, `create_scan`, watchlist writes, option/crypto trading, deposits/withdrawals/transfers.

Execution flags unchanged: `auto_execution=false`, `live_trade_actions_allowed=false`, `require_human_approval=true`.

**Next permanent component:** Live Robinhood order review remains gated off (`review_equity_order` still forbidden until live flags are explicitly enabled).

---

## 2026-08-30 — Robinhood Review-Only Bridge

**Type:** `config_change` / `robinhood_review_only`

Permanent preflight component after a still-valid APPROVED ApprovalPacket. Revalidates quote / portfolio / thesis / risk state, re-checks Risk Gate, and calls `review_equity_order` only. Persists `ReviewResult`. `REVIEW_ACCEPTED` does not execute. Does not place or cancel. Does not move money.

**Controlled test:** one APPROVED NKE REDUCE paper/live-shaped packet. Status `REVIEW_FAILED` (`REVIEW_DIFFERS_FROM_ORDER_PLAN`): Robinhood last $39.60 vs approved notional $200. Did not place. See `reports/2026-08-30_review.md`.

**MCP called:** `review_equity_order` (informational/preflight).

**MCP NOT called:** place/cancel, `create_scan`, watchlist writes, option/crypto trading, deposits/withdrawals/transfers.

Execution flags unchanged: `auto_execution=false`, `live_trade_actions_allowed=false`, `require_human_approval=true`.

**Next permanent component:** Live placement remains gated off (`place_equity_order` still forbidden).

---

## 2026-08-30 — LIVE runtime source of truth

**Type:** `config_change` / `live_portfolio_snapshot`

LIVE mode now uses the Agentic Robinhood account as the single source of truth for NAV, cash, buying power, positions, quantities, market values, allocations, concentration, HWM/drawdown, daily P/L baseline, dashboard, family-account scaling, monitoring holdings, and Risk Gate inputs.

Paper $10,000 book, paper fills, paper thesis activation, and paper execution state remain for tests/dev only. LIVE fails closed on the wrong account, missing Robinhood data, or paper contamination.

**Observed (read-only MCP):** Agentic `549688554` confirmed (`agentic_allowed`); NAV $500; cash $500; buying power $500; no positions. Weekend / regular hours closed. Live placement disabled.

**MCP called:** `get_accounts`, `get_portfolio`, `get_equity_positions`, `get_equity_quotes`, `get_equity_orders`.

**MCP NOT called:** `place_equity_order`, `cancel_equity_order`, `review_equity_order`, option/crypto trading, deposits/withdrawals/transfers.

Launch check: `PYTHONPATH=src python scripts/run_live_launch_check.py`.

Execution flags unchanged: `auto_execution=false`, `live_trade_actions_allowed=false`, `require_human_approval=true`.

**Next permanent component:** Live placement remains gated off. Real AI + `place_equity_order` still blocked until an explicit human enable.

---

## 2026-08-30 — Production AI Gateway (proposal-only)

**Type:** `config_change` / `ai_runtime`

Added a centralized AI Gateway (`src/agentic_portfolio/ai/`) with OpenAI and Anthropic adapters. No other application code may call an AI provider. Model roles live in `config/ai.json` (screening `gpt-5.6-luna`, research `gpt-5.6-terra`, escalation `gpt-5.6-sol`, fallback Claude Sonnet). Structured JSON schemas only. OpenAI adapter uses `POST /v1/responses`.

Hard global AI budget: **$10 USD per calendar month**. Every request estimates, reserves, executes only if allowed, records actual usage, and persists the ledger (`state/ai_budget/`). Restart cannot reset the cap. $8 conserving, $9.50 critical-only, $10 hard stop. The rest of the system continues when AI is blocked.

LIVE AI may research and create proposals against the confirmed Agentic snapshot. Invariants: `LIVE_AI_ALLOWED=true`, `LIVE_PROPOSALS_ALLOWED=true`, `LIVE_ORDER_PLACEMENT=false`. `place_equity_order` / `cancel_equity_order` remain forbidden. PAPER AI artifacts cannot appear as LIVE decisions.

Raspberry Pi scheduler: PREMARKET / MARKET HOURS / POSTMARKET. Cursor is development-only. AI providers are reasoning services. Risk Gate remains deterministic authority. Broker remains account source of truth.

Check: `PYTHONPATH=src python scripts/run_live_ai_check.py --scripted`. Real OpenAI: `PYTHONPATH=src python scripts/run_live_ai_check.py --use-real-ai`.

**MCP NOT called:** `place_equity_order`, `cancel_equity_order`, option/crypto trading, deposits/withdrawals/transfers.

---

## 2026-08-30 — First real OpenAI LIVE AI test preparation

**Type:** `config_change` / `ai_runtime`

Mapped production models to the restricted OpenAI project: screening `gpt-5.6-luna`, research `gpt-5.6-terra`, escalation `gpt-5.6-sol`. Anthropic remains optional fallback and is not configured. OpenAI adapter uses `POST https://api.openai.com/v1/responses` (not `/v1/chat/completions`) with `text.format` JSON schema. Pricing table updated from https://developers.openai.com/api/docs/pricing (2026-08-30 standard short-context uncached rates). Combined monthly cap remains **$10**. API key is read only from `OPENAI_API_KEY` and is not logged or persisted. `scripts/run_live_ai_check.py --use-real-ai` is the explicit real-provider path; a present key cannot silently fall back to scripted.

Invariants unchanged: `LIVE_AI_ALLOWED=true`, `LIVE_PROPOSALS_ALLOWED=true`, `LIVE_ORDER_PLACEMENT=false`. `auto_execution=false`, `live_trade_actions_allowed=false`. `place_equity_order` / `cancel_equity_order` remain forbidden.

**MCP NOT called:** `place_equity_order`, `cancel_equity_order`, option/crypto trading, deposits/withdrawals/transfers.

---

## 2026-08-30 — 24/7 Agent Runtime

**Type:** `architecture` / `runtime`

Corrected direction: the application is a 24/7 autonomous portfolio-management service, not a market-hours script. Added `src/agentic_portfolio/agent/` (session phases, job orchestrator, heartbeat, connection manager, lifecycle), persistent LIVE watch/thesis (`watch/`), LIVE approval queue (`live_approval/`; APPROVE → `APPROVED_AWAITING_EXECUTION_IMPLEMENTATION`), and dashboard notifications (`notify/`). Complete process: `scripts/run_service.py`. Pi unit: `deploy/systemd/agentic-portfolio.service`.

Invariants unchanged: `auto_execution=false`, `live_trade_actions_allowed=false`, `LIVE_ORDER_PLACEMENT=false`. No reachable live place/cancel/review path. $10/month AI cap; 24/7 does not mean constant AI calls.

**MCP NOT called:** `place_equity_order`, `cancel_equity_order`, option/crypto trading, deposits/withdrawals/transfers.

---

## 2026-08-30 — Raspberry Pi deployment hardening (LIVE, proposal-only)

**Type:** `config_change` / `ops`

Production systemd unit now runs as a dedicated non-root user (`User=agentic` / `Group=agentic`; replace with the actual Pi service username). Python lives in `/opt/agentic-portfolio/.venv`. `OPENAI_API_KEY` is loaded only from `/etc/agentic-portfolio/env` (`root:agentic` `640`). Robinhood OAuth remains user-specific (`~/.agentic-portfolio/readonly-mcp/`) and must be bootstrapped as the same account that runs systemd. Dashboard stays on `127.0.0.1:3100`; operator access is SSH port forwarding, not public exposure.

Invariants unchanged: `auto_execution=false`, `live_trade_actions_allowed=false`, `LIVE_ORDER_PLACEMENT=false`. No new broker mutation path. `--once` smoke test does not wire paid AI.

**MCP NOT called:** `place_equity_order`, `cancel_equity_order`, option/crypto trading, deposits/withdrawals/transfers.

---

## Template

```
## YYYY-MM-DD HH:MM ET — <title>
**NAV (dynamic) / HWM / DD / state / daily halt:**
**Sleeves % / cash % / holdings count (observed):**
**Decision / symbol / sleeve / class:**
**Thesis / concentration review:**
**MCP called / NOT called:**
**Capital transfer:** none (forbidden)
**Outcome:** not sent
```
