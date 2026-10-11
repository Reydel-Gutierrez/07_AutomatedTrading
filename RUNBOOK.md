# Runbook

Commands run on the Pi as the service user:
`sudo -u agentic /opt/agentic-portfolio/.venv/bin/python scripts/<script> …`

## Daily

Nothing is required. Tim decides at 10:05 ET on trading days; the broker is
reconciled every 2 minutes in market hours (15 off-hours); marks are taken at
16:20 ET; an encrypted backup runs at 02:30 ET. Telegram (if configured) and
HQ show anything that needs you.

## Things that need you

| HQ shows | Meaning | Do |
|---|---|---|
| **BUYS PAUSED** | Books ≠ broker, unexplained cash, or an order the firm did not place | Capital → *Needs your decision*; classify the cash or acknowledge the order |
| **KILL SWITCH** | Ambiguous order, account mismatch, or someone engaged it | Read System and Activity; confirm the order state in Robinhood; release with a note |
| **DRAWDOWN BRAKE** | Firm fell 20% from its flow-adjusted peak | Decide whether to resume buys (System) |
| Manager **NEEDS ATTENTION** | That manager raised an error; others keep running | Activity shows the error; it retries on its next run |
| Backup failed | Off-Pi backup or restore check failed | System → Backups; `firm_admin.py backup-now` to retry |
| Robinhood approval required | Agent trades need in-app approval | Turn the setting off in the Robinhood app, or approve each order there |

## Money in and out

Deposit or withdraw in the Robinhood app. The reconciler books deposits to
unallocated and applies the deposit split (Capital page). Withdrawals come
from unallocated; any shortfall pauses buys until you pick a manager.

## Emergency stop

HQ → System → *Engage kill switch*, or
`scripts/kill_switch.py engage --reason "…"`, or `AGENTIC_KILL_SWITCH=1` in
`/etc/agentic-portfolio/env` plus a restart. Release from HQ or
`kill_switch.py release --note "…"` (a note is required).

## Changing a manager

* Mode (HQ manager page): SIM ↔ LIVE ↔ PAUSED. LIVE needs a typed confirmation.
* Mandate (HQ manager page → Adjust): versioned, a reason is required; the manager re-runs.
* Broker writes are the Pi switch `AGENTIC_LIVE_ORDER_PLACEMENT` (restart to apply).

## Backups and restore

`firm_admin.py backup-now` runs one now. Restore:
`firm_admin.py backup-restore <file.db.gz.enc> /tmp/restored.db` with the key
(`backup-show-key`; keep a copy in your password manager). To put a restored
database in service: stop the service, replace `state/firm/firm.db`, start.

## Deploy and rollback

`python scripts/deploy_pi.py` (from the dev machine), `--rollback` to restore
the previous code. Details: deploy/README.md.
