# Agentic Capital — an autonomous investment firm

The owner provides capital; a team of managers invests it on the dedicated
Robinhood **Agentic** account, each with an independent virtual portfolio,
and Investment HQ shows everything they do.

| Agent | Role | Status |
|---|---|---|
| CIO | Chief Investment Officer: conversation, oversight, allocation advice | Phase 2 |
| Tim | Core manager: diversified ETFs, contributions, rebalancing (no AI) | Phase 1 |
| Alex | Strategic growth: systematic stock selection with AI research | Phase 3 |
| Max | Short-term swing trading (simulation first) | Phase 4 |

**Read next:** [ARCHITECTURE.md](ARCHITECTURE.md) (how it works),
[RUNBOOK.md](RUNBOOK.md) (operating it), [deploy/README.md](deploy/README.md)
(the Pi), [docs/firm/PHASE1_DESIGN.md](docs/firm/PHASE1_DESIGN.md) (design).

## Non-negotiables

* The firm never moves money in or out of Robinhood. Deposits and withdrawals are yours.
* One code path places orders: the Execution Desk (`src/firm/desk.py`).
* Live orders need both the Pi master switch (`AGENTIC_LIVE_ORDER_PLACEMENT=true`) and a manager in `LIVE` mode, which only you set.
* The kill switch stops everything; only you release it.
* Every decision, order, and owner action is recorded in the audit stream.

## Development

```bash
pip install -e ".[dev]"
python -m pytest
cd web && npm ci && npm run dev        # HQ at http://localhost:5173 (proxies to :3100)
python scripts/firm_service.py         # service + API on 127.0.0.1:3100
```

Deploy with `python scripts/deploy_pi.py` (see deploy/README.md).
