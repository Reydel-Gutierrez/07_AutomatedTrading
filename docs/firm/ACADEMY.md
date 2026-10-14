# The 537 Academy

Status (2026-10-13): three floors, seven agents, nightly research sprints. No live trading.
The only order code is the Demo Desk, and it is hard-pinned to OANDA's **practice** server; the
$50 live balance is untouched.

## Floors

| Floor | Question it answers | Who decides the move up |
|---|---|---|
| **1 · Research** (map A1) | Does the strategy have an edge on years of OANDA history? No trading. | Automatic: an agent moves to Floor 2 the night it passes every gate |
| **2 · Paper trading** (A2) | Does it hold up on live prices? A fresh $10,000 virtual account, 4+ weeks, minimum trades (day 40, swing 15), results in line with the backtest | **Owner** gives a Demo Desk seat |
| **3 · OANDA Demo Desk** (A3) | Does it work through a real broker? Real orders on its own practice sub-account, 4+ weeks | **Owner** decides on a live chair, after Graham's memo |
| Alumni | Retired agents; records kept | **Owner** retires |

**Polish on Floor 1, prove on Floors 2–3.** Agents above Floor 1 are never modified. If one
needs changes, it is sent back to research, and its evidence clock restarts with the new version.

## Nightly research sprint (01:00 New York, every Floor 1 agent)

The sprint is `src/forex/research.py`; the engine schedules it.

1. **Candidates.** Each tunable setting moved one or two steps, or switched to another choice
   (e.g. 4-hour vs daily entries, a trading-cost filter, sessions). Plus dropping the pair with
   the worst paper and backtest evidence, and two-setting combinations. Up to 16 candidates for
   day traders and 24 for swing traders. Settings tried on earlier nights are skipped.
2. **Score.** Every candidate runs on everything *before* the locked final year, split into
   four periods. Its score is a t-like average R, and it must hold up across the periods.
3. **Gates.** The best candidate (or the current version, if nothing beats it) goes through:
   * **Validation:** in-sample, out-of-sample, and the locked final year; at least 10
     random-direction baselines; a cost stress test; resampled drawdown ≤ 25%; and a
     trial-adjusted Sharpe that gets stricter with every variant tried.
   * **Day-1 test:** 30 fresh $10,000 accounts started on random historical dates, each run
     90 days (day traders) or 180 days (swing traders). Passes when ≥ 55% of starts end
     profitable, the median is positive, and the 10th percentile is no worse than −8%.
4. **Decide.**
   * Passes every gate → new version, promoted to Floor 2.
   * Clearly better on research data (margin 0.25 and no fewer positive periods) → new version,
     stays on Floor 1.
   * Otherwise → unchanged.

   After 14 sprints without passing, the report suggests retirement.
5. **Report.** It's stored as `sprint_report:<day>` and shown at HQ → Academy → reports. At
   08:00 New York the firm sends one short line ("Sprint report ready for review…": who was
   tested, who passed, who improved) to Telegram and/or Discord.

## Agents

| Agent | Class | Style | Strategy (`src/forex/strategies/`) |
|---|---|---|---|
| Atlas | 1 | swing | Daily trend regime → 4h (or daily) breakout or pullback; ATR stop; chandelier trail |
| Kestrel | 1 | day | Asian-range breakout in the London window with the 1h trend; out by 11:00 NY |
| Mira | 1 | day | Ranging-regime Bollinger rejection, quiet sessions |
| Orion | 1 | swing | Eight-currency strength rotation; pullback entries; carry filter |
| Sable | 2 | swing | Daily 55-day channel breakout, 20-day channel exit (low turnover) |
| Halcyon | 2 | swing | Daily mean reversion on range-bound crosses (EUR/GBP, AUD/NZD, EUR/CHF, AUD/CAD, USD/CAD) |
| Vega | 2 | swing | NR7 volatility-compression breakout on 4h, with the 50-day trend; flat on weekends |

The second class was designed around the first class's lesson that trading costs sink
high-turnover strategies. Shared risk rules:
* leverage ≤ 5:1 and margin ≤ 25%;
* −2% daily stop and a pause at −10% from the peak;
* spread ceilings, plus an optional cost filter (the spread may be at most a set fraction of the
  stop distance);
* no entries around the 17:00 rollover or after Friday noon;
* one position per pair, and a currency-concentration limit.

## Demo Desk setup (owner, when the first agent reaches Floor 2)

1. Open an OANDA US practice account, then create two sub-accounts in it (one per seat).
2. On the Pi, run:
   `sudo -u agentic /opt/agentic-portfolio/.venv/bin/python /opt/agentic-portfolio/scripts/oanda_credentials.py set-practice`
   It asks for the two sub-account IDs and the practice token, without echoing the token. It
   writes `~agentic/.agentic-portfolio/forex/oanda-practice.env` (0600) and refuses anything
   that isn't a practice account.
3. Give a Floor 2 agent a seat from its Academy page.

## Notifications

Telegram as before. For Discord, add `DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...`
to `/etc/agentic-portfolio/env` (owner), then restart the service.

## Where things are

* HQ → **Academy**: the floors, the latest sprint report, every agent's record and controls.
  * Floor 1: sprint now, nightly sprints on/off, validate, retire.
  * Floor 2: pause, give a Demo Desk seat, send back to research, retire.
  * Floor 3: pause, send back to research, retire.
* HQ → **Academy → reports**: every night's report per agent.
* HQ → **The Firm**: the map's Academy building has A1 Research, A2 Paper Trading, and A3 Demo
  Desk (with the Research Lead and the Alumni wall).
* Pi: `/opt/agentic-portfolio/state/forex/` holds `academy.db`, `marketdata.db`, `engine.log`,
  and the `inbox/`. `FIRM_FOREX_ENGINE=off` stops the engine.
