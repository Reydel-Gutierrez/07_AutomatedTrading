# Raspberry Pi deployment

The application is a 24/7 service, not a market-hours script.

This machine is not assumed to be available during development. Copy the
repo to the Pi later, then enable the systemd unit.

## Boot path

Raspberry Pi boots
→ `agentic-portfolio.service` starts
→ `scripts/run_service.py` starts
→ dashboard binds localhost (default `127.0.0.1:3100`)
→ Agent Runtime stays alive
→ Robinhood read-only transport loads persisted OAuth and reconnects
→ persisted watch/approval/notification state is resumed

## Install (on the Pi)

```bash
sudo mkdir -p /opt/agentic-portfolio
sudo rsync -a --exclude .venv --exclude __pycache__ ./ /opt/agentic-portfolio/
cd /opt/agentic-portfolio
python3 -m pip install -e .
sudo cp deploy/systemd/agentic-portfolio.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agentic-portfolio.service
```

## Lifecycle

```bash
sudo systemctl start agentic-portfolio
sudo systemctl stop agentic-portfolio
sudo systemctl restart agentic-portfolio
sudo systemctl status agentic-portfolio
```

Health file: `state/runtime/health.json`  
PID file: `state/runtime/agent.pid`

Dashboard: `http://127.0.0.1:3100` (do not bind publicly; use a Cloudflare Tunnel later).

## Auth

Run `python scripts/login_readonly_mcp.py` once on the Pi user that owns the
service so OAuth is persisted outside the repo. The service never stores a
Robinhood username or password.

## Execution

`auto_execution=false`, `live_trade_actions_allowed=false`, `LIVE_ORDER_PLACEMENT=false`.
Approving a proposal on the dashboard does not place an order.
