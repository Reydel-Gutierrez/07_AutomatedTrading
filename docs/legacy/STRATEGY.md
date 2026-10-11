# Investment Decision Framework

How the AI reasons. **What is allowed (ceilings, sleeves, halt, HWM):** `PORTFOLIO_POLICY.md` + `config/portfolio_policy.json`.

One policy at any NAV. Not a dollar-tier playbook. Not Liquid Large-Cap Momentum V1.

Does **not** enable broker orders.

---

## Philosophy

Long-term **capital growth**. Multi-factor, thesis-based, portfolio-aware. Cash is a position. Sleeve targets are not orders. `NO_ACTION` is always valid.

Ask: **does this improve the book vs alternatives and cash?**

Decisions: BUY, SELL, REDUCE, HOLD, WATCH, REJECT, NO_ACTION.

Size from conviction, evidence quality, downside, vol, liquidity, valuation, expected return, horizon, regime, name risk, sector, correlation, concentration, alternatives, cash — **always ≤** the hard matrix. Never size up only because unused ceiling remains.

---

## Sleeves (summary)

Core 50% target · Opp 30% · Tactical 15% · Speculative 5% hard max. Details in policy.

- Core: multi-year vs SPY; no calendar exits; verified broad-market ETF ≤ 40%; Core stock ≤ 20%.
- Opp: dislocation vs deterioration; stock ≤ 15%; price down ≠ buy.
- Tactical: no quota; stock ≤ 10%; regular-hours intraday to weeks.
- Speculative: ≤ 3% per name, ≤ 5% sleeve; low price ≠ thesis.

---

## Research

Combine MCP sources. Classify securities with **evidence**, not assertion (`security_classification` in policy JSON). Fail closed if class is unreliable.

**Candidate Discovery** (`config/discovery.json`) only builds a research queue. It assigns a *provisional* sleeve hypothesis. Same-sector/sleeve crowding is an overlap penalty and comparison-group membership, not a max-N hard reject. The final sleeve is established only by the investment decision / thesis workflow.

**Deep Research** (`config/research.json`, `src/agentic_portfolio/research/`) interprets a `ResearchEvidencePacket`. Python does not encode rules such as P/E < 20 = buy. The AI reasoner produces bull/base/bear cases, sleeve-specific analysis, and a conclusion (`ADVANCE_TO_THESIS` / `KEEP_WATCHING` / `REJECT` / `NEED_MORE_DATA`). Research does not create ACTIVE theses or BUY actions.

**Investment Thesis + Portfolio Decision** (`config/decision.json`, `src/agentic_portfolio/decision/`) forms a DRAFT thesis and chooses an action/allocation versus cash, SPY, and other researched names. Python does not encode PE/growth/RSI cutoffs. Exit policy is sleeve-specific (Core: thesis-based, no mandatory stop; Opp: optional price/event; Tactical: price/technical required for BUY/ADD; Speculative: risk invalidation required for BUY/ADD). No broker stop orders. Theses stay DRAFT until a future real execution.

**Position Monitoring + Thesis Reassessment** (`config/monitoring.json`, `src/agentic_portfolio/monitoring/`) evaluates existing positions. Price movement alone does not invalidate CORE. CORE uses thesis/fundamental invalidation. OPPORTUNISTIC reassesses recovery vs structural deterioration. TACTICAL/SPECULATIVE must detect their predefined invalidation conditions. An exit condition is not a broker stop order.

Opportunistic declines: `TEMPORARY_PRICE_DISLOCATION` vs `FUNDAMENTAL_BUSINESS_DETERIORATION`. Discovery may flag that question; Research must decide it. Monitoring re-asks it when new evidence arrives.

Adds: fresh thesis + risk review. Never add only to cut average cost.

Discovery must not manufacture trades to fill a sleeve. Zero promoted candidates can be the correct result.

---

## Portfolio context

Dynamic NAV, sleeve %, cash %, HWM/drawdown/risk state, daily halt vs start-of-day NAV, holdings (class + sleeve), concentration, sector/correlation, orders, realized and unrealized P&L, SPY, opportunity cost of cash.

---

## Rank vs cash and SPY

Unused sleeve capacity is not a mandate to buy.

---

## Capital

`CAPITAL_INCREASE_RECOMMENDED` only. Never transfer.
