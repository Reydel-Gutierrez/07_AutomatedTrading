# Runbook

Paper pipeline. Robinhood `review_equity_order` after a still-valid APPROVED packet. No place/cancel. No transfers.

Policy: `PORTFOLIO_POLICY.md`. Config: `config/pipeline.json`, `config/portfolio_policy.json`, `config/account_rules.json`.

## Execution state (unchanged)

`HUMAN_APPROVAL` · `auto_execution=false` · `live_trade_actions_allowed=false` · `require_human_approval=true`.

`HALTED` keeps auto-exec false. Human-only resume.

## Paper pipeline

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
12. **Human approval packet** — package the OrderPlan for a human. `APPROVED` does not place.  
13. **Robinhood review-only** — revalidate + Risk Gate re-check + `review_equity_order`. Persist `ReviewResult`. **Stop.** `REVIEW_ACCEPTED` does not execute. No place/cancel.  
14. **Position monitoring** — existing holdings; facts/triggers; optional Research refresh; thesis reassessment; HOLD/ADD/REDUCE/SELL/NO_ACTION to Risk Gate. Exit conditions are not broker stops.  
15. Journal  

Discovery finds things to research. Research determines whether the opportunity is real. Portfolio Decision decides what we want to do. Risk Gate decides whether we are allowed to do it. Position Monitoring reassesses holdings when new evidence arrives. Execution Controller converts a permitted action into a paper OrderPlan. Paper fill simulates that plan on an isolated paper book. Human Approval Packet packages the plan for a human; approval still does not trade. Robinhood review-only asks the broker; it still does not place.

## Daily startup (read-only)

Confirm Agentic account. `get_portfolio` → **current NAV** (not a budget). Positions, orders, P&L. SPY. Compute HWM, drawdown state, `DAILY_RISK_HALT` if SOD NAV known. Optional read-only discovery run (`PYTHONPATH=src python scripts/run_live_readonly_discovery.py`). Optional small-subset research run (`PYTHONPATH=src python scripts/run_live_readonly_research.py`). Optional paper thesis/decision run (`PYTHONPATH=src python scripts/run_paper_thesis_decision.py`). Optional paper position-monitor run (`PYTHONPATH=src python scripts/run_paper_position_monitor.py`). Optional paper execution run (`PYTHONPATH=src python scripts/run_paper_execution.py`). Optional paper fill run (`PYTHONPATH=src python scripts/run_paper_fill.py`). Optional paper approval run (`PYTHONPATH=src python scripts/run_paper_approval.py`). Optional review-only run (`PYTHONPATH=src python scripts/run_paper_review.py`). Journal snapshot. No place/cancel.

## Drawdown / daily halt

See `RISK_RULES.md`. Daily halt: −2% vs start-of-day NAV → no new risk-increasing buys/adds; SELL/REDUCE still allowed; no Core auto-liq.

## Adds

Thesis + risk review. Concentration still absolute.

## Promotion

Optional paper `CAPITAL_INCREASE_RECOMMENDED`. Never move money.

## Shutdown

Leave auto-exec false. Snapshot NAV, HWM, sleeves, cash, halt flags.
