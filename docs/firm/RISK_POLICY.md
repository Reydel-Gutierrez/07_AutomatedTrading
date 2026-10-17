# Risk policy

Owner-requested 2026-10-17. One place for every department's limits and every agent's risk.

## Who changes what

| Who | What | Where |
|---|---|---|
| Owner | Each department's planned capital, expected yearly return range, benchmark, drawdown limit, max risk per trade | HQ → **Risk** (`/risk`) |
| Owner | Any agent's own risk | The agent's office: Tim/Alex/Max through **Adjust** on the strategy card; forex agents in their Academy page or the **Risk** panel when you sit down with them |
| Graham | Reads all of it (`get_risk_policy`); may propose a forex agent's risk (`risk_change`) or a manager's mandate change | Proposals; nothing moves until the owner approves |

Every agent's risk must sit inside its department's limits. A change beyond them is refused,
for the owner and for Graham, and a department limit below what an agent already carries is
refused until that agent is lowered. Expected returns and benchmarks only grade departments:
no agent trades toward a target, so none takes more risk to catch up after a bad stretch.

## Defaults (planned $50,000)

| Department | Planned | Expected yearly | Benchmark | Drawdown limit | Max risk/trade |
|---|---|---|---|---|---|
| L5 Core Investments | $30,000 | 6–10% | S&P 500 | 25% (Alex's stop; Tim stays invested) | — |
| L3 Equity Trading | $5,000 | 8–20% | S&P 500 | 20% | 2% (position × stop) |
| L2 Forex Division | $10,000 | 5–15% | Cash (T-bills) | 15% | 1.5% |
| L4 Options Trading | $5,000 | 0–30% | Cash (T-bills) | 30% | 3% |

## How a forex risk change works

Risk sizes trades; it never changes a signal. So the owner's settings (`risk_pct`,
`max_positions`, `daily_loss`, `drawdown_pause`) are stored as an override (`owner_risk:<agent>`
in academy.db) applied on top of every strategy version, not as a new version. The agent keeps
its version, its paper weeks, and its validation. The command goes through the engine inbox
(`set_risk`) and is logged as `RISK_CHANGED`.

Stock Swing Class agents are shown but fixed while they train: their paper record is rebuilt
from daily bars with one set of settings, so changing size mid-way would rewrite their history.
Their risk is set when one takes a live swing office.

## Floors

At the planned capital, more seats would split each department too thin, so each dark floor has
2–3 offices like Tim's and Alex's: L4 Options offices 1–2; L3 Max's office and Swing office 2;
L2 the Market room, the day trader's office, and the swing trader's office.
