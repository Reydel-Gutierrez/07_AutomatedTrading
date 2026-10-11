"""The investment firm: CIO, managers, ledger, Execution Desk, reconciliation.

Layout:
  db, events, settings      persistence and the audit stream
  money                     exact decimal helpers
  ledger                    per-manager virtual portfolios (append-only)
  broker/                   Robinhood MCP (read + allowlisted write) and SimBroker
  market                    quotes and daily bars with a shared cache
  desk                      the only code path that places orders
  reconcile                 broker vs ledger, flow classification, holds
  performance               daily marks and time-weighted returns
  managers/                 strategy engines (Tim in Phase 1)
  scheduler, runtime        24/7 service wiring
  api/                      Investment HQ backend
"""
