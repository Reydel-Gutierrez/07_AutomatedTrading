# Drawdown safeguards: operational emergencies vs. investment declines

Status: implemented; **owner decision pending** on the two switches marked ⚑.

## The problem

The firm-wide drawdown brake (buys stop at −20% from the flow-adjusted peak,
until the owner resumes) treats a market decline like an emergency. For a
long-term ETF strategy, buying through declines *is* the strategy. Replaying
Tim on Robinhood bars 2021-01-04 → 2026-10-09 with $100/month:

| Policy for Tim | Final | TWR | Days buys were blocked |
|---|---|---|---|
| No investment brake | $11,612.00 | +74.4% | 0 |
| Firm −20% brake (released on recovery) | $11,577.84 | +73.3% | 76 (2022) |
| Proposed (stay invested + pacing + pause) | $11,612.00 | +74.4% | 0 |

The real brake waits for a human to resume it, so its actual cost would
likely be larger than this emulation shows.

## Two kinds of protection

**Operational emergencies — firm-wide, unchanged or stronger, every manager:**

| Protection | Trigger | Effect |
|---|---|---|
| Kill switch | You; ambiguous broker response; account mismatch | All orders stop; human release only |
| Reconcile hold | Books ≠ broker; unexplained cash; order the firm didn't place | Buys stop until resolved |
| Broker-write switch | `AGENTIC_LIVE_ORDER_PLACEMENT` | No Robinhood writes unless `true` |
| Robinhood app approval | Setting turned on in the app | Orders blocked + alert |
| Desk gates | Hours/window, quote age, spread, tradability, cash, owned shares, mandate universe, orders/day | Order not sent |
| **New: daily turnover breaker** | LIVE notional traded today > 100% of firm NAV (`max_daily_turnover_pct_nav`) | Further orders rejected today + critical alert (catches runaway trading from a bug) |

**Investment declines — per strategy, in each mandate:**

| Manager | Policy | Safeguards |
|---|---|---|
| Tim | `drawdown_policy: stay_invested` ⚑ | Keeps investing contributions and rebalancing. **Crash pacing:** when his sleeve is ≥15% below its peak, each trade is capped at 10% of the sleeve, spreading large rebalances over days. **Extreme-day pause:** no trades on a day a target ETF has moved ≥5% from yesterday's close. Universe locked to his ETFs. |
| Alex (Phase 3) | `sleeve_drawdown_pause` | New buys pause at −20% of his sleeve; exits continue; resume after CIO review + your OK |
| Max (Phase 4) | `sleeve_drawdown_stop`, daily loss limit, mandatory per-trade stops | New trades pause at −10% of sleeve or −3% in a day |
| Firm | `drawdown_brake_scope` ⚑ | `all` (today) or `active_strategies`: the −20% brake keeps stopping Alex/Max but not a `stay_invested` manager |
| Everyone | `drawdown_alert_levels` 10/20/30% | Firm and per-sleeve alerts (HQ + Telegram). Information only; nothing pauses. |

## Decisions for the owner (⚑)

1. Set `firm_limits.drawdown_brake_scope` to `active_strategies`.
2. Tim mandate v2: add `drawdown_policy: stay_invested`, `crash_pacing {threshold 0.15, max_trade_pct_of_sleeve 0.10}`, `volatility_pause_pct 0.05`.

Until approved, behavior is unchanged: the brake applies to every manager.
Approving both changes is two recorded actions (a settings version and a mandate version).
