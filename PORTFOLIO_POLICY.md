# Portfolio Investment Policy V1

Canonical **permanent** investment policy. Machine-readable: `config/portfolio_policy.json`.

One policy. Any Agentic NAV. **Percent of current NAV** — never a $500 / $5k / $40k / $1M strategy.

NAV is read dynamically from the dedicated Agentic account. Observed balances are facts, not policy constraints.

This file does not authorize broker review, place, cancel, or capital movement. Execution flags stay in `config/account_rules.json`.

---

## Objective

**Sole objective:** long-term capital growth. Income is not an objective. Dividends may add to total return; do not select names for yield.

---

## NAV formulas (no fixed portfolio size)

Let `NAV` = current Agentic portfolio value.

```
sleeve_allocation_pct     = sleeve_market_value / NAV
position_concentration_pct = position_market_value / NAV
cash_allocation_pct       = cash / NAV
daily_portfolio_return    = (NAV - start_of_day_NAV) / start_of_day_NAV
drawdown                  = (NAV / high_water_mark_NAV) - 1
```

Never substitute a hardcoded dollar portfolio size into these.

---

## Sleeves (targets, not mandates)

| Sleeve | Target % NAV | Hard sleeve max |
|---|---|---|
| `CORE_GROWTH` | 50% | — (position ceilings below) |
| `OPPORTUNISTIC` | 30% | — |
| `TACTICAL` | 15% | — |
| `SPECULATIVE` | 5% (target **and** hard max) | **5% NAV** |
| Cash | residual | valid position |

Never buy to fill a quota. Remain below targets when risk-adjusted opportunity is lacking. Substantial cash is acceptable. Consider cash yield / opportunity cost of cash.

Position **count** is not a hard constraint. It is reported. It emerges from opportunity, diversification, concentration, correlation, liquidity, risk, evidence, cash, and regime.

---

## Concentration matrix (hard ceilings of **total NAV**)

Ceilings are **not** default sizes. Unused room is not a reason to add. The engine may choose much smaller weights.

Limits use **security class + sleeve**. There is no universal individual-equity cap.

| Class + context | Hard max % NAV |
|---|---|
| `BROAD_MARKET_INDEX_ETF` (any sleeve where held) | **40%** |
| `OTHER_DIVERSIFIED_ETF` | **25%** |
| `CORE_GROWTH` + `INDIVIDUAL_EQUITY` | **20%** |
| `OPPORTUNISTIC` + `INDIVIDUAL_EQUITY` | **15%** |
| `TACTICAL` + `INDIVIDUAL_EQUITY` | **10%** |
| `SPECULATIVE` (any class) per security | **3%** |
| `SPECULATIVE` sleeve total | **5%** |

The autonomous agent may be **more** conservative. It may **never** loosen these ceilings without explicit human policy authorization.

### Enhanced concentration review

Any proposed **individual security** (after the trade) **> 10% NAV** requires `ENHANCED_CONCENTRATION_REVIEW` documenting: why concentration is justified, thesis strength, bull/bear, security-specific downside, valuation, fundamental-deterioration risk, volatility, liquidity, correlation/overlap, sector exposure after, alternatives, cash instead, why smaller is inferior, invalidation.

`INDIVIDUAL_EQUITY` **> 15% NAV** → `CONCENTRATION_LEVEL = HIGH` (stronger justification). Still cannot exceed the hard max for that sleeve (e.g. Core 20%, Opp 15%).

This review is a research/risk gate. It does **not** by itself require human approval under a future `AUTO_EXECUTION` mode. Hard maxes remain absolute.

---

## Security classification (verifiable; fail closed)

Classes: `BROAD_MARKET_INDEX_ETF` | `OTHER_DIVERSIFIED_ETF` | `INDIVIDUAL_EQUITY`.

The AI must **not** assert `BROAD_MARKET_INDEX_ETF` to unlock 40%. Classification uses evidence (see below). Seed tickers (SPY, VOO, …) are **supporting only**, never sufficient alone. If class cannot be established: **fail closed** — `INSUFFICIENT_EVIDENCE`, no 40% bucket. Known ETF without broad-market proof → `OTHER_DIVERSIFIED_ETF` (25%). Unknown instrument → `INDIVIDUAL_EQUITY` + sleeve.

Implemented in `src/agentic_portfolio/classification.py`. Additional public fund data sources can feed the same evidence object later without rewriting the risk gate.

Leveraged ETFs remain **prohibited** (not a concentration class).

### How classification should work

**Retrieve** (read-only MCP and instrument metadata): tradability / instrument type (equity vs ETF), fundamentals/description, index membership when available, name/legal type. Do not use share price.

**Validate**

- `BROAD_MARKET_INDEX_ETF`: ETF/fund **and** tracks a broad market equity index (e.g. S&P 500, total U.S. market) **and** not sector/thematic/single-stock **and** not leveraged.
- `OTHER_DIVERSIFIED_ETF`: ETF/fund with diversified holdings that is **not** broad-market (sector, factor, international-specific, thematic, etc.), not leveraged, not single-stock.
- `INDIVIDUAL_EQUITY`: single-company stock, or anything that fails the ETF tests including single-stock ETFs.

**Cache:** symbol → class, evidence refs, `as_of`. Privileged buckets (25%/40%) only apply from a **validated cached** class.

**Refresh:** at session start for held names; always before a BUY/add that relies on 25% or 40%; on split, ticker change, or fund-structure news.

---

## CORE_GROWTH — target 50%

Long-term compounding vs **SPY / S&P 500**. Broad-market ETFs, other diversified ETFs, or high-quality individual companies — based on valuation, fundamentals, long-term growth, risk-adjusted return, conditions, construction, alternatives.

A verified `BROAD_MARKET_INDEX_ETF` may reach **40% NAV** when justified. A Core individual company may reach **20% NAV** when justified. Multi-year holds while thesis intact. **No arbitrary time-based exits.**

---

## OPPORTUNISTIC — target 30%

Individual equity max **15% NAV**. Broad search (not one mechanic): quality beaten-down, rebounds, overreactions, dislocations, post-earnings selloffs, sector rotation, struggling growth, catalysts, momentum, other evidence.

Price decline ≠ buy. Classify **TEMPORARY_PRICE_DISLOCATION** vs **FUNDAMENTAL_BUSINESS_DETERIORATION**.

---

## TACTICAL — target 15%

Individual equity max **10% NAV**. Technicals, momentum, S/R, price/volume, RS, vol, catalysts, regime, other short-duration evidence. Day trading **not** required. Horizon: intraday (regular hours) to several weeks. No trade quota. `NO_ACTION` valid.

---

## SPECULATIVE — target / max 5%

Per name **3% NAV** (max, not default). Sleeve **never** over **5% NAV**. One name may be 3%; combined Speculative still ≤ 5%.

May include micro/small-cap, emerging, turnarounds, disruptive tech, high-growth/high-risk, beaten-down speculative, low-priced names, other asymmetric evidence. Below $5 and below $2 **may** be considered. **Low price is never a thesis.**

---

## Adding to a position

No blanket ban on “averaging down.” Any increase requires a fresh **INVESTMENT_THESIS_REVIEW** and **RISK_REVIEW**. Lower price alone is not enough. Re-check: thesis still valid? new evidence? risk/reward? fundamentals? concentration? alternatives? cash?

Never add only to lower average cost. Ceilings still absolute. One **consolidated** position per symbol; multiple tax lots allowed underneath.

Sleeve is a persisted assignment. `TACTICAL` cannot silently become `CORE_GROWTH`. Reassignment requires `SLEEVE_RECLASSIFICATION_REVIEW` and a new thesis. Positions that appear at Robinhood with no registry entry are `UNREGISTERED_POSITION` — analyze and SELL/REDUCE allowed; no risk-increasing ADD until sleeve and thesis are explicit.

---

## Sector concentration

Canonical internal taxonomy is GICS-style 11 sectors plus `UNKNOWN` (`src/agentic_portfolio/sectors.py`). Robinhood MCP currently returns FactSet-style labels (e.g. `Electronic Technology`); those are mapped deterministically. The AI may not freely rename sectors to pass the 30%/45% rules. Unmapped labels are `UNKNOWN` (fail closed for new individual-equity risk). ETF `Miscellaneous` is not treated as an economic sector.

- **Review:** one economic sector **> 30%** NAV → `SECTOR_CONCENTRATION_REVIEW` (does not auto-block).
- **Hard ceiling:** **45%** NAV. The agent may not intentionally increase a sector beyond 45%. It may use a *lower* internal limit; it may never raise 45%.
- **Drift:** if appreciation pushes a sector over a cap, flag, block further increase, review, **do not** mechanically liquidate. `PASSIVE_MARKET_DRIFT_BREACH` ≠ `PROPOSED_ACTION_BREACH`.

ETF embedded sector weights are recorded only when observed. If holdings are not available (current Robinhood MCP), `embedded_sector_exposure_status` is `UNKNOWN` or `PARTIAL` — never invented. Broad-market classification may still proceed on other verifiable diversification evidence (index, mandate, constituent count, definitional broad-index identity).

---

## Correlation / overlap

Pairwise, sleeve-level, sector overlap, and common-factor exposure are observables (`AVAILABLE` / `PARTIAL` / `INSUFFICIENT_DATA`). There is **no** hard numeric correlation cap. The risk engine may warn; it must not reject a trade solely on an invented threshold. A future validated limit can attach to the existing schema (`future_hard_limit`) without rewriting portfolio context.

---

## Liquidity

Share price is **not** a liquidity proxy. Use dollar volume (20-session median ADV$), recent volume, and spread when available.

- **Normal:** planned notional ≤ **1%** of 20-session median daily dollar volume (`config/portfolio_policy.json` → `liquidity`).
- **Speculative:** `SPECULATIVE_LIQUIDITY_REVIEW` plus ≤ **2%** of that ADV (configurable). Evaluate spread, slippage, volume stability, exitability, event risk.
- Missing liquidity data on new risk → **fail closed**.

---

## Daily portfolio risk halt

Threshold: **2% of start-of-trading-session NAV**.

Timezone: **America/New_York**. SOD NAV is anchored to the official U.S. equities session calendar (`src/agentic_portfolio/calendar.py`), not calendar midnight. Weekends and NYSE holidays do not create sessions. Early-close days keep the same session id. If the calendar cannot be resolved, **fail safe**: do not reset daily-risk state.

When `daily_portfolio_return ≤ −0.02`: `DAILY_RISK_HALT = true` for the rest of that session.

**Prohibit:** new risk-increasing BUY; increasing existing positions.  
**Allow:** SELL, REDUCE, HOLD, WATCH, REJECT, NO_ACTION, analysis.

Do **not** auto-liquidate long-term Core because the daily halt fired. Halt may reset next session unless a **higher** HWM risk state still blocks new risk.

---

## Drawdown vs cash-flow-adjusted high-water mark

External deposits/withdrawals are **not** performance. HWM is scaled:

```
nav_pre_flow = NAV_after - external_capital_flow
hwm_after_market = max(prior_HWM, nav_pre_flow)
hwm_after_flow = hwm_after_market * (NAV_after / nav_pre_flow)   # nav_pre_flow > 0
drawdown = (NAV_after / hwm_after_flow) - 1
performance_since_prior = (nav_pre_flow / prior_NAV) - 1
```

Flows must be **explicit** (the agent never infers a deposit to rewrite HWM). Manual HWM reset after HALTED is **human only**.

`drawdown = (NAV / cash_flow_adjusted_HWM) - 1`

| State | Drawdown | Behavior |
|---|---|---|
| `NORMAL` | 0% to **&lt; −5%** | Ordinary policy |
| `WARNING` | **−5%** | Enhanced portfolio review. **No** auto-liquidation. |
| `RISK_REDUCTION` | **−10%** | No new Spec/Tactical; Core with validation; Opp enhanced review; SELL/REDUCE OK. **No** Core auto-liq. |
| `DEFENSIVE` | **−15%** | No new Spec/Tactical; Opp add only if explicitly risk-reducing; new Core = **validated** `BROAD_MARKET_INDEX_ETF` only. No mechanical liquidation. |
| `HALTED` | **−20%** | `auto_execution` false. May **recommend** SELL/REDUCE/HOLD/NO_ACTION. May **not** execute until explicit human authorization. No self-resume. No invented liquidation. |

---

## Benchmark

Entire book vs **SPY**, same window: total return, SPY return, excess return, max DD, HWM, cash %, sleeve performance, realized/unrealized P&L, volatility when enough data. Sleeve analytics required. Question: did active management beat holding SPY?

---

## Capital (human only)

Never deposit, withdraw, or transfer — even if MCP later exposes tools. May emit `CAPITAL_INCREASE_RECOMMENDED` (amount, evidence, risks, why). Only the human acts.

---

## Execution vs policy

One investment policy. Execution states (`RESEARCH_ONLY`, `HUMAN_APPROVAL`, `AUTO_EXECUTION`) do **not** change philosophy with NAV.

**Current:** `HUMAN_APPROVAL` with `auto_execution=false`, `live_trade_actions_allowed=false`, `require_human_approval=true`.

---

## Hard-risk immutability

Agent may tighten (more cash, smaller size, extra research). Agent may **not** autonomously exceed any matrix ceiling, weaken drawdown/daily halt, skip classification, or disable risk gates. Loosening requires **explicit human policy authorization**.

---

## Observability / future dashboard

NAV, HWM, drawdown, risk state, cash and sleeve %, holdings, class, sleeve, position and sector concentration, overlap/correlation, P&L, SPY and excess return, daily halt, auto-exec status, recent decisions, theses, capital recommendations.

Local dashboard later. **No transfer UI.**
