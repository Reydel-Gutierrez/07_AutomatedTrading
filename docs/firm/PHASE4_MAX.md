# Phase 4 — Max, Short-Term Trading Manager (and Graham's capital pool)

## Max: trend-pullback swing trading (no AI)

Code: `src/firm/managers/max.py`, tests: `tests/firm_suite/test_max.py`.

| Part | Rule (mandate defaults, owner-adjustable on Max's page) |
|---|---|
| Universe | 22 highly liquid names: SPY, QQQ, IWM, DIA, nine sector SPDRs, SMH, and eight mega caps. The desk refuses anything else. |
| Entry (15:15 ET) | Price above the 200-day average **and** RSI(2) below 10: an oversold dip inside an uptrend. Most oversold first, up to 4 positions of 25% of the sleeve each. The live price stands in for today's close. |
| Stop (every run) | Sell when the price is at or below entry × (1 − 6%). Checked at 10:30 ET and 15:15 ET. |
| Exits (15:15 ET) | Target: close above the 5-day average. Time: 10 sessions held. Trend: close below the 200-day average. |
| Sleeve stops | Owner-approved: new buys pause at −10% from the sleeve's peak (owner resumes) or −3% in a day (clears next day). |
| Limits | Spread ≤ 20 bps, ≤ 10 orders/day, minimum trade $5, cash only, inside the firm's 10:00–15:30 window. |

Stops are evaluated twice a day, not continuously: a gap or a fast intraday fall can fill
below the stop level. That is the trade-off of running on the firm's scheduler rather than
placing resting stop orders at the broker, which the desk does not support today.

Rollout: built and deployed INACTIVE. The owner allocates capital and switches Max to SIM.
Validation before LIVE: at least 20 trading days in SIM (a few dozen trades), zero reconcile
breaks, and the owner's review of his trade log.

### Entry guards (owner-approved 2026-10-16)

Graham's first-week review showed two weaknesses: Max bought SMH back the same afternoon it
hit its stop, and all four positions (NVDA, QQQ, SMH, XLK) were one tech bet. Two guards now
apply to new entries only (exits are unchanged):

* **Cooldown after a stop.** No re-entry in a name stopped out within the last
  `stop_cooldown_sessions` sessions (default 3; a stop earlier the same day is session 0). Read
  from the desk's filled sell orders whose reason starts with "stop hit".
* **Group cap.** At most `max_per_group` positions (default 2) in names that move together:
  tech (QQQ, XLK, SMH, NVDA, AVGO, AAPL, MSFT, META, GOOGL), broad market (SPY, DIA, IWM),
  consumer (XLY, AMZN), financials (XLF, JPM); any other name is its own group. The mandate's
  `groups` map overrides or extends this.

Both use code defaults, so mandates written before them pick them up without a new version.
Skipped names show in the run detail as `cooling_off` and `group_full`.

## Graham's capital pool

Code: `src/firm/capital_pool.py`, tests: `tests/firm_suite/test_pool.py`.

* The owner funds the pool like a manager: HQ → Capital → "Graham's pool", or the deposit split.
* Graham moves pool money to managers on his own judgment of performance, with tools
  `allocate_from_pool` / `recall_to_pool`. He reviews when the pool is funded and weekly
  (trading days, 10:30–15:00 ET); each review is a CIO conversation the owner can read.
* Hard rules in code: only LIVE managers receive pool money; a grant may not push a manager
  above the owner's cap (share of the whole firm; defaults Tim 90%, Alex 30%, Max 10%);
  recalls only of Graham's own net grants and only from free cash; minimum $5; at most 4
  moves a day; every move is a `CIO_ALLOCATION` event, alerted to the owner's phone.
* Money the owner allocates directly to a manager is never Graham's to move.
