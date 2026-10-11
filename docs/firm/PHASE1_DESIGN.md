# Phase 1 — Firm Foundation, Tim Live, Investment HQ v1

Status: design, pending owner decisions (§13). Branch: `firm`.

Phase 1 builds the deterministic firm everything else runs on: the ledger,
the Execution Desk, reconciliation, a simulation mode for every order, the
manager framework, Tim as the first manager, and the new HQ dashboard. It
also retires the legacy engine.

The trading path in Phase 1 contains **no AI**. The AI budget setting ships
in Phase 1 so the owner can set it from the dashboard, but it is enforced
starting in Phase 2, when the CIO becomes the first AI consumer.

| Phase | Delivers |
|---|---|
| **1** | Ledger, Execution Desk, reconciler, simulated execution, scheduler, manager framework, **Tim** (simulation → live), **HQ v1**, legacy cutover |
| 2 | CIO office: AI gateway with the $25/month budget split per agent, memory, read-only tools, proposals that need confirmation; manager chat |
| 3 | Alex: systematic screen plus AI research; simulation → live |
| 4 | Max: swing strategy in simulation; promotion gate; live at small size |
| 5 | CIO automatic reallocation within limits the owner grants; animated workspaces |

---

## 1. Runtime and process model

* **One systemd unit, unchanged:** `agentic-portfolio.service`, with the same
  `ExecStart` (`scripts/run_service.py`), service user (`agentic`), and env
  file. The new firm takes over that entry point. Neither the unit file nor
  the sudoers rules change, and the old engine cannot run alongside the new one.
* **One Python process** containing:
  * the API server (FastAPI + uvicorn) on `127.0.0.1:3100`, behind
    Cloudflare Access;
  * the **scheduler** thread, which runs jobs in a small worker pool. Each
    manager job runs in its own thread with a timeout. An exception, timeout,
    or provider failure marks only that manager `DEGRADED`; the others keep
    running;
  * the **Execution Desk**, which serializes every order through one lock.
* **State:** `state/firm/firm.db` (SQLite in WAL mode). The process is the
  only writer; the API reads through the same module. A nightly online backup
  goes to `state/firm/backups/` (14 kept). Copying backups off the Pi is a
  later step (see §13).
* **No JSON state sprawl.** Every financial fact, event, order, and setting
  lives in the database. Configuration that the owner edits (mandates, limits,
  budget) is versioned in the database, not in git.

## 2. Data model

Rules: append-only where money is involved. Corrections are reversing
entries, never `UPDATE`s. Every row carries `book` (`LIVE` | `SIM`).

```sql
managers      (id TEXT PK,            -- 'tim','alex','max'
               display_name, role,
               mode TEXT,             -- 'SIM' | 'LIVE' | 'PAUSED' | 'INACTIVE'
               mode_changed_at, mode_changed_by)

mandates      (id PK, manager_id, version INT, params_json,
               created_at, created_by, confirmed_by, active BOOL)
               -- params: universe, target weights, bands, limits, windows

ledger        (id PK, ts, book, kind, manager_id NULL,  -- NULL = firm level
               symbol NULL, quantity REAL NULL, cash REAL,  -- +in / -out of the bucket
               price REAL NULL, order_id NULL, ref TEXT, memo, created_by)
  -- kinds: EXTERNAL_IN, EXTERNAL_OUT       (firm <-> outside world, detected)
  --        ALLOCATE, DEALLOCATE            (firm unallocated <-> manager)
  --        BUY, SELL, FEE, DIVIDEND, INTEREST
  --        SUSPENSE_IN, SUSPENSE_OUT       (unexplained broker deltas)
  --        OPENING_BALANCE, REVERSAL

lots          (id PK, book, manager_id, symbol, opened_at, quantity,
               cost_basis, closed_quantity, realized_pnl)   -- FIFO, rebuildable from ledger

orders        (id PK, client_ref UNIQUE, book, manager_id, symbol, side,
               notional REAL NULL, quantity REAL NULL, order_type, status,
               broker_order_id NULL, decision_event_id, created_at,
               submitted_at, terminal_at, block_reasons_json)
fills         (id PK, order_id, broker_execution_id UNIQUE, quantity, price, ts)

broker_snaps  (id PK, ts, nav, cash, buying_power, positions_json,
               open_orders_json, reconciled BOOL, diff_json)
marks         (date, book, manager_id NULL, nav, cash, invested,
               net_flow, benchmark_close)  -- one row per manager per close; firm row has NULL manager
events        (id PK, ts, actor, type, manager_id NULL, symbol NULL,
               summary TEXT, payload_json)  -- activity + audit stream (UI, CIO, animation)
settings      (key PK, value_json, updated_at, updated_by, version)
users         (username PK, password_hash, role, created_at)  -- no default password
legacy_import (key PK, source_path, sha256, imported_at, summary_json)
```

`events` is the single narrative stream. Every manager decision, desk step,
reconcile result, and owner action writes one row. The dashboard activity
feed, the Phase 2 CIO tools, and the Phase 5 animations all read only from
this table. That makes the rule "agents may only describe what happened"
structural rather than a convention.

## 3. Accounting

**Buckets (per book):** `firm:unallocated`, `firm:suspense`, and one cash
bucket plus a set of lots per manager.

**Invariants on the LIVE book**, checked after every fill and on every
broker snapshot:

```
broker.cash  == unallocated + suspense + Σ manager.cash  − pending_buy_notional
broker.qty[s] == Σ manager.lots[s].open_qty          for every symbol s
```

Tolerances: $0.01 on cash, 1e-6 shares on quantity. A breach puts the firm in
`RECONCILE_HOLD`: no new BUY from any manager, sells still allowed, an
`events` row plus a Telegram alert, and a "Reconciliation" card on HQ that
shows the diff. The hold clears automatically once the invariant holds again.

**External flows.** The broker has no transfer or dividend feed, so the
reconciler classifies unexplained cash:

| Observation | Treatment |
|---|---|
| Cash up, no fills, no position change, amount ≥ $1 and not dividend-shaped | `EXTERNAL_IN` → `firm:unallocated`, then auto-allocated by the **deposit split** setting (§13). Logged and shown on HQ. |
| Cash up that matches a held ETF's distribution (≤ 2% of that symbol's firm market value, and the symbol is held by some manager) | `DIVIDEND`, attributed pro rata to the managers by open quantity in that symbol |
| Cash down with no fills | `EXTERNAL_OUT` from `firm:unallocated`. If unallocated cash is short, the remainder goes to suspense and the owner is asked which manager funds it. |
| Anything else (position appears or disappears, unknown order) | `SUSPENSE_*` → `RECONCILE_HOLD` until the owner classifies it on HQ |

**Owner capital actions (all virtual):** allocate or deallocate between
`unallocated` and a manager, by % or $. A deallocation may only take a
manager's free cash. If the owner wants more, the manager is told to raise
cash, which produces sells through its own rules. "Withdraw" on HQ means
"move to unallocated and show the Robinhood withdrawal instructions". The
firm never initiates a transfer.

**Performance.** Each manager and the firm get a daily mark at 16:15 ET.
Returns are **time-weighted**, with allocations, deallocations, and external
flows as cash flows, compared with SPY TWR over the same dates. Every number
on HQ comes from `marks` and `ledger`, never from AI text.

**Taxes.** FIFO lots per manager. Firm-wide, a sale at a loss that has a
same-symbol buy by any manager within ±30 days is flagged as a
`WASH_SALE_RISK` event. It is information only in Phase 1.

## 4. Execution Desk (the only broker writer)

```
OrderIntent(manager, book, symbol, side, notional|quantity, reason, decision_event_id)
  1. firm gates      kill switch · RECONCILE_HOLD (blocks buys) · broker connected
  2. manager gates   mode (SIM/LIVE, PAUSED → reject) · mandate universe ·
                     max position % of the manager's NAV · max orders/day · min order $5
  3. money gates     BUY: manager.cash ≥ notional AND broker cash ≥ firm pending + notional (no margin)
                     SELL: manager open lots ≥ quantity (cannot sell another manager's shares)
  4. market gates    regular hours AND inside window (10:00–15:30 ET default) ·
                     fresh quote ≤ 15s · spread ≤ mandate max_spread_bps · tradable
  5. persist         orders row with client_ref = firm:<book>:<uuid> (idempotency) BEFORE any broker call
  6. route           SIM  → SimBroker fills at ask/bid with the live quote; ledger posted at once
                     LIVE → review_equity_order → place_equity_order (dollar BUY, quantity SELL, market, GFD)
                            requires AGENTIC_LIVE_ORDER_PLACEMENT=true (Pi env, owner-owned)
  7. ack handling    clear ack → SUBMITTED; reject → REJECTED; timeout/ambiguous →
                     UNKNOWN + kill switch (kept from today's executor)
  8. fills           order poll (every 60s while any order is open) → fills (unique execution id)
                     → ledger BUY/SELL + lots in one transaction → reconcile
```

A deferrable condition (closed, outside the window, wide spread, stale quote)
returns the intent to the manager to retry next cycle. Desk state never
consumes a manager's decision silently; every rejection is an `events` row
with the reason code.

**Carried over from `live_execution/` (rewritten smaller, not
copy-pasted):** account-identity check on every LIVE order (the Agentic
account number must match), dollar-BUY sizing, ambiguous acknowledgement →
UNKNOWN + kill switch, broker review before placing, and the allowlisted
write transport (`LiveWriteAdapter`). Cancel stays forbidden in Phase 1
because market GFD orders do not need it.

**Foreign orders.** Any broker order without a `firm:` client ref (a manual
trade, another tool, a surviving legacy process) is recorded as
`FOREIGN_ORDER`, triggers `RECONCILE_HOLD`, and alerts. It is never adopted
automatically.

## 5. Controls

| Control | Where | Owner of change |
|---|---|---|
| Broker write master switch | `AGENTIC_LIVE_ORDER_PLACEMENT` in `/etc/agentic-portfolio/env` | Owner on the Pi |
| Manager mode SIM / LIVE / PAUSED | HQ → Manager → Mode (admin, confirm dialog, events row) | Owner |
| Kill switch | HQ System, `scripts/kill_switch.py` (kept), or `AGENTIC_KILL_SWITCH=1` | Anyone may engage; the owner releases |
| Firm limits | `settings.firm_limits` (HQ, versioned) | Owner |
| Mandates | `mandates` (versioned; HQ form in Phase 1, CIO proposals in Phase 2) | Owner confirms |
| AI budget | `settings.ai_budget` (default $25/mo; per-agent split in Phase 2) | Owner |

`AGENTIC_EXECUTION_MODE` is obsolete. If it is set, the firm logs a warning
and ignores it.

**Default firm limits** (editable): cash only (never use margin buying
power); long-only US equities and ETFs; regular hours only; no options or
crypto. **Firm drawdown brake:** at −20% from the cash-flow-adjusted high
water mark, new buys stop for all managers until the owner resumes. Tim has
no per-manager stop, because a long-term ETF portfolio should ride
drawdowns. Alex and Max get a per-sleeve pause in their mandates (Phases 3
and 4).

## 6. Manager framework and Tim

```python
class Manager(Protocol):
    id: str
    def schedule(self) -> list[Trigger]          # ET, calendar-aware: e.g. MarketDaily("10:05"), OnEvent("ALLOCATION_CHANGED")
    def run(self, ctx: ManagerContext) -> RunResult   # pure decision: returns OrderIntents + a narrative events row
```

`ManagerContext` holds a read-only view of the manager's own book (cash,
lots, marks), its active mandate, a quotes/historicals client with a shared
cache and rate limit, the clock, and the calendar. A manager **cannot** see
another manager's book, cannot call the broker, and cannot write the ledger.
It only returns intents, which the scheduler hands to the Desk.

**Tim: Core ETF manager.** Deterministic; no AI in Phase 1.

| Parameter (mandate) | Default (see §13) |
|---|---|
| Target weights | 3-fund: `VTI` 60% / `VXUS` 25% / `BND` 15% |
| Cash buffer | 1% of the sleeve (absorbs rounding and dividends) |
| Invest new cash | Same day at the first window run after the cash lands; buy the most underweight funds first (no selling) |
| Rebalance | Daily check. Trade only if a fund is off target by > 5 percentage points absolute or > 25% relative. Then sell overweight and buy underweight to targets. A **calendar check each quarter** rebalances if drift is > 1 pp. |
| Window | One run per trading day at 10:05 ET, plus a run on `ALLOCATION_CHANGED` and `DIVIDEND` (deferred to the window) |
| Minimum trade | $5; anything smaller waits |
| Limits | Universe = the target funds only. Max orders per day 6. Max spread 25 bps. |

"Automatic contributions": the owner sets a recurring deposit in Robinhood.
The reconciler books it as `EXTERNAL_IN`, the **deposit split** allocates
it, and Tim invests his share at the next run. The firm itself never pulls
money.

**Alex and Max in Phase 1.** They exist as managers in mode `INACTIVE`.
Capital allocated to them sits as cash in their sleeves, visible on HQ as
"awaiting activation". Their engines arrive in Phases 3 and 4. When Max is
built (Phase 4), his simulated book mirrors his live sleeve size, so
promotion is just a mode change and nothing about his sizing changes.

## 7. Simulation

* `SimBroker` (from today's `autonomy/paper_broker.py`) implements the same
  `BrokerClient` protocol and fills at the live ask/bid. The SIM book is a
  separate set of ledger rows (`book='SIM'`), so a manager can run in
  simulation while the firm is live.
* **Backtest harness** (`firm.backtest`): replays daily historicals from the
  broker (cached in `state/firm/market_cache/`) through
  `Manager.run` + SimBroker + ledger. Phase 1 uses it to validate Tim's
  rebalancing and accounting over 3–5 years (turnover, drift, fees = 0,
  dividends excluded and noted). It is not a performance forecast.
* **Tim's promotion gate to LIVE:** at least 5 trading days in SIM on the Pi
  with zero reconcile breaks, every intent explained in `events`, the
  backtest invariants green, then the owner flips the mode.

## 8. Investment HQ v1

**Stack:** FastAPI (typed JSON API + Server-Sent Events) and React + Vite +
TypeScript + Tailwind. The frontend is built on the dev machine;
`deploy_pi.py` builds `web/` and adds `web/dist` to the release tarball, so
the Pi needs no Node. The legacy Flask dashboard is deleted at cutover.

**Auth:** one admin account created at first boot with
`scripts/firm_admin.py set-password`. There is no default password. Session
cookie is `Secure`, `SameSite=Strict`, with CSRF on writes. Binding is
`127.0.0.1` only; remote access goes through Cloudflare Access (already in
place).

**Pages in Phase 1:**

| Page | Content |
|---|---|
| **HQ** | Firm NAV, day and since-inception TWR vs SPY, allocation donut (current vs target per manager), holdings grouped by manager, four agent cards (live status from `events`; the CIO card reads "opens in Phase 2"), recent activity feed (SSE) |
| **Capital** | Unallocated cash, allocate/deallocate by % or $, deposit-split setting, unclassified cash items to resolve, external flow history |
| **Manager: Tim** | Now doing / next scheduled run, sleeve NAV and TWR vs SPY, target vs actual weights, lots, orders and fills, mandate (view, plus edit with confirm), decision log with reasons |
| **Manager: Alex / Max** | Allocation, cash, "activates in Phase 3/4", planned mandate |
| **Activity** | Full `events` stream with filters (manager, type, symbol): the audit trail |
| **System** | Broker connection, last snapshot, reconcile status and diff, kill switch, manager modes, broker-write switch (read-only display), AI budget setting, deployed commit, job health |

API sketch: `GET /api/firm`, `/api/managers/{id}`, `/api/events?…`,
`/api/stream` (SSE), `POST /api/capital/allocate`, `POST /api/managers/{id}/mode`,
`POST /api/managers/{id}/mandate`, `POST /api/kill-switch`, `PUT /api/settings/{key}`.
Every POST writes an `events` row with the actor.

## 9. Migration and decommission

The live account today: $1,010, 100% cash, no positions, no open orders,
and no order ever placed. The cutover therefore carries no position risk.

| Step | Action | Reversible |
|---|---|---|
| M0 (now) | Owner sets `AGENTIC_EXECUTION_MODE=OBSERVE` (placement is on today) and changes the legacy admin password | yes |
| M1 Archive | `tar` of `state/`, `logs/`, `reports/` plus the legacy code at the deployed sha → `~/agentic-archives/legacy-<date>.tar.zst` on the Pi, with a sha256, **and copied to the dev machine** | n/a |
| M2 Import | `firm migrate` (as `agentic`) creates `firm.db` and records into `legacy_import`: the live NAV history (for since-inception performance), AI spend history, and the archive pointer and hash. Research reports stay in the archive (Alex may mine them in Phase 3). | yes (drop db) |
| M3 Cutover | Outside market hours: preflight (no open broker orders, no in-flight legacy intents) → `deploy_pi.py <firm sha>`. rsync `--delete` removes all legacy modules and scripts. On first start: fresh broker snapshot, `OPENING_BALANCE` = broker cash → `firm:unallocated`, all managers `INACTIVE` / `SIM`, admin password prompt. | yes: `deploy_pi.py --rollback` restores the legacy code; legacy state files are untouched |
| M4 Allocate | Owner allocates on HQ. Tim runs in SIM. | yes |
| M5 Tim LIVE | After the promotion gate (§7), the owner flips Tim to LIVE. | yes (PAUSED) |
| M6 Cleanup | 30 days after M5: delete legacy state dirs on the Pi (the archive remains) | — |

**Why the old engine cannot interfere:** its code is deleted from the
deployed tree; the single unit now runs the firm; the firm never reads legacy
state paths; and any broker order without a `firm:` ref trips
`RECONCILE_HOLD`. That last check also catches manual trades and the
Robinhood MCP that is still configured with write access in Cursor on the
dev machine.

## 10. Repository restructure

New package `src/firm/`. At cutover `src/agentic_portfolio/` is deleted,
except the modules ported into `firm`:

| Ported into `firm` (reviewed, slimmed) | From |
|---|---|
| `firm.broker.mcp` (OAuth store, read transport, allowlisted write adapter) | `adapters/readonly_mcp_auth.py`, `adapters/readonly_runtime.py`, `adapters/robinhood_read.py`, `live_execution/broker.py` |
| `firm.calendar` | `calendar.py`, `session.py` |
| `firm.desk.kill_switch` | `autonomy/kill_switch.py` |
| `firm.desk.sim` | `autonomy/paper_broker.py` |
| `firm.notify` | `notify/` (Telegram sink + new event kinds) |
| `firm.ai` (Phase 2) | `ai/gateway.py`, `ai/budget.py`, `ai/ledger.py`, `ai/providers/` |

Deleted (about 35k lines): `discovery/`, `research/`, `decision/`,
`monitoring/`, `watch/`, `approval/`, `live_approval/`, `execution/`,
`paper_fill/`, `review/`, `dashboard/`, `agent/`, `autonomy/` (except the
parts ported above), `risk_gate.py` and the policy/thesis/sleeve registries,
and the legacy `scripts/run_*` / `config/*.json` files. The top-level docs
(`ARCHITECTURE.md`, `RUNBOOK.md`, `AGENT_RULES.md`, `PORTFOLIO_POLICY.md`,
`RISK_RULES.md`, `STRATEGY.md`) are rewritten for the firm. `JOURNAL.md`
moves to `docs/legacy/`.

Kept as-is: `scripts/deploy_pi.py`, `deploy/` (README rewritten),
`scripts/kill_switch.py` (re-pointed), and the systemd unit.

## 11. Testing

* **Ledger property tests:** for random sequences of allocations, fills,
  dividends, flows, and reversals, the invariants hold, lots rebuild exactly
  from the ledger, and TWR matches a hand-computed reference.
* **Desk tests** against `FakeBroker` and `SimBroker`: every gate, idempotent
  retry after a crash between persist and place, ambiguous ack → UNKNOWN +
  kill switch, partial fills, foreign order → hold, insufficient manager cash
  versus firm cash, and a manager selling shares it does not own.
* **Tim tests:** invest-new-cash ordering, band and calendar rebalances, the
  minimum-trade rule, and dividend reinvestment, plus the backtest harness run.
* **Isolation test:** a manager that raises or hangs does not delay another
  manager or the desk.
* **Static guards:** carried over from today's AI/broker isolation test.
  Nothing outside `firm.desk` imports the write adapter; managers cannot
  import `firm.desk` or the ledger writers.
* **API tests:** auth, CSRF, and every POST writing an event.
* **Pi gates:** the deploy pipeline (tests on the Pi, smoke, rollback) stays.
  The smoke test is rewritten for the firm.

## 12. Implementation sequence

| # | Milestone | Exit criterion |
|---|---|---|
| 1 | `firm` skeleton: db + migrations, `events`, settings, calendar + broker ports, scheduler with isolation | Tests green; runs locally in SIM against SimBroker |
| 2 | Ledger + lots + reconciler (including the flow classifier) + marks/TWR | Property tests green; replayed against the archived live snapshots without false holds |
| 3 | Execution Desk (SIM + LIVE routes) | Desk test matrix green; LIVE route proven against FakeBroker only |
| 4 | Tim + backtest harness | Tim tests and backtest invariants green |
| 5 | HQ v1 API + frontend + auth; deploy builds `web/` | All Phase 1 pages working on real SIM data locally |
| 6 | Migration tooling (`firm migrate`, archive) + new smoke test | Dry run against a copy of the Pi state |
| 7 | **Cutover** (M1–M3) outside market hours | Pi runs the firm; opening balance reconciles to the broker |
| 8 | Tim in SIM on the Pi ≥ 5 trading days → owner flips Tim to LIVE | First live fills reconcile; HQ shows attribution |
| 9 | Cleanup (M6) and documentation rewrite | Legacy removed; docs describe the firm |

Milestones 1–6 do not touch production. Milestone 7 is the only step that
changes what runs on the Pi before Tim trades.

## 13. Decisions needed from the owner

See the conversation summary. Defaults above apply unless changed.

## 14. Known risks

* **Dividend classification is heuristic.** It is safe because
  misclassification lands in suspense, not in someone's P&L. The owner
  resolves the occasional item on HQ.
* **Dollar-based fractional market orders:** fills may come in slightly
  under or over the dollar amount. The ledger posts the actual fills; the
  cash buffer absorbs the difference.
* **Pi availability:** a Pi outage means no trading and no HQ. That is
  acceptable for Tim's daily cadence. Revisit before Max goes live.
* **Concentration of trust in SQLite on an SD card:** WAL mode, nightly
  online backups, and off-Pi copies (see §13).
