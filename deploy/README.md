# Raspberry Pi deployment (LIVE)

The application is a 24/7 service, not a market-hours script.

This machine is not assumed to be available during development. Copy the
repo to the Pi, install as a dedicated non-root user, then enable systemd.

Do **not** run the application as root.
Do **not** install into global system Python.
Do **not** store `OPENAI_API_KEY` in Git, source, the systemd unit, logs, or state.
Do **not** expose the dashboard publicly. It stays on `127.0.0.1:3100`.

Order placement remains off until you set `AGENTIC_LIVE_ORDER_PLACEMENT=true` in
`/etc/agentic-portfolio/env`. Committed default: `auto_execution=false`,
`LIVE_ORDER_PLACEMENT=false`. Approving a proposal does not place an order
while that switch is false.

## Boot path

Raspberry Pi boots
→ `agentic-portfolio.service` starts as user `agentic` (or the username you set)
→ `/opt/agentic-portfolio/.venv/bin/python scripts/run_service.py` starts
→ dashboard binds localhost (`127.0.0.1:3100`)
→ Agent Runtime stays alive
→ Robinhood read-only transport loads that **same user's** persisted OAuth and reconnects
→ persisted watch / approval / notification / AI-budget state is resumed

## Service user

The production unit is:

```
User=agentic
Group=agentic
```

If this Pi already has a dedicated service account, replace those two lines in
`deploy/systemd/agentic-portfolio.service` with the actual username **before**
installing the unit. Robinhood OAuth is user-specific. The login script and
systemd must run as the **same** account.

Create the documented example user (once):

```bash
sudo useradd --system --create-home --home-dir /home/agentic \
  --shell /usr/sbin/nologin --comment "Agentic Portfolio service" agentic
sudo chmod 700 /home/agentic
```

`--create-home` is required. OAuth tokens are written under
`~/.agentic-portfolio/readonly-mcp/oauth.json` for that user
(`/home/agentic/.agentic-portfolio/readonly-mcp/oauth.json` for the example
account). A system user without a home directory cannot persist OAuth.

## Install (on the Pi)

```bash
sudo mkdir -p /opt/agentic-portfolio
sudo rsync -a --exclude .venv --exclude __pycache__ --exclude .git ./ /opt/agentic-portfolio/
sudo chown -R agentic:agentic /opt/agentic-portfolio
sudo chmod 750 /opt/agentic-portfolio

cd /opt/agentic-portfolio
sudo -u agentic python3 -m venv /opt/agentic-portfolio/.venv
sudo -u agentic /opt/agentic-portfolio/.venv/bin/python -m pip install --upgrade pip
sudo -u agentic /opt/agentic-portfolio/.venv/bin/pip install -e .
```

Do not run `pip install` with system `python3` or as root into global site-packages.

### Secret environment file

```bash
sudo mkdir -p /etc/agentic-portfolio
sudo cp /opt/agentic-portfolio/deploy/env.example /etc/agentic-portfolio/env
sudo editor /etc/agentic-portfolio/env
```

The systemd unit loads it with:

```
EnvironmentFile=/etc/agentic-portfolio/env
```

Set `OPENAI_API_KEY` on the Pi only. The file may contain:

```
OPENAI_API_KEY=...
AGENTIC_RUNTIME_MODE=LIVE
DASHBOARD_ENVIRONMENT=LIVE
```

Then:

```bash
sudo chown root:agentic /etc/agentic-portfolio/env
sudo chmod 640 /etc/agentic-portfolio/env
```

Confirm the key is present without printing it:

```bash
sudo -u agentic /opt/agentic-portfolio/.venv/bin/python -c \
  "from pathlib import Path
keys=set()
for line in Path('/etc/agentic-portfolio/env').read_text().splitlines():
    line=line.strip()
    if line and not line.startswith('#') and '=' in line:
        keys.add(line.split('=',1)[0].strip())
print('OPENAI_API_KEY_PRESENT=' + str('OPENAI_API_KEY' in keys))"
```

### Writable directories (no root needed at runtime)

The service user must own and write `state/`, `logs/`, and `reports/`. It does
not need root after install.

```bash
sudo mkdir -p /opt/agentic-portfolio/state /opt/agentic-portfolio/logs /opt/agentic-portfolio/reports
sudo chown -R agentic:agentic \
  /opt/agentic-portfolio/state \
  /opt/agentic-portfolio/logs \
  /opt/agentic-portfolio/reports
sudo chmod 770 \
  /opt/agentic-portfolio/state \
  /opt/agentic-portfolio/logs \
  /opt/agentic-portfolio/reports
```

If you later tighten ownership so root owns source and `agentic` only owns
runtime dirs, keep `.venv` executable by `agentic` and keep these three trees
writable by `agentic`. Never run the unit as root to "fix permissions".

### Install the systemd unit

```bash
sudo cp /opt/agentic-portfolio/deploy/systemd/agentic-portfolio.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Do **not** enable the unit until startup validation and the one-cycle smoke
test below have passed.

## OAuth bootstrap (same user as systemd)

Robinhood OAuth is stored outside the repo, per OS user. Cursor's MCP login is
a different credential store and is not reused.

```bash
sudo -u agentic \
  /opt/agentic-portfolio/.venv/bin/python \
  scripts/login_readonly_mcp.py
```

Complete the browser flow on the Pi (or via SSH with X11/Wayland forwarding if
needed). Then:

```bash
sudo -u agentic \
  /opt/agentic-portfolio/.venv/bin/python \
  scripts/login_readonly_mcp.py --status
```

Expected: `status: authenticated (valid)` and a store path under that user's
home. The token value is not printed. The systemd `User=` must be this same
account so the service can read `~/.agentic-portfolio/readonly-mcp/oauth.json`.

The service never stores a Robinhood username or password.

## Dashboard binding

The dashboard binds `127.0.0.1:3100` only. Do not set `DASHBOARD_HOST=0.0.0.0`.
Do not add Cloudflare, Tailscale, or any public exposure as part of this
install.

From a laptop, forward the port:

```bash
ssh -L 3100:127.0.0.1:3100 <pi>
```

Then open `http://127.0.0.1:3100` on the laptop. Traffic stays on localhost at
both ends.

## Startup validation (before enable)

Run these as checks. None of them should print `OPENAI_API_KEY`.

### Python >= 3.11

```bash
/opt/agentic-portfolio/.venv/bin/python -c \
  "import sys; assert sys.version_info >= (3, 11), sys.version; print(sys.version.split()[0])"
```

### Robinhood OAuth valid (service user)

```bash
sudo -u agentic \
  /opt/agentic-portfolio/.venv/bin/python \
  scripts/login_readonly_mcp.py --status
```

### OPENAI key visible to the service environment (presence only)

```bash
sudo systemd-run --wait --collect --uid=agentic --gid=agentic \
  --property=EnvironmentFile=/etc/agentic-portfolio/env \
  /opt/agentic-portfolio/.venv/bin/python -c \
  "import os; print('OPENAI_API_KEY_PRESENT=' + str(bool(os.environ.get('OPENAI_API_KEY'))))"
```

### LIVE runtime selected and order placement off

```bash
sudo systemd-run --wait --collect --uid=agentic --gid=agentic \
  --working-directory=/opt/agentic-portfolio \
  --property=EnvironmentFile=/etc/agentic-portfolio/env \
  --property=Environment=PYTHONPATH=src \
  /opt/agentic-portfolio/.venv/bin/python -c \
  "from agentic_portfolio.runtime import get_active_runtime, LIVE_ORDER_PLACEMENT
from agentic_portfolio.policy import load_account_rules
exe = dict(load_account_rules().get('execution') or {})
print('runtime=' + get_active_runtime().value)
print('LIVE_ORDER_PLACEMENT=' + str(LIVE_ORDER_PLACEMENT).lower())
print('auto_execution=' + str(bool(exe.get('auto_execution'))).lower())
print('live_trade_actions_allowed=' + str(bool(exe.get('live_trade_actions_allowed'))).lower())
assert get_active_runtime().value == 'LIVE'
assert LIVE_ORDER_PLACEMENT is False
assert exe.get('auto_execution') is False
assert exe.get('live_trade_actions_allowed') is False"
```

Expected:

```
runtime=LIVE
LIVE_ORDER_PLACEMENT=false
auto_execution=false
live_trade_actions_allowed=false
```

### One-cycle smoke test (no paid AI)

`--once` runs one orchestration cycle and exits. Production `run_service.py`
does not wire a paid AI provider callback, so this cycle must not spend OpenAI
budget. It does load LIVE mode, OAuth, and writable state dirs.

```bash
sudo -u agentic bash -c '
set -a
. /etc/agentic-portfolio/env
set +a
cd /opt/agentic-portfolio
/opt/agentic-portfolio/.venv/bin/python scripts/run_service.py --once
'
```

Equivalent if the env file is already exported into that user's environment:

```bash
sudo -u agentic \
  /opt/agentic-portfolio/.venv/bin/python \
  scripts/run_service.py --once
```

Confirm `state/runtime/health.json` was written, `runtime_mode` is `LIVE`,
`LIVE_ORDER_PLACEMENT` is false, and the process exited. Then enable systemd.

## Enable and health

```bash
sudo systemctl enable --now agentic-portfolio
sudo systemctl status agentic-portfolio
journalctl -u agentic-portfolio -f
```

Health file:

```bash
python3 -c "import json; print(json.load(open('/opt/agentic-portfolio/state/runtime/health.json')))"
```

Expect `"agent": "ONLINE"`, `"runtime_mode": "LIVE"`,
`"LIVE_ORDER_PLACEMENT": false`. Robinhood should show a reconnect/healthy
snapshot after OAuth loads.

Dashboard health (on the Pi, or via SSH tunnel):

```bash
curl -sS http://127.0.0.1:3100/healthz
```

`/healthz` does not require dashboard login. It must report live order
placement disabled.

## Restart test

```bash
sudo systemctl restart agentic-portfolio
sudo systemctl status agentic-portfolio
```

Verify after restart:

| Check | Where |
|---|---|
| Service returns ONLINE | `state/runtime/health.json` `"agent": "ONLINE"` and `systemctl status` |
| Watchlist survives | `state/live_ai/watch/` unchanged tickers |
| Approval state survives | `state/live_ai/approvals/` |
| AI budget ledger survives | `state/ai_budget/` spent/remaining unchanged |
| Robinhood reconnects | health `robinhood` / journal reconnect, no credential prompt |
| No duplicate jobs | single PID; `state/runtime/locks/` held by this process only |
| No PAPER contamination | `"runtime_mode": "LIVE"`; LIVE watch/approvals stay under `state/live_ai/` |

## Lifecycle

```bash
sudo systemctl start agentic-portfolio
sudo systemctl stop agentic-portfolio
sudo systemctl restart agentic-portfolio
sudo systemctl status agentic-portfolio
```

PID file: `state/runtime/agent.pid`

## Execution (release candidate)

`auto_execution=false`, `live_trade_actions_allowed=false`, committed `LIVE_ORDER_PLACEMENT=false` / `AGENTIC_LIVE_ORDER_PLACEMENT=false`.

Human APPROVE is mandatory. With the switch off, APPROVE becomes `APPROVED_EXECUTION_DISABLED` and does not call `place_equity_order`.

Do **not** set `AGENTIC_LIVE_ORDER_PLACEMENT=true` until this RC has been validated on the Pi (real discovery, research, approval cards). Then enable it only in `/etc/agentic-portfolio/env`, never in git. The only placement surface is `LiveOrderExecutor`.
