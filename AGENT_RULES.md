# Agent Rules

Policy: `PORTFOLIO_POLICY.md`. Risk: `RISK_RULES.md`. Architecture: `ARCHITECTURE.md`.

AI may reason and may **tighten**. AI may **never** loosen hard ceilings, skip classification, or move money.

## Layers

1. Intelligence — `STRATEGY.md`  
2. Risk — `config/portfolio_policy.json` (hard veto)  
3. Authorization — `config/execution_policy.json` (human or deterministic autonomous policy; never AI)  
4. Execution — `OrderExecutor` only (PAPER → PaperBroker, LIVE → Robinhood when `AGENTIC_LIVE_ORDER_PLACEMENT=true`)  

`NO_ACTION` always valid. Never buy to fill sleeve targets.

## Identity

Agentic `account_number` in `config/account_rules.json` only. Confirm via `get_accounts` before any future mutation. Long US stocks/ETFs only.

NAV is whatever `get_portfolio` returns on the configured Agentic account. It is **not** a policy input you hardcode. LIVE runtime must not use paper $10,000 NAV, paper positions, paper fills, or paper thesis activation.

## Execution

| Question | Answer (single source) |
|---|---|
| Who may authorize an order? | `config/execution_policy.json` / `AGENTIC_EXECUTION_MODE`: `OBSERVE` (nobody), `HUMAN_APPROVAL` (a human), `AUTONOMOUS` (the deterministic OrderAuthority for clean PASS proposals within limits; everything else → human). Committed default `HUMAN_APPROVAL`. |
| May the executor write to Robinhood? | `AGENTIC_LIVE_ORDER_PLACEMENT=true` **and** the write transport is bound (LIVE). PAPER always writes to the PaperBroker. |
| Is everything stopped? | Kill switch (dashboard / `scripts/kill_switch.py` / `AGENTIC_KILL_SWITCH`). Human release only. |

`HALTED` → no autonomous authorization of anything; SELL/REDUCE are escalated to a human. No self-resume.

## Forbidden

Never: AI self-approval or authorization, AI access to any broker client, `cancel_equity_order`, option/crypto trading, deposit/withdraw/transfer.

`place_equity_order` / `review_equity_order` are called only by `OrderExecutor` (`live_execution/executor.py`), after authorization (human APPROVE or the deterministic autonomous policy), kill-switch check, send-time revalidation, and a full Risk Gate re-run on fresh facts.

## Never (hard)

- Other accounts; options/crypto/futures; shorts; margin borrow; leveraged ETFs; extended hours  
- Exceed matrix: 40 / 25 / 20 / 15 / 10 / 3 / 5 (see policy)  
- Exceed **45%** sector NAV (intentional increase)  
- Treat unverified ETF as `BROAD_MARKET_INDEX_ETF`  
- Bypass daily 2% halt or HWM states  
- Add only to lower cost basis  
- Invent auto-liquidation
- Silently reassign sleeves (TACTICAL → CORE_GROWTH requires SLEEVE_RECLASSIFICATION_REVIEW)
- Fabricate ETF holdings or sector weights
- Reset daily-risk SOD on weekend/holiday midnight
- Skip Discovery → Research → Thesis and jump a candidate to BUY
- Treat `URGENT_RESEARCH` as trade urgency
- Hard-reject Discovery candidates solely because N same-sector/sleeve names appeared first
- Let Research rewrite NAV, positions, classification, or risk limits

Canonical sectors are GICS-style 11 + UNKNOWN. SOD NAV uses America/New_York equity sessions.  

## Output

Thesis: `config/thesis_schema.json` with sleeve, **verified** `security_class`, NAV, % before/after, facts vs interpretation. Enhanced concentration review if name > 10% NAV.

Discovery output is a `Candidate` / research-queue entry, not a thesis and not an order. Do not call `review_equity_order` / `place_equity_order` / `cancel_equity_order` from Discovery. Do not turn a candidate into BUY.

Research output is a `ResearchReport`. Do not call execution tools from Research. Do not treat `ADVANCE_TO_THESIS` as permission to trade. Portfolio Decision and Risk Gate still apply.

Thesis/Decision output is a DRAFT `InvestmentThesis` plus a `ProposedAction` sent to Risk Gate. Do not call execution tools. Do not activate the thesis. Do not create broker stop orders. `NO_ACTION`, cash, and SPY are valid.

Position monitoring output is a monitoring state plus HOLD/ADD/REDUCE/SELL/NO_ACTION. Do not call execution tools. Do not treat an exit condition as a broker stop. Price movement alone must not invalidate CORE. Python detects facts/triggers; AI interprets. Reuse Research, Thesis, Portfolio Decision, and Risk Gate — do not add a separate stock-picking rule engine.

Execution Controller output is a paper `OrderPlan` for BUY/ADD/REDUCE/SELL. HOLD/WATCH/REJECT/NO_ACTION create no plan. Do not call `review_equity_order` / `place_equity_order` / `cancel_equity_order`. Do not invent stop orders. Status remains `PAPER_ONLY` / `BLOCKED_FROM_LIVE`. (Legacy offline tool; the runtime uses ProposalIntake → OrderExecutor.)

Paper fill output is a `PaperFill`, blotter line, updated isolated paper book, and reconciliation result. Fill at the eligible quote/reference price. Do not apply invented broker behavior. Do not call execution tools. Do not move money. Do not modify live thesis/account state. A paper BUY fill may mark an isolated paper thesis ACTIVE.

Order proposals (runtime) go through `autonomy.ProposalIntake` only: Risk-Gate-permitted `ProposedAction` → persisted snapshot → `OrderAuthority` → `PENDING` (human) or `APPROVED` (autonomous) → `OrderExecutor`. OBSERVE mode journals the proposal and creates no order.

The `ApprovalPacket` / `ReviewResult` objects of the legacy offline paper toolchain (`approval/`, `review/`, `scripts/run_paper_*.py`) are not part of the runtime. They never place.

Python collects facts and calculates deterministic values. AI Research interprets evidence. AI Thesis/Decision allocates and chooses action. AI Monitoring reassesses existing theses. Execution is mechanical. Do not expand Python with large qualitative stock-picking rule sets.

## Production AI runtime

Cursor = development agent. Raspberry Pi application = 24/7 autonomous production runtime (`scripts/run_service.py`). The scheduler is internal orchestration, not process lifetime. AI providers (OpenAI, Anthropic) = reasoning services used only through the AI Gateway. Risk Gate = deterministic permission. OrderAuthority = deterministic authorization. Broker = authoritative account/execution state.

The process stays alive on weekends and holidays. Off-hours work must not treat stale liquidity as executable; off-hours authorizations wait for the next send window and are valid for that session only. With `AGENTIC_LIVE_ORDER_PLACEMENT=false` an approved order becomes `APPROVED_EXECUTION_DISABLED` and nothing is placed.

AI must **never** be described as having trading authority. Autonomy is the deterministic policy acting on AI *proposals*, inside hard limits, with a kill switch.

AI never calls `place_equity_order`, `review_equity_order`, or `cancel_equity_order`, and never calls an approve/authorize method. The only placement surface is `src/agentic_portfolio/live_execution/executor.py`. Any blocked attempt fails closed and writes an audit event.

All external AI calls go through `src/agentic_portfolio/ai/gateway.py`. Model names live in `config/ai.json`. Combined spend across every provider and model must never exceed **$10 USD per calendar month**. Structured JSON only. Application facts are assembled in Python; the model interprets them and must not replace NAV/cash/positions.

LIVE AI artifacts (`state/live_ai/`) must not mix with PAPER thesis/approval artifacts.
