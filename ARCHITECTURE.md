# Architecture

AI agentic portfolio manager on Robinhood Trading MCP. **One** percent-of-NAV policy at any Agentic NAV.

Canonical policy: `PORTFOLIO_POLICY.md` + `config/portfolio_policy.json`.  
Reasoning: `STRATEGY.md`. Risk: `RISK_RULES.md`.

Live trading **off** except informational `review_equity_order` after a still-valid APPROVED packet. Stop after Robinhood review-only. No place/cancel. No transfers.

## Layers

| Layer | Authority |
|---|---|
| AI intelligence | Advisory |
| Deterministic risk (class + sleeve % NAV, daily halt, HWM) | Hard veto; agent may tighten only |
| Execution | Paper OrderPlan + paper fill/blotter + human approval packet + Robinhood review-only; place/cancel gated off |

## Components

Keep these modules separate:

| Module | Role |
|---|---|
| Robinhood Read Adapter | Facts only (`src/agentic_portfolio/adapters/`) |
| Portfolio Context | Observed book + session SOD + HWM |
| Security Classification | Deterministic class; AI cannot override |
| Sleeve Registry | Persisted role; no silent sleeve changes |
| Thesis Registry | Decision/review records; not a reasoning engine. New theses from this stage stay DRAFT until a future real execution. |
| Candidate Discovery | Research-queue generation (`src/agentic_portfolio/discovery/`). Finds what is worth researching. Does **not** buy, size, or write ACTIVE theses. Same-sector names are overlap-penalized and comparison-grouped, not count-rejected. |
| Deep Research | Advisory interpretation (`src/agentic_portfolio/research/`). Python collects facts and derived metrics. A provider-agnostic `ResearchReasoner` interprets. Output is a persisted `ResearchReport`. Does **not** allocate, buy, or bypass Risk Gate. |
| Investment Thesis + Portfolio Decision | Advisory (`src/agentic_portfolio/decision/`). AI forms/updates a DRAFT thesis and chooses BUY/ADD/HOLD/REDUCE/SELL/WATCH/REJECT/NO_ACTION vs cash and SPY. Python validates, persists, converts to `ProposedAction`, and sends it to the existing Risk Gate. No stock-picking thresholds. No broker stops. |
| Position Monitoring + Thesis Reassessment | Advisory (`src/agentic_portfolio/monitoring/`). Python collects position facts and detects triggers. AI interprets new evidence. Meaningful triggers request Research refresh, reassess the thesis, run Portfolio Decision, and send HOLD/ADD/REDUCE/SELL/NO_ACTION to Risk Gate. Price movement alone does not invalidate CORE. Exit conditions are not broker stop orders. |
| Risk Validation | Hard veto |
| Execution Controller | Mechanical paper OrderPlan (`src/agentic_portfolio/execution/`). BUY/ADD/REDUCE/SELL only. HOLD/WATCH/REJECT/NO_ACTION create no plan. Does not invent stop orders. Live review/place/cancel remain off. |
| Paper Fill + Blotter | Mechanical paper simulator (`src/agentic_portfolio/paper_fill/`). Fills PAPER_ONLY plans at the eligible quote/reference price. Updates isolated paper cash/quantity/average cost/P&L/NAV. Reconciles the blotter. Does not call live order tools. Does not modify live thesis/account state. |
| Human Approval Packet | Packaging (`src/agentic_portfolio/approval/`). Turns a Risk-Gate-approved paper OrderPlan plus thesis/research/decision/risk/context into a human-readable packet. APPROVED does not place an order. Packets expire or are superseded when facts drift. |
| Robinhood Review-Only | Preflight (`src/agentic_portfolio/review/`). Takes a still-valid APPROVED ApprovalPacket, revalidates, re-checks Risk Gate, and calls `review_equity_order`. Persists `ReviewResult`. REVIEW_ACCEPTED does not execute. Does not place or cancel. |
| Journal | Append-only records |

Discovery finds things to research. Research determines whether the opportunity is real. Portfolio Decision decides what we want to do. Risk Gate decides whether we are allowed to do it. Position Monitoring decides whether existing holdings still deserve that chain. Execution Controller converts a permitted action into a paper OrderPlan. Paper fill simulates the fill on an isolated paper book. Human Approval Packet packages that plan for a human; approval still does not trade. Robinhood review-only asks the broker what it would say; it still does not place.

```
MARKET / SECURITY DATA (read-only adapters + classification)
        ↓
CANDIDATE DISCOVERY
        ↓
DEEP RESEARCH
        ↓
INVESTMENT THESIS
        ↓
PORTFOLIO DECISION
        ↓
DETERMINISTIC RISK GATE
        ↓
EXECUTION CONTROLLER (paper OrderPlan; live gated off)
        ↓
PAPER FILL / BLOTTER (isolated paper book; live gated off)
        ↓
HUMAN APPROVAL PACKET (APPROVED still does not place)
        ↓
ROBINHOOD REVIEW-ONLY (review_equity_order; REVIEW_ACCEPTED does not place)
        ↓
POSITION MONITORING / JOURNAL
```

Discovery must not skip later stages. A candidate cannot become a BUY `ProposedAction`. Research cannot either. A favorable `ResearchReport` is not permission to trade.

## Responsibility split

| Stage | Owns |
|---|---|
| Data / Python | Retrieve, normalize, provenance, persist, portfolio/account facts, deterministic calculations, deterministic safety/risk |
| AI Research | Interpret evidence, compare, judge dislocation vs deterioration, form research conclusions |
| Thesis / Portfolio Decision | Form/update DRAFT thesis; choose action and desired % NAV vs cash/SPY/peers (not yet permitted) |
| Risk Gate | Whether a proposed action is permitted (absolute authority on limits) |
| Execution | Paper OrderPlan + paper fill/blotter + human approval packet + Robinhood review-only; place/cancel gated off |

AI cannot rewrite observed facts, override classification, alter NAV/positions, or modify risk limits.

Holdings count is **observed**, not a cap.

## Pipeline

```
MARKET DATA (NAV, SPY, session SOD NAV — America/New_York)
  → REGIME (thin input; UNKNOWN if missing — never fabricated)
  → CANDIDATE DISCOVERY (provisional sleeve + research queue; no BUY; overlap penalty not max-N reject)
  → DEEP RESEARCH (ResearchEvidencePacket → ResearchReasoner → ResearchReport; no BUY)
  → INVESTMENT THESIS + PORTFOLIO DECISION (DRAFT thesis; compare vs cash/SPY; ProposedAction; no execution)
  → DETERMINISTIC RISK GATE
  → ORDER PLAN (paper Execution Controller; BUY/ADD/REDUCE/SELL; no stops)
  → PAPER FILL / BLOTTER (isolated paper book; no broker)
  → HUMAN APPROVAL PACKET (human-readable; APPROVED does not place)
  → ROBINHOOD REVIEW-ONLY (review_equity_order; REVIEW_ACCEPTED does not place) → ████ STOP ████
  → (future) place
  → POSITION MONITORING (existing holdings; thesis reassessment; ProposedAction to Risk Gate; no broker stops)
  → JOURNAL
```

## NAV

Always `current_NAV` from the Agentic account. Formulas in policy JSON. No encoded portfolio size.

## MCP

Read-only plus informational `review_equity_order` after a still-valid APPROVED packet. Forbidden now: place/cancel. Never: option/crypto trading; any transfer/deposit/withdraw.

## Config

- Policy % / halt / HWM / class → `config/portfolio_policy.json`  
- Account + execution flags → `config/account_rules.json`  
- Discovery heuristics (not backtested) → `config/discovery.json`  
- Research freshness + sleeve questions → `config/research.json`  
- Thesis + portfolio decision (no buy thresholds) → `config/decision.json`  
- Position monitoring + thesis reassessment (no investment-rule engine) → `config/monitoring.json`  
- Paper Execution Controller / OrderPlan (no broker calls) → `config/execution.json`  
- Paper fill + blotter reconciliation (isolated paper book; no broker calls) → `config/paper_fill.json`  
- Human Approval Packet (no broker calls; APPROVED does not place) → `config/approval.json`  
- Robinhood review-only (`review_equity_order` preflight; does not place) → `config/review.json`  
- Pipeline stage flags → `config/pipeline.json`  

## Kernel (permanent)

`src/agentic_portfolio/` — portfolio context, session SOD, classification adapter, sleeve/thesis registries, candidate discovery, deep research, investment thesis + portfolio decision, position monitoring + thesis reassessment, deterministic risk gate, **paper Execution Controller**, **paper fill + blotter**, **human approval packet**, **Robinhood review-only**. Theses created at Decision stay DRAFT until a future real execution. A paper BUY fill may mark an isolated paper thesis ACTIVE; live thesis/account state is untouched. Monitoring may mark an existing thesis WEAKENED/INVALIDATED; that is not a broker stop and not live trading. Execution Controller does not invent stop orders. Paper fill does not invent broker behavior. Approving a packet does not place an order. Review-only may call `review_equity_order` and must not place or cancel.

Facts (NAV, cash, positions) vs interpretation (research/thesis/monitoring) stay separated. Classification is deterministic. Discovery scores structured signals; crowded sector/sleeve names get `OVERLAP_PRIORITY_PENALTY` / `DEFERRED_DUE_TO_RESEARCH_QUEUE_OVERLAP` and a comparison group — they are not hard-rejected. Research interprets a `ResearchEvidencePacket` through `ResearchReasoner` (programmatic; Cursor is not the runtime). Tests in `tests/` prove **scale invariance** (same % → same verdict at $1k–$1M; identical opportunity → identical discovery score; research conclusions do not depend on a hardcoded NAV).
