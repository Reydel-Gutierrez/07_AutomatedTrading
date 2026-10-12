# Phase 3 — Alex, Strategic Growth Manager

Alex invests his sleeve in large US stocks with a systematic screen, and uses AI
research only to veto names the screen picked. Code: `src/firm/managers/alex.py`
(decisions), `src/firm/research.py` (research desk), tests in
`tests/firm_suite/test_alex.py`.

## Strategy (mandate v2 defaults; owner-adjustable on Alex's page)

| Step | Rule |
|---|---|
| Universe | ~80 large, liquid US stocks across sectors (`universe` in the mandate). The desk refuses any order outside it. |
| Screen | Score = 12-1 month momentum (close 21 sessions ago ÷ close 252 sessions ago − 1) ÷ annualized 63-day volatility. Eligible only when the last close is above the 200-day average and momentum is positive. Stored daily closes; deterministic. |
| Research | Each trading day from 08:30 ET the desk researches the top 16 eligible names (and any held name) lacking a note from the last 30 days, at most 12 per day. Claude (claude-opus-5-5, effort medium) with web search returns approve/reject, conviction 1–5, thesis, risks, sources. A source is kept only if it appeared in that call's search results. |
| Portfolio | Top 8 names with a fresh approval, equal weight, ≤ 15% each, 2% cash buffer. |
| Exits (daily) | Close below the 200-day average, research turned to reject, or the name left the universe → sell the whole position. |
| Rebalance | First run of each month (and on allocation/mode/mandate changes): sell names that left the target, then buy underweights. Idle cash ≥ 5% of the sleeve is invested between rebalances. Sales first; proceeds invested 10 minutes later. |
| Run time | 10:20 ET on trading days, inside the firm's 10:00–15:30 trading window. |

## Risk (unchanged, owner-approved)

* Sleeve pause at −20% from the sleeve's peak (`sleeve_drawdown_pause`); exits continue.
* All firm gates apply: kill switch, reconcile hold, broker-write switch, turnover breaker,
  spread ≤ 30 bps, ≤ 16 orders/day, minimum trade $5, cash only.
* AI spend is capped at Alex's share of the monthly AI budget (35% of $25 by default); each
  note costs roughly $0.10–0.25 (tokens plus $0.01 per web search) and is recorded.

## Rollout

1. Built and deployed INACTIVE (this release).
2. Owner allocates capital to Alex and switches him to **SIM**. Research runs the same day;
   simulated trades follow at the next run.
3. Validation gate before LIVE: ≥ 10 trading days in SIM, research notes reviewed by the
   owner, zero reconcile breaks. LIVE is the owner's switch on Alex's page.
