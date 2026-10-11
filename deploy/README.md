# Raspberry Pi deployment

The firm runs 24/7 as `agentic-portfolio.service`, user `agentic`, from
`/opt/agentic-portfolio` with the venv at `.venv/`. HQ binds `127.0.0.1:3100`;
remote access is through Cloudflare Access (`portfolio.reydelgutierrez.com`)
or `ssh -L 3100:127.0.0.1:3100 <pi>`.

## Environment (`/etc/agentic-portfolio/env`, root:agentic 640)

| Variable | Meaning |
|---|---|
| `AGENTIC_LIVE_ORDER_PLACEMENT` | `true` lets the Execution Desk write to Robinhood. Anything else: off. |
| `AGENTIC_KILL_SWITCH` | `1` forces the kill switch on. |
| `TELEGRAM_NOTIFICATIONS`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Alerts to your phone |
| `AGENTIC_PUBLIC_DASHBOARD_URL` | Link included in alerts |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | Phase 2+ (CIO) |

`AGENTIC_EXECUTION_MODE`, `AGENTIC_RUNTIME_MODE`, and `DASHBOARD_ENVIRONMENT` are legacy and ignored.

## Robinhood OAuth (service user)

```bash
sudo -u agentic /opt/agentic-portfolio/.venv/bin/python scripts/firm_admin.py login-mcp --status
```

The token store is `~agentic/.agentic-portfolio/readonly-mcp/oauth.json`
(unchanged from the legacy system, so the existing login keeps working).

## Deploying updates

From the development machine (SSH as the deploy user with passwordless
`systemctl start|stop|restart|status agentic-portfolio.service` and
`sudo -u agentic` for the venv's `python`/`pip`; npm installed locally):

```bash
python scripts/deploy_pi.py --status     # deployed commit, service, in-flight orders
python scripts/deploy_pi.py --dry-run    # all gates, no activation
python scripts/deploy_pi.py [<ref>]      # deploy (default HEAD)
python scripts/deploy_pi.py --rollback   # restore the code the last deploy replaced
```

A deploy ships a `git archive` of a pushed commit plus the HQ frontend built
from that same export. It refuses to run in market hours or while an order is
in flight. Before touching the service: the full test suite passes locally
and on the Pi (production venv, scratch copy; declared dependencies are
installed first), and a baseline smoke test is recorded. Then: back up the
code (`~/agentic-deploys/backups/`, newest 5), stop, sync (never `state/`,
`logs/`, `reports/`, `.venv/`, `var/`), install changed dependencies, write
`DEPLOYED_COMMIT`, start. Verification: a fresh process ONLINE, every file
byte-identical, `/healthz`, and no smoke regression (or, with
`--require-smoke-pass`, every new check passing). Any failure rolls back.

## Backups

Nightly at 02:30 ET (and `firm_admin.py backup-now`): SQLite online copy →
integrity check → gzip → AES-256-GCM → force-push one commit holding the
retention set (7 daily, 8 weekly, 12 monthly) to the private repository in
`config/firm.json` (`backup.remote`) → fresh clone, decrypt, open, compare
the ledger fingerprint.

* Encryption key: `~agentic/.agentic-portfolio/backup.key` (`firm_admin.py backup-init`; copy it to your password manager with `backup-show-key`).
* Repository access: a write deploy key at `~agentic/.agentic-portfolio/backup_deploy_key` registered on that repository only.

## Cutover from the legacy engine (one time)

1. Weekend or after the close. `deploy_pi.py --status` shows no in-flight orders.
2. `deploy_pi.py <firm sha> --require-smoke-pass`: the service restarts running the firm with its books closed (it trades nothing).
3. `firm_admin.py migrate --stamp YYYYMMDD`: archives legacy `state/` `logs/` `reports/` with a SHA-256 into `state/firm/archive/`, imports the live NAV history, and opens the books from a fresh broker snapshot.
4. Owner: `firm_admin.py set-password` (HQ admin; there is no default).
5. Allocate (HQ Capital or `firm_admin.py allocate tim <amount>`), set Tim to SIM.
6. After the validation period, the owner switches Tim to LIVE from HQ.

Rollback: `deploy_pi.py --rollback` restores the legacy code; legacy state is
untouched until the post-cutover cleanup.
