# Runbook

One loop for PAPER and LIVE: discovery → research → thesis/portfolio decision → Risk Gate → ProposalIntake → authorization (human or deterministic autonomous policy) → `OrderExecutor` (kill switch, send-time revalidation, Risk Gate re-run, broker review, place) → reconcile → position monitoring. See `ARCHITECTURE.md`.

Policy: `PORTFOLIO_POLICY.md`. Config: `config/execution_policy.json`, `config/portfolio_policy.json`, `config/account_rules.json`, `config/live_execution.json`, `config/pipeline.json`.

## Execution state

| Switch | Committed | How to change |
|---|---|---|
| `AGENTIC_EXECUTION_MODE` | `HUMAN_APPROVAL` | `/etc/agentic-portfolio/env` + restart (`OBSERVE` / `HUMAN_APPROVAL` / `AUTONOMOUS`) |
| `AGENTIC_LIVE_ORDER_PLACEMENT` | `false` | `/etc/agentic-portfolio/env` + restart |
| Kill switch | released | Dashboard System page, `scripts/kill_switch.py`, or `AGENTIC_KILL_SWITCH=1` |

`HALTED` (−20% vs HWM): nothing is authorized autonomously; SELL/REDUCE recommendations wait for you. Human-only resume.

## Daily operation in AUTONOMOUS mode

* Dashboard **System** shows mode, broker writes, kill switch, orders sent today vs limit, failure streak.
* Telegram: `ORDER AUTHORIZED` (policy authorized; executes in the next send window), `ORDER SUBMITTED/FILLED/REJECTED`, `TRADE APPROVAL REQUIRED` (escalated — open the approval to see why), `KILL SWITCH`.
* Escalations you should expect: Risk Gate `REQUIRES_ENHANCED_REVIEW` (>10% NAV single name, >30% sector, speculative liquidity), any HALTED exit, daily order/buy limits reached, cash reserve, missing snapshot.
* `python scripts/diagnose_pipeline.py` prints the whole funnel, authorized-but-unsent orders, escalations, and the execution status.

## Kill switch procedure

1. Engage: System page → **ENGAGE KILL SWITCH** (no password needed to stop), or `python scripts/kill_switch.py engage --reason "..."`.
2. Investigate: `logs/execution_audit.jsonl`, `logs/kill_switch.jsonl`, `state/live_execution/`, Robinhood order history. For an ambiguous submission, run `LIVE_ORDER_RECONCILE` (it runs every 5 minutes while the market is open) until no order is `UNKNOWN_RECONCILIATION_REQUIRED`.
3. Release: System page with your admin password and a note, or `python scripts/kill_switch.py release --by <name> --note "..."`. Approvals blocked while engaged were consumed; the pipeline re-proposes on fresh facts.

## Legacy offline paper toolchain

Historical stage list kept for the `scripts/run_paper_*.py` tools and their tests. The runtime does not use OrderPlans, approval packets, or review-only results; it uses the loop described at the top of this file.

1. Market data (incl. **SPY**); start-of-day NAV if a **valid U.S. equities session** is open (America/New_York; not midnight; weekends/holidays do not roll SOD)  
2. Regime (thin structured input; `UNKNOWN` if missing — do not fabricate)  
3. **Candidate Discovery** — research queue only (`config/discovery.json`). No BUY. No ACTIVE theses. `NO_HIGH_QUALITY_CANDIDATES` is valid. Same-sector names are overlap-penalized, not count-rejected.  
4. **Deep Research** — `ResearchReport` from a `ResearchEvidencePacket` + `ResearchReasoner`. Selective (queued names only). No BUY. No ACTIVE theses. Classification remains deterministic and fail-closed.  
5. **Investment Thesis + Portfolio Decision** — DRAFT thesis from a completed report; portfolio-level compare vs cash/SPY; `ProposedAction` to the existing Risk Gate. No ACTIVE theses. No broker stops. `NO_ACTION` is valid.  
6. Rank vs cash (inside the portfolio decision)  
7. Portfolio analysis: dynamic NAV, sleeves, cash, HWM/drawdown, **daily return vs SOD NAV**, overlap, P&L  
8. Allocation — quotas are not orders  
9. Risk: concentration matrix, sleeve Spec 5%, daily halt, HWM state, BP  
10. Order plan (paper Execution Controller). BUY/ADD/REDUCE/SELL only. HOLD/WATCH/REJECT/NO_ACTION create no plan.  
11. **Paper fill / blotter** — simulate PAPER_ONLY fills on an isolated paper book. Reconcile cash/quantity/P&L/NAV. No review/place/cancel. No stop orders.  
12. **Human approval packet** — package the Risk-Gate-approved action. `APPROVED` does not place while placement is off.  
13. **Send-time revalidation** — quote, NAV, cash, buying power, tradability, session, Risk Gate, thesis, duplicate-order, idempotency. Fail closed if anything changed.  
14. **LiveOrderExecutor** — sole `place_equity_order` caller, only if `AGENTIC_LIVE_ORDER_PLACEMENT=true`. Prefer fractional/dollar BUY. Ambiguous timeouts do not retry.  
15. **Reconcile** — poll broker status/fills, refresh account/positions, mark approval EXECUTED, journal.  
14. **Position monitoring** — existing holdings; facts/triggers; optional Research refresh; thesis reassessment; HOLD/ADD/REDUCE/SELL/NO_ACTION to Risk Gate. Exit conditions are not broker stops.  
15. Journal  

Discovery finds things to research. Research determines whether the opportunity is real. Portfolio Decision decides what we want to do. Risk Gate decides whether we are allowed to do it. Position Monitoring reassesses holdings when new evidence arrives. Execution Controller converts a permitted action into a paper OrderPlan. Paper fill simulates that plan on an isolated paper book. Human Approval Packet packages the plan for a human; approval still does not trade. Robinhood review-only asks the broker; it still does not place.

## Daily startup (read-only)

Confirm Agentic account. `PYTHONPATH=src python scripts/run_live_launch_check.py` fetches `get_portfolio` / `get_equity_positions` → **current NAV** (not a budget), persists `state/live_book`, and fails closed if paper state leaks. Positions, orders, P/L. SPY. Compute HWM, drawdown state, `DAILY_RISK_HALT` if SOD NAV known. Optional read-only discovery run (`PYTHONPATH=src python scripts/run_live_readonly_discovery.py`). Optional small-subset research run (`PYTHONPATH=src python scripts/run_live_readonly_research.py`). Optional paper thesis/decision run (`PYTHONPATH=src python scripts/run_paper_thesis_decision.py`). Optional paper position-monitor run (`PYTHONPATH=src python scripts/run_paper_position_monitor.py`). Optional paper execution run (`PYTHONPATH=src python scripts/run_paper_execution.py`). Optional paper fill run (`PYTHONPATH=src python scripts/run_paper_fill.py`). Optional paper approval run (`PYTHONPATH=src python scripts/run_paper_approval.py`). Optional review-only run (`PYTHONPATH=src python scripts/run_paper_review.py`). Journal snapshot. No place/cancel.

LIVE dashboard: `DASHBOARD_ENVIRONMENT=LIVE PYTHONPATH=src python scripts/run_dashboard.py`. Family NAV tracks the LIVE snapshot. Paper book remains for tests/dev only.

Proposal-only LIVE AI check: `PYTHONPATH=src python scripts/run_live_ai_check.py`. Uses the confirmed Agentic snapshot. Does not place. Default is a scripted provider so the $10 monthly cap is not consumed; `--use-real-ai` is opt-in.

The Raspberry Pi scheduler (`scripts/run_scheduler.py`) is an internal orchestrator inside the 24/7 Agent Runtime (`scripts/run_service.py`). The process never exits after one cycle. PREMARKET / MARKET HOURS / POSTMARKET / OVERNIGHT / WEEKEND / HOLIDAY jobs run according to session phase. Duplicate jobs are skipped. AI is not called on every tick.

`RESEARCH_QUEUE_WORKER` consumes the research queue during MARKET_OPEN (every 30 minutes, max 1 deep-research item) as well as overnight/premarket/postmarket/closed days (max 2). Lightweight `CANDIDATE_DISCOVERY` also runs during MARKET_OPEN every 15 minutes; broader discovery remains POSTMARKET / closed days. Discovery ending in `CANDIDATES_READY_FOR_RESEARCH` is no longer a terminal stop. Dynamic live universe construction is wired (`LIVE_DISCOVERY_WIRED=true`). Read-only dump: `PYTHONPATH=src python scripts/diagnose_pipeline.py`.

Committed defaults: `AGENTIC_EXECUTION_MODE=HUMAN_APPROVAL`, `AGENTIC_LIVE_ORDER_PLACEMENT=false`. With broker writes off, an approved order becomes `APPROVED_EXECUTION_DISABLED` and `place_equity_order` is not called. Enabling writes and autonomy are manual Pi steps (`deploy/README.md` → "going from validation to autonomous operation"), never git defaults.

Execution path: kill switch → send-time revalidation → Risk Gate re-run → OrderAuthority pre-send checks → `review_equity_order` → `place_equity_order` via `OrderExecutor` only → `LIVE_ORDER_RECONCILE` → holdings refresh. Ambiguous broker acks fail closed, trip the kill switch, and require reconcile. Double APPROVE and process restart share one execution intent id.

PAPER: `AGENTIC_RUNTIME_MODE=PAPER python scripts/run_service.py` runs the identical loop against a simulated PaperBroker account (state under `var/paper`), using real read-only market data.

$10/month AI cap: `config/ai.json` + persisted `state/ai_budget/`. $0–$8 normal, $8+ conserving, $9.50+ critical reassessment only, $10 blocks all external AI until next calendar month. Market monitoring, broker sync, Risk Gate, dashboard, watch conditions, and logs continue when AI is blocked.

## Drawdown / daily halt

See `RISK_RULES.md`. Daily halt: −2% vs start-of-day NAV → no new risk-increasing buys/adds; SELL/REDUCE still allowed; no Core auto-liq.

## Adds

Thesis + risk review. Concentration still absolute.

## Promotion

Optional paper `CAPITAL_INCREASE_RECOMMENDED`. Never move money.

## Shutdown

Engage the kill switch first if anything is in doubt. Snapshot NAV, HWM, sleeves, cash, halt flags. `python scripts/service_ctl.py stop` or `systemctl stop agentic-portfolio`.

## 24/7 production service

Complete local application (dashboard + runtime, no PowerShell after start):

```
$env:PYTHONPATH = "src"
$env:AGENTIC_RUNTIME_MODE = "LIVE"
$env:DASHBOARD_ENVIRONMENT = "LIVE"
python scripts/run_service.py
```

Dashboard: `http://127.0.0.1:3100` (localhost only). Approvals: `http://127.0.0.1:3100/approvals`. APPROVE is required. Default LIVE ORDER PLACEMENT is OFF.

Classify leftover scripted/PAPER/duplicate rows without deleting history: `PYTHONPATH=src python scripts/classify_artifacts.py`.

Raspberry Pi production: install as dedicated user `agentic` (or replace `User=`/`Group=` in the unit), venv at `/opt/agentic-portfolio/.venv`, secrets in `/etc/agentic-portfolio/env`. OAuth must be created as the same user that runs systemd. Full procedure: `deploy/README.md`. Unit: `deploy/systemd/agentic-portfolio.service`. Do not run as root. SSH tunnel: `ssh -L 3100:127.0.0.1:3100 <pi>`.
