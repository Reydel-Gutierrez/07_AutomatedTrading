# Equity Academy: the Stock Swing Class

Status (2026-10-16): **built** (Floors 1 and 2). The owner approved the build, asked Claude to name
the agents, allowed each agent its own list of names, and decided that a winner gets **its own live
seat** (beside Max's desk, not replacing it), or more tests, by the owner's approval. The live
seat's trading code is built when the first challenger finishes Floor 2; nothing here can place an
order.

## What runs where

| Piece | Where |
|---|---|
| Engine (Floor 1 sprints, Floor 2 paper records, owner commands) | `src/equity/`, `scripts/equity_academy.py`; a separate process started by the firm service (`FIRM_EQUITY_ACADEMY=off` disables it), nice 15, allowlisted environment |
| State | `state/equity_academy/academy.db`, `engine.log`, `inbox/` (`EQUITY_STATE_ROOT` overrides) |
| Market data | the firm's daily bars (`firm.db`, read only). The firm's `equity_academy_bars` job (17:15 New York) fetches the names the engine asks for; a name with under 1,000 stored bars gets ~6.5 years once |
| HQ | `/equity-academy` (class and agent pages, linked from the Academy page), `/api/equity-academy`, Graham's `get_stock_swing_class`, one line in the 08:00 message |
| Map | The class lives in **The 537 Academy**: "Stock Swing Class · Research" on Floor 1 and "Stock Swing Class · Paper" on Floor 2, beside the forex rooms (the building was widened for them). L3 Equity Trading is for real-money traders only: the **live swing desk**, where Max holds live seat 1 and seats 2 to 4 wait for graduates the owner approves |

## The class

| Agent | Strategy | Names |
|---|---|---|
| **Juniper**, steady dip buyer | RSI(2) pullback in an uptrend, stops sized to each name's daily range (ATR), 5 positions | Max's 22 ETFs and mega caps |
| **Rook**, breakout trader | close above the prior 20-day high in an uptrend, ATR trailing stop, 5 positions | 20 large-cap leaders across sectors |
| **Meridian**, sector rotator | every 5 sessions hold the top 3 by 63-day return that are above their trend | 11 sector ETFs, SMH, GLD, TLT |
| **Talon**, high-beta risk taker | the pullback rule on volatile growth names, 20% positions, wide ATR stops | TSLA, AMD, MU, PLTR, COIN, SHOP, NET, CRWD, DDOG, SNOW, RBLX, DKNG, AFRM, ROKU, ENPH |

Every agent runs with the desk's guards: no re-entry for 3 sessions after a stop, and at most 2
positions per group of names that move together.

## Simulation (the same code for research and paper)

Decisions at a day's close use only bars up to that day; orders fill at the next open plus costs
(5 to 10 bps a fill by agent). Stops fill at the stop, or at the open when a day gaps through it; a
buy whose next open is already below its stop is refused. Long only, no leverage, fractional shares.
**Cash account:** sale proceeds settle the next session. Buys may use unsettled proceeds, as
Robinhood allows, but selling that position before the money settles is a good-faith violation, and
the run counts them.

## Floor 1: research (nightly, 01:30 New York)

Same method as the forex Academy: up to 16 variants around the current version (settings moved,
the weakest name dropped, two-setting combinations), scored on the research window in four periods,
then the best goes through the gates. Gates (all must pass):

- history: 4+ years after warm-up for at least two thirds of the agent's names
- evidence: 40+ trades on unseen data (out-of-sample plus the locked final year)
- expectancy: unseen average R > 0 and profit factor >= 1.1
- Sharpe: unseen Sharpe >= 0.5 after costs
- hold-out: the locked final year's average R >= -0.05
- skill: beats 20 random-name baselines (same entry days, stop distances and exits) at p <= 0.10
- luck: trial-adjusted Sharpe >= 0.3 (stricter with every variant the agent has ever tried)
- cost stress: still profitable on unseen data with 10 bps more cost on every fill
- drawdown: at least 80% of rolling one-year windows stay within the desk's 10% sleeve stop
- cash account: never 3 good-faith violations within 12 months
- day-1 test: 30 fresh $10,000 accounts on random start dates, 6 months each: >= 55% profitable,
  median > 0, 10th percentile >= -8%

Calibration on synthetic data: 0 of 48 sprints passed on random-walk prices (no edge), and a
built-in short-term reversal edge passed in most runs.

## Floor 2: paper trading

From the night it passes, an agent's $10,000 paper record is rebuilt each time new daily bars arrive,
with its frozen version, starting from its last known close before promotion. HQ shows it week by
week and against Max's desk over the same days. After at least 20 sessions and 10 trades, the owner
decides: a live seat, or more tests (back to research). Graham can read it all but can't move anyone.

## Known limits

- **Survivorship**: each list is today's names, which flatters backtests, especially Talon's. Floor 2
  weighs more than backtests.
- **Prices only**: dividends aren't modeled, so ETF returns are slightly understated.
- **History**: Robinhood's daily history may not reach back far enough for the youngest names; an
  agent waits (NO_DATA) rather than testing on too little.
- **Day trading** stays out: in a cash account of this size it wouldn't be meaningful.

## Next, when a challenger finishes Floor 2

1. The live seat: a firm manager that runs the challenger's frozen version through the desk (the
   same safeguards as Max: sleeve stops, spread limit, order limit), with an owner-set allocation.
2. A `desk_challenge` / seat proposal Graham may file after 2+ good weeks (applies only on approval).
