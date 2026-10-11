# Architecture

```
 You ── Investment HQ (React, src/firm/api) ──────────── audit + live events
                │ owner actions (allocate, mode, mandate, kill switch)
 ┌──────────────┴───────────────────────────────────────────────────────┐
 │ Firm (src/firm/core.py)                                              │
 │  managers/  Tim (Phase 1) · Alex · Max  ── OrderIntents ──┐          │
 │                                                           ▼          │
 │  desk.py  Execution Desk: firm → manager → money → market gates      │
 │           persist (idempotent ref) → SIM fill | Robinhood review+place│
 │  ledger.py  append-only, per-manager buckets, FIFO lots, LIVE + SIM   │
 │  reconcile.py  broker ⇄ ledger, flows, foreign orders, holds          │
 │  performance.py  daily marks, time-weighted returns vs SPY           │
 │  controls.py  kill switch · reconcile hold · drawdown brake          │
 └──────────────────────────────────────────────────────────────────────┘
 scheduler.py / runtime.py: isolated jobs on a 24/7 Raspberry Pi service
```

## Money model

One Robinhood account; many virtual portfolios. The ledger has buckets:
`firm` (unallocated cash), `suspense` (unexplained broker deltas), and one per
manager. Every movement is a ledger transaction; nothing is ever updated in
place. Two books share the schema: **LIVE** (real money, reconciled to the
broker) and **SIM** (simulated fills at live quotes). Capital movements are
mirrored into SIM so a manager's simulation starts from exactly its live
capital, which makes promotion from SIM to LIVE a mode change.

A manager may only spend its own free cash and only sell shares its own lots
hold. The reconciler requires broker cash and positions to equal the sum of
all buckets. A mismatch, an order the firm did not place, or cash it cannot
explain puts the firm in a **reconcile hold** (buys paused) until resolved.

## Execution

`OrderIntent → Desk`:
1. **Firm:** kill switch; buys also stop on reconcile hold or drawdown brake.
2. **Manager:** mode matches the book; symbol inside the mandate; orders/day; minimum size.
3. **Money:** manager free cash (and broker cash for LIVE; margin never used); owned shares for sells.
4. **Market:** regular hours inside the firm window; quote ≤ 15 s old; spread ≤ mandate max; tradable.

The order row with its idempotency `ref_id` is persisted *before* any broker
call. LIVE orders: account identity check, Robinhood app-approval check,
`review_equity_order` (any alert blocks), `place_equity_order` (dollar-based
market buys, share-quantity sells, GFD, regular hours). An ambiguous outcome
marks the order UNKNOWN and engages the kill switch; the reconciler then finds
the order by its ref and books it. Deferrable conditions create no order.

## Managers

A manager is a pure function of its own book, mandate, and market data that
returns order intents and a narrative. It has no broker client and no ledger
access. Failures are isolated to the manager (status DEGRADED; others keep
running). Mandates are versioned; every change records who, when, and why.

**Tim** (`managers/tim.py`): target ETF weights (default VTI 70 / VXUS 25 /
BND 5), 1% cash buffer, invests new cash into underweights, rebalances beyond
a 5 pp absolute or 25% relative band and on a quarterly check, never trades
below $5, runs daily at 10:05 ET.

## Data and history

`state/firm/firm.db` (SQLite, WAL). Tables: ledger, lots, orders, events
(the audit stream), marks, broker_snaps, mandates, managers, settings,
users, legacy_import. Encrypted nightly backups go off-Pi with restore
verification (`backup.py`). Legacy records are archived with a checksum
(`migrate.py`); their NAV history is imported.

## Authority

| Who | May |
|---|---|
| Owner | Allocate capital, set modes and mandates, release the kill switch, enable broker writes on the Pi |
| Managers | Propose orders inside their mandate |
| Desk | Send orders that pass every gate |
| Reconciler | Classify flows, hold buys, engage the kill switch on account mismatch |
| CIO (Phase 2) | Advise and file proposals; reallocation requires owner confirmation |
