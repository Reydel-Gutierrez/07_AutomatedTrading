# Equity Academy: the Stock Swing Class (design, not built)

Status: design approved in direction by the owner on 2026-10-16 ("keep Max's desk, give it its
own floor, and let an equity Academy class compete for it"). Nothing here is built yet. Building
it needs the owner's go-ahead; putting a challenger's strategy on the desk needs the owner's
approval each time.

## The idea in one paragraph

Max is a **desk**, not a strategy. The desk is the seat that holds real (or SIM) capital on the
Equity Trading floor (L3), with the owner-approved sleeve stops, the spread limit, the daily order
limit, and the cooldown and group guards. Today the desk runs one strategy, `trend_pullback_v1`.
That strategy is the **incumbent**. The Stock Swing Class is a set of Academy agents that research
and paper trade **challenger** strategies on US stocks and ETFs. A challenger that beats the
incumbent over the same weeks, on paper, after costs, can take over the desk, but only with the
owner's approval. Max keeps his name, his office and his record. What changes is the strategy he
runs, as a new mandate version.

We don't open a second swing desk now. With about $124 on the desk, splitting it would make every
position too small to matter. A second seat makes sense once the desk holds about $1,000 and two
challengers with different behavior both qualify (see "Later" below).

## How it follows the forex Academy, and where it differs

| | Forex Academy | Stock Swing Class |
|---|---|---|
| Floor 1, Research | nightly sprint on OANDA history | nightly sprint on daily US stock history |
| Floor 2, Paper | $10k virtual on live OANDA prices | $10k virtual on live Robinhood quotes, in the same 15:15 ET window Max uses |
| Floor 3 | Demo Desk on an OANDA practice account | **none**: Robinhood has no practice account. Floor 2 already runs side by side with the desk on the same days |
| Promotion | owner gives a live chair | owner swaps the desk's strategy (Max's mandate) to the challenger's |
| Bars | intraday + daily | daily only (swing holds of 1 to 20 sessions) |

Code reuse: the Academy's validation, revision and sprint machinery (`src/forex/validation.py`,
`research.py`, `revision.py`) is written for forex instruments. The class adds an equity data
adapter and an equity simulator next to them, with its own `state/equity_academy/academy.db`. It
runs in the existing sidecar process (`src/firm/sidecar.py`, nice 10), **never** in the firm's
trading loop, and reads quotes from the firm's market cache only. Tim's path is untouched.

## The starting class (4 agents)

Every challenger has a per-trade stop and the desk's guards. The desk refuses any order without a
stop.

1. **Pullback+**: the incumbent, tuned. RSI(2) below 15 to 20 instead of 10, 5 to 6 smaller
   positions instead of 4, and stops sized to each name's typical daily range (ATR) instead of a
   fixed 6%. This answers Graham's options 1 and 2 with evidence, before any setting changes on
   the desk.
2. **Breakout**: buys a close above the 20-day high while above the 200-day average, with an
   ATR trailing stop. It does well in rising markets, where pullback buying sits idle.
3. **Rotation**: once a week, holds the 2 to 3 strongest sector ETFs by 1 to 3 month relative
   strength. Each position has a stop, and it exits on the weekly rerank. It is the most
   diversified of the four by construction.
4. **High-beta pullback**: the pullback rule on more volatile names (TSLA, AMD, MU, PLTR, COIN),
   with ATR stops and at most one position per group. This is the "risk taker" Graham proposed,
   tested on paper instead of with the desk's money.

The owner names the agents, as in the forex Academy. Each agent may trade only its own fixed list
of symbols, so a challenger can't drift into names nobody approved.

## Gates

**Floor 1 → Floor 2** (automatic, nightly):
- walk-forward over at least 5 years of daily bars, including 2022, with the last 12 months
  locked out of tuning
- out-of-sample Sharpe above 0.5 after costs: half the quoted spread plus 5 bps slippage on each
  side
- at least 40 out-of-sample trades
- beats a random-entry baseline that uses the same exits, sizing and symbols (p < 0.05)
- max drawdown inside the desk's 10% sleeve stop on at least 90% of rolling one-year windows
- **cash-account check**: the simulator spends only settled cash (T+1) and counts would-be
  good-faith violations. A strategy that needs unsettled cash to work fails.

**Floor 2 → the desk** (owner's decision):
- at least 4 weeks on paper, judged week by week, the same rule as forex
- ahead of the incumbent over the same weeks, as a percentage return after costs, and not just
  ahead of zero
- no more than its share of the overlap problem: its average overlap with Alex's holdings is
  reported next to its return
- Graham may file a `desk_challenge` proposal (a new proposal type, modeled on
  `academy_promotion`) after 2 profitable weeks ahead of the incumbent. It does nothing until the
  owner approves it.

**On approval**: Max's mandate gets a new version with the challenger's strategy id and
parameters. The desk keeps its current mode: if Max is in SIM, the new strategy starts in SIM, and
the LIVE gate (at least 20 SIM trading days) restarts. The old strategy stays on Floor 2 as a
challenger, so a swap can be reversed on evidence.

## What the floor shows

- **Max's office**: the desk's live record and current strategy.
- **Stock Swing Class room**: the challengers, with each one's floor, weeks on paper, and return
  against Max over the same weeks. It is a placeholder on the map until the build.
- **Trading Boardroom** (`/analytics/trading`): the desk against the S&P 500. After the build,
  it also shows a leaderboard of challengers against the incumbent.

## Build plan (after the owner's go-ahead)

1. **Data**: daily bars for each challenger's symbols, 5+ years. Today `refresh_bars` fetches 400
   days, so it needs a longer one-time backfill, stored in the equity Academy's own database.
   Prices only: dividends aren't modeled, so ETF returns are slightly understated, and this is
   said on every report.
2. **Simulator + gates**: an equity simulator that models settled cash, the desk's guards, costs,
   walk-forward, the random baseline, and the locked final year. Tests use known synthetic series.
3. **Floor 1**: the four agents, a nightly sprint at 01:30 New York (after the forex sprint), and
   the sprint report in the 08:00 Telegram line.
4. **Floor 2**: paper trading at 15:15 ET from the firm's quote cache, and a weekly comparison
   against the desk.
5. **Desk challenge**: the `desk_challenge` proposal, owner approval in Max's office, and the
   mandate swap.
6. **Map**: the Stock Swing Class room becomes live, and the challenger leaderboard appears in
   the Trading Boardroom.

Rough size: steps 1 and 2 are the bulk, comparable to the forex Academy's first build. Steps 3 to 6
reuse the forex patterns.

## Known limits, said up front

- **Survivorship**: each challenger's list is today's names, and names that failed aren't in the
  history. This flatters backtests, especially high-beta, so out-of-sample results and Floor 2 weigh
  more than backtests.
- **Small money**: on about $124, fractional shares work, but a $1 minimum and spreads matter. The
  simulator uses the desk's real minimum trade.
- **Day trading stays out**: in a cash account, settlement limits how often money can be reused,
  and with this sleeve size intraday trading wouldn't be meaningful. It can be reconsidered when
  the sleeve and the account type change.

## Decisions for the owner

1. Approve the build (steps 1 to 6).
2. Name the four agents.
3. May the high-beta challenger trade names outside Max's current 22? (On paper only, until it
   wins the desk.)
4. Should a winning challenger replace the desk's strategy (recommended), or wait for a second
   seat?

## Later

- A second swing seat once the desk holds about $1,000 and two qualified challengers behave
  differently. The Stock Swing Class room already has two seats drawn for this.
- An equity class for Alex's style (monthly momentum) if the owner wants Alex's research desk to
  compete as well.
