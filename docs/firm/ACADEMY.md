# The 537 Academy: Forex paper trading (built 2026-10-12)

Status: running in **paper only**. No code path places, modifies, or cancels an OANDA order. The
$50 OANDA balance is untouched and reserved for a separately authorized live test.

## How it runs

```
agentic-portfolio.service (unchanged unit)
 ├─ firm process: Tim/Alex/Max, Robinhood desk, CIO, HQ API  ── firm.db
 │    reads academy.db read-only; owner commands → state/forex/inbox/*.json
 └─ forex engine (scripts/forex_engine.py): a separate child process, nice 10,
    started and restarted by firm/sidecar.py with an allowlisted environment
    (no Robinhood, AI, or Telegram secrets)                    ── academy.db, marketdata.db
```

* `src/firm` never imports `src/forex`; a test enforces this. A crash in the engine can't stop Tim.
* Market data: OANDA bid/ask candles (M15, H1, H4, D; up to 10 years) in `state/forex/marketdata.db`.
  Prices are polled every ~10 s, and stops/targets are checked on 1-minute candles.
* Each agent has its own $10,000 virtual account in `academy.db` (accounts, positions, trades,
  15-minute equity marks, events).

## The founding class

| Agent | Style | Strategy (code: `src/forex/strategies/`) | Risk envelope |
|---|---|---|---|
| **Atlas** | Swing | Daily trend regime (200 EMA, 50 EMA slope, ADX) → 4h Donchian breakout or RSI pullback; 2.5×ATR stop; chandelier trail after 1R; exits when the regime fails | 0.75%/trade, ≤3 positions, weekend holds under limits |
| **Kestrel** | Day | Asian range (19:00–02:00 NY) → first London-window close outside it, sized vs daily ATR, with the 1h trend; stop at range mid, 1.6R target, breakeven at 1R, out by 11:00 NY | 0.5%/trade, ≤2 positions, flat before rollover |
| **Mira** | Day | Ranging regime only (1h ADX low, flat 1h EMA) → 15m Bollinger rejection with stretched RSI; target the mid-band; exits if the trend returns or after 6h | 0.5%/trade, ≤2 positions, flat before rollover |
| **Orion** | Swing | Eight-currency strength score (vol-adjusted 20/60-day momentum) → top strength-gap pairs; daily trend + turning 4h pullback; carry filter; 1.5×daily-ATR stop, 2.5R target, trailing | 0.75%/trade, ≤2 positions, weekend holds under limits |

All agents share these limits: leverage ≤ 5:1, margin ≤ 25% of equity, −2% daily-loss stop,
pause at −10% from peak, spread ceilings per pair, no entries in the 16:55–17:10 NY
rollover or after Friday 12:00 NY, at most 2 positions leaning the same way on one currency,
and one position per pair. Fills follow OANDA: entries at the ask/bid plus slippage; stops gap
to the worse open price; financing is charged at each 17:00 NY rollover.

## Evidence and learning

* **Validation (the Examiner, `forex/validation.py`)** checks every version on OANDA history across
  in-sample, out-of-sample, and a locked final year. It requires enough trades, positive expectancy,
  a non-losing hold-out, a win over 10 random-direction baselines, a pass under extra costs, and a
  resampled drawdown ≤ 25%. A trial-adjusted Sharpe discounts how many variants have been tried.
* **Revisions (`forex/revision.py`)** run every Saturday, at most once every 21 days per agent:
  * candidates are one-step parameter changes within bounds, plus dropping a pair that is losing
    in paper and backtest;
  * candidates are chosen using in-sample results and confirmed out-of-sample (never on the hold-out);
  * a change is adopted only if it passes the full validation, or if it improves a version that
    is already failing.
* **Promotion readiness** (shown per agent) needs all of: passed validation, paper days and trades
  minimums, paper results within the validated band, and drawdown < 10%. It then always needs
  **Graham's memo and the owner's authorization**. Nothing promotes automatically, and no live
  order code exists.

## Where to look

* HQ → **Academy**: leaderboard, live OANDA rates, the engine's status, the journal, and each agent's page
  (equity, validation report, positions, trades, diagnostics, versions, controls).
* HQ → **The Firm → The 537 Academy**: the agents at their desks; the Strategy Lab lights up during validations.
* HQ → **The Firm → Forex Division**: the rates board shows live OANDA prices; all four live chairs are open.
* Pi logs: `/opt/agentic-portfolio/state/forex/engine.log`.

## Owner controls

Pause/resume, re-validate, review now, and auto-revise on/off, per agent, from its Academy page.
`FIRM_FOREX_ENGINE=off` in the Pi env file stops the engine entirely.
