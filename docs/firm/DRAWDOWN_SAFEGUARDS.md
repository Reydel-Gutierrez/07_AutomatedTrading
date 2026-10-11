# Drawdown safeguards: operational emergencies vs. investment declines

Status: **approved by the owner 2026-10-11, in force.**

## Principles

1. Operational emergencies stop everything, firm-wide. They are never relaxed.
2. Market declines are an investment matter, governed by each strategy's mandate.
3. Tim (long-term ETFs) keeps investing through normal drawdowns.
4. Alerts inform; they never pause anything.

## Operational protections (firm-wide, every manager, unchanged)

| Protection | Trigger | Effect |
|---|---|---|
| Kill switch | Owner; ambiguous broker response; account mismatch | All orders stop; owner release only |
| Reconcile hold | Books ≠ broker; unexplained cash; order the firm didn't place | Buys stop until resolved |
| Broker-write switch | `AGENTIC_LIVE_ORDER_PLACEMENT` | No Robinhood writes unless `true` |
| Robinhood app approval | Setting on in the app | Orders blocked + alert |
| Desk gates | Hours/window, quote age, spread ≤ mandate max, tradability, cash, owned shares, universe, orders/day | Order not sent |
| Daily turnover breaker | LIVE **buys** or LIVE **sells** in one day exceed 100% of firm value (`max_daily_turnover_pct_nav`) | Further orders on that side wait until tomorrow; one critical alert per day |

The turnover breaker counts buys and sells separately. A full rebalance that
sells X and buys X back uses X, not 2X, so a complete reallocation fits in a
day. The base is the larger of the latest broker NAV and the ledger total, so
a new deposit can be invested the same day. Tested: initial investment of 89%
of NAV, sell-everything-and-rebuy, and the over-limit case.

## Investment drawdown policies (per mandate)

| Manager | Policy | Rules |
|---|---|---|
| **Tim** | `stay_invested` | Not stopped by the firm drawdown brake. Keeps investing new cash and rebalancing. **Crash pacing:** at ≥15% below his sleeve's peak, each trade ≤10% of the sleeve. **Extreme-day rule** (`extreme_day_pct` 5%): on a day a target ETF is ≥5% from yesterday's close, *buying continues*; only rebalancing **sales** are postponed, and never two sessions in a row. |
| **Alex** (Phase 3) | `sleeve_pause` | New buys pause at −20% from his sleeve's peak; exits continue; owner resumes after review (manager page). |
| **Max** (Phase 4) | `sleeve_stop` | New buys pause at −10% from peak (owner resume) or −3% in a day (clears next day); per-trade stops required when his strategy is built. |
| Firm brake | `drawdown_brake_scope: active_strategies` | −20% from the flow-adjusted peak stops buys for managers whose policy is not `stay_invested`. |
| Alerts | 10% / 20% / 30% | Firm and each sleeve; HQ + Telegram; information only. |

## Why the extreme-day rule never blocks buying

On Robinhood daily bars from 2021-01-04 to 2026-10-09, a target ETF moved ≥5% in a day only 4 times.
Two of those were the 2025-04-03/04 sell-off, just before a +10% rebound (2025-04-09).
Blocking buys on those days would have bought later at higher prices. Dislocated prices
are already handled by the desk's spread limit (25 bps for Tim).

## Evidence

Tim's 2021–2026 replay with $100/month:
- The firm −20% brake applied to Tim would have blocked buying for 76 days and cost 1.1 pp of TWR.
- Tim's final mandate matched a rule-free mandate exactly: $11,612.00, +74.43% TWR, 0 invariant failures.
